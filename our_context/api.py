import asyncio
import hashlib
import hmac
import json
import secrets
import threading
import time
import httpx
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings
from .engine import Engine, DATE_FIELDS, answer_date
from .gitea import Gitea
from .llm import LLM, function
from .store import Store, dumps, uid


class SourceInput(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=5_000_000)
    kind: str = "note"
    logical_key: str | None = None
    occurred_at: str | None = None


class QueryInput(BaseModel):
    text: str = Field(min_length=1, max_length=12000)
    revision: str | None = None
    scope: str = ""


class ProposalInput(BaseModel):
    base_revision: str
    operations: list[dict] = Field(min_length=1, max_length=100)
    reason: str = ""
    assumptions: list[str] = []


def create_app(settings=None):
    s = settings or Settings()
    store = Store(s.root)
    engine = Engine(s, store)
    app = FastAPI(title="Our Context", docs_url=None, redoc_url=None)
    app.state.store, app.state.engine = store, engine
    idem_lock = threading.RLock()

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(httpx.RequestError)
    async def connection_error(request, exc):
        return JSONResponse(
            {"detail": "接続先 URL と Docker のネットワークを確認してください。"},
            status_code=502,
        )

    @app.middleware("http")
    async def authentication(request, call_next):
        path = request.url.path
        if int(request.headers.get("content-length", "0")) > 6 * 1024 * 1024:
            return JSONResponse({"detail": "入力を 5 MiB 以内に分けてください。"}, 413)
        if path.startswith("/api/") and path not in (
            "/api/v1/auth/login",
            "/api/v1/session",
        ):
            token = request.cookies.get("our_context_session", "")
            with store.db() as c:
                session = c.execute(
                    "SELECT * FROM sessions WHERE token=? AND expires>?",
                    (token, time.time()),
                ).fetchone()
            if not session:
                return JSONResponse({"detail": "ログインしてください。"}, 401)
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                origin = request.headers.get("origin")
                if origin and urlparse(origin).netloc != request.headers.get("host"):
                    return JSONResponse(
                        {"detail": "画面を再読み込みしてください。"}, 403
                    )
                if not secrets.compare_digest(
                    request.headers.get("x-csrf-token", ""), session["csrf"]
                ):
                    return JSONResponse({"detail": "ログインを更新してください。"}, 403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    def idem(request, payload, action):
        key = request.headers.get("idempotency-key")
        if not key or len(key) > 200:
            raise HTTPException(400, "Idempotency-Key を指定してください。")
        full_key = request.url.path + ":" + key
        digest = hashlib.sha256(dumps(payload).encode()).hexdigest()
        with idem_lock:
            with store.db() as c:
                row = c.execute(
                    "SELECT * FROM idempotency WHERE key=?", (full_key,)
                ).fetchone()
            if row:
                if row["hash"] != digest:
                    raise HTTPException(
                        409, "同じ要求キーの入力内容を確認してください。"
                    )
                return json.loads(row["result"])
            result = action(full_key)
            with store.db() as c:
                c.execute(
                    "INSERT INTO idempotency VALUES(?,?,?)",
                    (full_key, digest, dumps(result)),
                )
            return result

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/readyz")
    def ready():
        engine.wiki.snapshot()
        return {"status": "ready"}

    @app.post("/api/v1/auth/login")
    async def login(request: Request):
        if not s.password:
            raise HTTPException(
                503, "APP_PASSWORD を .env に設定し、Docker を再起動してください。"
            )
        body = await request.json()
        if not secrets.compare_digest(
            str(body.get("password", "")).encode(), s.password.encode()
        ):
            raise HTTPException(401, "パスワードを確認してください。")
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with store.db() as c:
            c.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
            c.execute(
                "INSERT INTO sessions VALUES(?,?,?)",
                (token, csrf, time.time() + 86400 * 7),
            )
        response = JSONResponse({"authenticated": True, "csrf": csrf})
        response.set_cookie(
            "our_context_session",
            token,
            httponly=True,
            samesite="lax",
            secure=s.cookie_secure,
            max_age=86400 * 7,
        )
        return response

    @app.get("/api/v1/session")
    def session(request: Request):
        with store.db() as c:
            row = c.execute(
                "SELECT * FROM sessions WHERE token=? AND expires>?",
                (request.cookies.get("our_context_session", ""), time.time()),
            ).fetchone()
        return {"authenticated": bool(row), "csrf": row["csrf"] if row else ""}

    @app.post("/api/v1/auth/logout")
    def logout(request: Request):
        with store.db() as c:
            c.execute(
                "DELETE FROM sessions WHERE token=?",
                (request.cookies.get("our_context_session", ""),),
            )
        result = JSONResponse({"status": "signed_out"})
        result.delete_cookie("our_context_session")
        return result

    @app.get("/api/v1/bootstrap")
    def bootstrap():
        snapshot = engine.wiki.snapshot()
        pages = list(snapshot["pages"].values())
        heartbeat = store.get("worker_heartbeat", 0)
        return {
            "revision": snapshot["revision"],
            "last_publish": store.get("last_publish"),
            "worker_online": time.time() - heartbeat < 30,
            "pages": pages,
            "scope_members": {
                p["id"]: sorted(engine.wiki.scope_ids(snapshot, p["id"]))
                for p in pages
                if p.get("type") == "team"
            },
            "tasks": engine.task_list(snapshot),
            "questions": engine.questions(),
            "settings": s.public(),
        }

    @app.post("/api/v1/sources", status_code=202)
    def add_source(body: SourceInput, request: Request):
        def action(key):
            source = store.source(
                body.title,
                body.text,
                body.kind,
                body.logical_key or key,
                {"occurred_at": body.occurred_at, "recorder": "owner"},
            )
            job_id = store.enqueue(
                "ingest", {"source_ids": [source["id"]]}, "source:" + source["id"]
            )
            return {
                "source_id": source["id"],
                "job_id": job_id,
                "state": "reflection_pending",
            }

        return idem(request, body.model_dump(), action)

    @app.get("/api/v1/sources")
    def sources():
        with store.db() as c:
            return [
                dict(row)
                for row in c.execute(
                    "SELECT id,logical_key,title,metadata,created FROM sources ORDER BY created DESC LIMIT 200"
                )
            ]

    @app.get("/api/v1/sources/{source_id}")
    def source(source_id: str):
        return store.read_source(source_id)

    @app.post("/api/v1/connectors/gitea/sync", status_code=202)
    async def sync(request: Request):
        payload = await request.json()
        return idem(
            request,
            payload,
            lambda key: {
                "job_id": store.enqueue(
                    "sync", {"force": bool(payload.get("force"))}, key
                )
            },
        )

    @app.post("/hooks/gitea")
    async def webhook(request: Request):
        body = await request.body()
        if len(body) > 2 * 1024 * 1024:
            raise HTTPException(413, "通知が受付上限に達しました。")
        expected = hmac.new(s.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        if not s.webhook_secret or not secrets.compare_digest(
            expected, request.headers.get("x-gitea-signature", "")
        ):
            raise HTTPException(403, "Webhook の署名を確認してください。")
        payload = json.loads(body)
        if (
            request.headers.get("x-gitea-event") != "push"
            or payload.get("repository", {}).get("full_name") != s.gitea_repo
            or payload.get("ref") != "refs/heads/" + s.gitea_branch
        ):
            return JSONResponse({"status": "ignored"}, 200)
        commit = payload.get("after", "")
        if not re_commit(commit):
            raise HTTPException(422, "通知の commit を確認してください。")
        digest = hashlib.sha256(body).hexdigest()
        delivery = request.headers.get("x-gitea-delivery") or digest
        with store.db() as c:
            c.execute("BEGIN IMMEDIATE")
            old = c.execute(
                "SELECT * FROM deliveries WHERE id=?", (delivery,)
            ).fetchone()
            if old:
                if old["hash"] != digest:
                    raise HTTPException(409, "通知 ID と内容の対応を確認してください。")
                return JSONResponse({"job_id": old["job_id"]}, 202)
            job_id = store.enqueue("sync", {"commit": commit}, "push:" + commit, c)
            c.execute(
                "INSERT INTO deliveries VALUES(?,?,?)", (delivery, digest, job_id)
            )
        return JSONResponse({"job_id": job_id}, 202)

    @app.get("/api/v1/wiki/pages")
    def pages(q: str = "", scope: str = "", revision: str | None = None):
        snap = engine.wiki.snapshot(revision)
        return {
            "revision": snap["revision"],
            "pages": engine.wiki.search(snap, q, scope),
        }

    @app.get("/api/v1/wiki/pages/{page_id}")
    def page(page_id: str, revision: str | None = None):
        snap = engine.wiki.snapshot(revision)
        if page_id not in snap["pages"]:
            raise HTTPException(404, "ページを確認してください。")
        return {"revision": snap["revision"], **snap["pages"][page_id]}

    @app.get("/api/v1/tasks")
    @app.get("/api/v1/gantt")
    def tasks(
        revision: str | None = None,
        project_id: str = "",
        status: str = "",
        assignee: str = "",
        start: str = "",
        end: str = "",
    ):
        snap = engine.wiki.snapshot(revision)
        return {"revision": snap["revision"], "tasks": engine.task_list(snap, locals())}

    @app.post("/api/v1/planning/simulations")
    async def simulate(request: Request):
        body = await request.json()
        return engine.simulate(
            engine.wiki.snapshot(body.get("revision")), body.get("operations", [])
        )

    @app.post("/api/v1/change-proposals")
    def proposal(body: ProposalInput):
        return engine.propose(
            body.base_revision, body.operations, body.reason, body.assumptions
        )

    @app.post("/api/v1/change-proposals/{proposal_id}/apply", status_code=202)
    def apply(proposal_id: str, request: Request):
        def action(key):
            with store.db() as c:
                c.execute("BEGIN IMMEDIATE")
                p = c.execute(
                    "SELECT * FROM proposals WHERE id=?", (proposal_id,)
                ).fetchone()
                if not p:
                    raise HTTPException(404, "変更案を確認してください。")
                current = c.execute(
                    "SELECT value FROM meta WHERE key='revision'"
                ).fetchone()
                if p["revision"] != json.loads(current[0]):
                    raise HTTPException(
                        409, "参照版が更新されました。変更案を作成し直してください。"
                    )
                if time.time() - p["created"] > 86400:
                    raise HTTPException(
                        409, "変更案の有効期間は24時間です。作成し直してください。"
                    )
                jid = store.enqueue(
                    "apply", {"proposal_id": proposal_id}, "apply:" + proposal_id, c
                )
                return {"job_id": jid}

        return idem(request, {"proposal_id": proposal_id}, action)

    @app.get("/api/v1/questions")
    def questions():
        return engine.questions()

    @app.post("/api/v1/questions/{question_id}/answers", status_code=202)
    async def answer(question_id: str, request: Request):
        payload = await request.json()
        text = str(payload.get("text", "")).strip()
        if not text:
            raise HTTPException(422, "回答を入力してください。")

        def action(key):
            with store.db() as c:
                row = c.execute(
                    "SELECT * FROM questions WHERE id=?", (question_id,)
                ).fetchone()
            if row is None:
                raise HTTPException(404, "質問を確認してください。")
            if payload.get("question_version") != row["version"]:
                raise HTTPException(
                    409, "質問が更新されました。内容を確認してください。"
                )
            q = json.loads(row["data"])
            if q.get("field") in DATE_FIELDS:
                try:
                    answer_date(text)
                except ValueError as exc:
                    raise HTTPException(422, str(exc)) from exc
            body = (
                "質問："
                + q["question"]
                + "\n対象："
                + str(q.get("subject_id", ""))
                + "\n回答："
                + text
            )
            source = store.source(
                "確認事項への回答",
                body,
                "answer",
                key,
                {"question_id": question_id, "recorder": "owner"},
            )
            with store.db() as c:
                c.execute(
                    "UPDATE questions SET status='answering',answer_source=?,version=version+1,updated=? WHERE id=?",
                    (source["id"], time.time(), question_id),
                )
                jid = store.enqueue(
                    "ingest",
                    {"source_ids": [source["id"]], "question_id": question_id},
                    "answer:" + source["id"],
                    c,
                )
            return {
                "source_id": source["id"],
                "job_id": jid,
                "state": "reflection_pending",
            }

        return idem(request, payload, action)

    @app.patch("/api/v1/questions/{question_id}")
    async def question_state(question_id: str, request: Request):
        payload = await request.json()
        status = payload.get("status")
        if status not in ("open", "deferred", "dismissed"):
            raise HTTPException(422, "質問の状態を確認してください。")
        if payload.get("snooze_until"):
            date.fromisoformat(payload["snooze_until"])
        with store.db() as c:
            result = c.execute(
                "UPDATE questions SET status=?,snooze_until=?,version=version+1,updated=? WHERE id=? AND version=? AND status NOT IN ('answering')",
                (
                    status,
                    payload.get("snooze_until"),
                    time.time(),
                    question_id,
                    payload.get("version"),
                ),
            )
            if not result.rowcount:
                raise HTTPException(409, "質問を再読み込みしてください。")
        return {"status": status}

    @app.post("/api/v1/conversations")
    def conversation():
        return {"id": uid("conversation")}

    @app.post("/api/v1/conversations/{conversation_id}/queries", status_code=202)
    def query(conversation_id: str, body: QueryInput, request: Request):
        def action(key):
            revision = body.revision or store.get("revision")
            with store.db() as c:
                history = [
                    {"role": r["role"], "content": r["body"]}
                    for r in c.execute(
                        "SELECT * FROM messages WHERE conversation=? ORDER BY created DESC LIMIT 8",
                        (conversation_id,),
                    )
                ][::-1]
                c.execute(
                    "INSERT INTO messages VALUES(?,?,?,?,?,?)",
                    (uid("msg"), conversation_id, "user", body.text, "{}", time.time()),
                )
                jid = store.enqueue(
                    "query",
                    {
                        **body.model_dump(),
                        "revision": revision,
                        "conversation": conversation_id,
                        "history": history,
                    },
                    key,
                    c,
                )
            return {"job_id": jid}

        return idem(request, body.model_dump(), action)

    @app.get("/api/v1/conversations/{conversation_id}")
    def history(conversation_id: str):
        with store.db() as c:
            return [
                {**dict(row), "data": json.loads(row["data"])}
                for row in c.execute(
                    "SELECT * FROM messages WHERE conversation=? ORDER BY created LIMIT 100",
                    (conversation_id,),
                )
            ]

    @app.post("/api/v1/messages/{message_id}/save", status_code=202)
    def save_message(message_id: str, request: Request):
        return idem(
            request,
            {"message_id": message_id},
            lambda key: {
                "job_id": store.enqueue(
                    "save_message", {"message_id": message_id}, "save:" + message_id
                )
            },
        )

    @app.post("/api/v1/lint-runs", status_code=202)
    def lint(request: Request):
        return idem(
            request,
            {},
            lambda key: {
                "job_id": store.enqueue(
                    "lint", {"revision": store.get("revision")}, key
                )
            },
        )

    @app.post("/api/v1/exports", status_code=202)
    async def export(request: Request):
        body = await request.json()
        if body.get("format", "xlsx") not in ("xlsx", "markdown"):
            raise HTTPException(422, "出力形式を確認してください。")
        for field in ("start", "end"):
            if body.get(field):
                date.fromisoformat(body[field])
        if body.get("start") and body.get("end") and body["start"] > body["end"]:
            raise HTTPException(422, "対象期間の開始日と終了日を確認してください。")
        return idem(
            request,
            body,
            lambda key: {
                "job_id": store.enqueue(
                    "export",
                    {**body, "revision": body.get("revision") or store.get("revision")},
                    key,
                )
            },
        )

    @app.get("/api/v1/exports/{export_id}/download")
    def download(export_id: str):
        with store.db() as c:
            row = c.execute("SELECT * FROM exports WHERE id=?", (export_id,)).fetchone()
        if not row or not Path(row["path"]).exists():
            raise HTTPException(404, "出力ファイルを確認してください。")
        path = Path(row["path"])
        return FileResponse(path, filename="our-context" + path.suffix)

    @app.get("/api/v1/jobs")
    def jobs():
        with store.db() as c:
            return [
                dict(row)
                for row in c.execute(
                    "SELECT id,kind,status,stage,error,created,updated,result FROM jobs ORDER BY created DESC LIMIT 100"
                )
            ]

    @app.get("/api/v1/jobs/{job_id}")
    def job(job_id: str):
        return store.job(job_id)

    @app.get("/api/v1/jobs/{job_id}/events")
    async def events(job_id: str):
        async def stream():
            for _ in range(900):
                job = store.job(job_id)
                yield "data: " + dumps(job) + "\n\n"
                if job["status"] in ("succeeded", "failed", "cancelled"):
                    break
                await asyncio.sleep(1)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.post("/api/v1/jobs/{job_id}/retry", status_code=202)
    def retry(job_id: str):
        with store.db() as c:
            result = c.execute(
                "UPDATE jobs SET status='queued',error=NULL,updated=? WHERE id=? AND status='failed'",
                (time.time(), job_id),
            )
            if not result.rowcount:
                raise HTTPException(409, "失敗したジョブを選択してください。")
        return {"job_id": job_id}

    @app.get("/api/v1/settings/status")
    def settings_status():
        return {
            **s.public(),
            "revision": store.get("revision"),
            "last_publish": store.get("last_publish"),
        }

    @app.get("/api/v1/guides/connections")
    def guide():
        return (
            Path(__file__).resolve().parent.parent / "docs/connections.md"
        ).read_text()

    @app.post("/api/v1/settings/check/{provider}")
    def check(provider: str):
        if provider == "gitea":
            result = Gitea(s).check()
        elif provider == "llm":
            llm = LLM(s)
            response = llm.completion(
                [{"role": "user", "content": "Reply with the word connected."}],
                model=s.query_model,
            )
            tool = function(
                "connection_test", "接続確認", {"value": {"type": "string"}}
            )
            check_response = llm.completion(
                [
                    {
                        "role": "user",
                        "content": "Call connection_test with value connected.",
                    }
                ],
                tools=[tool],
                model=s.query_model,
            )
            result = {
                "response": response.get("content"),
                "tool_calling": bool(check_response.get("tool_calls")),
                "usage": llm.usage,
            }
        else:
            raise HTTPException(404, "接続先を確認してください。")
        store.set("check:" + provider, {"at": time.time(), "result": result})
        return result

    static = Path(__file__).resolve().parent.parent / "static"
    if static.exists():
        app.mount("/", StaticFiles(directory=static, html=True), name="ui")
    return app


def re_commit(commit):
    import re

    return bool(re.fullmatch(r"[a-f0-9]{40,64}", commit)) and set(commit) != {"0"}


app = create_app()

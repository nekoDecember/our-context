"""実 Gitea・LiteLLM・推論モデルを使う一連の業務フロー。"""

import base64
from datetime import datetime, timezone
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
import uuid

import httpx
from openpyxl import load_workbook

HERE = Path("/source/tests/e2e")
EVIDENCE = Path("/evidence")
WORK = Path("/tmp/our-context-e2e-notes")
started = time.monotonic()
outcome = {
    "started": datetime.now(timezone.utc).isoformat(),
    "mode": "live-model",
    "model": os.environ["LLM_INGEST_MODEL"],
    "steps": [],
}
client = httpx.Client(base_url="http://app:8096", timeout=240)
repo = os.environ["GITEA_REPO"]


def record(step, **data):
    item = {
        "step": step,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        **data,
    }
    outcome["steps"].append(item)
    (EVIDENCE / "outcome.json").write_text(
        json.dumps(outcome, ensure_ascii=False, indent=2)
    )
    print(json.dumps(item, ensure_ascii=False), flush=True)


def get(path):
    r = client.get("/api/v1" + path)
    r.raise_for_status()
    return r.json()


def post(path, body=None):
    r = client.post(
        "/api/v1" + path, json=body or {}, headers={"Idempotency-Key": uuid.uuid4().hex}
    )
    if r.is_error:
        raise AssertionError(f"{path}: HTTP {r.status_code} {r.text[:900]}")
    return r.json()


def wait(result, timeout=900):
    until = time.monotonic() + timeout
    last = None
    while time.monotonic() < until:
        job = get("/jobs/" + result["job_id"])
        if job["status"] == "succeeded":
            return job["result"]
        if job["status"] == "failed":
            raise AssertionError(f"job={job['id']}: {job['error']}")
        stage = (job["status"], job.get("stage"))
        if stage != last:
            print(
                json.dumps(
                    {"job": job["id"], "status": stage[0], "stage": stage[1]},
                    ensure_ascii=False,
                ),
                flush=True,
            )
            last = stage
        time.sleep(1)
    raise AssertionError("ジョブの待ち時間が上限に達しました。")


def command(*args):
    env = os.environ.copy()
    credential = base64.b64encode(
        (os.environ["GITEA_USERNAME"] + ":" + os.environ["E2E_GITEA_PASSWORD"]).encode()
    ).decode()
    env.update(
        GIT_TERMINAL_PROMPT="0",
        GIT_CONFIG_COUNT="1",
        GIT_CONFIG_KEY_0="http.http://gitea:3000/.extraheader",
        GIT_CONFIG_VALUE_0="Authorization: Basic " + credential,
    )
    r = subprocess.run(
        ["git", *args], env=env, capture_output=True, text=True, timeout=120
    )
    if r.returncode:
        raise AssertionError(
            r.stderr.replace(os.environ["E2E_GITEA_PASSWORD"], "[key]")
        )
    return r.stdout.strip()


def db(path):
    connection = sqlite3.connect("file:" + path + "?mode=ro", uri=True, timeout=15)
    connection.row_factory = sqlite3.Row
    return connection


def push_note(text, message):
    (WORK / "work-log.md").write_text(text)
    (WORK / "private-ignored.txt").write_text("対象外のテスト情報。")
    command("-C", str(WORK), "add", ".")
    command("-C", str(WORK), "commit", "-m", message)
    commit = command("-C", str(WORK), "rev-parse", "HEAD")
    command("-C", str(WORK), "push", "origin", "main")
    until = time.monotonic() + 60
    while time.monotonic() < until:
        with db("/workspace/state.sqlite3") as c:
            row = c.execute(
                "SELECT d.id,j.id AS job_id FROM deliveries d JOIN jobs j ON j.id=d.job_id WHERE json_extract(j.payload,'$.commit')=?",
                (commit,),
            ).fetchone()
        if row:
            record(
                "Gitea が push を通知",
                commit=commit,
                delivery_id=row["id"],
                job_id=row["job_id"],
            )
            result = wait({"job_id": row["job_id"]})
            with db("/gitea-data/gitea/gitea.db") as c:
                delivered = c.execute(
                    "SELECT uuid,is_succeed FROM hook_task WHERE uuid=?", (row["id"],)
                ).fetchone()
            assert delivered and delivered["is_succeed"], (
                "Gitea の配信結果を確認してください。"
            )
            record("Gitea の配信成功を照合", delivery_id=row["id"], **result)
            return commit
        time.sleep(1)
    raise AssertionError("push に対応する Gitea Webhook を確認してください。")


def task(boot):
    return next(t for t in boot["tasks"] if "結合テスト" in t["title"])


def main():
    for _ in range(60):
        try:
            if client.get("/healthz").is_success:
                break
        except httpx.NetworkError:
            pass
        time.sleep(1)
    r = client.post("/api/v1/auth/login", json={"password": os.environ["APP_PASSWORD"]})
    r.raise_for_status()
    client.headers["X-CSRF-Token"] = r.json()["csrf"]
    assert post("/settings/check/gitea")["full_name"] == repo
    for attempt in range(180):
        try:
            ready = httpx.get(
                os.environ["LLM_BASE_URL"] + "/models",
                headers={
                    "Authorization": "Bearer " + os.environ.get("LLM_API_KEY", "")
                },
                timeout=5,
            )
            if ready.is_success:
                break
        except httpx.NetworkError:
            pass
        if attempt % 15 == 0:
            print("LiteLLM の起動を待っています。", flush=True)
        time.sleep(1)
    else:
        raise AssertionError("LiteLLM の起動とモデル一覧を確認してください。")
    llm = post("/settings/check/llm")
    assert llm["tool_calling"] and llm["usage"]["completion_tokens"] > 0
    record("実 LiteLLM の推論とツール呼び出し", **llm)

    for job in get("/jobs"):
        if job["status"] in ("queued", "running"):
            wait({"job_id": job["id"]})
    command("clone", "http://gitea:3000/" + repo + ".git", str(WORK))
    command("-C", str(WORK), "config", "user.name", "Our Context E2E")
    command("-C", str(WORK), "config", "user.email", "e2e@example.invalid")
    first = (HERE / "day1.md").read_text()
    first_commit = push_note(first, "Record work and blocker")
    boot = get("/bootstrap")
    types = {p["type"] for p in boot["pages"]}
    assert {"project", "team", "person", "task", "context"} <= types, types
    current = task(boot)
    project = next(
        p for p in boot["pages"] if p["type"] == "project" and "検索基盤" in p["title"]
    )
    assert current["project_id"] == project["id"], current
    assert current["status"] == "blocked", current
    assert (
        current["plan_start"] == "2026-11-02" and current["plan_end"] == "2026-11-03"
    ), current
    assert not current.get("deadline"), current
    person = next(
        p for p in boot["pages"] if p["type"] == "person" and "田中" in p["title"]
    )
    blocker = next(
        p
        for p in boot["pages"]
        if p["type"] == "context" and "権限" in json.dumps(p, ensure_ascii=False)
    )
    assert blocker["data"]["attribution"] == "reported_statement", blocker
    assert blocker["data"]["speaker_id"] == person["id"], blocker
    assert any("権限" in e["quote"] for e in blocker["evidence"]), blocker
    assert person["id"] in current["assignee_ids"], current
    all_sources = [get("/sources/" + s["id"]) for s in get("/sources")]
    assert any(s["body"] == first for s in all_sources)
    assert all(s["title"] != "private-ignored.txt" for s in all_sources)
    for page in boot["pages"]:
        for evidence in page.get("evidence", []):
            original = next(
                s["body"] for s in all_sources if s["id"] == evidence["source_id"]
            )
            assert evidence["quote"] in original
    record(
        "メモを Wiki に整理",
        pages=len(boot["pages"]),
        page_types=sorted(types),
        task_id=current["id"],
        task_status=current["status"],
        first_commit=first_commit,
    )
    (EVIDENCE / "wiki-initial.json").write_text(
        json.dumps(boot, ensure_ascii=False, indent=2)
    )

    conversation = post("/conversations")["id"]
    answer = wait(
        post(
            "/conversations/" + conversation + "/queries",
            {
                "text": "田中さんは今、何に困っていますか？ 記録の出典を付けて教えてください。",
                "revision": boot["revision"],
            },
        )
    )
    assert answer["citations"] and "権限" in answer["answer"], answer
    assert any("権限" in c["quote"] for c in answer["citations"]), answer
    record(
        "業務の文脈を参照して回答",
        answer=answer["answer"],
        citations=answer["citations"],
        usage=answer["usage"],
    )
    gantt = wait(
        post(
            "/conversations/" + conversation + "/queries",
            {
                "text": "検索基盤刷新のガントを表示してください。",
                "revision": boot["revision"],
            },
        )
    )
    assert any(t["type"] == "gantt" for t in gantt["tools"]), gantt
    assert (
        next(t for t in gantt["tools"] if t["type"] == "gantt")["project_id"]
        == project["id"]
    ), gantt
    gantt_tasks = get("/gantt?project_id=" + project["id"])["tasks"]
    assert current["id"] in [t["id"] for t in gantt_tasks]
    record("相談からガントを表示", tools=gantt["tools"])

    question = next(
        q
        for q in get("/questions")
        if q["status"] == "open"
        and (
            q["data"].get("field") == "deadline"
            or any(w in q["data"]["question"] for w in ("締め切り", "締切", "期限"))
        )
    )
    wait(
        post(
            "/questions/" + question["id"] + "/answers",
            {
                "text": "顧客検索の結合テストの締め切りは2026-11-06です。",
                "question_version": question["version"],
            },
        )
    )
    boot = get("/bootstrap")
    assert task(boot)["deadline"] == "2026-11-06", task(boot)
    assert (
        next(q for q in get("/questions") if q["id"] == question["id"])["status"]
        == "answered"
    )
    record("確認への回答を Wiki と予定へ反映", deadline=task(boot)["deadline"])

    second = first + "\n" + (HERE / "day2.md").read_text()
    second_commit = push_note(second, "Resolve blocker and begin test")
    boot = get("/bootstrap")
    current = task(boot)
    assert current["status"] == "in_progress", current
    assert current["deadline"] == "2026-11-06", current
    assert any(
        p["type"] == "context"
        and p.get("data", {}).get("resolution_state") == "resolved"
        for p in boot["pages"]
    )
    record(
        "同じノートへの追記を反映",
        second_commit=second_commit,
        status=current["status"],
        deadline=current["deadline"],
    )

    updated_conversation = post("/conversations")["id"]
    updated = wait(
        post(
            "/conversations/" + updated_conversation + "/queries",
            {
                "text": "2026-11-02の最新の追記では、田中さんの接続権限の困りごとと結合テストはどうなりましたか？",
                "revision": boot["revision"],
            },
        )
    )
    assert any(word in updated["answer"] for word in ("解決", "取得")), updated
    assert any(word in updated["answer"] for word in ("進行中", "着手", "開始")), (
        updated
    )
    assert any(
        any(word in item["quote"] for word in ("取得", "進行中", "着手"))
        for item in updated["citations"]
    ), updated
    record(
        "追記後の状況を出典付きで回答",
        answer=updated["answer"],
        citations=updated["citations"],
        usage=updated["usage"],
    )

    proposal = post(
        "/change-proposals",
        {
            "base_revision": boot["revision"],
            "operations": [
                {
                    "task_id": current["id"],
                    "set": {"plan_start": "2026-11-04", "plan_end": "2026-11-05"},
                }
            ],
            "reason": "検証用に日程を変更",
        },
    )
    assert task(get("/bootstrap"))["plan_start"] == "2026-11-02"
    wait(post("/change-proposals/" + proposal["id"] + "/apply"))
    boot = get("/bootstrap")
    assert task(boot)["plan_end"] == "2026-11-05"
    record(
        "変更案を確認して予定を適用",
        proposal_id=proposal["id"],
        plan_start=task(boot)["plan_start"],
        plan_end=task(boot)["plan_end"],
    )

    report = wait(
        post(
            "/exports",
            {
                "format": "xlsx",
                "revision": boot["revision"],
                "start": "2026-11-01",
                "end": "2026-11-07",
            },
        )
    )
    download = client.get(report["url"])
    download.raise_for_status()
    book = load_workbook(BytesIO(download.content))
    assert book.sheetnames == ["出力条件", "タスク", "進捗", "困りごと・確認事項"]
    rows = list(book["タスク"].values)
    assert any(row[6] == "2026-11-05" and row[7] == "2026-11-06" for row in rows[1:])
    (EVIDENCE / "weekly-report.xlsx").write_bytes(download.content)
    markdown = wait(
        post("/exports", {"format": "markdown", "revision": boot["revision"]})
    )
    assert "進行中" in markdown["text"] and "2026-11-06" in markdown["text"]
    (EVIDENCE / "weekly-report.md").write_text(markdown["text"])
    record("Excel と報告文を出力", sheets=book.sheetnames, bytes=len(download.content))

    exported = wait(
        post(
            "/conversations/" + conversation + "/queries",
            {
                "text": "検索基盤刷新の2026年11月1日から7日の週次報告を Excel に出力してください。",
                "revision": boot["revision"],
            },
        )
    )
    assert any(t["type"] == "export" for t in exported["tools"]), exported
    chat_export = next(t for t in exported["tools"] if t["type"] == "export")
    assert chat_export["filters"]["project_id"] == project["id"], chat_export
    chat_download = client.get(chat_export["url"])
    chat_download.raise_for_status()
    chat_book = load_workbook(BytesIO(chat_download.content))
    assert any(
        row[6] == "2026-11-05" and row[7] == "2026-11-06"
        for row in list(chat_book["タスク"].values)[1:]
    )
    record("相談から Excel を作成", tools=exported["tools"])
    wait(post("/messages/" + answer["message_id"] + "/save"))
    assert any(p["type"] == "analysis" for p in get("/bootstrap")["pages"])
    record("相談を Wiki に保存")
    lint = wait(post("/lint-runs"))
    record("Wiki を点検", issues=lint.get("issues", []))
    outcome["result"] = "passed"
    record("一連のテスト完了", result="passed")


try:
    main()
except Exception as exc:
    outcome["result"] = "failed"
    record("テスト中断", error=str(exc))
    raise

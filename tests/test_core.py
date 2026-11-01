import copy
import hashlib
import hmac
import json
import os
import subprocess

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from our_context.api import create_app
from our_context.config import Settings
from our_context.engine import Engine
from our_context.gitea import Gitea
from our_context.store import Store, uid
from our_context.wiki import git
from fixture_model import SAMPLE, compile_fixture
from our_context.language import assert_plain_japanese


class FixtureLLM:
    usage = {"calls": 1, "prompt_tokens": 100, "completion_tokens": 100}

    def __init__(self, settings):
        pass

    def run(self, system, user, tools, handler, model=None, **kwargs):
        data = json.loads(user)
        raw = handler(
            "read_sources",
            {"id": data["sources"][0]["id"], "start_line": 1, "line_count": 200},
        )
        old = handler("read_pages", {"ids": [p["id"] for p in data.get("index", [])]})
        return compile_fixture(
            raw["id"], "\n".join(line["text"] for line in raw["lines"]), old
        )


@pytest.fixture
def context(tmp_path):
    s = Settings(
        root=tmp_path,
        password="test-password",
        gitea_repo="team/notes",
        webhook_secret="test-secret",
    )
    store = Store(tmp_path)
    engine = Engine(s, store, FixtureLLM)
    return s, store, engine


def ingest(store, engine, body=SAMPLE, key="note"):
    source = store.source("作業メモ", body, logical_key=key)
    jid = store.enqueue("ingest", {"source_ids": [source["id"]]})
    result = engine.process(store.job(jid))
    return source, jid, result


def test_llm_repairs_prose_response_as_json(context):
    from our_context.llm import LLM

    s, _, _ = context
    llm = LLM(s)
    calls = []
    expected = {
        "pages": [
            {
                "id": "project_auth",
                "body": "目的を記録する。",
                "evidence": [{"source_id": "src_test", "quote": "原文の引用"}],
            }
        ]
    }

    def completion(messages, **kwargs):
        calls.append((messages, kwargs))
        if len(calls) == 1:
            return {"role": "assistant", "content": "整理結果を説明します。"}
        return {
            "role": "assistant",
            "content": json.dumps(expected, ensure_ascii=False),
        }

    llm.completion = completion
    result = llm.run("指定した JSON で整理する。", "原文の引用", [], lambda *_: None)
    assert result == expected
    assert calls[1][1]["json_mode"] is True
    assert calls[1][0][-2]["content"] == "整理結果を説明します。"


def test_json_inside_explanation_uses_complete_result():
    from our_context.llm import extract_json

    text = '整理します。\n```json\n{"id":"task_test"}\n```\n最終結果です。\n```json\n{"pages":[{"id":"task_test"}],"questions":[]}\n```\n保存します。'
    assert extract_json(text) == {"pages": [{"id": "task_test"}], "questions": []}


def test_schema_unsupported_provider_uses_json_object(context, monkeypatch):
    import httpx
    from our_context.llm import LLM

    settings = context[0]
    settings.llm_url = "http://model.invalid/v1"
    settings.ingest_model = "test"
    payloads = []
    responses = [
        httpx.Response(400, json={"error": "json_schema unsupported"}),
        httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": '{"value":"確認済み"}',
                        }
                    }
                ]
            },
        ),
    ]

    def post(*args, **kwargs):
        payloads.append(copy.deepcopy(kwargs["json"]))
        return responses.pop(0)

    monkeypatch.setattr("our_context.llm.httpx.post", post)
    message = LLM(settings).completion([], output_schema={"type": "object"})
    assert json.loads(message["content"])["value"] == "確認済み"
    assert [p["response_format"]["type"] for p in payloads] == [
        "json_schema",
        "json_object",
    ]


def test_display_tool_finishes_without_another_model_call(context):
    from our_context.llm import LLM

    llm = LLM(context[0])
    calls = []

    def completion(*args, **kwargs):
        calls.append(1)
        assert len(calls) == 1
        return {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "gantt",
                    "type": "function",
                    "function": {
                        "name": "show_gantt",
                        "arguments": '{"project_id":"project_auth"}',
                    },
                }
            ],
        }

    llm.completion = completion
    seen = []
    result = llm.run(
        "予定を表示する。",
        "ガントを表示",
        [],
        lambda name, args: seen.append(args) or {"type": "gantt"},
        final_tools={"show_gantt": "ガントを開けます。"},
    )
    assert result["answer"] == "ガントを開けます。"
    assert seen == [{"project_id": "project_auth"}]


def test_initial_ingest_repairs_missing_project_relationship(context):
    _, store, engine = context

    class MissingProject(FixtureLLM):
        def run(self, *args, **kwargs):
            assert kwargs.get("structured") is True
            schema = kwargs["response_schema"]
            for variant in schema["properties"]["pages"]["items"]["anyOf"]:
                assert "enum" not in variant["properties"]["title"]
                evidence = variant["properties"]["evidence"]["items"]["properties"]
                assert "enum" in evidence["source_id"]
                assert evidence["quote"]["enum"] == list(
                    dict.fromkeys(
                        line.strip() for line in SAMPLE.splitlines() if line.strip()
                    )
                )
                assert not set(evidence["source_id"]["enum"]) & set(
                    evidence["quote"]["enum"]
                )
            result = super().run(*args, **kwargs)
            task = next(p for p in result["pages"] if p["type"] == "task")
            task["data"].pop("project_id")
            with pytest.raises(ValueError) as error:
                kwargs["validator"](result)
            assert task["id"] in error.value.repair_page_ids
            task["data"]["project_id"] = "project_auth"
            kwargs["validator"](result)
            return result

    engine.llm_factory = MissingProject
    ingest(store, engine)
    assert (
        engine.task_list(engine.wiki.snapshot(), {"project_id": "project_auth"})[0][
            "project_id"
        ]
        == "project_auth"
    )


def test_validation_repair_keeps_previous_page_candidates(context):
    _, store, engine = context

    class RepairLLM(FixtureLLM):
        def run(self, *args, **kwargs):
            result = super().run(*args, **kwargs)
            result["pages"][0]["links"].append("person_recorder")
            with pytest.raises(ValueError):
                kwargs["validator"](result)
            corrected = {
                "pages": [
                    {
                        "id": "person_recorder",
                        "type": "person",
                        "title": "記録者",
                        "body": "記録を担当する。",
                        "data": {"role": None, "team_ids": [], "project_ids": []},
                        "links": [],
                        "evidence": result["pages"][0]["evidence"],
                    }
                ],
                "questions": result["questions"],
            }
            kwargs["validator"](corrected)
            return corrected

    engine.llm_factory = RepairLLM
    ingest(store, engine)
    pages = engine.wiki.snapshot()["pages"]
    assert len(pages) == 5
    assert {"person_recorder", "task_test"} <= pages.keys()


def test_ingest_preserves_raw_and_links(context):
    _, store, engine = context
    source, jid, result = ingest(store, engine)
    snap = engine.wiki.snapshot()
    assert len(snap["pages"]) == 4
    assert (
        snap["pages"]["context_environment"]["data"]["attribution"]
        == "reported_statement"
    )
    assert (store.root / "raw" / source["id"] / "content.md").read_text() == SAMPLE
    assert source["id"] in snap["manifest"]["source_ids"]
    assert len(engine.questions()) == 1
    before = snap["revision"]
    engine.process(store.job(jid))
    assert engine.wiki.snapshot()["revision"] == before


def test_llm_corrects_invalid_tool_result(context):
    from our_context.llm import LLM

    llm = LLM(context[0])
    seen = []
    options = []

    def completion(messages, **kwargs):
        seen.append(copy.deepcopy(messages))
        options.append(kwargs)
        if len(seen) == 2:
            return {
                "role": "assistant",
                "content": '{"quote":"原文","pages":[{"id":"person_sato"},{"id":"person_tanaka"}]}',
            }
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": str(len(seen)),
                    "type": "function",
                    "function": {
                        "name": "finish_result",
                        "arguments": json.dumps(
                            {"quote": "原文" if len(seen) == 2 else "言い換え"}
                        ),
                    },
                }
            ],
        }

    def validate(result):
        if result["quote"] != "原文":
            error = ValueError("原文から引用してください。")
            error.missing_page_ids = ["person_sato"]
            error.repair_page_ids = ["person_sato"]
            raise error

    llm.completion = completion
    assert llm.run(
        "全ページを再作成する。",
        "原文",
        [],
        lambda *_: None,
        response_schema={"type": "object"},
        validator=validate,
        repair_policy="不足項目を補正する。",
    ) == {"quote": "原文", "pages": [{"id": "person_sato"}]}
    repair = json.loads(seen[1][-1]["content"])
    assert "原文から引用" in repair["validation_errors"][0]
    assert repair["pages_to_add"] == [{"id": "person_sato"}]
    assert repair["result_to_correct"]["pages"] == []
    assert len(seen[1]) == 2
    assert "不足項目を補正" in seen[1][0]["content"]
    assert "全ページを再作成" not in seen[1][0]["content"]
    assert seen[1][-1]["role"] == "user"
    assert options[1]["json_mode"] is True and options[1]["tools"] is None


def test_missing_deadline_question_is_created_and_deduplicated(context):
    _, store, engine = context

    class NoQuestions(FixtureLLM):
        def run(self, *args, **kwargs):
            return {**super().run(*args, **kwargs), "questions": []}

    engine.llm_factory = NoQuestions
    ingest(store, engine)
    ingest(store, engine, SAMPLE + "\n進捗を確認した。")
    questions = engine.questions()
    assert len(questions) == 1
    assert questions[0]["data"]["field"] == "deadline"
    assert questions[0]["data"]["subject_id"] == "task_test"


def test_correction_keeps_previous_revision(context):
    _, store, engine = context
    source, _, old = ingest(store, engine)
    corrected = SAMPLE + "\n回答：締め切りは2026-11-06。\n"
    newer, _, result = ingest(store, engine, corrected)
    assert newer["id"] != source["id"]
    assert (
        engine.wiki.snapshot()["pages"]["task_test"]["data"]["deadline"] == "2026-11-06"
    )
    assert (
        engine.wiki.snapshot(old["revision"])["pages"]["task_test"]["data"]["deadline"]
        is None
    )


def test_fabricated_quote_keeps_active_wiki(context):
    _, store, engine = context
    ingest(store, engine)
    before = engine.wiki.snapshot()
    broken = copy.deepcopy(before)
    broken["pages"]["task_test"]["evidence"][0]["quote"] = "原文に存在しない引用"
    with pytest.raises(ValueError):
        engine.wiki.publish(broken, uid("job"))
    assert store.get("revision") == before["revision"]


@pytest.mark.parametrize(
    "issue", ["missing_recorder", "unknown_status", "missing_attribution"]
)
def test_incomplete_entity_result_is_rejected(context, issue):
    _, store, engine = context
    ingest(store, engine)
    snapshot = engine.wiki.snapshot()
    if issue == "missing_recorder":
        snapshot["pages"]["context_environment"]["data"]["recorder_id"] = (
            "person_missing"
        )
    elif issue == "unknown_status":
        snapshot["pages"]["task_test"]["data"]["status"] = "unknown"
        page = snapshot["pages"]["context_environment"]
        page["body"] = "作業は現在停止中。"
        page["data"].update(subject_ids=["task_test"], resolution_state="open")
    else:
        del snapshot["pages"]["context_environment"]["data"]["attribution"]
    with pytest.raises(ValueError):
        engine.wiki.validate(snapshot["pages"], set(snapshot["manifest"]["source_ids"]))


def test_model_failure_retains_source(context):
    _, store, engine = context
    ingest(store, engine)
    before = store.get("revision")
    source = store.source("追記", "追加のメモ")

    class FailedLLM(FixtureLLM):
        def run(self, *args, **kwargs):
            raise ValueError("fixture unavailable")

    engine.llm_factory = FailedLLM
    jid = store.enqueue("ingest", {"source_ids": [source["id"]]})
    with pytest.raises(ValueError):
        engine.process(store.job(jid))
    assert store.get("revision") == before
    assert store.read_source(source["id"])["body"] == "追加のメモ"


def test_auth_csrf_idempotency(context):
    s, _, _ = context
    client = TestClient(create_app(s))
    assert client.get("/api/v1/bootstrap").status_code == 401
    login = client.post("/api/v1/auth/login", json={"password": "test-password"})
    csrf = login.json()["csrf"]
    assert (
        client.post("/api/v1/sources", json={"title": "a", "text": "b"}).status_code
        == 403
    )
    headers = {"x-csrf-token": csrf, "Idempotency-Key": "same"}
    body = {"title": "メモ", "text": "作業内容"}
    first = client.post("/api/v1/sources", json=body, headers=headers)
    second = client.post("/api/v1/sources", json=body, headers=headers)
    assert first.status_code == 202
    assert second.json() == first.json()
    assert (
        client.post(
            "/api/v1/sources", json={**body, "text": "変更"}, headers=headers
        ).status_code
        == 409
    )


def test_webhook_signature_scope_and_deduplication(context):
    s, _, _ = context
    client = TestClient(create_app(s))
    payload = {
        "repository": {"full_name": "team/notes"},
        "ref": "refs/heads/main",
        "after": "a" * 40,
    }
    body = json.dumps(payload).encode()
    headers = {"x-gitea-event": "push", "x-gitea-delivery": "delivery-1"}
    assert client.post("/hooks/gitea", content=body, headers=headers).status_code == 403
    headers["x-gitea-signature"] = hmac.new(
        s.webhook_secret.encode(), body, hashlib.sha256
    ).hexdigest()
    first = client.post("/hooks/gitea", content=body, headers=headers)
    assert first.status_code == 202
    assert (
        client.post("/hooks/gitea", content=body, headers=headers).json()
        == first.json()
    )
    payload["ref"] = "refs/heads/other"
    body = json.dumps(payload).encode()
    headers["x-gitea-signature"] = hmac.new(
        s.webhook_secret.encode(), body, hashlib.sha256
    ).hexdigest()
    assert (
        client.post("/hooks/gitea", content=body, headers=headers).json()["status"]
        == "ignored"
    )


def test_answer_reflection_and_defer(context):
    s, store, engine = context
    ingest(store, engine)
    question = engine.questions()[0]
    client = TestClient(create_app(s))
    csrf = client.post("/api/v1/auth/login", json={"password": s.password}).json()[
        "csrf"
    ]
    headers = {"x-csrf-token": csrf, "Idempotency-Key": "answer-1"}
    result = client.post(
        f"/api/v1/questions/{question['id']}/answers",
        json={
            "text": "締め切りは2026-11-06。",
            "question_version": question["version"],
        },
        headers=headers,
    )
    assert result.status_code == 202
    assert engine.questions()[0]["status"] == "answering"

    class NoDateModel(FixtureLLM):
        def run(self, *args, **kwargs):
            raise AssertionError("明確な日付は確認した項目へ直接反映する。")

    engine.llm_factory = NoDateModel
    reflected = engine.process(store.job(result.json()["job_id"]))
    assert reflected["confirmed_field"] == "deadline"
    assert engine.questions()[0]["status"] == "answered"
    assert (
        engine.wiki.snapshot()["pages"]["task_test"]["data"]["deadline"] == "2026-11-06"
    )


def test_progress_update_retains_known_dates(context):
    _, store, engine = context
    known = SAMPLE + "\n回答：締め切りは2026-11-06。\n"
    ingest(store, engine)
    ingest(store, engine, known)
    assert (
        engine.wiki.snapshot()["pages"]["task_test"]["data"]["deadline"] == "2026-11-06"
    )

    class ForgotDeadline(FixtureLLM):
        def run(self, *args, **kwargs):
            result = super().run(*args, **kwargs)
            for page in result["pages"]:
                if page["type"] == "task":
                    page["data"].pop("deadline", None)
            return result

    engine.llm_factory = ForgotDeadline
    ingest(store, engine, known + "\n田中さんがテストの準備を進めた。")
    assert (
        engine.wiki.snapshot()["pages"]["task_test"]["data"]["deadline"] == "2026-11-06"
    )
    ingest(store, engine, known + "\n締め切りは未定に戻す。")
    assert not engine.wiki.snapshot()["pages"]["task_test"]["data"].get("deadline")


def test_query_tools_resolve_project_names(context):
    _, store, engine = context
    ingest(store, engine)

    class NamedProject(FixtureLLM):
        def run(self, system, user, tools, handler, **kwargs):
            assert {"id": "project_auth", "title": "認証改善"} in json.loads(user)[
                "projects"
            ]
            tool = next(t for t in tools if t["function"]["name"] == "create_export")
            assert (
                "project_auth"
                in tool["function"]["parameters"]["properties"]["project_id"]["enum"]
            )
            related = handler("list_context_records", {"subject_id": "person_tanaka"})
            assert any(
                p["type"] == "task" and p["data"]["status"] == "blocked"
                for p in related
            )
            handler("show_gantt", {"project_id": "認証"})
            handler(
                "create_export",
                {
                    "project_id": "認証改善",
                    "format": "xlsx",
                    "start": "2026-11-01",
                    "end": "2026-11-30",
                },
            )
            return {
                "answer": "ガントと報告を作成しました。",
                "citations": [],
                "questions": [],
                "insufficient": False,
            }

    engine.llm_factory = NamedProject
    jid = store.enqueue(
        "query", {"text": "ガントと Excel を表示", "conversation": "named-project"}
    )
    result = engine.process(store.job(jid))
    assert result["tools"][0]["project_id"] == "project_auth"
    assert result["citations"]
    assert "田中" in result["answer"]
    export = result["tools"][1]
    assert export["filters"]["project_id"] == "project_auth"
    with store.db() as c:
        path = c.execute(
            "SELECT path FROM exports WHERE id=?", (export["id"],)
        ).fetchone()["path"]
    assert load_workbook(path)["タスク"].max_row == 2


def test_incremental_progress_updates_state_and_preserves_deadline(context):
    _, store, engine = context
    ingest(store, engine)
    known = SAMPLE + "\n回答：締め切りは2026-11-06。\n"
    ingest(store, engine, known)

    class DeltaModel(FixtureLLM):
        def run(self, system, user, tools, handler, **kwargs):
            assert kwargs.get("structured") is True
            data = json.loads(user)
            quote = next(
                line
                for line in data["sources"][0]["changes"].splitlines()
                if "権限を取得" in line
            )
            return {
                "updates": [
                    {
                        "id": "task_test",
                        "data": {"status": "in_progress", "deadline": None},
                        "append": "接続権限を取得して結合テストを開始した。",
                        "quote": quote,
                        "clear_fields": [],
                    },
                    {
                        "id": "context_environment",
                        "data": {"resolution_state": "resolved"},
                        "append": "環境の困りごとは解決した。",
                        "quote": quote,
                        "clear_fields": [],
                    },
                ],
                "needs_new_pages": False,
                "questions": [],
                "resolved_questions": [],
            }

    engine.llm_factory = DeltaModel
    source, _, _ = ingest(
        store,
        engine,
        known
        + "\n## 2026-11-02\n田中さんは接続権限を取得した。環境の困りごとは解決し、結合テストは進行中。",
    )
    pages = engine.wiki.snapshot()["pages"]
    assert pages["task_test"]["data"]["status"] == "in_progress"
    assert pages["task_test"]["data"]["deadline"] == "2026-11-06"
    assert pages["context_environment"]["data"]["resolution_state"] == "resolved"
    assert pages["task_test"]["data"]["events"][-1]["source_id"] == source["id"]
    assert pages["task_test"]["data"]["events"][-1]["date"] == "2026-11-02"


def test_lint_repairs_question_citation_shape(context):
    from our_context.llm import LLM

    settings, store, engine = context
    ingest(store, engine)
    llm = LLM(settings)
    calls = []

    def completion(messages, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            value = {
                "issues": [],
                "questions": [
                    {
                        "question": "締め切りを確認してください。",
                        "reason": "予定を確認するため。",
                        "impact": "予定の調整",
                        "evidence": "田中さんの発言",
                    }
                ],
            }
        else:
            assert kwargs.get("output_schema")
            value = {"issues": [], "questions": []}
        return {"role": "assistant", "content": json.dumps(value, ensure_ascii=False)}

    llm.completion = completion
    engine.llm_factory = lambda _: llm
    job_id = store.enqueue("lint", {})
    result = engine.process(store.job(job_id))
    assert isinstance(result["issues"], list)
    assert len(calls) == 2


def test_simulation_cascades_and_finds_deadline(context):
    _, store, engine = context
    ingest(store, engine)
    snap = engine.wiki.snapshot()
    next_task = copy.deepcopy(snap["pages"]["task_test"])
    next_task.update(id="task_release", title="リリース")
    next_task["data"].update(
        plan_start="2026-11-04",
        plan_end="2026-11-05",
        deadline="2026-11-05",
        dependency_ids=["task_test"],
    )
    snap["pages"]["task_release"] = next_task
    result = engine.simulate(
        snap, [{"task_id": "task_test", "set": {"plan_end": "2026-11-05"}}]
    )
    release = next(i for i in result["operations"] if i["task_id"] == "task_release")
    assert release["set"]["plan_start"] == "2026-11-06"
    assert release["set"]["plan_end"] == "2026-11-07"
    assert result["warnings"]
    assert (
        engine.wiki.snapshot()["pages"]["task_test"]["data"]["plan_end"] == "2026-11-03"
    )


def test_apply_and_stale_proposal(context):
    s, store, engine = context
    ingest(store, engine)
    old = store.get("revision")
    proposal = engine.propose(
        old,
        [
            {
                "task_id": "task_test",
                "set": {"plan_start": "2026-11-04", "plan_end": "2026-11-05"},
            }
        ],
    )
    jid = store.enqueue("apply", {"proposal_id": proposal["id"]})
    engine.process(store.job(jid))
    assert (
        engine.wiki.snapshot()["pages"]["task_test"]["data"]["plan_end"] == "2026-11-05"
    )
    client = TestClient(create_app(s))
    csrf = client.post("/api/v1/auth/login", json={"password": s.password}).json()[
        "csrf"
    ]
    response = client.post(
        "/api/v1/change-proposals/" + proposal["id"] + "/apply",
        json={},
        headers={"x-csrf-token": csrf, "Idempotency-Key": "apply-stale"},
    )
    assert response.status_code == 409


def test_excel_keeps_strings_and_period(context):
    _, store, engine = context
    ingest(store, engine)
    snap = engine.wiki.snapshot()
    snap["pages"]["task_test"]["title"] = '=HYPERLINK("https://example.invalid")'
    result = engine.export(
        snap, {"format": "xlsx", "start": "2026-11-02", "end": "2026-11-03"}
    )
    with store.db() as c:
        path = c.execute(
            "SELECT path FROM exports WHERE id=?", (result["id"],)
        ).fetchone()[0]
    workbook = load_workbook(path)
    assert workbook["タスク"]["C2"].data_type == "s"
    assert workbook["進捗"].max_row == 1
    assert "現在のタスク" in result["text"]


def test_export_filters_person_and_keeps_related_questions(context):
    _, store, engine = context
    ingest(store, engine)
    snap = engine.wiki.snapshot()
    unrelated = copy.deepcopy(snap["pages"]["task_test"])
    unrelated.update(
        id="task_other", title="別担当の作業", body="別担当の作業を進めた。", links=[]
    )
    unrelated["data"]["assignee_ids"] = []
    snap["pages"]["task_other"] = unrelated
    result = engine.export(snap, {"format": "markdown", "scope": "person_tanaka"})
    assert "別担当の作業" not in result["text"]
    assert "結合テスト：困りごとあり。担当：田中さん" in result["text"]
    assert "結合テストの締め切りはいつですか？" in result["text"]


def test_team_scope_follows_projects_and_questions(context):
    _, store, engine = context
    source, _, _ = ingest(store, engine)
    snap = engine.wiki.snapshot()
    snap["pages"]["team_auth"] = {
        "id": "team_auth",
        "type": "team",
        "title": "認証チーム",
        "body": "認証改善を担当する。",
        "data": {},
        "links": ["project_auth"],
        "evidence": [{"source_id": source["id"], "quote": "## 2026-11-01"}],
    }
    unrelated = copy.deepcopy(snap["pages"]["task_test"])
    unrelated.update(id="task_other", title="他チームの作業")
    unrelated["data"].update(project_id="project_other", assignee_ids=[])
    snap["pages"]["task_other"] = unrelated
    assert [t["id"] for t in engine.task_list(snap, {"scope": "team_auth"})] == [
        "task_test"
    ]
    report = engine.export(snap, {"format": "markdown", "scope": "team_auth"})
    assert "結合テストの締め切りはいつですか？" in report["text"]
    assert "他チームの作業：" not in report["text"].split("## 現在のタスク")[1]


def test_backup_restore(context, tmp_path):
    s, store, engine = context
    ingest(store, engine)
    import sys

    environment = {**os.environ, "WORKSPACE_DIR": str(s.root)}
    subprocess.check_call(
        [sys.executable, "-m", "our_context.manage", "backup"], env=environment
    )
    backup = next((s.root / "backups").glob("*.tar.gz"))
    destination = s.root / "restored"
    subprocess.check_call(
        [
            sys.executable,
            "-m",
            "our_context.manage",
            "restore",
            "--file",
            str(backup),
            "--destination",
            str(destination),
        ],
        env=environment,
    )
    restored = Store(destination)
    assert (
        Wiki_for_restore(restored).snapshot()["pages"]["task_test"]["title"]
        == "結合テスト"
    )


def Wiki_for_restore(store):
    from our_context.wiki import Wiki

    return Wiki(store)


def test_real_git_diff_and_allowed_paths(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-b", "main")
    git(work, "config", "user.name", "test")
    git(work, "config", "user.email", "test@localhost")
    (work / "work-log.md").write_text(SAMPLE)
    (work / "secret.md").write_text("対象外")
    git(work, "add", ".")
    git(work, "commit", "-m", "Initial")
    server = tmp_path / "server"
    bare = server / "team/notes.git"
    bare.parent.mkdir(parents=True)
    subprocess.check_call(["git", "clone", "--bare", str(work), str(bare)])
    root = tmp_path / "app"
    root.mkdir()
    s = Settings(
        root=root,
        gitea_url=str(server),
        gitea_repo="team/notes",
        allowed_paths=["work-log.md"],
    )
    monkeypatch.setattr(Gitea, "check", lambda self: {"id": 1})
    connector = Gitea(s)
    commit, files = connector.fetch()
    assert set(files) == {"work-log.md"}
    assert connector.read(commit, "work-log.md") == SAMPLE
    assert connector.fetch(commit, commit)[1] is None


def test_plain_japanese_preserves_quotes():
    assert_plain_japanese("接続先は環境変数で設定する。")
    assert_plain_japanese("引用：『この件ではない』。")
    with pytest.raises(ValueError):
        assert_plain_japanese("これは説明ではない。")

"""接続テスト用の小さなモデル。固定の業務メモを整理する。"""

import json
import re


SAMPLE = """## 2026-11-01
認証改善プロジェクトではログイン障害を減らす。
田中さんが結合テストの環境構築で困っていると話した。
結合テストは2026-11-02から2026-11-03に実施予定。期限は未定。
田中さんが結合テストを担当する。
"""


def compile_fixture(source_id, body, old_pages=None):
    old_pages = old_pages or []
    evidence = [
        {
            "source_id": source_id,
            "quote": next(line for line in body.splitlines() if line.strip()),
        }
    ]
    if old_pages:
        pages = []
        for old in old_pages:
            page = json.loads(json.dumps(old))
            if page["id"] == "task_test":
                page["body"] += "\n\n" + body
                page["evidence"] += evidence
                found = re.search(r"回答：.*?(2026-\d{2}-\d{2})", body, re.S)
                if found:
                    page["data"]["deadline"] = found[1]
            pages.append(page)
        return {"pages": pages, "questions": [], "resolved_questions": []}
    pages = [
        {
            "id": "project_auth",
            "type": "project",
            "title": "認証改善",
            "summary": "ログイン障害を減らす。",
            "body": "# 認証改善\n\n" + body,
            "data": {
                "purpose": "ログイン障害を減らす。",
                "team_ids": [],
                "member_ids": ["person_tanaka"],
            },
            "evidence": evidence,
            "links": ["person_tanaka", "task_test", "context_environment"],
        },
        {
            "id": "person_tanaka",
            "type": "person",
            "title": "田中さん",
            "summary": "結合テストを担当する。",
            "body": "# 田中さん\n\n" + body,
            "data": {"role": None, "team_ids": [], "project_ids": ["project_auth"]},
            "evidence": evidence,
            "links": ["project_auth", "context_environment"],
        },
        {
            "id": "task_test",
            "type": "task",
            "title": "結合テスト",
            "summary": "テスト環境を整え、結合テストを行う。",
            "body": "# 結合テスト\n\n" + body,
            "data": {
                "project_id": "project_auth",
                "assignee_ids": ["person_tanaka"],
                "status": "blocked",
                "plan_start": "2026-11-02",
                "plan_end": "2026-11-03",
                "deadline": None,
                "ticket": None,
                "completion_criteria": None,
                "dependency_ids": [],
                "events": [
                    {
                        "date": "2026-11-01",
                        "description": "テスト環境の困りごとを記録した。",
                        "source_id": source_id,
                    }
                ],
            },
            "evidence": evidence,
            "links": ["project_auth", "person_tanaka", "context_environment"],
        },
        {
            "id": "context_environment",
            "type": "context",
            "title": "田中さんの環境構築の困りごと",
            "summary": "田中さんが結合テストの環境構築で困っていると話した。",
            "body": "# 環境構築の困りごと\n\n" + body,
            "data": {
                "kind": "blocker",
                "subject_ids": ["person_tanaka", "project_auth"],
                "speaker_id": "person_tanaka",
                "recorder_id": "owner",
                "attribution": "reported_statement",
                "occurred_at": "2026-11-01",
                "confirmation_state": "confirmed",
                "resolution_state": "open",
            },
            "evidence": evidence,
            "links": ["person_tanaka", "task_test", "project_auth"],
        },
    ]
    return {
        "pages": pages,
        "questions": [
            {
                "semantic_key": "task_test:deadline",
                "subject_id": "task_test",
                "field": "deadline",
                "question": "結合テストの締め切りはいつですか？",
                "reason": "実施予定と締め切りの関係を確認するため。",
                "impact": "テストとリリースの予定",
                "priority": "normal",
                "evidence": evidence,
            }
        ],
    }


def model_response(messages, tools):
    if any(t.get("function", {}).get("name") == "connection_test" for t in tools or []):
        return call("connection_test", {"value": "connected"})
    first_user = next(
        (m.get("content", "") for m in messages if m["role"] == "user"), ""
    )
    try:
        data = json.loads(first_user)
    except ValueError:
        return {"role": "assistant", "content": "connected"}
    tool_messages = [m for m in messages if m["role"] == "tool"]
    if "sources" in data:
        if not tool_messages:
            return call(
                "read_sources",
                {"id": data["sources"][0]["id"], "start_line": 1, "line_count": 200},
            )
        source = next(
            json.loads(m["content"])
            for m in tool_messages
            if isinstance(json.loads(m["content"]), dict)
            and "lines" in json.loads(m["content"])
        )
        if data.get("index") and len(tool_messages) == 1:
            return call("read_pages", {"ids": [p["id"] for p in data["index"]]})
        old = json.loads(tool_messages[-1]["content"]) if len(tool_messages) > 1 else []
        body = "\n".join(line["text"] for line in source["lines"])
        return final(compile_fixture(source["id"], body, old))
    if "question" in data:
        if not tool_messages:
            return call("search_wiki", {"query": ""})
        if len(tool_messages) == 1:
            index = json.loads(tool_messages[0]["content"])
            return call("read_pages", {"ids": [p["id"] for p in index]})
        pages = next(
            (
                json.loads(m["content"])
                for m in tool_messages
                if isinstance(json.loads(m["content"]), list)
                and json.loads(m["content"])
                and "body" in json.loads(m["content"])[0]
            ),
            [],
        )
        if "ガント" in data["question"] and len(tool_messages) == 2:
            return call("show_gantt", {"project_id": "project_auth"})
        if "Excel" in data["question"] and len(tool_messages) == 2:
            return call(
                "create_export",
                {
                    "format": "xlsx",
                    "project_id": "project_auth",
                    "start": "2026-11-01",
                    "end": "2026-11-07",
                },
            )
        page = next(
            (p for p in pages if p["id"] == "context_environment"),
            pages[0] if pages else None,
        )
        return final(
            {
                "answer": page["summary"] if page else "業務の記録を追加してください。",
                "citations": page["evidence"] if page else [],
                "questions": [],
                "insufficient": not bool(page),
            }
        )
    return final({"issues": [], "questions": []})


def call(name, arguments):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_" + name,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            }
        ],
    }


def final(data):
    return {"role": "assistant", "content": json.dumps(data, ensure_ascii=False)}

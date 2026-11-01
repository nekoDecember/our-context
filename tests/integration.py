"""Docker 上の模擬サービスを使う一連の業務フロー。"""

import hashlib
import hmac
from io import BytesIO
import json
import os
import time
import uuid

import httpx
from openpyxl import load_workbook


base = os.getenv("TEST_URL", "http://app:8096")
client = httpx.Client(base_url=base, timeout=20)
for _ in range(60):
    try:
        if client.get("/healthz").is_success:
            break
    except httpx.NetworkError:
        pass
    time.sleep(0.5)
else:
    raise RuntimeError("API の起動を確認してください。")

login = client.post("/api/v1/auth/login", json={"password": "integration-only"})
login.raise_for_status()
csrf = login.json()["csrf"]


def post(path, body=None, method="POST"):
    response = client.request(
        method,
        "/api/v1" + path,
        json=body or {},
        headers={"x-csrf-token": csrf, "Idempotency-Key": str(uuid.uuid4())},
    )
    if response.is_error:
        raise AssertionError(f"{path}: {response.status_code} {response.text}")
    return response.json()


def wait(result):
    for _ in range(120):
        job = client.get("/api/v1/jobs/" + result["job_id"]).json()
        if job["status"] == "succeeded":
            return job["result"]
        if job["status"] == "failed":
            raise AssertionError(job["error"])
        time.sleep(0.5)
    raise AssertionError("ジョブが制限時間に達しました。")


assert post("/settings/check/gitea")["full_name"] == "team/notes"
assert post("/settings/check/llm")["tool_calling"]
wait(post("/connectors/gitea/sync"))
boot = client.get("/api/v1/bootstrap").json()
assert len(boot["pages"]) == 4
assert boot["tasks"][0]["status"] == "blocked"
assert all(s["title"] != "secret.txt" for s in client.get("/api/v1/sources").json())

advance = httpx.post("http://fixture:9080/__test__/advance", timeout=20).json()
payload = json.dumps(
    {
        "repository": {"full_name": "team/notes"},
        "ref": "refs/heads/main",
        "after": advance["commit"],
    }
).encode()
headers = {
    "x-gitea-event": "push",
    "x-gitea-delivery": str(uuid.uuid4()),
    "x-gitea-signature": hmac.new(
        b"fixture-secret", payload, hashlib.sha256
    ).hexdigest(),
}
hook = client.post("/hooks/gitea", content=payload, headers=headers)
assert hook.status_code == 202, hook.text
assert (
    client.post("/hooks/gitea", content=payload, headers=headers).json() == hook.json()
)
wait(hook.json())

question = client.get("/api/v1/questions").json()[0]
wait(
    post(
        "/questions/" + question["id"] + "/answers",
        {"text": "締め切りは2026-11-06。", "question_version": question["version"]},
    )
)
assert client.get("/api/v1/questions").json()[0]["status"] == "answered"
boot = client.get("/api/v1/bootstrap").json()
assert boot["tasks"][0]["deadline"] == "2026-11-06"
proposal = post(
    "/change-proposals",
    {
        "base_revision": boot["revision"],
        "operations": [
            {
                "task_id": "task_test",
                "set": {
                    "plan_start": "2026-11-04",
                    "plan_end": "2026-11-05",
                    "status": "in_progress",
                },
            }
        ],
        "reason": "結合テストの予定変更",
    },
)
wait(post("/change-proposals/" + proposal["id"] + "/apply"))
boot = client.get("/api/v1/bootstrap").json()
assert boot["tasks"][0]["plan_end"] == "2026-11-05"

conversation = post("/conversations")["id"]
reply = wait(
    post(
        "/conversations/" + conversation + "/queries",
        {"text": "誰が何に困っている？ ガントも見せて", "revision": boot["revision"]},
    )
)
assert reply["citations"] and reply["tools"][0]["type"] == "gantt"
reply2 = wait(
    post(
        "/conversations/" + conversation + "/queries",
        {"text": "Excel で週次報告を作成して", "revision": boot["revision"]},
    )
)
assert reply2["tools"][0]["type"] == "export"
assert client.get(reply2["tools"][0]["url"]).is_success

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
file = client.get(report["url"])
file.raise_for_status()
book = load_workbook(BytesIO(file.content))
assert book.sheetnames == ["出力条件", "タスク", "進捗", "困りごと・確認事項"]
assert book["タスク"]["G2"].value == "2026-11-05"
markdown = wait(post("/exports", {"format": "markdown", "revision": boot["revision"]}))
assert "現在のタスク" in client.get(markdown["url"]).text
wait(post("/messages/" + reply["message_id"] + "/save"))
assert any(
    p["type"] == "analysis" for p in client.get("/api/v1/bootstrap").json()["pages"]
)
wait(post("/lint-runs"))

print(
    json.dumps(
        {
            "result": "passed",
            "checks": [
                "Gitea API",
                "Git 取得",
                "LiteLLM tool calling",
                "Wiki 整理",
                "push 通知",
                "通知重複",
                "確認への回答",
                "予定変更",
                "文脈付き相談",
                "ガント操作",
                "会話からの Excel 出力",
                "Excel ダウンロード",
                "報告文",
                "回答の Wiki 保存",
                "Wiki 点検",
            ],
        },
        ensure_ascii=False,
        indent=2,
    )
)

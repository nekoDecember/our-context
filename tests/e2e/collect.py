"""成功した E2E の通知・公開版・原資料を照合して記録する。"""

import json
import os
from pathlib import Path
import sqlite3

import httpx


evidence = Path("/evidence")
outcome = json.loads((evidence / "outcome.json").read_text())
assert outcome["result"] == "passed", outcome["result"]
client = httpx.Client(base_url="http://app:8096", timeout=30)
r = client.post("/api/v1/auth/login", json={"password": os.environ["APP_PASSWORD"]})
r.raise_for_status()
boot = client.get("/api/v1/bootstrap").raise_for_status().json()
sources = client.get("/api/v1/sources").raise_for_status().json()
originals = {
    s["id"]: client.get("/api/v1/sources/" + s["id"]).raise_for_status().json()
    for s in sources
}
for page in boot["pages"]:
    for item in page.get("evidence", []):
        assert item["quote"] in originals[item["source_id"]]["body"]

gitea = sqlite3.connect("file:/gitea-data/gitea/gitea.db?mode=ro", uri=True)
gitea.row_factory = sqlite3.Row
deliveries = []
for step in outcome["steps"]:
    if step["step"] != "Gitea が push を通知":
        continue
    row = gitea.execute(
        "SELECT uuid,is_succeed,response_content FROM hook_task WHERE uuid=?",
        (step["delivery_id"],),
    ).fetchone()
    assert row and row["is_succeed"]
    response = json.loads(row["response_content"])
    assert response["status"] == 202, response
    deliveries.append(
        {
            "commit": step["commit"],
            "delivery_id": row["uuid"],
            "job_id": step["job_id"],
            "http_status": response["status"],
        }
    )
assert len(deliveries) == 2, deliveries
result = {
    "result": "passed",
    "revision": boot["revision"],
    "deliveries": deliveries,
    "citations_match_originals": True,
    "pages": len(boot["pages"]),
    "sources": len(sources),
    "tasks": boot["tasks"],
}
for name, data in (("verification.json", result), ("wiki-final.json", boot)):
    (evidence / name).write_text(json.dumps(data, ensure_ascii=False, indent=2))
print(
    json.dumps(
        {"result": result["result"], "deliveries": deliveries}, ensure_ascii=False
    )
)

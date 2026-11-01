"""専用 Gitea の利用者・読取トークン・リポジトリ・Webhook を用意する。"""

import base64
import json
from pathlib import Path
import subprocess
import shutil
import time
import urllib.error
import urllib.request
import uuid

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
ENV = HERE / ".env"
values = dict(
    line.split("=", 1)
    for line in ENV.read_text().splitlines()
    if line and not line.startswith("#") and "=" in line
)
BASE = "http://127.0.0.1:13096/api/v1"
auth = base64.b64encode(
    (values["GITEA_USERNAME"] + ":" + values["E2E_GITEA_PASSWORD"]).encode()
).decode()


def api(path, body=None, method=None, allow=(), authenticated=True):
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Content-Type": "application/json",
            **({"Authorization": "Basic " + auth} if authenticated else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        if exc.code in allow:
            return None
        raise RuntimeError(f"Gitea {path}: HTTP {exc.code}") from exc


for _ in range(60):
    try:
        version = api("/version", authenticated=False)
        break
    except (OSError, RuntimeError):
        time.sleep(1)
else:
    raise RuntimeError("Gitea の起動を確認してください。")

if api("/users/" + values["GITEA_USERNAME"], allow=(404,), authenticated=False) is None:
    result = subprocess.run(
        [
            shutil.which("docker-compose") or "docker",
            *([] if shutil.which("docker-compose") else ["compose"]),
            "--env-file",
            str(ENV),
            "-f",
            str(HERE / "compose.yaml"),
            "exec",
            "-T",
            "--user",
            "git",
            "gitea",
            "sh",
            "-c",
            "gitea --config /data/gitea/conf/app.ini admin user create "
            "--username e2e-owner --email e2e@example.invalid --admin "
            '--password "$E2E_GITEA_PASSWORD" --must-change-password=false',
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.replace(values["E2E_GITEA_PASSWORD"], "[key]"))

repo_path = "/repos/" + values["GITEA_REPO"]
repo = api(repo_path, allow=(404,))
if repo is None:
    repo = api(
        "/user/repos",
        {
            "name": "work-notes",
            "private": True,
            "auto_init": True,
            "default_branch": "main",
        },
    )

if not values.get("GITEA_READ_TOKEN"):
    token = api(
        "/users/e2e-owner/tokens",
        {
            "name": "our-context-read-" + uuid.uuid4().hex[:8],
            "scopes": ["read:repository"],
        },
    )
    values["GITEA_READ_TOKEN"] = token["sha1"]
    ENV.write_text("".join(key + "=" + value + "\n" for key, value in values.items()))
    ENV.chmod(0o600)

target = "http://app:8096/hooks/gitea"
hooks = api(repo_path + "/hooks")
hook = next(
    (item for item in hooks if item.get("config", {}).get("url") == target), None
)
if hook is None:
    hook = api(
        repo_path + "/hooks",
        {
            "type": "gitea",
            "active": True,
            "events": ["push"],
            "branch_filter": "main",
            "config": {
                "url": target,
                "content_type": "json",
                "http_method": "POST",
                "secret": values["GITEA_WEBHOOK_SECRET"],
            },
        },
    )
evidence = {
    "gitea_version": version["version"],
    "repository": repo["full_name"],
    "private": repo["private"],
    "hook_id": hook["id"],
    "webhook_target": target,
    "read_token_scope": "read:repository",
}
(HERE / ".runtime" / "provision.json").write_text(
    json.dumps(evidence, ensure_ascii=False, indent=2)
)
print(json.dumps(evidence, ensure_ascii=False, indent=2))

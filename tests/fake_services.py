import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess

from fixture_model import SAMPLE, model_response


def command(directory, *args):
    return (
        subprocess.check_output(
            ["git", "-C", str(directory), *args], stderr=subprocess.DEVNULL
        )
        .decode()
        .strip()
    )


ROOT = Path("/tmp/our-context-fixture")
WORK = ROOT / "work"
REPO = ROOT / "public/team/notes.git"
WORK.mkdir(parents=True, exist_ok=True)
command(WORK, "init", "-b", "main")
command(WORK, "config", "user.name", "Fixture")
command(WORK, "config", "user.email", "fixture@localhost")
(WORK / "work-log.md").write_text(SAMPLE)
(WORK / "secret.txt").write_text("対象外ファイル")
command(WORK, "add", ".")
command(WORK, "commit", "-m", "Initial note")
REPO.parent.mkdir(parents=True, exist_ok=True)
subprocess.check_call(
    ["git", "clone", "--bare", str(WORK), str(REPO)], stderr=subprocess.DEVNULL
)
command(REPO, "update-server-info")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / "public"), **kwargs)

    def respond(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/v1/repos/team/notes"):
            return self.respond(
                {"id": 1, "full_name": "team/notes", "default_branch": "main"}
            )
        if self.path == "/__test__/head":
            return self.respond({"commit": command(WORK, "rev-parse", "HEAD")})
        return super().do_GET()

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.path == "/v1/chat/completions":
            payload = json.loads(body)
            message = model_response(payload["messages"], payload.get("tools"))
            return self.respond(
                {
                    "id": "fixture-response",
                    "model": "fixture",
                    "choices": [
                        {
                            "index": 0,
                            "message": message,
                            "finish_reason": "tool_calls"
                            if message.get("tool_calls")
                            else "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 100},
                }
            )
        if self.path == "/__test__/advance":
            with (WORK / "work-log.md").open("a") as f:
                f.write("\n## 2026-11-02\nテスト環境の準備を進めた。\n")
            command(WORK, "add", ".")
            command(WORK, "commit", "-m", "Advance note")
            command(REPO, "fetch", str(WORK), "+refs/heads/main:refs/heads/main")
            command(REPO, "update-server-info")
            return self.respond({"commit": command(WORK, "rev-parse", "HEAD")})
        self.respond({"error": "unknown path"}, 404)


ThreadingHTTPServer(("0.0.0.0", 9080), Handler).serve_forever()

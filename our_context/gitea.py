import base64
import fnmatch
import os
import re
import subprocess

import httpx


class Gitea:
    def __init__(self, settings):
        self.s = settings
        self.path = settings.root / "gitea.git"

    def check(self):
        s = self.s
        if not s.gitea_url or not re.fullmatch(r"[^/\s]+/[^/\s]+", s.gitea_repo):
            raise ValueError("Gitea の URL と所有者/リポジトリ名を設定してください。")
        headers = {"Authorization": "token " + s.gitea_token} if s.gitea_token else {}
        response = httpx.get(
            f"{s.gitea_url}/api/v1/repos/{s.gitea_repo}", headers=headers, timeout=20
        )
        if response.is_error:
            raise ValueError(
                f"Gitea HTTP {response.status_code}。URL・リポジトリ名・読取トークンを確認してください。"
            )
        data = response.json()
        return {
            "id": data["id"],
            "full_name": data["full_name"],
            "default_branch": data.get("default_branch"),
        }

    def command(self, *args, check=True):
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        if self.s.gitea_token:
            credential = base64.b64encode(
                f"{self.s.gitea_user or 'git'}:{self.s.gitea_token}".encode()
            ).decode()
            env.update(
                GIT_CONFIG_COUNT="1",
                GIT_CONFIG_KEY_0=f"http.{self.s.gitea_url}/.extraheader",
                GIT_CONFIG_VALUE_0="Authorization: Basic " + credential,
            )
        result = subprocess.run(
            ["git", "-C", str(self.path), *args],
            env=env,
            capture_output=True,
            timeout=120,
        )
        if result.returncode and check:
            message = result.stderr.decode(errors="replace")
            if self.s.gitea_token:
                message = message.replace(self.s.gitea_token, "[key]")
            raise ValueError("Gitea 取得エラー: " + message[:900])
        return result

    def fetch(self, target=None, previous=None, force=False):
        self.check()
        if not self.path.exists():
            self.path.mkdir(parents=True)
            self.command("init", "--bare")
        self.command("check-ref-format", "refs/heads/" + self.s.gitea_branch)
        url = self.s.gitea_url + "/" + self.s.gitea_repo + ".git"
        self.command(
            "fetch",
            "--no-tags",
            url,
            "+refs/heads/" + self.s.gitea_branch + ":refs/heads/tracked",
        )
        head = self.command("rev-parse", "refs/heads/tracked").stdout.decode().strip()
        target = target or head
        if not re.fullmatch(r"[a-f0-9]{40,64}", target):
            raise ValueError("取得する commit の形式を確認してください。")
        if self.command(
            "merge-base", "--is-ancestor", target, head, check=False
        ).returncode:
            raise ValueError(
                "通知された版と現在のブランチが分岐しています。再同期を実行してください。"
            )
        if previous and not force:
            if not self.command(
                "merge-base", "--is-ancestor", target, previous, check=False
            ).returncode:
                return previous, None
            if self.command(
                "merge-base", "--is-ancestor", previous, target, check=False
            ).returncode:
                raise ValueError(
                    "履歴が分岐しています。設定画面から再同期を実行してください。"
                )
        raw = self.command("ls-tree", "-rz", "--full-tree", target).stdout
        files = {}
        for entry in raw.split(b"\0"):
            if not entry:
                continue
            info, path = entry.split(b"\t", 1)
            mode, kind, blob = info.decode().split()
            name = path.decode()
            if (
                mode not in ("100644", "100755")
                or kind != "blob"
                or not name.lower().endswith((".md", ".txt"))
            ):
                continue
            if any(
                fnmatch.fnmatchcase(name, pattern) for pattern in self.s.allowed_paths
            ):
                files[name] = blob
        return target, files

    def read(self, commit, path):
        body = self.command("show", commit + ":" + path).stdout
        if len(body) > 5 * 1024 * 1024:
            raise ValueError(f"{path} が 5 MiB を超えています。")
        return body.decode("utf-8-sig")

import argparse
from pathlib import Path
import secrets
import sqlite3
import tarfile
from datetime import datetime, timezone

from .config import Settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command", choices=["setup", "check-llm", "check-gitea", "backup", "restore"]
    )
    parser.add_argument("--file")
    parser.add_argument("--destination")
    args = parser.parse_args()
    if args.command == "setup":
        path = Path(".env")
        if not path.exists():
            path.write_text(Path(".env.example").read_text())
        text = path.read_text()
        for key in ("APP_PASSWORD", "GITEA_WEBHOOK_SECRET"):
            if f"{key}=\n" in text:
                text = text.replace(f"{key}=\n", f"{key}={secrets.token_urlsafe(24)}\n")
        path.write_text(text)
        path.chmod(0o600)
        print(".env を作成しました。接続先を記入してください。")
        return
    s = Settings()
    if args.command == "check-gitea":
        from .gitea import Gitea

        print(Gitea(s).check())
    elif args.command == "check-llm":
        from .llm import LLM, function

        client = LLM(s)
        print(
            client.completion(
                [
                    {
                        "role": "user",
                        "content": "Call connection_test with value connected.",
                    }
                ],
                tools=[
                    function(
                        "connection_test", "接続確認", {"value": {"type": "string"}}
                    )
                ],
                model=s.query_model,
            )
        )
    elif args.command == "backup":
        directory = s.root / "backups"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        db_copy = directory / (stamp + ".sqlite3")
        with (
            sqlite3.connect(s.root / "state.sqlite3") as source,
            sqlite3.connect(db_copy) as target,
        ):
            source.backup(target)
        destination = directory / ("our-context-" + stamp + ".tar.gz")
        with tarfile.open(destination, "w:gz") as archive:
            for path in (s.root / "raw", s.root / "knowledge"):
                if path.exists():
                    archive.add(path, arcname=path.name)
            archive.add(db_copy, arcname="state.sqlite3")
        db_copy.unlink()
        print(destination)
    else:
        if not args.file or not args.destination:
            parser.error("--file と --destination を指定してください。")
        destination = Path(args.destination).resolve()
        if destination.exists() and any(destination.iterdir()):
            raise ValueError("空の復元先を指定してください。")
        destination.mkdir(parents=True, exist_ok=True)
        with tarfile.open(args.file, "r:gz") as archive:
            for member in archive.getmembers():
                target = (destination / member.name).resolve()
                if (
                    not target.is_relative_to(destination)
                    or member.issym()
                    or member.islnk()
                ):
                    raise ValueError("バックアップ内のパスを確認してください。")
            archive.extractall(destination, filter="data")
        with sqlite3.connect(destination / "state.sqlite3") as c:
            c.execute("UPDATE jobs SET status='queued' WHERE status='running'")
            c.execute("DELETE FROM sessions")
        print(destination)


if __name__ == "__main__":
    main()

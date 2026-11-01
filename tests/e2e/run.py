"""実サービスを起動して一連のテストを実行する。外部 LiteLLM とローカル推論に対応。"""

import argparse
import os
from pathlib import Path
import platform
import secrets
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
RUNTIME = HERE / ".runtime"
RUNTIME.mkdir(exist_ok=True)
ENV = HERE / ".env"
COMPOSE = [
    shutil.which("docker-compose") or "docker",
    *([] if shutil.which("docker-compose") else ["compose"]),
    "--env-file",
    str(ENV),
    "-f",
    str(HERE / "compose.yaml"),
    "--profile",
    "gateway",
]


def load(path):
    return (
        dict(
            line.split("=", 1)
            for line in path.read_text().splitlines()
            if line and not line.startswith("#") and "=" in line
        )
        if path.exists()
        else {}
    )


def save(values):
    ENV.write_text("".join(key + "=" + value + "\n" for key, value in values.items()))
    ENV.chmod(0o600)


def compose(*args):
    subprocess.run([*COMPOSE, *args], cwd=ROOT, check=True)


def stop_local():
    pid_file = RUNTIME / "ollama.pid"
    if pid_file.exists():
        pid = int(pid_file.read_text())
        check = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True
        )
        if str(RUNTIME / "ollama-bin" / "ollama") in check.stdout:
            os.killpg(pid, signal.SIGTERM)
        pid_file.unlink()


def local_model():
    if platform.system() != "Darwin":
        raise RuntimeError(
            "--local は macOS 用です。外部 LiteLLM の設定を指定してください。"
        )
    binary = RUNTIME / "ollama-bin" / "ollama"
    if not binary.exists():
        archive = RUNTIME / "ollama-darwin.tgz"
        print("Ollama 0.40.13 を公式配布元から取得します。", flush=True)
        urllib.request.urlretrieve(
            "https://github.com/ollama/ollama/releases/download/v0.40.13/ollama-darwin.tgz",
            archive,
        )
        binary.parent.mkdir(exist_ok=True)
        subprocess.run(
            ["tar", "-xzf", str(archive), "-C", str(binary.parent)], check=True
        )
    try:
        urllib.request.urlopen("http://127.0.0.1:11436/api/version", timeout=2)
    except OSError:
        log = (RUNTIME / "ollama.log").open("ab")
        process = subprocess.Popen(
            [str(binary), "serve"],
            env={
                **os.environ,
                "OLLAMA_HOST": "127.0.0.1:11436",
                "OLLAMA_MODELS": str(RUNTIME / "models"),
                "OLLAMA_CONTEXT_LENGTH": "16384",
                "OLLAMA_NUM_PARALLEL": "1",
                "OLLAMA_MAX_LOADED_MODELS": "1",
                "OLLAMA_KEEP_ALIVE": "15m",
                "OLLAMA_NO_CLOUD": "1",
            },
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        (RUNTIME / "ollama.pid").write_text(str(process.pid))
        for _ in range(30):
            try:
                urllib.request.urlopen("http://127.0.0.1:11436/api/version", timeout=2)
                break
            except OSError:
                time.sleep(1)
        else:
            raise RuntimeError("Ollama の起動ログを確認してください。")
    print(
        "Qwen2.5 7B Instruct を用意します。初回は約 4.7 GB を取得します。", flush=True
    )
    with (RUNTIME / "model-pull.log").open("w") as log:
        subprocess.run(
            [str(binary), "pull", "qwen2.5:7b-instruct"],
            env={**os.environ, "OLLAMA_HOST": "http://127.0.0.1:11436"},
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--local", action="store_true", help="Ollama と Qwen2.5 7B Instruct を使用"
    )
    parser.add_argument("--reset", action="store_true", help="専用 E2E データを初期化")
    parser.add_argument("--keep", action="store_true", help="確認用にテスト環境を稼働")
    parser.add_argument(
        "--stop", action="store_true", help="専用サービスとローカル推論を停止"
    )
    args = parser.parse_args()
    if args.stop:
        compose("down")
        stop_local()
        return
    values = load(ENV)
    defaults = {
        "APP_PASSWORD": secrets.token_urlsafe(24),
        "APP_PUBLIC_URL": "http://localhost:19096",
        "GITEA_BASE_URL": "http://gitea:3000",
        "GITEA_REPO": "e2e-owner/work-notes",
        "GITEA_BRANCH": "main",
        "GITEA_ALLOWED_PATHS": "work-log.md,context/*.md",
        "GITEA_USERNAME": "e2e-owner",
        "GITEA_READ_TOKEN": "",
        "GITEA_WEBHOOK_SECRET": secrets.token_urlsafe(24),
        "E2E_GITEA_PASSWORD": secrets.token_urlsafe(24),
        "LLM_TIMEOUT": "300",
        "LLM_MAX_ROUNDS": "16",
        "LLM_MAX_INPUT_CHARS": "80000",
        "SYNC_INTERVAL_SECONDS": "86400",
        "APP_TIMEZONE": "Asia/Tokyo",
        "E2E_SUBNET": "10.203.16.0/24",
    }
    values = {**defaults, **values}
    if args.local:
        values.update(
            LLM_BASE_URL="http://litellm:4000/v1",
            LLM_API_KEY=values.get("LLM_API_KEY") or "sk-" + secrets.token_urlsafe(24),
            LLM_INGEST_MODEL="e2e-local",
            LLM_QUERY_MODEL="e2e-local",
        )
    else:
        main_env = load(ROOT / ".env")
        for key in (
            "LLM_BASE_URL",
            "LLM_API_KEY",
            "LLM_INGEST_MODEL",
            "LLM_QUERY_MODEL",
        ):
            values[key] = main_env.get(key, "")
        if not values.get("LLM_BASE_URL") or not values.get("LLM_INGEST_MODEL"):
            raise RuntimeError(
                ".env に LiteLLM の URL とモデル名を設定してください。ローカル推論は --local を指定します。"
            )
    save(values)
    if args.reset:
        attempts = RUNTIME / "attempts" / time.strftime("%Y%m%d-%H%M%S")
        for name in (
            "outcome.json",
            "model-calls.ndjson",
            "wiki-initial.json",
            "wiki-final.json",
            "verification.json",
            "weekly-report.xlsx",
            "weekly-report.md",
        ):
            old_result = RUNTIME / name
            if old_result.exists():
                attempts.mkdir(parents=True, exist_ok=True)
                shutil.move(old_result, attempts / name)
        compose("down", "-v")
        values["GITEA_READ_TOKEN"] = ""
        save(values)
    elif (RUNTIME / "outcome.json").exists():
        raise RuntimeError(
            "再実行は --reset を指定してください。専用 E2E データを初期化して確認します。"
        )
    try:
        subprocess.run(
            ["docker", "build", "--target", "app", "-t", "our-context:local", "."],
            cwd=ROOT,
            check=True,
        )
        if args.local:
            local_model()
        compose("up", "-d", "gitea")
        subprocess.run(
            [sys.executable, str(HERE / "bootstrap.py")], cwd=ROOT, check=True
        )
        compose("up", "-d", "app", "worker", *(["litellm"] if args.local else []))
        compose("exec", "-T", "app", "python", "/source/tests/e2e/live_flow.py")
        compose("exec", "-T", "app", "python", "/source/tests/e2e/collect.py")
    finally:
        if not args.keep:
            compose("down")
            stop_local()


if __name__ == "__main__":
    main()

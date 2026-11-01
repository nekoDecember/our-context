import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    root: Path = field(
        default_factory=lambda: Path(os.getenv("WORKSPACE_DIR", "workspace"))
    )
    password: str = field(default_factory=lambda: os.getenv("APP_PASSWORD", ""))
    public_url: str = field(
        default_factory=lambda: os.getenv("APP_PUBLIC_URL", "http://localhost:8096")
    )
    timezone: str = field(
        default_factory=lambda: os.getenv("APP_TIMEZONE", "Asia/Tokyo")
    )
    llm_url: str = field(
        default_factory=lambda: os.getenv("LLM_BASE_URL", "").rstrip("/")
    )
    llm_key: str = field(default_factory=lambda: os.getenv("LLM_API_KEY", ""))
    ingest_model: str = field(default_factory=lambda: os.getenv("LLM_INGEST_MODEL", ""))
    query_model: str = field(
        default_factory=lambda: (
            os.getenv("LLM_QUERY_MODEL") or os.getenv("LLM_INGEST_MODEL", "")
        )
    )
    llm_timeout: int = field(
        default_factory=lambda: int(os.getenv("LLM_TIMEOUT", "120"))
    )
    max_rounds: int = field(
        default_factory=lambda: int(os.getenv("LLM_MAX_ROUNDS", "16"))
    )
    max_input: int = field(
        default_factory=lambda: int(os.getenv("LLM_MAX_INPUT_CHARS", "80000"))
    )
    gitea_url: str = field(
        default_factory=lambda: os.getenv("GITEA_BASE_URL", "").rstrip("/")
    )
    gitea_repo: str = field(
        default_factory=lambda: os.getenv("GITEA_REPO", "").strip("/")
    )
    gitea_branch: str = field(default_factory=lambda: os.getenv("GITEA_BRANCH", "main"))
    gitea_user: str = field(default_factory=lambda: os.getenv("GITEA_USERNAME", ""))
    gitea_token: str = field(default_factory=lambda: os.getenv("GITEA_READ_TOKEN", ""))
    webhook_secret: str = field(
        default_factory=lambda: os.getenv("GITEA_WEBHOOK_SECRET", "")
    )
    allowed_paths: list[str] = field(
        default_factory=lambda: [
            x.strip()
            for x in os.getenv(
                "GITEA_ALLOWED_PATHS", "work-log.md,context/*.md,context/**/*.md"
            ).split(",")
            if x.strip()
        ]
    )
    sync_interval: int = field(
        default_factory=lambda: int(os.getenv("SYNC_INTERVAL_SECONDS", "900"))
    )
    cookie_secure: bool = field(
        default_factory=lambda: os.getenv("COOKIE_SECURE", "false").lower() == "true"
    )

    def public(self):
        return {
            "llm_url": self.llm_url,
            "ingest_model": self.ingest_model,
            "query_model": self.query_model,
            "llm_key_set": bool(self.llm_key),
            "gitea_url": self.gitea_url,
            "gitea_repo": self.gitea_repo,
            "gitea_branch": self.gitea_branch,
            "allowed_paths": self.allowed_paths,
            "gitea_token_set": bool(self.gitea_token),
            "webhook_secret_set": bool(self.webhook_secret),
            "public_url": self.public_url,
            "timezone": self.timezone,
            "sync_interval": self.sync_interval,
        }

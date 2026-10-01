from pydantic_settings import BaseSettings, SettingsConfigDict
import os


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── PostgreSQL ─────────────────────────────────────────
    database_url: str | None = None  # ← Railway will provide this

    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "demo_db"
    db_user: str = "postgres"
    db_password: str = ""
    db_schema: str = "auctor"

    @property
    def db_dsn(self) -> str:
        # 🔥 PRIORITY: use Railway DATABASE_URL if available
        if self.database_url:
            return self.database_url

        # fallback for local development
        return (
            f"postgresql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    # ── OpenAI ─────────────────────────────────────────────
    openai_api_key: str = ""

    # ── GitHub ─────────────────────────────────────────────
    github_token: str = ""

    # ── Score weights (must sum to 1.0) ─────────────────────────────────
    weight_github:     float = 0.25
    weight_leetcode:   float = 0.15
    weight_badges:     float = 0.30
    weight_projects:   float = 0.15
    weight_experience: float = 0.15

    # ── CORS ───────────────────────────────────────────────
    allowed_origins: str = "http://localhost:3000,http://localhost:8080"
    github_client_id: str = ""
    github_client_secret: str = ""
    github_redirect_uri: str = "http://localhost:8000/api/github/callback"
    web_url: str = "http://localhost:8080"
    reviewer_emails: str = ""
    storage_path: str = "_data"

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",")]


settings = Settings()

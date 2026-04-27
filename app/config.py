"""app/config.py — centralised settings loaded from .env"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── PostgreSQL ────────────────────────────────────────────────────────────
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "demo_db"
    db_user: str = "postgres"
    db_password: str = "2526"
    db_schema: str = "auctor"

    @property
    def db_dsn(self) -> str:
        return (
            f"postgresql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    # ── OpenAI ────────────────────────────────────────────────────────────────
    openai_api_key: str = ""

    # ── GitHub ────────────────────────────────────────────────────────────────
    github_token: str = ""

    # ── CORS ──────────────────────────────────────────────────────────────────
    allowed_origins: str = "http://localhost:3000,http://10.0.2.2:8000"

    # ── Score weights ─────────────────────────────────────────────────────────
    weight_github: float = 0.25
    weight_leetcode: float = 0.15
    weight_badges: float = 0.30
    weight_projects: float = 0.15
    weight_experience: float = 0.15

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",")]


settings = Settings()

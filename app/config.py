from pydantic_settings import BaseSettings, SettingsConfigDict
import os
import ipaddress
from pathlib import Path
from urllib.parse import urlsplit


def public_https_origin(value: str, label: str) -> str:
    url = urlsplit(value)
    host = (url.hostname or "").lower()
    if url.scheme != "https" or not host or url.username or url.password or url.query or url.fragment or url.path not in ("", "/"):
        raise RuntimeError(f"Production {label} must be a public HTTPS origin")
    if host == "localhost" or host.endswith((".localhost", ".local", ".invalid")) or host == "example.com":
        raise RuntimeError(f"Production {label} must identify the confirmed public deployment")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise RuntimeError(f"Production {label} cannot use a private address")
    return value.rstrip("/")


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
    app_env: str = "development"
    public_api_url: str = ""
    # Configure a mounted private volume. Railway supplies its mount path at runtime.
    storage_volume_path: str = ""
    railway_volume_mount_path: str = ""

    def validate_deployment(self) -> None:
        """Refuse production defaults before opening the database or accepting uploads.

        A configured volume is an operator assertion of persistence; this checks its
        existing mount directory and containment, rather than claiming a backup exists.
        Errors never include connection strings or provider credentials.
        """
        if self.app_env not in ("development", "test", "production"):
            raise RuntimeError("APP_ENV must be development, test or production")
        if self.app_env != "production":
            return
        if not self.database_url:
            raise RuntimeError("Production requires a private DATABASE_URL")
        database = urlsplit(self.database_url)
        if database.scheme not in ("postgres", "postgresql") or not database.hostname or not database.path.strip("/"):
            raise RuntimeError("Production DATABASE_URL must identify a PostgreSQL database")
        api = public_https_origin(self.public_api_url, "PUBLIC_API_URL")
        web = public_https_origin(self.web_url, "WEB_URL")
        origins = [public_https_origin(origin, "ALLOWED_ORIGINS") for origin in self.cors_origins]
        if not origins or web not in origins:
            raise RuntimeError("Production ALLOWED_ORIGINS must include WEB_URL explicitly")
        if bool(self.github_client_id) != bool(self.github_client_secret):
            raise RuntimeError("Configure both GitHub OAuth client settings or leave both empty")
        if self.github_client_id and self.github_redirect_uri != api + "/api/github/callback":
            raise RuntimeError("GitHub callback must match PUBLIC_API_URL/api/github/callback")
        volume_value = self.storage_volume_path or self.railway_volume_mount_path
        if not volume_value:
            raise RuntimeError("Production requires STORAGE_VOLUME_PATH or a Railway volume mount")
        volume, storage = Path(volume_value), Path(self.storage_path)
        if not volume.is_absolute() or not volume.is_dir() or not storage.is_absolute():
            raise RuntimeError("Production storage must use an existing absolute private volume")
        if not storage.resolve().is_relative_to(volume.resolve()):
            raise RuntimeError("Production STORAGE_PATH must remain inside the private volume")

    def prepare_storage(self) -> None:
        self.validate_deployment()
        root = Path(self.storage_path)
        root.mkdir(parents=True, exist_ok=True)
        if self.app_env == "production":
            if os.name != "nt":
                root.chmod(0o700)
            # Check actual write access under the configured runtime identity.
            from tempfile import TemporaryFile
            with TemporaryFile(dir=root) as probe:
                probe.write(b"storage-readiness")

    @property
    def storage_mode(self) -> str:
        return "private-volume" if self.app_env == "production" else "local-private"

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",")]


settings = Settings()

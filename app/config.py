from __future__ import annotations

import os
import secrets
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Ревизор НТД"
    app_host: str = "0.0.0.0"
    app_port: int = 8080
    app_secret: str = ""

    data_dir: Path = ROOT_DIR / "data"
    upload_max_mb: int = 120

    admin_username: str = "admin"
    admin_password: str = "Revizor#2026"
    admin_full_name: str = "Администратор"

    gemini_api_key: str = ""
    groq_api_key: str = ""
    openrouter_api_key: str = ""
    gigachat_credentials: str = ""
    gigachat_scope: str = "GIGACHAT_API_PERS"
    yandex_api_key: str = ""
    yandex_folder_id: str = ""
    openai_compat_base_url: str = ""
    openai_compat_api_key: str = ""
    openai_compat_model: str = ""

    ollama_base_url: str = "http://127.0.0.1:11434"
    local_gguf_path: str = ""
    local_gguf_n_ctx: int = 2048
    local_gguf_n_threads: int = 4

    ntd_online_check: bool = True
    ntd_online_timeout: float = 12.0
    ocr_enabled: bool = True

    jwt_expire_hours: int = 12

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def secret_path(self) -> Path:
        return self.data_dir / "secret.key"

    def ensure_dirs(self) -> None:
        for p in (
            self.data_dir,
            self.uploads_dir,
            self.reports_dir,
            self.tmp_dir,
            self.models_dir,
        ):
            p.mkdir(parents=True, exist_ok=True)

    def resolve_secret(self) -> str:
        if self.app_secret:
            return self.app_secret
        self.ensure_dirs()
        if self.secret_path.exists():
            return self.secret_path.read_text(encoding="utf-8").strip()
        value = secrets.token_urlsafe(48)
        self.secret_path.write_text(value, encoding="utf-8")
        try:
            os.chmod(self.secret_path, 0o600)
        except OSError:
            pass
        return value


settings = Settings()
settings.data_dir = Path(settings.data_dir).resolve()
settings.ensure_dirs()
SECRET_KEY = settings.resolve_secret()

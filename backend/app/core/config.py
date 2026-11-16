from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]
ROOT_DIR = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(ROOT_DIR / ".env", BACKEND_DIR / ".env"), extra="ignore"
    )

    app_name: str = "PII Sentinel"
    environment: str = "development"
    database_url: str = "postgresql+psycopg://pii_sentinel:change-me@localhost:5432/pii_sentinel"
    test_database_url: str = ""
    secret_key: str = "change-me-in-env"
    rate_limit_enabled: bool = True
    auth_rate_limit: str = "10/minute"
    access_token_minutes: int = 15
    refresh_token_days: int = 7
    cors_origins: str = "http://localhost:5173"
    storage_dir: str = str(ROOT_DIR / "storage")
    max_upload_mb: int = 20
    max_pdf_pages: int = 200
    scan_timeout_seconds: int = 120
    max_extracted_chars: int = 2_000_000
    ocr_dpi: int = 200
    ocr_min_text_chars: int = 20
    ocr_warmup: bool = True
    # --- detection
    min_confidence: float = 0.3
    max_entities_per_scan: int = 5000
    ner_model: str = ""  # Hugging Face id or local path; empty = rules-only mode
    ner_local_only: bool = False
    ner_max_length: int = 256
    ner_stride: int = 64
    ner_batch_size: int = 8
    ner_min_score: float = 0.5
    ner_max_chars: int = 200_000

    heavy_rate_limit: str = "60/minute"  # upload / scan / redact, per client address
    rate_limit_storage_uri: str = "memory://"  # e.g. redis://host:6379 to share limits between processes/hosts

    # --- encryption at rest (see app/security/crypto.py)
    encryption_keys: str = ""  # comma-separated Fernet keys; first encrypts, all decrypt. Required outside development

    # --- retention (0 = keep forever)
    retention_days: int = 0
    audit_retention_days: int = 0
    retention_interval_hours: int = 6

    # --- accounts
    allow_registration: bool = True
    max_failed_logins: int = 5
    lockout_minutes: int = 15

    @model_validator(mode="after")
    def _refuse_weak_secrets_outside_dev(self) -> "Settings":
        if self.environment != "development" and (self.secret_key == "change-me-in-env" or len(self.secret_key) < 32):
            raise ValueError("SECRET_KEY must be a random value of at least 32 characters outside development")
        if self.environment != "development" and not self.encryption_keys.strip():
            raise ValueError("ENCRYPTION_KEYS is required outside development (sensitive data is encrypted at rest)")
        for k in [k.strip() for k in self.encryption_keys.split(",") if k.strip()]:
            try:
                from cryptography.fernet import Fernet

                Fernet(k.encode())
            except Exception as exc:
                raise ValueError("ENCRYPTION_KEYS must be comma-separated Fernet keys (python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\")") from exc
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()

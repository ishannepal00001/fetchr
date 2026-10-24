"""
Settings: the ONLY place that reads environment variables / .env (see .env.example).
Everything else receives these values as arguments from main.py.

Secrets are SecretStr, so they show as '**********' in logs and reprs;
call .get_secret_value() where the real value is needed.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FETCHR_",
        env_file=".env",
        extra="ignore",
        populate_by_name=True,  # Settings(b2_bucket=...) works in tests, not just B2_BUCKET
    )

    # --- App / DB ---
    database_url: str = "sqlite:///./fetchr.db"
    tmp_dir: Path = Path("./tmp")

    # --- Rolling code auth ---
    client_id: str
    seed_code: SecretStr
    code_grace_seconds: int = Field(default=60, ge=0)

    # --- Downloads ---
    cookies_file: Path | None = None
    upload_retries: int = Field(default=3, ge=1)

    # --- Backblaze B2 (no FETCHR_ prefix, matching .env.example) ---
    b2_endpoint_url: str = Field(validation_alias=AliasChoices("B2_ENDPOINT_URL", "b2_endpoint_url"))
    b2_key_id: str = Field(validation_alias=AliasChoices("B2_KEY_ID", "b2_key_id"))
    b2_application_key: SecretStr = Field(validation_alias=AliasChoices("B2_APPLICATION_KEY", "b2_application_key"))
    b2_bucket: str = Field(validation_alias=AliasChoices("B2_BUCKET", "b2_bucket"))
    b2_presign_expiry: int = Field(default=3600, gt=0, validation_alias=AliasChoices("B2_PRESIGN_EXPIRY", "b2_presign_expiry"))

    @field_validator("cookies_file", mode="before")
    @classmethod
    def _empty_means_none(cls, value):
        # `FETCHR_COOKIES_FILE=` (empty) would otherwise become Path(".").
        return None if value == "" else value


@lru_cache
def get_settings() -> Settings:
    """Read once per process. Raises a ValidationError naming any missing variable."""
    return Settings()

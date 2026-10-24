"""core/config tests. _env_file=None so a developer's real .env never leaks in."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings

REQUIRED_ENV = {
    "FETCHR_CLIENT_ID": "my-client",
    "FETCHR_SEED_CODE": "s3cret-seed",
    "B2_ENDPOINT_URL": "https://s3.us-west-004.backblazeb2.com",
    "B2_KEY_ID": "key-id",
    "B2_APPLICATION_KEY": "app-key",
    "B2_BUCKET": "bucket",
}


@pytest.fixture
def env(monkeypatch):
    for name, value in REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


def test_reads_prefixed_and_b2_variables(env):
    env.setenv("FETCHR_CODE_GRACE_SECONDS", "30")
    env.setenv("B2_PRESIGN_EXPIRY", "600")

    settings = Settings(_env_file=None)

    assert settings.client_id == "my-client"
    assert settings.seed_code.get_secret_value() == "s3cret-seed"
    assert settings.code_grace_seconds == 30
    assert settings.b2_bucket == "bucket"
    assert settings.b2_presign_expiry == 600


def test_defaults(env):
    settings = Settings(_env_file=None)

    assert settings.database_url == "sqlite:///./fetchr.db"
    assert settings.tmp_dir == Path("./tmp")
    assert settings.upload_retries == 3
    assert settings.cookies_file is None


def test_empty_cookies_file_means_none(env):
    env.setenv("FETCHR_COOKIES_FILE", "")
    assert Settings(_env_file=None).cookies_file is None

    env.setenv("FETCHR_COOKIES_FILE", "/srv/cookies.txt")
    assert Settings(_env_file=None).cookies_file == Path("/srv/cookies.txt")


def test_secrets_are_hidden_in_repr(env):
    text = repr(Settings(_env_file=None))

    assert "s3cret-seed" not in text
    assert "app-key" not in text


def test_missing_required_variable_fails_fast(env):
    env.delenv("FETCHR_SEED_CODE")

    with pytest.raises(ValidationError, match="seed_code"):
        Settings(_env_file=None)


def test_reads_dotenv_file(tmp_path, monkeypatch):
    for name in REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("\n".join(f"{k}={v}" for k, v in REQUIRED_ENV.items()))

    settings = Settings(_env_file=env_file)

    assert settings.client_id == "my-client"
    assert settings.b2_key_id == "key-id"

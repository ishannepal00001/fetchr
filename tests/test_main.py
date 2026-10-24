"""App factory tests: startup creates tables, seeds the first code, and wires the middleware."""

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.middlewares.auth import NEXT_CODE_HEADER


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'fetchr.db'}",
        tmp_dir=tmp_path / "tmp",
        client_id="my-client",
        seed_code="seed",
        code_grace_seconds=0,  # grace is covered in test_auth_services; here reuse must fail
        b2_endpoint_url="https://b2.example",
        b2_key_id="id",
        b2_application_key="key",
        b2_bucket="bucket",
    )


def test_health_needs_no_code(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").json() == {"status": "ok"}


def test_startup_creates_tmp_dir_and_seeds_code(settings):
    with TestClient(create_app(settings)) as client:
        assert settings.tmp_dir.is_dir()

        # No API routes yet, so a 404 still proves the seed code was accepted and rotated.
        first = client.get("/anything", headers={"X-Client-Id": "my-client", "X-Fetchr-Code": "seed"})
        assert first.status_code == 404
        next_code = first.headers[NEXT_CODE_HEADER]

        assert client.get("/anything", headers={"X-Client-Id": "my-client", "X-Fetchr-Code": "seed"}).status_code == 401
        assert client.get("/anything", headers={"X-Client-Id": "my-client", "X-Fetchr-Code": next_code}).status_code == 404


def test_restart_keeps_rotated_code(settings):
    """seed_client must not reset an existing credential when the same DB file survives."""
    with TestClient(create_app(settings)) as client:
        next_code = client.get("/x", headers={"X-Client-Id": "my-client", "X-Fetchr-Code": "seed"}).headers[NEXT_CODE_HEADER]

    with TestClient(create_app(settings)) as client:
        assert client.get("/x", headers={"X-Client-Id": "my-client", "X-Fetchr-Code": "seed"}).status_code == 401
        assert client.get("/x", headers={"X-Client-Id": "my-client", "X-Fetchr-Code": next_code}).status_code == 404

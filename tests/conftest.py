"""
Shared test fixtures.

Planned fixtures (later stages):
- client: TestClient(app) with get_session overridden
- seeded_client: a ClientCredential with a known seed code; returns (client_id, code)
- b2: moto mock_aws S3 bucket standing in for Backblaze B2
"""

from collections.abc import Callable, Iterator

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import app.modules.downloads.model  # noqa: F401  registers the table on SQLModel.metadata


@pytest.fixture
def session_factory() -> Iterator[Callable[[], Session]]:
    """In-memory SQLite shared across sessions (StaticPool), tables created."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    yield lambda: Session(engine)
    engine.dispose()

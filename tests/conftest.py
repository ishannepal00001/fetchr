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
from sqlmodel import Session

from app.core.database import create_db_and_tables, make_engine, make_session_factory


@pytest.fixture
def session_factory() -> Iterator[Callable[[], Session]]:
    """In-memory SQLite shared across sessions (StaticPool), tables created."""
    engine = make_engine("sqlite://", poolclass=StaticPool)
    create_db_and_tables(engine)
    yield make_session_factory(engine)
    engine.dispose()

"""core/database tests."""

from sqlalchemy import inspect, text
from sqlalchemy.pool import StaticPool

from app.core.database import create_db_and_tables, make_engine


def test_existing_db_gets_new_nullable_columns(tmp_path):
    """A DB kept in a volume from before download.thumbnail existed is upgraded in place."""
    engine = make_engine(f"sqlite:///{tmp_path / 'old.db'}", poolclass=StaticPool)
    create_db_and_tables(engine)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE download DROP COLUMN thumbnail"))  # as created by an older fetchr
        conn.execute(
            text(
                "INSERT INTO download (id, client_id, source_url, status, created_at, updated_at) "
                "VALUES ('1', 'alice', 'u', 'COMPLETED', '2026-01-01', '2026-01-01')"
            )
        )
    assert "thumbnail" not in {c["name"] for c in inspect(engine).get_columns("download")}

    create_db_and_tables(engine)

    assert "thumbnail" in {c["name"] for c in inspect(engine).get_columns("download")}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT thumbnail FROM download")).scalar_one() is None
    engine.dispose()

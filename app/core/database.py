"""
Database setup (SQLite through SQLModel). Nothing here reads settings: main.py
passes the URL in, and tests pass an in-memory one.
"""

from collections.abc import Callable
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

# Importing the models registers their tables on SQLModel.metadata.
import app.modules.auth.model  # noqa: F401
import app.modules.downloads.model  # noqa: F401

SessionFactory = Callable[[], Session]


def make_engine(url: str, **kwargs: Any) -> Engine:
    """
    Build the engine. For SQLite, every new connection gets:
    - journal_mode=WAL: readers don't block the writer (API reads while a download updates its row)
    - busy_timeout: wait up to 5s for the write lock instead of failing at once
    """
    is_sqlite = url.startswith("sqlite")
    if is_sqlite:
        # The background download thread uses its own session, so connections cross threads.
        kwargs.setdefault("connect_args", {}).setdefault("check_same_thread", False)
    engine = create_engine(url, **kwargs)

    if is_sqlite:

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    return engine


def create_db_and_tables(engine: Engine) -> None:
    SQLModel.metadata.create_all(engine)


def make_session_factory(engine: Engine) -> SessionFactory:
    """What the services take: each call opens a new Session (use it in a `with` block)."""
    return lambda: Session(engine)

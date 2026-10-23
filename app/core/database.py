"""
Database setup (SQLite through SQLModel).

Planned contents:
- engine: create_engine(settings.database_url, connect_args={"check_same_thread": False})
- on connect, set the pragmas journal_mode=WAL (readers don't block the writer) and
  busy_timeout (wait for the write lock instead of failing at once)
- create_db_and_tables(): SQLModel.metadata.create_all(engine)
- get_session(): yields a Session per request (FastAPI dependency; also used by middleware
  and background work through a plain `with Session(engine)` block)
"""

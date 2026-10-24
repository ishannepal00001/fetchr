"""
App entrypoint. Run with the factory flag:

    uvicorn app.main:create_app --factory

This is where config meets the rest of the code: settings are read here and
passed into the engine, services and middleware.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import Settings, get_settings
from app.core.database import create_db_and_tables, make_engine, make_session_factory
from app.middlewares.auth import RollingCodeMiddleware
from app.modules.auth.services import AuthService


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    auth = AuthService(session_factory, grace_seconds=settings.code_grace_seconds)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.tmp_dir.mkdir(parents=True, exist_ok=True)
        create_db_and_tables(engine)  # Alembic is skipped: on Render the DB is recreated each start
        auth.seed_client(settings.client_id, settings.seed_code.get_secret_value())
        yield
        engine.dispose()

    app = FastAPI(title="fetchr", lifespan=lifespan)
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.auth = auth

    app.add_middleware(RollingCodeMiddleware, auth=auth)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    # TODO (Stage 3/4): build B2Storage + DownloadService here and include api.v1.router.

    return app

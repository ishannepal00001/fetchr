"""
App entrypoint. Run with the factory flag:

    uvicorn app.main:create_app --factory

This is where config meets the rest of the code: settings are read here and
passed into the engine, services and middleware.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.database import create_db_and_tables, make_engine, make_session_factory
from app.core.downloader import YtDlpDownloader
from app.core.storage import B2Storage
from app.middlewares.auth import RollingCodeMiddleware
from app.modules.auth.services import AuthService
from app.modules.downloads.services import DownloadService


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    auth = AuthService(session_factory, grace_seconds=settings.code_grace_seconds)
    storage = B2Storage(
        endpoint_url=settings.b2_endpoint_url,
        key_id=settings.b2_key_id,
        application_key=settings.b2_application_key.get_secret_value(),
        bucket=settings.b2_bucket,
        presign_expiry=settings.b2_presign_expiry,
    )
    downloads = DownloadService(
        session_factory=session_factory,
        downloader=YtDlpDownloader(cookies_file=settings.cookies_file),
        storage=storage,
        tmp_dir=settings.tmp_dir,
        upload_retries=settings.upload_retries,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.tmp_dir.mkdir(parents=True, exist_ok=True)
        create_db_and_tables(engine)  # Alembic is skipped: on Render the DB is recreated each start
        auth.seed_client(settings.client_id, settings.seed_code.get_secret_value())
        downloads.fail_interrupted()  # work in flight when the last process died can't resume
        yield
        engine.dispose()

    app = FastAPI(title="fetchr", lifespan=lifespan)
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.auth = auth
    app.state.downloads = downloads

    app.add_middleware(RollingCodeMiddleware, auth=auth)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    app.include_router(api_router)

    return app

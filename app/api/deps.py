"""
Shared FastAPI dependencies. They read what main.py and RollingCodeMiddleware put on
app.state / request.state, so routers never build services or check codes themselves.
"""

from fastapi import Request

from app.core.config import Settings
from app.modules.downloads.services import DownloadService


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_downloads(request: Request) -> DownloadService:
    return request.app.state.downloads


def current_client_id(request: Request) -> str:
    """Set by RollingCodeMiddleware after it verified the code."""
    return request.state.client_id


def next_code(request: Request) -> str:
    """The client's next rolling code, echoed in JSON bodies as next_code."""
    return request.state.next_code

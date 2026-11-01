"""Combines all v1 feature routers under /api/v1."""

from fastapi import APIRouter

from app.api.v1.downloads.router import router as downloads_router
from app.api.v1.renders.router import router as renders_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(downloads_router, prefix="/downloads", tags=["downloads"])
api_router.include_router(renders_router, prefix="/renders", tags=["renders"])

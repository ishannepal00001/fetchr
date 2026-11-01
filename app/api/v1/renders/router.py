"""
v1 renders endpoints. Auth has already happened in RollingCodeMiddleware.

A render runs through the same DownloadService and BackgroundTasks as a download (see
downloads/router.py) and is returned in the same shape.
"""

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status

from app.api.deps import current_client_id, get_downloads, next_code
from app.api.v1.downloads.schemas import DownloadRead, DownloadResponse
from app.api.v1.renders.schemas import RenderCreate
from app.modules.downloads.model import Download
from app.modules.downloads.services import DownloadNotFound, DownloadService, InvalidRender

router = APIRouter()

NOT_FOUND = {404: {"description": "No such render for this client"}}


def _response(render: Download, code: str) -> DownloadResponse:
    return DownloadResponse(**DownloadRead.model_validate(render).model_dump(), next_code=code)


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DownloadResponse,
    responses={422: {"description": "Invalid body, a source_key outside your downloads, or clips too long in total"}},
)
def create_render(
    body: RenderCreate,
    background_tasks: BackgroundTasks,
    service: DownloadService = Depends(get_downloads),
    client_id: str = Depends(current_client_id),
    code: str = Depends(next_code),
) -> DownloadResponse:
    """Queue a render. Poll GET /{id} until status is completed or failed."""
    try:
        render = service.create_render(client_id, body.source_key, body.to_spec())
    except InvalidRender as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    background_tasks.add_task(service.process_download, render.id)
    return _response(render, code)


@router.get("/{render_id}", response_model=DownloadResponse, responses=NOT_FOUND)
def get_render(
    render_id: uuid.UUID,
    service: DownloadService = Depends(get_downloads),
    client_id: str = Depends(current_client_id),
    code: str = Depends(next_code),
) -> DownloadResponse:
    try:
        render = service.get_render(client_id, render_id)
    except DownloadNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "render not found") from None
    return _response(render, code)

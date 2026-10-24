"""
v1 downloads endpoints. Auth has already happened in RollingCodeMiddleware.

Handlers are plain `def` (not async): the service does blocking DB work, so FastAPI
runs them in its threadpool. process_download is handed to BackgroundTasks, which
runs it after the 202 response has been sent.
"""

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse

from app.api.deps import current_client_id, get_downloads, get_settings, next_code
from app.api.v1.downloads.schemas import DownloadCreate, DownloadList, DownloadRead, DownloadResponse, FileLink
from app.core.config import Settings
from app.modules.downloads.model import Download
from app.modules.downloads.services import (
    DownloadNotCancellable,
    DownloadNotFound,
    DownloadNotReady,
    DownloadService,
)

router = APIRouter()

NOT_FOUND = {404: {"description": "No such download for this client"}}


def _response(download: Download, code: str) -> DownloadResponse:
    return DownloadResponse(**DownloadRead.model_validate(download).model_dump(), next_code=code)


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=DownloadResponse)
def create_download(
    body: DownloadCreate,
    background_tasks: BackgroundTasks,
    service: DownloadService = Depends(get_downloads),
    client_id: str = Depends(current_client_id),
    code: str = Depends(next_code),
) -> DownloadResponse:
    """Queue a download. Poll GET /{id} until status is completed or failed."""
    download = service.create_download(client_id, str(body.url), body.format)
    background_tasks.add_task(service.process_download, download.id)
    return _response(download, code)


@router.get("", response_model=DownloadList)
def list_downloads(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    service: DownloadService = Depends(get_downloads),
    client_id: str = Depends(current_client_id),
    code: str = Depends(next_code),
) -> DownloadList:
    items = service.list_downloads(client_id, offset=offset, limit=limit)
    return DownloadList(
        items=[DownloadRead.model_validate(d) for d in items], offset=offset, limit=limit, next_code=code
    )


@router.get("/{download_id}", response_model=DownloadResponse, responses=NOT_FOUND)
def get_download(
    download_id: uuid.UUID,
    service: DownloadService = Depends(get_downloads),
    client_id: str = Depends(current_client_id),
    code: str = Depends(next_code),
) -> DownloadResponse:
    try:
        download = service.get_download(client_id, download_id)
    except DownloadNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "download not found") from None
    return _response(download, code)


@router.post(
    "/{download_id}/cancel",
    response_model=DownloadResponse,
    responses={**NOT_FOUND, 409: {"description": "Download already completed or failed"}},
)
def cancel_download(
    download_id: uuid.UUID,
    service: DownloadService = Depends(get_downloads),
    client_id: str = Depends(current_client_id),
    code: str = Depends(next_code),
) -> DownloadResponse:
    """Stop a queued or running download. It shows as cancelled at once; cleanup follows shortly."""
    try:
        download = service.cancel_download(client_id, download_id)
    except DownloadNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "download not found") from None
    except DownloadNotCancellable:
        raise HTTPException(status.HTTP_409_CONFLICT, "download already finished") from None
    return _response(download, code)


@router.get(
    "/{download_id}/file",
    response_model=FileLink,
    responses={**NOT_FOUND, 307: {"description": "Redirect to the file (?redirect=true)"}, 409: {"description": "Download not completed yet"}},
)
def file_link(
    download_id: uuid.UUID,
    redirect: bool = False,
    service: DownloadService = Depends(get_downloads),
    settings: Settings = Depends(get_settings),
    client_id: str = Depends(current_client_id),
    code: str = Depends(next_code),
):
    """A presigned B2 link; the client downloads straight from B2. ?redirect=true sends a 307 to it."""
    try:
        url = service.file_link(client_id, download_id)
    except DownloadNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "download not found") from None
    except DownloadNotReady:
        raise HTTPException(status.HTTP_409_CONFLICT, "download not completed yet") from None
    if redirect:
        return RedirectResponse(url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    return FileLink(url=url, expires_in=settings.b2_presign_expiry, next_code=code)

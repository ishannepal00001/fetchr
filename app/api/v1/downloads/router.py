"""
v1 downloads endpoints. Auth has already happened in RollingCodeMiddleware, so
handlers read client_id with Depends(current_client_id).

Planned endpoints:
- POST   ""             body DownloadCreate -> DownloadRead (202)
         services.create_download(), then BackgroundTasks.add_task(services.process_download, id)
- GET    ""             ?offset&limit -> DownloadList
- GET    "/{id}"        -> DownloadRead, 404 if not found or owned by another client
- GET    "/{id}/file"   -> FileLink (presigned B2 URL); ?redirect=true -> 307 to it.
                        409 if status != completed.
"""

"""
Request/response schemas for v1 downloads. These validate what goes in and out
over HTTP; the DB table lives in app/modules/downloads/model.py.

Planned contents:
- CodeEnvelope: base for JSON responses, with field next_code: str (the client's next
  rolling code, also sent as the X-Fetchr-Next-Code header)
- DownloadCreate (request): url: HttpUrl, format: str | None
- DownloadRead (response, CodeEnvelope): id, source_url, status, title, filename,
  filesize, error, created_at, updated_at. Internal fields (b2_key, client_id) are left out.
  Built with DownloadRead.model_validate(download) from the module model.
- DownloadList (response, CodeEnvelope): items: list[DownloadRead], offset, limit
- FileLink (response, CodeEnvelope): url: str, expires_in: int
"""

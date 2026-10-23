"""
modules/downloads/services tests (planned):
- happy path: queued -> downloading -> uploading -> verifying -> completed; object in B2;
  tmp/<id>/ deleted
- verification mismatch: retries, then failed; B2 object deleted; tmp/<id>/ KEPT
- yt-dlp error: failed with error set; tmp/<id>/ removed
- get/list are scoped to client_id
"""

# fetchr

A small self-hosted media downloader API. A client sends a URL (mostly Facebook in v1). fetchr downloads it with **yt-dlp**, uploads it to **Backblaze B2**, checks that the upload arrived intact, and records it in a **SQLite** database. Every request is authenticated with a **rolling one-time code**.

> Status: **Stage 0**. Folder structure only. Every file holds comments describing what it will contain. See [Roadmap](#roadmap).

---

## How it works

```
client ──POST /api/v1/downloads {url}──▶ RollingCodeMiddleware ──▶ router ──▶ modules/downloads/services
                                          (verify + rotate code)                  │
                                                                                  ▼ background
                                                       yt-dlp → tmp/<id>/ → upload to B2 → verify → delete tmp
                                                                                  │
client ◀── X-Fetchr-Next-Code + {id, status: queued} ◀────────────────────────────┘ DB row updated at each step
client ──GET /api/v1/downloads/{id}──▶ poll until status = completed
client ──GET /api/v1/downloads/{id}/file──▶ presigned B2 link (download straight from B2)
```

### Rolling one-time code
1. **Seed**: the first code is hardcoded in `.env` (`FETCHR_SEED_CODE`). On first startup its sha256 hash is stored for `FETCHR_CLIENT_ID`. Plaintext codes are never stored.
2. **Each request** sends the headers `X-Client-Id` and `X-Fetchr-Code`.
3. `RollingCodeMiddleware` calls `modules.auth.services.verify_and_rotate()`. This runs one atomic SQL `UPDATE ... WHERE code_hash = <hash of sent code>`:
   - 1 row changed → valid. The code is replaced by a new random one.
   - 0 rows changed → `401`, and the endpoint never runs.
4. **Every response**, including error responses, returns the new code in `X-Fetchr-Next-Code` (and `next_code` in JSON bodies). The client must use it next time.
5. **Lost response?** The previous code is accepted once within `FETCHR_CODE_GRACE_SECONDS`. `reset_code()` handles admin recovery.

Because the rotation is a single atomic statement, two requests with the same code can't both succeed.

### Download lifecycle
`queued → downloading → uploading → verifying → completed | failed`

1. yt-dlp downloads into `tmp/<id>/`, using a cookies file for Facebook if one is configured.
2. The local file's sha256 and size are computed.
3. The file is uploaded to B2 at `downloads/<client_id>/<id>/<filename>`, with the sha256 stored in object metadata.
4. **Verify**: `head_object` must report the same size and sha256.
5. **Only after verification passes** is the row marked `completed` and `tmp/<id>/` deleted.
6. If verification keeps failing (after `FETCHR_UPLOAD_RETRIES` attempts), the B2 object is removed, the row is marked `failed`, and the **temp file is kept** so the video isn't lost.

---

## Project layout

```
app/
├── main.py                  app factory, lifespan (tables + seed code), middleware, routers
├── core/                    shared infrastructure
│   ├── config.py            settings from env / .env
│   ├── database.py          SQLite engine (WAL), sessions
│   ├── security.py          generate / hash / compare codes
│   ├── downloader.py        yt-dlp wrapper (implements the downloads Downloader protocol)
│   └── storage.py           Backblaze B2: upload, verify, presigned URL, delete
├── middlewares/
│   └── auth.py              RollingCodeMiddleware → modules/auth/services
├── modules/                 domain layer: model.py (DB tables) + services.py (logic)
│   ├── auth/                ClientCredential, seed / verify_and_rotate / reset
│   └── downloads/           Download, create / get / list / process_download
└── api/                     HTTP layer, versioned: schemas.py + router.py only
    ├── deps.py              session, current_client_id
    └── v1/
        ├── router.py        mounts feature routers under /api/v1
        └── downloads/
            ├── schemas.py   request/response validation
            └── router.py    endpoints
tests/                       mirrors app/: middlewares/, modules/, api/v1/
tmp/                         yt-dlp scratch space (gitignored)
```

**Layering rule**
- `modules/` holds the DB tables and business logic, with no HTTP knowledge. Every API version shares it.
- `api/vN/` holds only validation (`schemas.py`) and endpoints (`router.py`). A future `api/v2` adds new schemas and routers and reuses the same modules.
- `middlewares/` handles cross-cutting concerns such as auth, before routing.

---

## API (v1)

| Method | Path | Description |
|---|---|---|
| POST | `/api/v1/downloads` | `{url, format?}` → create a download and start processing (202) |
| GET | `/api/v1/downloads` | list your downloads (`?offset&limit`) |
| GET | `/api/v1/downloads/{id}` | status + metadata |
| GET | `/api/v1/downloads/{id}/file` | presigned B2 link (`?redirect=true` → 307) |
| GET | `/health` | liveness, no auth |

Every endpoint except `/health` and the docs requires `X-Client-Id` + `X-Fetchr-Code`.

---

## Configuration
Copy `.env.example` to `.env` and fill it in. Key values:
- `FETCHR_CLIENT_ID` and `FETCHR_SEED_CODE`: the hardcoded first credential.
- `B2_*`: Backblaze B2 S3-compatible endpoint, key, and bucket.
- `FETCHR_COOKIES_FILE`: optional, for Facebook videos that need login.

## Running (from Stage 1)
Requires **ffmpeg** on the PATH: Facebook usually serves video and audio as separate streams, and yt-dlp uses ffmpeg to merge them.

```bash
uv sync                       # or: pip install -e .
cp .env.example .env
uvicorn app.main:app --reload
```

---

## Roadmap
0. **Structure**: folders and comment-only files *(done)*
1. **Core**: config, database, app factory, `/health`
2. **Auth**: `ClientCredential`, `RollingCodeMiddleware`, rotation + grace window, with tests
3. **Storage**: B2 upload / verify / presign / delete (tested with moto)
4. **Downloads**: `Download` model, `process_download` pipeline, v1 schemas + routes
5. **Deploy**: Alembic migrations, systemd + uvicorn (1 worker, because of SQLite), nginx, B2 lifecycle rules, rate limiting

Later: live progress streaming (SSE).

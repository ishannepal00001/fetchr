# fetchr

A small self-hosted media downloader API. A client sends a URL (mostly Facebook in v1). fetchr downloads it with **yt-dlp**, uploads it to **Backblaze B2**, checks that the upload arrived intact, and records it in a **SQLite** database. It can also **render** a video already in B2: cut clips from it, join them into one MP4, burn in a news-style caption with **ffmpeg**, and upload the result. Every request is authenticated with a **rolling one-time code**.

> Status: **Stages 1–4 done**, plus renders. The API works end to end (auth, downloads, renders, B2); deployment to Render is next. See [Roadmap](#roadmap).

---

## How it works

```
client ──POST /api/v1/downloads {url}──▶ RollingCodeMiddleware ──▶ router ──▶ modules/downloads/services
                                          (verify + rotate code)                  │
                                                                                  ▼ background
                                  yt-dlp → tmp/<id>/ → thumbnail (ffmpeg) → upload to B2 → verify → delete tmp
                                                                                  │
client ◀── X-Fetchr-Next-Code + {id, status: queued} ◀────────────────────────────┘ DB row updated at each step
client ──GET /api/v1/downloads/{id}──▶ poll until status = completed
client ──GET /api/v1/downloads/{id}/file──▶ presigned B2 link (download straight from B2)

client ──POST /api/v1/renders {source_key, clips, caption?}──▶ same middleware, same job (see Renders)
                                  B2 source → tmp/<id>/ → ffprobe → ffmpeg (cut + join + caption) → thumbnail → upload to renders/… → verify → delete tmp
client ──GET /api/v1/renders/{id}──▶ poll until status = completed
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
`queued → downloading → uploading → verifying → completed | failed | cancelled`

1. yt-dlp downloads into `tmp/<id>/`, using a cookies file for Facebook if one is configured. It also saves the video's poster image there, if it has one.
2. The local file's sha256 and size are computed.
3. **Thumbnail** (best effort, see [Thumbnails](#thumbnails)): `thumbnail.jpg` is made and uploaded to `downloads/<client_id>/<id>/thumbnail.jpg`.
4. The file is uploaded to B2 at `downloads/<client_id>/<id>/<filename>`, with the sha256 stored in object metadata.
5. **Verify**: `head_object` must report the same size and sha256 metadata (the bytes aren't re-downloaded).
6. **Only after verification passes** is the row marked `completed` (and `thumbnail` set) and `tmp/<id>/` deleted.
7. If verification keeps failing (after `FETCHR_UPLOAD_RETRIES` attempts), the B2 objects (video and thumbnail) are removed, the row is marked `failed`, and the **temp file is kept** so the video isn't lost.

**Cancelling** (`POST /api/v1/downloads/{id}/cancel`) flips the row to `cancelled` straight away. The worker checks for that about once a second while yt-dlp downloads and while the file uploads (and before each status change), stops, and deletes `tmp/<id>/` and any B2 objects (video and thumbnail). The ffmpeg merge at the end of a download can't be interrupted, so a cancel during it takes effect right after. A cancelled download is never retried or resumed.

### Thumbnails
Every download gets a `thumbnail.jpg` next to the video in B2, at `downloads/<client_id>/<id>/thumbnail.jpg`, made in the same job before it is marked `completed`:

1. **Poster** (preferred): the video's own poster image, which yt-dlp downloads alongside the video (`writethumbnail`, the best entry of `info["thumbnails"]`). Its Facebook CDN URL is never stored or returned.
2. **Fallback**: if there is no poster, or ffmpeg can't read it, ffmpeg grabs a frame at 1s into the video (0s if the video is shorter).

Either way the output is a JPEG scaled to 640px wide keeping the aspect ratio (`-vf scale=640:-2 -q:v 5`, roughly quality 80, tens of KB), uploaded with `Content-Type: image/jpeg` and verified like the video. Intermediate files go in a temp dir inside `tmp/<id>/` (never `/app`, which is read-only in the container).

**A thumbnail never fails a download.** Any error (no poster and an unreadable video, ffmpeg error, upload error) is logged as a warning, any partial thumbnail object is deleted, and the download completes with `thumbnail: null`.

### Renders
A render takes one video the client already downloaded (in B2 under `downloads/<client_id>/…`), cuts clips from it, joins them in the requested order into **one MP4**, optionally burns a caption into it, and uploads the result to `renders/<client_id>/<id>/clip.mp4`.

A render is a download row with a render spec attached, so it runs through the same job: the same `BackgroundTasks` worker, the same statuses, thumbnail, upload, verify and retry steps, and the same handling of jobs cut off by a restart (marked `failed`). Only the `downloading` step differs: instead of yt-dlp, it fetches the source from B2 into `tmp/<id>/` and renders it there. Renders and downloads never show up in each other's endpoints.

1. **Fetch** `source_key` from B2 into `tmp/<id>/` (status `downloading`).
2. **ffprobe** it for the duration, display size (rotation metadata applied), and whether it has audio. Any clip ending more than 0.1s after the video's end fails the job with `Clip ends after the video does`.
3. **Caption** (if any): drawn as a transparent PNG as wide as the video. A red bar holds the headline in bold white, and a translucent black bar below holds the details (line breaks kept). Font sizes scale with the video width. If the caption would cover more than half the frame, the fonts shrink, and as a last resort trailing detail lines are cut with `…`.
4. **One ffmpeg run**, re-encoded so every cut is frame accurate (no stream copy):
   - each clip is read as its own input (`-ss`/`-t`, so ffmpeg seeks instead of decoding from the start), then `trim`/`atrim` + `setpts`/`asetpts=PTS-STARTPTS`
   - a source without audio gets silence (`anullsrc`) per clip, so the output always has an audio track
   - `concat` in request order; odd dimensions are cropped down to even
   - the caption is overlaid at the bottom (`overlay=0:H-h`) from the **start of the output** (not each clip), fully visible for `hold` seconds, then its alpha fades out over `fade` seconds
   - `libx264 -preset veryfast -crf 20 -pix_fmt yuv420p`, AAC 160k, `-movflags +faststart`, at the source's resolution
5. **Thumbnail**: the output's first frame, as `thumbnail.jpg` (640px wide, best effort as for downloads).
6. **Upload + verify** both, as for downloads, then `completed`.

`tmp/<id>/` is deleted whatever the outcome (unlike a download, the source is still in B2, so nothing is lost). On failure, `error` is a short message such as `Clip ends after the video does`, `Source video not found`, `The source isn't a readable video` or `ffmpeg could not render the video`. The ffmpeg command and the tail of its stderr go to the log. A single ffmpeg run is killed after `FETCHR_RENDER_TIMEOUT_SECONDS`.

---

## Project layout

```
app/
├── main.py                  create_app(): reads settings, lifespan (tables + seed code), middleware, routers
├── core/                    shared infrastructure
│   ├── config.py            settings from env / .env (the only place env is read)
│   ├── database.py          SQLite engine (WAL), sessions
│   ├── security.py          generate / hash codes
│   ├── downloader.py        yt-dlp wrapper (implements the downloads Downloader protocol)
│   ├── thumbnails.py        ffmpeg thumbnailer: poster or video frame → 640px JPEG
│   ├── renderer.py          ffmpeg renderer: clips → one captioned MP4 (implements Renderer)
│   ├── captions.py          caption layout + drawtext → transparent PNG
│   └── storage.py           Backblaze B2: upload, verify, presigned URL, delete
├── middlewares/
│   └── auth.py              RollingCodeMiddleware → modules/auth/services
├── modules/                 domain layer: model.py (DB tables) + services.py (logic)
│   ├── auth/                ClientCredential, seed / verify_and_rotate / reset
│   └── downloads/           Download, create / get / list / process_download, create_render / get_render
└── api/                     HTTP layer, versioned: schemas.py + router.py only
    ├── deps.py              session, current_client_id
    └── v1/
        ├── router.py        mounts feature routers under /api/v1
        ├── downloads/
        │   ├── schemas.py   request/response validation
        │   └── router.py    endpoints
        └── renders/
            ├── schemas.py   render request validation (responses reuse the download schema)
            └── router.py    endpoints
tests/                       mirrors app/: middlewares/, modules/, api/v1/
tmp/                         yt-dlp and render scratch space (gitignored)
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
| POST | `/api/v1/downloads/{id}/cancel` | stop a queued or running download |
| POST | `/api/v1/renders` | `{source_key, clips, caption?}` → create a render and start processing (202) |
| GET | `/api/v1/renders/{id}` | status + metadata of a render |
| GET | `/health` | liveness, no auth |

Every endpoint except `/health` and the docs requires `X-Client-Id` + `X-Fetchr-Code`.

### Headers

| Header | Direction | Notes |
|---|---|---|
| `X-Client-Id` | request | your client id |
| `X-Fetchr-Code` | request | your current one-time code |
| `X-Fetchr-Next-Code` | response | the code to send next time. On every response after auth passes (2xx, 307, 4xx, 500); not on a `401` |

JSON success bodies also carry the next code as `next_code`. Error bodies (`404`, `409`, `422`, `500`) don't, so read it from the header.

### Request: `POST /api/v1/downloads`

```json
{
  "url": "https://www.facebook.com/watch?v=…",
  "format": null
}
```

| Field | Type | Notes |
|---|---|---|
| `url` | string, required | http(s) URL, max 2048 chars |
| `format` | string \| null | yt-dlp format selector, max 200 chars. `null` = `bv*+ba/b` (best video + best audio, merged to mp4) |

Query parameters:
- `GET /api/v1/downloads`: `offset` (≥ 0, default 0), `limit` (1–100, default 50)
- `GET /api/v1/downloads/{id}/file`: `redirect` (bool, default `false`)

### Response: a download

Returned by `POST /api/v1/downloads` (202) and `GET /api/v1/downloads/{id}` (200).

```json
{
  "id": "3f6c2a1e-…",
  "source_url": "https://www.facebook.com/watch?v=…",
  "status": "completed",
  "title": "Some video",
  "filename": "1234567890.mp4",
  "content_type": "video/mp4",
  "filesize": 48213374,
  "thumbnail": "thumbnail.jpg",
  "error": null,
  "created_at": "2026-10-24T14:00:00Z",
  "updated_at": "2026-10-24T14:01:12Z",
  "next_code": "…"
}
```

A download row is both the job and its result, so its fields fill in at different times:

| Field | Type | Set when |
|---|---|---|
| `id`, `source_url`, `created_at` | uuid, string, datetime | at creation |
| `status` | `queued` \| `downloading` \| `uploading` \| `verifying` \| `completed` \| `failed` \| `cancelled` | changes at each step |
| `updated_at` | datetime | every status change |
| `title`, `filename`, `content_type`, `filesize` (bytes) | string \| null, int \| null | after yt-dlp has fetched the file (from `uploading` on). `null` before that |
| `thumbnail` | string \| null | only when `status` is `completed`: the bare file name (`"thumbnail.jpg"`), stored next to the video at `downloads/<client_id>/<id>/<thumbnail>`. `null` before that, on `failed`/`cancelled`, and on a completed download whose thumbnail couldn't be made |
| `error` | string \| null | only when `status` is `failed`: why it failed. `null` otherwise |
| `next_code` | string | always |

`POST` returns the row as it was inserted: `status: "queued"` and every result field `null`. Poll `GET /api/v1/downloads/{id}` until `status` is `completed`, `failed` or `cancelled`. A cancelled download has `error: null`; any result fields already filled in stay as they were.

### Request: `POST /api/v1/renders`

```json
{
  "source_key": "downloads/my-client/3f6c2a1e-…/1234567890.mp4",
  "clips": [ { "start": 12.4, "end": 31.0 }, { "start": 2, "end": 5.5 } ],
  "caption": {
    "headline": "Flood waters recede in Kathmandu",
    "details": "Officials say 1,200 families were moved.\nRoads reopen Monday.",
    "hold": 6,
    "fade": 1.5
  }
}
```

| Field | Type | Notes |
|---|---|---|
| `source_key` | string, required | B2 key of one of **your** downloads: must start with `downloads/<your client id>/` (no `.`, `..` or empty path segments) |
| `clips` | list, 1–20 items | in output order. Clips may overlap |
| `clips[].start` | number | seconds into the source, ≥ 0 |
| `clips[].end` | number | seconds into the source, at least 0.5 after `start`, and at most 0.1s past the source's end (checked once the job has the file) |
| `caption` | object \| null | optional; `null` or missing = no caption. One caption for the whole output, shown from its start |
| `caption.headline` | string, 1–200 chars | not blank |
| `caption.details` | string, 1–600 chars | may contain `\n` |
| `caption.hold` | number, 0–600, default `6` | seconds fully visible |
| `caption.fade` | number, 0–60, default `1.5` | seconds to fade out after `hold`. `hold + fade` must be > 0 |

The clips may add up to at most `FETCHR_RENDER_MAX_SECONDS` (default 600) seconds of output.

### Response: a render

`POST /api/v1/renders` (202) and `GET /api/v1/renders/{id}` (200) return a render in **exactly the shape of a download** (see above), so the same parsing works:

| Field | For a render |
|---|---|
| `source_url` | the `source_key` |
| `status` | `queued` → `downloading` (fetching the source and rendering) → `uploading` → `verifying` → `completed` \| `failed` |
| `title` | `null` |
| `filename`, `content_type`, `filesize` | `"clip.mp4"`, `"video/mp4"`, bytes, once rendered |
| `thumbnail` | `"thumbnail.jpg"` (the output's first frame) on `completed`, else `null` |
| `error` | on `failed`, a short message such as `"Clip ends after the video does"` |

The output is at `renders/<client_id>/<id>/clip.mp4` in B2, with the thumbnail next to it. Renders don't appear in `GET /api/v1/downloads`, and the download endpoints answer `404` for a render id (and vice versa).

### Request: `POST /api/v1/downloads/{id}/cancel`

No body. Returns the download (200) with `status: "cancelled"`. Cancelling an already cancelled download returns it again (200); a `completed` or `failed` one gets `409`.

### Response: list of downloads

`GET /api/v1/downloads` (200), newest first. Items don't carry their own `next_code`.

```json
{
  "items": [ { "id": "…", "status": "completed", "…": "same fields as a download, without next_code" } ],
  "offset": 0,
  "limit": 50,
  "next_code": "…"
}
```

### Response: file link

`GET /api/v1/downloads/{id}/file` (200), only once `status` is `completed`. Download the file straight from `url` (it is served as an attachment).

```json
{
  "url": "https://s3.us-west-004.backblazeb2.com/…?X-Amz-Signature=…",
  "expires_in": 3600,
  "next_code": "…"
}
```

With `?redirect=true` you get a `307` to that URL instead, with no body.

### Errors

| Status | Body | When |
|---|---|---|
| `401` | `{"detail": "unauthorized"}` | missing headers or wrong code. The endpoint never ran and the code didn't rotate |
| `404` | `{"detail": "download not found"}` / `{"detail": "render not found"}` | no such id, or it belongs to another client |
| `409` | `{"detail": "download not completed yet"}` | `/file` asked for a download that isn't `completed` |
| `409` | `{"detail": "download already finished"}` | `/cancel` on a download that is `completed` or `failed` |
| `422` | FastAPI's `{"detail": [ … ]}` | invalid body, query parameter, or id (not a UUID) |
| `422` | `{"detail": "<message>"}` | a render whose `source_key` isn't one of your downloads, or whose clips add up to more than `FETCHR_RENDER_MAX_SECONDS` |
| `500` | `{"detail": "internal server error"}` | an unexpected failure (e.g. the database or disk). Still carries `X-Fetchr-Next-Code` |

---

## Configuration
Copy `.env.example` to `.env` and fill it in. Key values:
- `FETCHR_CLIENT_ID` and `FETCHR_SEED_CODE`: the hardcoded first credential.
- `B2_*`: Backblaze B2 S3-compatible endpoint, key, and bucket.
- `FETCHR_COOKIES_FILE`: optional, for Facebook videos that need login.
- `FETCHR_FFMPEG_PATH`, `FETCHR_FFPROBE_PATH`: the ffmpeg binaries, used for thumbnails and renders (default `ffmpeg` / `ffprobe` on the `PATH`).
- `FETCHR_RENDER_MAX_SECONDS`: the most output a render may ask for, as the sum of its clip lengths (default `600`).
- `FETCHR_RENDER_TIMEOUT_SECONDS`: a single ffmpeg render is killed after this long (default `1800`).
- `FETCHR_CAPTION_FONT`, `FETCHR_CAPTION_BOLD_FONT`: caption fonts, each a font file path or a fontconfig pattern (default `Sans` / `Sans:bold`; the image ships DejaVu Sans). Text layout assumes a Latin sans font, so scripts that need shaping (e.g. Devanagari) won't render correctly with ffmpeg 5.1's drawtext.

## Running
Runs in Docker; the image includes **ffmpeg**, which yt-dlp uses to merge Facebook's separate video and audio streams and renders use to cut, join and caption, plus the DejaVu fonts for captions.

```bash
cp .env.example .env          # fill in FETCHR_SEED_CODE and the B2_* values
./start.sh                    # build + run the image: http://127.0.0.1:8000 (PORT=9000 to change)
uv run pytest                 # run the tests
```

An existing DB from before a new nullable column (such as `download.thumbnail`) is upgraded in place at startup: the column is added and old rows get `null`.

`start.sh` passes `.env` to the container but points the DB, tmp dir and cookies file at container paths. The SQLite DB lives in the `fetchr-data` volume (`docker volume rm fetchr-data` resets it). On Render, settings come from the dashboard's environment variables. The server runs a single worker on purpose: SQLite allows one writer, and background downloads run inside the server process.

---

## Roadmap
0. **Structure**: folders and comment-only files *(done)*
1. **Core**: config, database, app factory, `/health` *(done)*
2. **Auth**: `ClientCredential`, `RollingCodeMiddleware`, rotation + grace window, with tests *(done)*
3. **Storage**: B2 upload / verify / presign / delete (tested with moto) *(done)*
4. **Downloads**: `Download` model, `process_download` pipeline, yt-dlp downloader, v1 schemas + routes *(done)*
5. **Deploy**: Render (Dockerfile with ffmpeg, 1 worker because of SQLite), rate limiting
6. **Renders**: cut + join clips from a download into one captioned MP4 (`/api/v1/renders`) *(done)*

Open render follow-ups: a concurrency limit (renders encode in parallel today), a proper caption renderer with text shaping for non-Latin scripts, and cancelling a render.

Later: live progress streaming (SSE).

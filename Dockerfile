# fetchr production image.
#   docker build -t fetchr .
#   docker run --env-file .env -p 8000:8000 fetchr
#
# Two stages: uv installs the locked deps into /app/.venv, then only that venv and
# the app code are copied into a slim runtime image with ffmpeg (yt-dlp merges
# video + audio with it). Secrets come from the environment, never the image.

FROM python:3.13-slim-bookworm AS builder
COPY --from=ghcr.io/astral-sh/uv:0.8.15 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Deps first so code changes don't reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
RUN uv sync --frozen --no-dev


FROM python:3.13-slim-bookworm
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 fetchr

WORKDIR /app
COPY --from=builder --chown=fetchr:fetchr /app /app
COPY --chown=fetchr:fetchr scripts/start.sh ./scripts/start.sh

# SQLite DB and download scratch space live under /app/data, which is writable by the
# app user. Mount a volume there to keep them across restarts; otherwise they reset.
RUN mkdir -p /app/data/tmp && chown -R fetchr:fetchr /app/data
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    FETCHR_DATABASE_URL=sqlite:////app/data/fetchr.db \
    FETCHR_TMP_DIR=/app/data/tmp

USER fetchr
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health', timeout=4)"

# No uv in this image, so start.sh runs `python -m uvicorn` from the venv on PATH.
CMD ["scripts/start.sh"]

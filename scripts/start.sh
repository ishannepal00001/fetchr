#!/usr/bin/env bash
# Production server (Render start command: scripts/start.sh).
# Settings come from real environment variables (Render's dashboard); .env is optional.
#
# - listens on 0.0.0.0:$PORT (Render sets PORT; defaults to 8000)
# - exactly 1 worker: SQLite has one writer, and background downloads run in-process
# - trusts X-Forwarded-* from Render's proxy so client IPs/scheme are correct
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v ffmpeg >/dev/null; then
    echo "warning: ffmpeg not found; downloads that merge video + audio will fail" >&2
fi

# Use uv when it's installed; otherwise assume deps were installed with pip.
if command -v uv >/dev/null; then
    run=(uv run --frozen --no-dev)
else
    run=(python -m)
fi

exec "${run[@]}" uvicorn app.main:create_app --factory \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --workers 1 \
    --proxy-headers \
    --forwarded-allow-ips '*'

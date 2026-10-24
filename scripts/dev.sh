#!/usr/bin/env bash
# Local development server: auto-reload on code changes, localhost only.
#   scripts/dev.sh              -> http://127.0.0.1:8000
#   PORT=9000 scripts/dev.sh    -> another port
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
    echo "No .env found. Create one first:" >&2
    echo "    cp .env.example .env    # then fill in FETCHR_SEED_CODE and the B2_* values" >&2
    exit 1
fi

if ! command -v ffmpeg >/dev/null; then
    echo "warning: ffmpeg not found; downloads that merge video + audio will fail" >&2
fi

uv sync --quiet

exec uv run uvicorn app.main:create_app --factory \
    --reload \
    --host 127.0.0.1 \
    --port "${PORT:-8000}"

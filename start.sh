#!/usr/bin/env bash
# Build and run the production Docker image locally.
#   ./start.sh                -> http://127.0.0.1:8000
#   PORT=9000 ./start.sh      -> another host port
#
# Secrets come from .env, but its paths point at the host checkout, so the DB, tmp dir and
# cookies file are overridden with container paths (-e wins over --env-file). The SQLite DB
# lives in the `fetchr-data` volume, so it survives restarts here; `docker volume rm fetchr-data`
# resets it.
set -euo pipefail
cd "$(dirname "$0")"

image=fetchr
name=fetchr
port="${PORT:-8000}"

if [[ ! -f .env ]]; then
    echo "No .env found. Create one first:" >&2
    echo "    cp .env.example .env    # then fill in FETCHR_SEED_CODE and the B2_* values" >&2
    exit 1
fi

docker build -t "$image" .

args=(
    --rm
    --name "$name"
    --env-file .env
    -e FETCHR_DATABASE_URL=sqlite:////app/data/fetchr.db
    -e FETCHR_TMP_DIR=/app/data/tmp
    -p "127.0.0.1:${port}:8000"
    -v fetchr-data:/app/data
)

# Facebook cookies are optional. Mounted read-write because yt-dlp saves refreshed cookies back.
if [[ -f fb-cookies.txt ]]; then
    args+=(-v "$PWD/fb-cookies.txt:/app/fb-cookies.txt" -e FETCHR_COOKIES_FILE=/app/fb-cookies.txt)
else
    args+=(-e FETCHR_COOKIES_FILE=)
fi

# Replace a leftover container from a previous run, if any.
docker rm -f "$name" >/dev/null 2>&1 || true

exec docker run "${args[@]}" "$image"

#!/usr/bin/env bash
# Run the test suite. Extra arguments go to pytest:
#   scripts/test.sh                      -> everything
#   scripts/test.sh tests/modules -k auth
set -euo pipefail
cd "$(dirname "$0")/.."

uv sync --quiet
exec uv run pytest "$@"

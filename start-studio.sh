#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
# Remote publishing is opt-in. No host, account or supplier credentials are bundled.
export ALBUM_PROTOTYPE_PORT="${ALBUM_PROTOTYPE_PORT:-8775}"
if [[ -x .venv/bin/python ]]; then
  exec .venv/bin/python server.py
fi
exec python3 server.py

#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8765}"
echo "Starting Parking Spot Rotation Calendar Server on http://127.0.0.1:${PORT} ..."
exec python3 server.py

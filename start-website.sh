#!/usr/bin/env sh
# Tora AI product website (macOS/Linux). Static site; needs only Python 3.
# Usage: ./start-website.sh [port]     Windows: start-website.bat
cd "$(dirname "$0")" || exit 1
PORT="${1:-4173}"
PY="$(command -v python3 || command -v python)"
[ -n "$PY" ] || { echo "Python 3 is required: https://www.python.org/downloads/"; exit 1; }
echo
echo "  Tora AI website"
echo "  Open:  http://localhost:$PORT/   (Ctrl+C to stop)"
echo
exec "$PY" -m http.server "$PORT" --bind 127.0.0.1 --directory website/dist

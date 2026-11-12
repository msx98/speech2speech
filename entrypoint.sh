#!/bin/sh
set -eux
cd /app

export MODELS_INI="${MODELS_INI:-/data/models.ini}"
export LD_LIBRARY_PATH="/opt/whisper/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export VIRTUAL_ENV=/usr/local/venv
export PATH="$VIRTUAL_ENV/bin:$PATH"
unset PYTHONPATH

exec "$VIRTUAL_ENV/bin/python" -m uvicorn server:app --host 0.0.0.0 --port 8000 --workers 1

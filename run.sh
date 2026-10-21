#!/usr/bin/env bash
# Creates a local virtualenv (first run only), installs dependencies and starts the web UI.
set -e
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
if [ ! -d .venv ]; then
  echo "Creating virtualenv..."
  $PY -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
fi
exec .venv/bin/python server.py "$@"

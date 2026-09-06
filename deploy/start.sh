#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/gunicorn --bind 127.0.0.1:8081 --workers 1 --threads 4 --timeout 120 --access-logfile - --error-logfile - wsgi:app

#!/bin/sh
# Runs uvicorn as PID 1 (exec) so SIGTERM from the orchestrator reaches it and
# in-flight requests drain instead of being killed. Any arguments are passed
# through, so `docker run <image> pytest -q` style overrides still work.
set -eu

if [ "$#" -gt 0 ]; then
  exec "$@"
fi

exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "${PORT:-8000}" \
  --workers "${UVICORN_WORKERS:-1}" \
  --proxy-headers \
  --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1}" \
  --log-level "$(printf '%s' "${LOG_LEVEL:-info}" | tr '[:upper:]' '[:lower:]')" \
  --timeout-graceful-shutdown "${GRACEFUL_SHUTDOWN_SECONDS:-30}"

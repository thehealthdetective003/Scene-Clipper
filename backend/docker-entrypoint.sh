#!/bin/sh
# Entrypoint for both roles. Configuration is validated before anything binds a
# port or claims a queue, so a misconfigured deployment fails fast (spec 7.5).
set -eu

role="${1:-api}"

# Validation runs only for the long-lived roles. Operator commands such as
# `generate-key` and `hash-password` must work *before* the deployment is
# configured, so they fall through to the catch-all branch untouched.
case "$role" in
  api)
    python -m app.cli check-config
    python -m app.cli check-media
    # The API owns migrations; the worker waits for the schema to exist.
    python -m alembic upgrade head
    exec uvicorn app.main:app \
      --host 0.0.0.0 \
      --port 8000 \
      --workers "${UVICORN_WORKERS:-2}" \
      --no-access-log \
      --proxy-headers \
      --forwarded-allow-ips '*'
    ;;
  worker)
    python -m app.cli check-config
    python -m app.cli check-media
    # Wait for the API's migration to land rather than racing it.
    until python -m app.cli db-ready >/dev/null 2>&1; do
      echo "waiting for database migrations..."
      sleep 2
    done
    exec python -m app.workers.main
    ;;
  *)
    exec "$@"
    ;;
esac

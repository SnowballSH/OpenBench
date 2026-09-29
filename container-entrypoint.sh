#!/bin/sh
set -eu

mkdir -p "${OPENBENCH_DATA_DIR}/Media" "${OPENBENCH_UPLOAD_TEMP_DIR}"
python manage.py migrate --noinput

exec gunicorn OpenSite.wsgi \
    --bind 0.0.0.0:8080 \
    --worker-class gthread \
    --workers "${OPENBENCH_WORKERS:-2}" \
    --threads "${OPENBENCH_THREADS:-4}" \
    --timeout 120 \
    --graceful-timeout 30 \
    --worker-tmp-dir /tmp \
    --no-control-socket \
    --access-logfile - \
    --error-logfile -

#!/bin/sh
# Runs INSIDE the disposable webinar_runtime preview container only; never production.
# Inputs are read-only bind mounts under /source and are copied to /app. The
# SQLite DB lives in the container's writable layer and disappears with it.
# Secrets are generated here and never echoed.
set -eu
# Leave a brief window for the managed launcher to capture failed-start logs.
trap 'code=$?; if [ "$code" -ne 0 ]; then sleep 10; fi' 0
umask 077

case "${WEBINAR_PREVIEW_RUN:-}" in
    pi-webinar-*) ;;
    *) echo "Not a managed webinar preview container" >&2; exit 64 ;;
esac
[ -f /tmp/preview-credential ] || { echo "Preview credential missing" >&2; exit 65; }
# OrbStack may preserve the host UID on copied files. Normalize only this file,
# then permanently drop the startup-only CHOWN capability before running app code.
if [ "${PREVIEW_OWNERSHIP_FIXED:-}" != 1 ]; then
    chown 0:0 /tmp/preview-credential
    chmod 600 /tmp/preview-credential
    export PREVIEW_OWNERSHIP_FIXED=1
    exec setpriv --bounding-set=-all --inh-caps=-all --ambient-caps=-all sh "$0"
fi

mkdir /app /tmp/preview
for item in manage.py requirements.txt config webinars preview; do
    cp -R "/source/$item" "/app/$item"
done
find /app -name __pycache__ -type d -prune -exec rm -rf {} +
cd /app

# Exact pins from requirements.txt; wheels only (no sdist build scripts).
python -m pip install --no-cache-dir --disable-pip-version-check --no-input --root-user-action=ignore \
    --only-binary=:all: -r requirements.txt

DJANGO_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(64))')"
INTEGRATION_MASTER_KEY="$(python -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"
DJANGO_SETTINGS_MODULE=preview.settings
export DJANGO_SECRET_KEY INTEGRATION_MASTER_KEY DJANGO_SETTINGS_MODULE

python manage.py migrate --noinput
python manage.py collectstatic --noinput --verbosity 0
python -m preview.seed

# Web process only: no worker, no destinations. Access log omits query strings.
exec gunicorn config.wsgi:application \
    --bind 0.0.0.0:4318 \
    --workers 2 \
    --timeout 30 \
    --access-logfile - \
    --access-logformat '%(t)s "%(m)s %(U)s %(H)s" %(s)s %(B)s' \
    --error-logfile -

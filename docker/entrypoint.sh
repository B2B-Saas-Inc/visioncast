#!/bin/sh
# Container entrypoint. One image, several roles:
#
#   web              gunicorn serving config.wsgi on 0.0.0.0:8000
#   worker           durable outbox / reminder worker (manage.py run_worker)
#   migrate          apply database migrations, then exit (run as a one-shot service)
#   manage <args>    pass-through to manage.py (e.g. `manage bootstrap_admin`)
#   <anything else>  executed as-is (e.g. `python -m pytest` in the test image)
#
# The web role never migrates automatically: schema changes are applied
# explicitly by the `migrate` service so that a backup can be taken first and
# several web replicas never race on migrations.
set -eu

role="${1:-web}"
if [ "$#" -gt 0 ]; then
    shift
fi

case "$role" in
    web)
        # Access log format deliberately omits the query string (%(U)s is the
        # path only), client IP, referrer and user agent so that request logs
        # contain no personal data or tokens. Cookies/headers are never logged.
        exec gunicorn config.wsgi:application \
            --bind 0.0.0.0:8000 \
            --workers "${WEB_CONCURRENCY:-3}" \
            --timeout "${GUNICORN_TIMEOUT:-30}" \
            --graceful-timeout 30 \
            --worker-tmp-dir /dev/shm \
            --max-requests 2000 \
            --max-requests-jitter 200 \
            --access-logfile - \
            --access-logformat '%(t)s "%(m)s %(U)s %(H)s" %(s)s %(B)s %(M)sms' \
            --error-logfile - \
            "$@"
        ;;
    worker)
        exec python manage.py run_worker "$@"
        ;;
    migrate)
        exec python manage.py migrate --noinput "$@"
        ;;
    manage)
        exec python manage.py "$@"
        ;;
    *)
        exec "$role" "$@"
        ;;
esac

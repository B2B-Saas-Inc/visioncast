#!/bin/bash
# Run only in a managed postgres:16-bookworm foreground test container.
set -euo pipefail
trap 'runuser -u postgres -- /usr/lib/postgresql/16/bin/pg_ctl -D /tmp/webinar-pg stop -m immediate >/dev/null 2>&1 || true' EXIT
# Bootstrap TLS from an operator-supplied public CA bundle (never disable verification).
if [[ ! -s /etc/ssl/certs/ca-certificates.crt ]]; then
  install -D -m 644 /workspace/Code/webinar-platform/artifacts/test-ca-bundle.pem /etc/ssl/certs/ca-certificates.crt
fi
find /etc/apt/sources.list.d -name '*.sources' -exec sed -i 's|http://deb.debian.org|https://deb.debian.org|g' {} +
apt-get update -qq
apt-get install -y -qq ca-certificates python3 python3-venv >/tmp/apt-python.log
python3 -m venv /tmp/testenv
export PATH=/tmp/testenv/bin:/usr/lib/postgresql/16/bin:$PATH
pip install -q uv
# Test the application's production Python series, not Debian's bootstrap Python.
uv python install 3.12
uv venv --python 3.12 /tmp/appenv
export PATH=/tmp/appenv/bin:$PATH
python -c 'import sys; assert sys.version_info[:2] == (3, 12), sys.version'
install -d -o postgres -g postgres /tmp/webinar-pg
runuser -u postgres -- initdb -D /tmp/webinar-pg --auth=trust >/tmp/initdb.log
runuser -u postgres -- pg_ctl -D /tmp/webinar-pg -l /tmp/pg.log -o '-h 127.0.0.1' start >/dev/null
runuser -u postgres -- createuser -h 127.0.0.1 --superuser webinars
runuser -u postgres -- createdb -h 127.0.0.1 -O webinars webinars
cd /workspace/Code/webinar-platform
uv pip install -r requirements-dev.txt >/tmp/deps.log 2>&1
export DJANGO_SECRET_KEY=$(python -c 'import secrets; print(secrets.token_urlsafe(64))')
export INTEGRATION_MASTER_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA= POSTGRES_HOST=127.0.0.1 POSTGRES_PASSWORD=disposable-test DJANGO_ALLOWED_HOSTS=localhost PUBLIC_BASE_URL=https://webinars.example.com
python manage.py migrate --noinput >artifacts/pg16-migrate.log 2>&1
python --version >artifacts/pg16-checks.log
postgres --version >>artifacts/pg16-checks.log
python manage.py check >>artifacts/pg16-checks.log 2>&1
python manage.py makemigrations --check --dry-run >>artifacts/pg16-checks.log 2>&1
python manage.py collectstatic --noinput >/tmp/static.log 2>&1
python -m pytest -q >artifacts/pg16-tests.log 2>&1 || { tail -100 artifacts/pg16-tests.log; exit 1; }
ruff check . >>artifacts/pg16-checks.log 2>&1
# Verify a logical backup restores the migrated schema and a synthetic durable row.
psql -h 127.0.0.1 -U webinars -d webinars -c 'CREATE TABLE release_restore_probe (id integer PRIMARY KEY, value text NOT NULL); INSERT INTO release_restore_probe VALUES (1, '\''synthetic-restore-check'\'');' >/dev/null
PYTHONPATH=. python tests/restore_probe.py seed
pg_dump -h 127.0.0.1 -U webinars -Fc webinars >/tmp/webinar-backup.dump
createdb -h 127.0.0.1 -U webinars webinar_restored
pg_restore -h 127.0.0.1 -U webinars -d webinar_restored --exit-on-error /tmp/webinar-backup.dump
[[ $(psql -h 127.0.0.1 -U webinars -d webinar_restored -Atc 'SELECT value FROM release_restore_probe WHERE id=1') == synthetic-restore-check ]]
psql -h 127.0.0.1 -U webinars -d webinar_restored -Atc 'SELECT count(*) FROM django_migrations' >artifacts/pg16-restore.log
printf '\nSchema and synthetic-row logical restore passed.\n' >>artifacts/pg16-restore.log
POSTGRES_DB=webinar_restored PYTHONPATH=. python tests/restore_probe.py verify >>artifacts/pg16-restore.log
tail -12 artifacts/pg16-tests.log
cat artifacts/pg16-checks.log artifacts/pg16-restore.log

#!/bin/bash
# ONLY inside a lifecycle-managed python:3.12-slim-bookworm test container.
set -euo pipefail
# Debian HTTP mirrors may be blocked; use TLS inside this disposable container.
python - <<'PY'
from pathlib import Path
p = Path('/etc/apt/sources.list.d/debian.sources')
p.write_text(p.read_text().replace('http://deb.debian.org', 'https://deb.debian.org'))
PY
apt-get update -qq
apt-get install -y -qq postgresql >/tmp/apt.log
pg_ctlcluster 15 main start
runuser -u postgres -- psql -c "CREATE ROLE webinars LOGIN SUPERUSER PASSWORD 'disposable-test';" >/dev/null
runuser -u postgres -- createdb -O webinars webinars
pip install -q uv
cd /workspace/Code/webinar-platform
uv pip install --system -r requirements-dev.txt >/tmp/deps.log 2>&1
export DJANGO_SECRET_KEY=$(python -c 'import secrets; print(secrets.token_urlsafe(64))')
export INTEGRATION_MASTER_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA= POSTGRES_HOST=127.0.0.1 POSTGRES_PASSWORD=disposable-test DJANGO_ALLOWED_HOSTS=localhost PUBLIC_BASE_URL=https://webinars.example.com
python manage.py check >artifacts/rebuild-checks.log 2>&1
python manage.py makemigrations --check --dry-run >>artifacts/rebuild-checks.log 2>&1
python manage.py collectstatic --noinput >artifacts/rebuild-static.log 2>&1
python -m pytest -q >artifacts/rebuild-pg.log 2>&1 || { tail -100 artifacts/rebuild-pg.log; exit 1; }
ruff check . >artifacts/rebuild-lint.log 2>&1
tail -15 artifacts/rebuild-pg.log
cat artifacts/rebuild-checks.log artifacts/rebuild-lint.log

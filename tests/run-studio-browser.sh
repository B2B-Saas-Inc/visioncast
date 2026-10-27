#!/bin/bash
# Run ONLY inside the lifecycle-managed Playwright test container.
set -euo pipefail
apt-get update -qq
apt-get install -y -qq python3-venv >/tmp/apt.log
python3 -m venv /tmp/venv
export PATH=/tmp/venv/bin:$PATH
cd /workspace/Code/webinar-platform
npm install --prefix /tmp/pw playwright@1.58.2 --no-audit --no-fund >/tmp/npm.log
export NODE_PATH=/tmp/pw/node_modules
mkdir /source
for item in manage.py requirements.txt config webinars preview; do cp -R "$item" /source/; done
export STUDIO_PASSWORD=$(python -c 'import secrets;print(secrets.token_urlsafe(24))')
python -c "import os;open('/tmp/preview-credential','w').write('password: '+os.environ['STUDIO_PASSWORD'])"
export WEBINAR_PREVIEW=1 WEBINAR_PREVIEW_RUN=pi-webinar-11111111-1111-1111-1111-111111111111
sh /source/preview/container-start.sh >artifacts/rebuild-browser-server.log 2>&1 & pid=$!
trap 'kill $pid 2>/dev/null || true' EXIT
ready=0
for i in $(seq 1 180); do
  if curl --max-time 2 -sf http://127.0.0.1:4318/__preview__/identity >/dev/null; then ready=1; break; fi
  if ! kill -0 "$pid" 2>/dev/null; then break; fi
  sleep 2
done
if [ "$ready" != 1 ]; then
  echo 'Preview did not become ready; see artifacts/rebuild-browser-server.log' >&2
  exit 1
fi
export STUDIO_BASE_URL=http://localhost:4318 STUDIO_USERNAME=preview
node tests/browser-studio.cjs >artifacts/rebuild-browser.log 2>&1 || { tail -40 artifacts/rebuild-browser.log; exit 1; }
cat artifacts/rebuild-browser.log
# Riverside fixtures: synthetic, seeded once into the same preview DB with the same
# preview settings (the seed only creates rows, so throwaway keys suffice here). The
# fixture holds test capabilities, so it stays in a private /tmp dir and never in artifacts/.
mkdir -m 700 /tmp/rv-seed /tmp/rv-fixture
cp tests/riverside_browser_seed.py /tmp/rv-seed/
trap 'kill $pid 2>/dev/null || true; rm -rf /tmp/rv-fixture' EXIT
(cd /app && umask 077 && \
  DJANGO_SETTINGS_MODULE=preview.settings \
  DJANGO_SECRET_KEY="$(python -c 'import secrets;print(secrets.token_urlsafe(64))')" \
  INTEGRATION_MASTER_KEY="$(python -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())')" \
  python -I -c 'import sys; sys.path.insert(0, "/app"); sys.argv = sys.argv[1:]; exec(open(sys.argv[0]).read())' \
    /tmp/rv-seed/riverside_browser_seed.py /tmp/rv-fixture/fixture.json) >artifacts/riverside-seed.log 2>&1 \
  || { echo 'Riverside fixture seed failed; see artifacts/riverside-seed.log' >&2; exit 1; }
RIVERSIDE_FIXTURE=/tmp/rv-fixture/fixture.json node tests/browser-riverside.cjs >artifacts/riverside-browser.log 2>&1 \
  || { tail -40 artifacts/riverside-browser.log; exit 1; }
cat artifacts/riverside-browser.log

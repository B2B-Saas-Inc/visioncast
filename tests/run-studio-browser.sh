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
for i in $(seq 1 60); do
  if curl -sf http://127.0.0.1:4318/__preview__/identity >/dev/null; then break; fi
  sleep 2
done
export STUDIO_BASE_URL=http://localhost:4318 STUDIO_USERNAME=preview
node tests/browser-studio.cjs >artifacts/rebuild-browser.log 2>&1 || { tail -40 artifacts/rebuild-browser.log; exit 1; }
cat artifacts/rebuild-browser.log

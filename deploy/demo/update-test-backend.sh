#!/bin/bash
# Fixed existing test backend only. No seed, dependencies, secrets or infrastructure changes.
set -euo pipefail
umask 077
[[ $(hostname) == visioncast-demo ]]
[[ $(curl -fsS --max-time 5 http://169.254.169.254/metadata/v1/id) == 612305850 ]]
base=/srv/visioncast
bundle=/srv/visioncast/gtm-update.tar
[[ ${1:-} == --apply ]] || {
  echo 'DRY RUN: backup database/code; install uploaded source; migrate; sync roles; collectstatic; restart existing web service. Preserve data/secrets and egress restrictions. No worker, seed, vendor calls or dependency installs.'
  exit 0
}
exec 9>/run/visioncast-update.lock
flock -n 9
stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup=$base/backups/$stamp
stage=$base/stage-$stamp
mkdir -p "$backup" "$stage"
chmod 700 "$backup"
tar -xf "$bundle" -C "$stage"
cmp "$stage/requirements.txt" "$base/app/requirements.txt"
test -f "$stage/webinars/studio/views_tracking.py"
chmod 755 "$stage"
runuser -u postgres -- pg_dump -Fc visioncast > "$backup/database.dump"
test -s "$backup/database.dump"
systemctl stop visioncast-web
swapped=0
recover() {
  code=$?
  if (( code != 0 )); then
    if (( swapped )); then
      mv "$base/app" "$backup/failed-app"
      mv "$backup/app" "$base/app"
    fi
    systemctl restart visioncast-web || true
    echo "Update failed; original source restored when swapped. Database not reversed. Backup: $backup" >&2
  fi
  exit "$code"
}
trap recover EXIT
mv "$base/app" "$backup/app"
if ! mv "$stage" "$base/app"; then mv "$backup/app" "$base/app"; exit 1; fi
swapped=1
/usr/local/sbin/visioncast-manage migrate --noinput
/usr/local/sbin/visioncast-manage sync_roles
/usr/local/sbin/visioncast-manage collectstatic --noinput
/usr/local/sbin/visioncast-manage check
systemctl restart visioncast-web
for i in $(seq 1 30); do
  if curl -fsS --max-time 3 -H 'Host: 64-225-16-137.sslip.io' -H 'X-Forwarded-Proto: https' http://127.0.0.1:8000/healthz >/dev/null; then
    echo "Updated existing test backend; backup: $backup"
    exit 0
  fi
  sleep 1
done
exit 1

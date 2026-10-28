#!/usr/bin/env bash
# Nightly backup for cron: deploy/backup.sh --encrypt-to <age recipient> into the backup
# directory, then prune encrypted dumps older than BACKUP_KEEP_DAYS. Parameters come from
# /srv/visioncast/bootstrap.conf (written by bootstrap.sh; no secrets in it).
#
#   backup-nightly.sh [--conf /srv/visioncast/bootstrap.conf]
#
# The dumps contain personal data and encrypted destination secrets. The age identity
# (private key) and INTEGRATION_MASTER_KEY must live OFF this host. Copy the .age files
# off-host (rclone/scp from your backup system; nothing here uploads anything).
set -Eeuo pipefail

CONF=/srv/visioncast/bootstrap.conf
if [[ ${1:-} == --conf ]]; then CONF=$2; fi
[[ -r $CONF ]] || { printf 'backup-nightly: %s not readable\n' "$CONF" >&2; exit 1; }

BASE=$(sed -n 's/^BASE=//p' "$CONF" | tail -n 1)
RECIPIENT=$(sed -n 's/^AGE_RECIPIENT=//p' "$CONF" | tail -n 1)
KEEP_DAYS=$(sed -n 's/^BACKUP_KEEP_DAYS=//p' "$CONF" | tail -n 1)
BASE=${BASE:-/srv/visioncast}
KEEP_DAYS=${KEEP_DAYS:-14}
BACKUP_DIR=$BASE/backups
[[ $RECIPIENT == age1* ]] || { printf 'backup-nightly: AGE_RECIPIENT missing in %s; refusing an unencrypted scheduled backup\n' "$CONF" >&2; exit 1; }
[[ $KEEP_DAYS =~ ^[0-9]+$ ]] || { printf 'backup-nightly: BACKUP_KEEP_DAYS must be a number\n' >&2; exit 1; }

cd "$BASE/app/current"
printf '%s start\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
deploy/backup.sh --encrypt-to "$RECIPIENT" "$BACKUP_DIR"
PRUNED=$(find "$BACKUP_DIR" -maxdepth 1 -name 'webinars-*.dump.age' -type f -mtime +"$KEEP_DAYS" -print -delete | wc -l | tr -d ' ')
COUNT=$(find "$BACKUP_DIR" -maxdepth 1 -name 'webinars-*.dump.age' -type f | wc -l | tr -d ' ')
printf '%s done: pruned %s older than %s days, %s encrypted dumps kept in %s (copy them off-host)\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$PRUNED" "$KEEP_DAYS" "$COUNT" "$BACKUP_DIR"

#!/usr/bin/env bash
# Logical backup of the Compose database (custom-format pg_dump, no secrets printed).
#
#   deploy/backup.sh                    writes ./backups/webinars-<UTC timestamp>.dump (mode 0600)
#   deploy/backup.sh /srv/backups       writes into that directory
#   deploy/backup.sh --encrypt-to age1…  additionally encrypts with age and shreds the plain dump
#
# Honours the usual Compose environment: COMPOSE_PROJECT_NAME, COMPOSE_FILE and
# the .env file next to compose.yaml. The dump is produced inside the `db`
# container with its own credentials; nothing from .env is echoed.
#
# The dump contains personal data and encrypted destination secrets. Encrypt it
# before it leaves the host and keep INTEGRATION_MASTER_KEY somewhere else: a
# dump alone cannot decrypt destination credentials (verified in docs/validation.md).
set -Eeuo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

OUT_DIR="$ROOT/backups"
RECIPIENT=""
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
usage() { sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
    case $1 in
        --encrypt-to) [[ $# -ge 2 ]] || die "--encrypt-to needs an age recipient"; RECIPIENT=$2; shift ;;
        -h|--help) usage; exit 0 ;;
        -*) die "unknown option: $1" ;;
        *) OUT_DIR=$1 ;;
    esac
    shift
done

command -v docker >/dev/null || die "docker not found"
[[ -z $RECIPIENT ]] || command -v age >/dev/null || die "age is not installed"
docker compose ps --status running --services 2>/dev/null | grep -qx db || die "the db service is not running (docker compose up -d db)"

umask 077
mkdir -p "$OUT_DIR"
TS=$(date -u +%Y%m%dT%H%M%SZ)
DUMP="$OUT_DIR/webinars-$TS.dump"
[[ ! -e $DUMP ]] || die "$DUMP already exists"

# --no-owner: restore target may use a different role name. Custom format is
# compressed and lets pg_restore reorder, filter and run with --exit-on-error.
docker compose exec -T db sh -c 'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --no-owner' > "$DUMP"
[[ -s $DUMP ]] || { rm -f "$DUMP"; die "dump is empty"; }
[[ $(head -c 5 "$DUMP") == "PGDMP" ]] || { rm -f "$DUMP"; die "dump does not start with the PGDMP magic"; }
chmod 0600 "$DUMP"
# Table of contents check: the archive must be readable by pg_restore.
docker compose exec -T db pg_restore --list < "$DUMP" >/dev/null || { rm -f "$DUMP"; die "pg_restore cannot read the dump"; }

SIZE=$(wc -c < "$DUMP" | tr -d ' ')
if [[ -n $RECIPIENT ]]; then
    age -r "$RECIPIENT" -o "$DUMP.age" "$DUMP"
    chmod 0600 "$DUMP.age"
    if command -v shred >/dev/null; then shred -u "$DUMP"; else rm -P -f "$DUMP" 2>/dev/null || rm -f "$DUMP"; fi
    printf 'backup: %s (%s bytes before encryption)\n' "$DUMP.age" "$SIZE"
else
    printf 'backup: %s (%s bytes, mode 0600, unencrypted: encrypt before copying off-host)\n' "$DUMP" "$SIZE"
fi

#!/usr/bin/env bash
# Encrypted backup of the live server's data: the database (rooms, crew, accounts, settings,
# engagement), uploads (slides, graphics, the brand) and speakers' local recordings. Run on the
# server as root; setup puts it in cron nightly. Recordings sent to a bucket aren't included.
#
# Needs, once:
#   apt-get install -y age sqlite3
#   LIVE_BACKUP_AGE_RECIPIENT=age1...   in deploy/live/.env (a public key; the private key stays
#                                       off this server, or nothing here could be trusted)
# Optional: LIVE_BACKUP_DIR (default /var/backups/visioncast-live), LIVE_BACKUP_KEEP_DAYS (14).
#
# Restore: stop the stack, then
#   age -d -i <identity file> live-<time>.tar.age | tar -x -C /var/lib/docker/volumes/<project>_live-data/_data
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
env_file="$here/.env"
die() { printf 'backup.sh: %s\n' "$*" >&2; exit 1; }
setting() { sed -n "s/^$1=//p" "$env_file" | tail -n 1; }

[[ $(id -u) -eq 0 ]] || die "run as root: it reads the Docker volume directly"
[[ -f $env_file ]] || die "$env_file is missing"
command -v age >/dev/null || die "age isn't installed: apt-get install -y age"
command -v sqlite3 >/dev/null || die "sqlite3 isn't installed: apt-get install -y sqlite3"
recipient=$(setting LIVE_BACKUP_AGE_RECIPIENT)
[[ $recipient =~ ^age1[0-9a-z]{58}$ ]] || die "set LIVE_BACKUP_AGE_RECIPIENT=age1... in $env_file"
dir=$(setting LIVE_BACKUP_DIR); dir=${dir:-/var/backups/visioncast-live}
keep=$(setting LIVE_BACKUP_KEEP_DAYS); keep=${keep:-14}
[[ $keep =~ ^[0-9]+$ ]] || die "LIVE_BACKUP_KEEP_DAYS must be a number of days"

# The live service's data volume, whatever the Compose project is called.
container=$(cd "$here" && docker compose ps -q live)
[[ -n $container ]] || die "the live service isn't running"
data=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' "$container")
[[ -d $data && -f $data/live.sqlite3 ]] || die "couldn't find the live database in the service's /data volume"

umask 077
mkdir -p "$dir"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
# A consistent copy of the database while the service keeps writing to it.
sqlite3 "$data/live.sqlite3" ".backup '$work/live.sqlite3'"
[[ $(sqlite3 "$work/live.sqlite3" 'PRAGMA integrity_check;') == ok ]] || die "the database copy failed its integrity check"

stamp=$(date -u +%Y%m%dT%H%M%SZ)
out="$dir/live-$stamp.tar.age"
# Files as they are, then the consistent database copy in place of the live one.
tar -C "$data" --exclude='./live.sqlite3' --exclude='./live.sqlite3-wal' --exclude='./live.sqlite3-shm' -cf "$work/files.tar" .
tar -C "$work" -rf "$work/files.tar" live.sqlite3
age -r "$recipient" -o "$out.part" "$work/files.tar"
mv "$out.part" "$out"
find "$dir" -maxdepth 1 -name 'live-*.tar.age' -mtime +"$keep" -delete
printf '%s backup: %s (%s)\n' "$(date -u +%H:%M:%S)" "$out" "$(du -h "$out" | cut -f1)"

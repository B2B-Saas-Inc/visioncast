#!/usr/bin/env bash
# Puts the live server's data back from a backup made by backup.sh: the database from one run, and
# every file that run listed. Run on the server as root, with the live stack stopped:
#
#   cd deploy/live && docker compose stop live
#   deploy/live/restore.sh <age identity file> [time, like 20261119T023001Z; default the newest]
#   docker compose start live
#
# Not in a backup, so not restored: videos uploaded for the stage (upload them again), and
# recordings, which are in the bucket. Speakers' recordings that had reached the bucket are listed
# from there.
#
# LIVE_BACKUP_DATA restores into a folder of your choosing instead of the service's volume, for a
# test or to look inside a backup without touching the server.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
env_file="${LIVE_BACKUP_ENV:-$here/.env}"
die() { printf 'restore.sh: %s\n' "$*" >&2; exit 1; }
setting() { [[ -f $env_file ]] && sed -n "s/^$1=//p" "$env_file" | tail -n 1; }

identity=${1:-}
[[ -n $identity && -f $identity ]] || die "usage: restore.sh <age identity file> [time]"
command -v age >/dev/null || die "age isn't installed: apt-get install -y age"
dir=$(setting LIVE_BACKUP_DIR || true); dir=${LIVE_BACKUP_DIR:-${dir:-/var/backups/visioncast-live}}
[[ -d $dir/lists ]] || die "no backups in $dir"
stamp=${2:-$(ls "$dir/lists" | sed -n 's/^live-\(.*\)\.tsv\.age$/\1/p' | sort | tail -n 1)}
[[ -f $dir/lists/live-$stamp.tsv.age && -f $dir/db/live-$stamp.sqlite3.age ]] || die "no backup called $stamp in $dir"

if [[ -n ${LIVE_BACKUP_DATA:-} ]]; then
    data=$LIVE_BACKUP_DATA
    mkdir -p "$data"
else
    [[ $(id -u) -eq 0 ]] || die "run as root: it writes the Docker volume directly"
    [[ -z $(cd "$here" && docker compose ps -q --status running live) ]] || die "stop the live service first: docker compose stop live"
    volume=$(docker volume ls -q | grep -E '_live-data$' | head -n 1)
    [[ -n $volume ]] || die "couldn't find the live-data volume"
    data=$(docker volume inspect -f '{{.Mountpoint}}' "$volume")
fi

umask 077
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
age -d -i "$identity" -o "$work/list.tsv" "$dir/lists/live-$stamp.tsv.age"
# Everything is checked before anything is written.
missing=0
while IFS=$'\t' read -r hash path; do
    [[ -f $dir/files/$hash.age ]] || { printf 'missing: %s\n' "$path" >&2; missing=$((missing + 1)); }
    [[ $path != /* && $path != *..* ]] || die "a listed path isn't inside the volume: $path"
done < "$work/list.tsv"
[[ $missing -eq 0 ]] || die "$missing listed files aren't in $dir/files; nothing was changed"

age -d -i "$identity" -o "$work/live.sqlite3" "$dir/db/live-$stamp.sqlite3.age"
rm -f "$data/live.sqlite3-wal" "$data/live.sqlite3-shm"
install -m 600 "$work/live.sqlite3" "$data/live.sqlite3"
restored=0
while IFS=$'\t' read -r hash path; do
    mkdir -p "$data/$(dirname "$path")"
    age -d -i "$identity" -o "$data/$path" "$dir/files/$hash.age"
    restored=$((restored + 1))
done < "$work/list.tsv"
# The service runs as its own user, which owned the volume before.
owner=$(stat -c '%u:%g' "$data")
chown -R "$owner" "$data"
printf 'restored %s: the database and %d files into %s. Stage videos need uploading again.\n' "$stamp" "$restored" "$data"

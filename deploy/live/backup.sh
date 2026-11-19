#!/usr/bin/env bash
# Encrypted backup of the live server's data, with each file kept once.
#
# Every run saves the database (rooms, crew, accounts, settings, engagement), which is small and
# changes daily, and a list of the files the data volume holds. The files themselves (slides,
# graphics, the brand, fonts, and speakers' own recordings that haven't reached the bucket) are
# stored once each, by their contents: a file that hasn't changed since the last run isn't stored
# again, however many nights it stays. Run on the server as root; setup puts it in cron nightly,
# and update.sh runs it before an update.
#
# Left out, because each already has its one copy somewhere else: videos uploaded for the stage
# (you have the original), uploads still in progress, speakers' recordings already copied to the
# bucket, and the main recordings, which are in the bucket.
#
# Needs, once:
#   apt-get install -y age sqlite3
#   LIVE_BACKUP_AGE_RECIPIENT=age1...   in deploy/live/.env (a public key; the private key stays
#                                       off this server, or nothing here could be trusted)
# Optional: LIVE_BACKUP_DIR (default /var/backups/visioncast-live), LIVE_BACKUP_KEEP_DAYS (14):
# how long each run's database and list are kept. A file goes when no kept run lists it. The
# newest run is always kept.
#
# What's in LIVE_BACKUP_DIR:
#   db/live-<time>.sqlite3.age   the database, each run
#   lists/live-<time>.tsv.age    the files the volume held then: content hash, path
#   lists/live-<time>.hashes     the same hashes without the paths, so old files can be cleared
#                                without the private key
#   files/<sha256>.age           each file's contents, once
#   seen.tsv                     path, size and time changed -> hash, so unchanged files aren't read
#
# Restore: deploy/live/restore.sh <identity file> [time]
#
# LIVE_BACKUP_DATA points at a data folder directly, instead of the running service's volume
# (for tests, or a volume restored elsewhere).
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
env_file="${LIVE_BACKUP_ENV:-$here/.env}"
die() { printf 'backup.sh: %s\n' "$*" >&2; exit 1; }
setting() { sed -n "s/^$1=//p" "$env_file" | tail -n 1; }

[[ -f $env_file ]] || die "$env_file is missing"
command -v age >/dev/null || die "age isn't installed: apt-get install -y age"
command -v sqlite3 >/dev/null || die "sqlite3 isn't installed: apt-get install -y sqlite3"
recipient=$(setting LIVE_BACKUP_AGE_RECIPIENT)
[[ $recipient =~ ^age1[0-9a-z]{58}$ ]] || die "set LIVE_BACKUP_AGE_RECIPIENT=age1... in $env_file"
dir=$(setting LIVE_BACKUP_DIR); dir=${LIVE_BACKUP_DIR:-${dir:-/var/backups/visioncast-live}}
keep=$(setting LIVE_BACKUP_KEEP_DAYS); keep=${keep:-14}
[[ $keep =~ ^[0-9]+$ ]] || die "LIVE_BACKUP_KEEP_DAYS must be a number of days"

if [[ -n ${LIVE_BACKUP_DATA:-} ]]; then
    data=$LIVE_BACKUP_DATA
else
    [[ $(id -u) -eq 0 ]] || die "run as root: it reads the Docker volume directly"
    # The live service's data volume, whatever the Compose project is called.
    container=$(cd "$here" && docker compose ps -q live)
    [[ -n $container ]] || die "the live service isn't running"
    data=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' "$container")
fi
[[ -d $data && -f $data/live.sqlite3 ]] || die "couldn't find the live database in $data"

umask 077
mkdir -p "$dir/db" "$dir/lists" "$dir/files"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# A consistent copy of the database while the service keeps writing to it.
sqlite3 "$data/live.sqlite3" ".backup '$work/live.sqlite3'"
[[ $(sqlite3 "$work/live.sqlite3" 'PRAGMA integrity_check;') == ok ]] || die "the database copy failed its integrity check"

# What the database says is kept elsewhere: stage videos, and speakers' recordings in the bucket.
sqlite3 "$work/live.sqlite3" "
    SELECT 'uploads/' || room || '/' || id FROM studio_docs
     WHERE kind = 'file' AND json_extract(json, '\$.kind') = 'media';
    SELECT 'uploads/' || room || '/' || id || '.local' FROM studio_docs
     WHERE kind = 'local' AND json_extract(json, '\$.bucketKey') IS NOT NULL;" | sort -u > "$work/elsewhere"

touch "$dir/seen.tsv"
: > "$work/list.tsv"; : > "$work/seen.tsv"
listed=0; stored=0; stored_bytes=0; left_out=0; busy=0
while IFS=$'\t' read -r path size changed; do
    # Uploads in progress, and what has its copy elsewhere.
    if [[ $path == *.part ]] || grep -qxF "$path" "$work/elsewhere"; then
        left_out=$((left_out + 1)); continue
    fi
    file="$data/$path"
    hash=$(awk -F'\t' -v p="$path" -v s="$size" -v c="$changed" '$1 == p && $2 == s && $3 == c { print $4; exit }' "$dir/seen.tsv")
    if [[ -z $hash ]]; then
        hash=$(sha256sum "$file" | cut -d' ' -f1)
    fi
    if [[ ! -f $dir/files/$hash.age ]]; then
        age -r "$recipient" -o "$dir/files/$hash.age.part" "$file"
        # A file still being written, like a recording in progress, waits for the next run.
        if [[ $(find "$file" -printf '%s\t%T@') != "$size"$'\t'"$changed" ]] || [[ $(sha256sum "$file" | cut -d' ' -f1) != "$hash" ]]; then
            rm -f "$dir/files/$hash.age.part"; busy=$((busy + 1)); continue
        fi
        mv "$dir/files/$hash.age.part" "$dir/files/$hash.age"
        stored=$((stored + 1)); stored_bytes=$((stored_bytes + size))
    fi
    printf '%s\t%s\n' "$hash" "$path" >> "$work/list.tsv"
    printf '%s\t%s\t%s\t%s\n' "$path" "$size" "$changed" "$hash" >> "$work/seen.tsv"
    listed=$((listed + 1))
done < <(cd "$data" && find . -type f ! -name 'live.sqlite3*' -printf '%P\t%s\t%T@\n' | grep -v $'\t.*\t.*\t' | sort)

# The run is saved last, so a run that stopped halfway lists nothing it didn't store.
stamp=$(date -u +%Y%m%dT%H%M%SZ)
age -r "$recipient" -o "$dir/db/live-$stamp.sqlite3.age.part" "$work/live.sqlite3"
mv "$dir/db/live-$stamp.sqlite3.age.part" "$dir/db/live-$stamp.sqlite3.age"
cut -f1 "$work/list.tsv" | sort -u > "$dir/lists/live-$stamp.hashes"
age -r "$recipient" -o "$dir/lists/live-$stamp.tsv.age.part" "$work/list.tsv"
mv "$dir/lists/live-$stamp.tsv.age.part" "$dir/lists/live-$stamp.tsv.age"
mv "$work/seen.tsv" "$dir/seen.tsv"

# Runs older than the limit go, except the newest. Then any file no kept run lists.
newest=$(ls "$dir/lists" | sed -n 's/^live-\(.*\)\.tsv\.age$/\1/p' | sort | tail -n 1)
find "$dir/lists" "$dir/db" -maxdepth 1 -type f -name 'live-*' -mtime +"$keep" ! -name "live-$newest.*" -delete
find "$dir/lists" -maxdepth 1 -name 'live-*.hashes' | while read -r h; do
    [[ -f ${h%.hashes}.tsv.age ]] || rm -f "$h"
done
cat "$dir"/lists/live-*.hashes 2>/dev/null | sort -u > "$work/kept"
cleared=0
for f in "$dir"/files/*.age "$dir"/files/*.age.part; do
    [[ -e $f ]] || continue
    name=$(basename "$f"); name=${name%.part}; name=${name%.age}
    if [[ $f == *.part ]] || ! grep -qxF "$name" "$work/kept"; then rm -f "$f"; cleared=$((cleared + 1)); fi
done
# Backups from before files were kept once, one whole archive a night, age out the same way.
find "$dir" -maxdepth 1 -name 'live-*.tar.age' -mtime +"$keep" -delete

printf '%s backup %s: %d files listed, %d new (%d MB), %d left out (kept elsewhere or in progress), %d still being written, %d cleared. %s in all\n' \
    "$(date -u +%H:%M:%S)" "$stamp" "$listed" "$stored" "$((stored_bytes / 1048576))" "$left_out" "$busy" "$cleared" "$(du -sh "$dir" | cut -f1)"

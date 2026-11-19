#!/usr/bin/env bash
# deploy/live/backup.sh and restore.sh against a made-up data volume, in a Debian container like
# the server: each file stored once, what's kept elsewhere left out, old runs and their files
# cleared, and a restore that gives back what was there. Needs Docker.
set -euo pipefail
repo=$(cd "$(dirname "$0")/../../.." && pwd)
exec docker run --rm -v "$repo/deploy/live:/scripts:ro" debian:bookworm-slim bash -c '
set -euo pipefail
apt-get update -qq >/dev/null && apt-get install -y -qq age sqlite3 >/dev/null
fail() { echo "FAIL: $*"; exit 1; }
count() { find "$1" -type f -name "$2" | wc -l; }
data=/tmp/data; backups=/tmp/backups; mkdir -p $data/uploads/r1 $data/uploads/_brand
age-keygen -o /tmp/key 2>/dev/null
printf "LIVE_BACKUP_AGE_RECIPIENT=%s\n" "$(age-keygen -y /tmp/key)" > /tmp/env
export LIVE_BACKUP_ENV=/tmp/env LIVE_BACKUP_DATA=$data LIVE_BACKUP_DIR=$backups
sqlite3 $data/live.sqlite3 "CREATE TABLE studio_docs (seq INTEGER PRIMARY KEY, room TEXT, kind TEXT, id TEXT, json TEXT);
  INSERT INTO studio_docs (room, kind, id, json) VALUES
   (\"r1\", \"file\", \"vid1\", json_object(\"kind\", \"media\")),
   (\"r1\", \"file\", \"deck1\", json_object(\"kind\", \"deck\")),
   (\"r1\", \"local\", \"rec1\", json_object(\"bucketKey\", \"recordings/r1/speakers/a.mp4\")),
   (\"r1\", \"local\", \"rec2\", json_object(\"bucketKey\", NULL));"
head -c 2000000 /dev/urandom > $data/uploads/r1/vid1          # a stage video: you have the original
head -c 100000 /dev/urandom > $data/uploads/r1/deck1          # slides
head -c 300000 /dev/urandom > $data/uploads/r1/rec1.local     # a speaker recording already in the bucket
head -c 300000 /dev/urandom > $data/uploads/r1/rec2.local     # one that is not
head -c 50000 /dev/urandom > $data/uploads/r1/up9.part        # an upload in progress
head -c 5000 /dev/urandom > $data/uploads/_brand/logo.png

out=$(/scripts/backup.sh); echo "$out"
echo "$out" | grep -q "3 files listed, 3 new" || fail "first run should store deck1, rec2 and the logo"
[[ $(count $backups/files "*.age") -eq 3 ]] || fail "three files stored"
out=$(/scripts/backup.sh); echo "$out"
echo "$out" | grep -q "3 files listed, 0 new" || fail "nothing new on a second run"
[[ $(count $backups/files "*.age") -eq 3 && $(count $backups/db "*.age") -eq 2 && $(count $backups/lists "*.tsv.age") -eq 2 ]] || fail "files once, database and list per run"
sleep 1; head -c 100000 /dev/urandom > $data/uploads/r1/deck1
out=$(/scripts/backup.sh); echo "$out"
echo "$out" | grep -q "1 new" || fail "a changed file is stored again"
[[ $(count $backups/files "*.age") -eq 4 ]] || fail "both versions while both runs are kept"
# The first runs age out, and with them the old version of deck1.
for f in $backups/lists/* $backups/db/*; do touch -d "30 days ago" "$f"; done
sleep 1; out=$(/scripts/backup.sh); echo "$out"
[[ $(count $backups/lists "*.tsv.age") -eq 1 ]] || fail "only the newest run left"
[[ $(count $backups/files "*.age") -eq 3 ]] || fail "the old version of deck1 cleared"
# Restore the newest run somewhere else and compare.
LIVE_BACKUP_DATA=/tmp/restored /scripts/restore.sh /tmp/key >/dev/null
for f in uploads/r1/deck1 uploads/r1/rec2.local uploads/_brand/logo.png; do cmp -s $data/$f /tmp/restored/$f || fail "$f restored"; done
for f in uploads/r1/vid1 uploads/r1/rec1.local uploads/r1/up9.part; do [[ ! -e /tmp/restored/$f ]] || fail "$f should not be in a backup"; done
[[ $(sqlite3 /tmp/restored/live.sqlite3 "PRAGMA integrity_check; SELECT count(*) FROM studio_docs;" | tr "\n" " ") == "ok 4 " ]] || fail "database restored"
grep -rqs "deck1" $backups/files && fail "file names must not appear in the clear" || true
echo "PASS"
'

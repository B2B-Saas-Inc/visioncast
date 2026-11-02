#!/bin/sh
# Is this server big enough for the session you're about to run? Run it on the server, on any provider.
#   deploy/live/size-check.sh <attendees> [hours] [speakers keeping their own recording]
# Assumes you record, broadcast and caption. --no-recording, --no-broadcast and --no-captions take those off.
# The sizes are estimates, from LiveKit's published benchmark: see Capacity in live/README.md.
set -u
recording=1 broadcast=1 captions=1
attendees="" hours=2 speakers=0 position=0
for arg in "$@"; do
  case "$arg" in
    --no-recording) recording=0 ;;
    --no-broadcast) broadcast=0 ;;
    --no-captions) captions=0 ;;
    *) position=$((position + 1))
       case "$position" in 1) attendees=$arg ;; 2) hours=$arg ;; 3) speakers=$arg ;; *) echo "Too many arguments." >&2; exit 2 ;; esac ;;
  esac
done
case "$attendees$hours$speakers" in ''|*[!0-9.]*) echo "Usage: $0 <attendees> [hours] [speakers keeping their own recording]" >&2; exit 2 ;; esac
[ -n "$attendees" ] || { echo "Usage: $0 <attendees> [hours] [speakers keeping their own recording]" >&2; exit 2; }

have_cpus=$(nproc)
have_memory=$(awk '/MemTotal/ { printf "%.0f", $2 / 1048576 }' /proc/meminfo)
have_disk=$(df -Pk "$(dirname "$0")" | awk 'NR == 2 { printf "%.0f", $4 / 1048576 }')

awk -v attendees="$attendees" -v hours="$hours" -v speakers="$speakers" -v recording="$recording" -v broadcast="$broadcast" -v captions="$captions" \
    -v cpus="$have_cpus" -v memory="$have_memory" -v disk="$have_disk" 'BEGIN {
  gbps = attendees * 3.128 / 1000
  media = gbps * 3.5
  need_cpus = int((media + 4 * recording + 4 * broadcast + 4 * captions) / 0.8 + 0.5)
  if (need_cpus < 4) need_cpus = 4
  need_memory = need_cpus * 2
  need_disk = 25 + 2 * hours * recording + 4.5 * hours * speakers
  printf "For %d attendees over %s hours:\n", attendees, hours
  printf "  CPUs     need about %d, have %d\n", need_cpus, cpus
  printf "  Memory   need about %d GB, have %d GB\n", need_memory, memory
  printf "  Free disk need about %d GB, have %d GB\n", need_disk - 25, disk
  printf "  Outbound need up to %.1f Gbps, and up to %.1f TB per hour. Check your plan allows this: this script cannot see it.\n", gbps, gbps * 0.45
  short = (cpus < need_cpus) + (memory < need_memory) + (disk < need_disk - 25)
  if (gbps > 8) { print "  That is close to the limit of a 10 Gbps server, and a room has to fit on one server. Send the overflow to YouTube or a CDN."; short = 1 }
  if (short) { print "Too small. Size it up before going live."; exit 1 }
  print "Big enough."
}'

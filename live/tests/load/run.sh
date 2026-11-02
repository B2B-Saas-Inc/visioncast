#!/usr/bin/env bash
# How much one server takes, on this machine: LiveKit's own load tester for media, and a script for
# what this service does itself (the event stream, chat, votes). Needs what tests/e2e/run.sh needs.
#   VIEWERS=200 PUBLISHERS=3 EVENT_CLIENTS=2000 live/tests/load/run.sh
# A laptop shares its CPU between the server and the load, so treat the results as a lower bound.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
e2e="$here/../e2e"
out="${LOAD_OUT:-$(mktemp -d)}"
mkdir -p "$out"
cd "$e2e"
spool=$(mktemp -d); chmod 777 "$spool"; export SLIDES_SPOOL=$spool
docker compose up -d --quiet-pull redis livekit egress s3
trap 'kill "${pid:-}" 2>/dev/null || true; [ -n "${KEEP_STACK:-}" ] || docker compose down -v >/dev/null 2>&1' EXIT
for _ in $(seq 1 60); do curl -sf -o /dev/null http://127.0.0.1:17880/ && break; sleep 1; done
export LIVE_SERVICE_SECRET=e2e-service-secret-e2e-service-secret-e2e LIVE_TICKET_KEY=e2e-ticket-key-e2e-ticket-key-e2e-ticket
export LIVEKIT_API_KEY=devkey LIVEKIT_API_SECRET=e2e-livekit-secret-e2e-livekit-secret-e2e
export LIVE_BIND=0.0.0.0:18090 LIVE_PUBLIC_URL=http://127.0.0.1:18090 LIVEKIT_URL=http://127.0.0.1:17880 LIVEKIT_PUBLIC_URL=ws://127.0.0.1:17880
work=$(mktemp -d); export LIVE_DB_PATH=$work/live.sqlite3 LIVE_UPLOAD_DIR=$work/uploads RUST_LOG=warn
curl -sf -o /dev/null http://127.0.0.1:18090/healthz && { echo "Something is already answering on port 18090. Stop it first, or these tests would run against it." >&2; exit 1; }
(cd ../.. && cargo build --release --quiet --bin visioncast-live)
../../target/release/visioncast-live > "$out/service.log" 2>&1 &
pid=$!
for _ in $(seq 1 30); do curl -sf -o /dev/null http://127.0.0.1:18090/healthz && break; sleep 0.5; done
ulimit -n 20000 2>/dev/null || true
echo "---- this service: event stream, chat and votes"
LIVE_BASE=http://127.0.0.1:18090 EVENT_CLIENTS="${EVENT_CLIENTS:-1000}" SECONDS_TO_RUN="${SECONDS_TO_RUN:-40}" node "$here/events.mjs" | tee "$out/events.json"
echo "---- LiveKit: real media to simulated viewers"
docker run --rm --network vc-live-e2e_default livekit/livekit-cli:latest load-test \
  --url ws://livekit:7880 --api-key devkey --api-secret "$LIVEKIT_API_SECRET" --room load-test \
  --video-publishers "${PUBLISHERS:-3}" --audio-publishers "${PUBLISHERS:-3}" --subscribers "${VIEWERS:-100}" --duration "${SECONDS_TO_RUN:-40}s" 2>&1 | tee "$out/livekit.txt" | tail -24
echo "Results in $out"

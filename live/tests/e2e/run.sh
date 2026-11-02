#!/usr/bin/env bash
# Real end-to-end run of visioncast-live. Needs Docker, cargo, ffprobe, Node, Chromium and
# playwright-core (PLAYWRIGHT_MODULE and PLAYWRIGHT_CHROMIUM_EXECUTABLE point at them).
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
cd "$here"
[ -f "$PWD/certs/server.key" ] || { mkdir -p certs; openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj "/CN=rtmp" -addext "subjectAltName=DNS:rtmp" -keyout certs/server.key -out certs/server.crt 2>/dev/null; chmod 644 certs/server.key; }
[ -f models/ggml-base.en.bin ] || { mkdir -p models; curl -fsSL -o models/ggml-base.en.bin https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin; }
[ "$(shasum -a 1 models/ggml-base.en.bin | cut -d' ' -f1)" = 137c40403d78fd54d454da0f9bd998f78703390c ]
spool=$(mktemp -d); chmod 777 "$spool"; export SLIDES_SPOOL=$spool
docker compose up -d --quiet-pull
trap 'docker compose logs --tail 30 egress > "${E2E_OUT:-/tmp}/egress.log" 2>&1 || true; docker compose logs --tail 30 slides > "${E2E_OUT:-/tmp}/slides.log" 2>&1 || true; kill "${pid:-}" 2>/dev/null || true; [ -n "${KEEP_STACK:-}" ] || docker compose down -v >/dev/null 2>&1' EXIT
for _ in $(seq 1 60); do curl -sf -o /dev/null http://127.0.0.1:17880/ && docker compose exec -T s3 sh -c 'echo "s3.bucket.create -name recordings" | weed shell' >/dev/null 2>&1 && curl -sf -o /dev/null http://127.0.0.1:19000/recordings && break; sleep 1; done

export LIVE_SERVICE_SECRET=e2e-service-secret-e2e-service-secret-e2e
export LIVE_TICKET_KEY=e2e-ticket-key-e2e-ticket-key-e2e-ticket
export LIVEKIT_API_KEY=devkey LIVEKIT_API_SECRET=e2e-livekit-secret-e2e-livekit-secret-e2e
export LIVE_BIND=0.0.0.0:18090 LIVE_PUBLIC_URL=http://127.0.0.1:18090
export LIVEKIT_URL=http://127.0.0.1:17880 LIVEKIT_PUBLIC_URL=ws://127.0.0.1:17880
export RECORDING_S3_BUCKET=recordings RECORDING_S3_REGION=us-east-1 RECORDING_S3_ENDPOINT=http://s3:9000
export RECORDING_S3_ACCESS_KEY=e2e-access-key RECORDING_S3_SECRET_KEY=e2e-secret-key-e2e-secret-key RECORDING_S3_FORCE_PATH_STYLE=1
work=$(mktemp -d); export LIVE_DB_PATH=$work/live.sqlite3 LIVE_UPLOAD_DIR=$work/uploads
export LIVE_INTERNAL_URL=http://host.docker.internal:18090 LIVE_RTMP_ALLOW_PRIVATE=${LIVE_RTMP_ALLOW_PRIVATE-1}
export LIVE_ADMIN_EMAIL=owner@example.com LIVE_ADMIN_PASSWORD=e2e-admin-password-e2e
export LIVE_CAPTIONS_URL=http://127.0.0.1:18095 RECORDING_S3_INTERNAL_ENDPOINT=http://127.0.0.1:19000
export LIVE_SLIDES_DIR=$spool LIVE_INGRESS_URL=rtmp://127.0.0.1:12935/x
export LIVE_TRANSLATE_URL=http://127.0.0.1:15000 LIVE_TRANSLATE_LANGUAGES=es
mkdir -p "${E2E_OUT:-/tmp}"
curl -sf -o /dev/null http://127.0.0.1:18090/healthz && { echo "Something is already answering on port 18090. Stop it first, or these tests would run against it." >&2; exit 1; }
(cd ../.. && cargo build --quiet)
../../target/debug/visioncast-live > "${E2E_OUT:-/tmp}/visioncast-live.log" 2>&1 &
pid=$!
for _ in $(seq 1 30); do curl -sf -o /dev/null http://127.0.0.1:18090/healthz && break; sleep 0.5; done
# Real speech for the fake microphones, so captions can be checked word by word.
for who in host guest; do
  case $who in
    host) words="Welcome everyone to the launch webinar. Today we will show you how the new product works." ;;
    guest) words="Thanks for having me. Our customers love the reporting dashboard and the mobile app." ;;
  esac
  say -v Samantha -o "$work/$who.aiff" "$words"
  ffmpeg -y -loglevel error -i "$work/$who.aiff" -af "apad=pad_dur=2" -ar 48000 -ac 1 "$work/$who.wav"
done
export SPEECH_HOST="$work/host.wav" SPEECH_GUEST="$work/guest.wav"
[ -n "${DECK_PYTHON:-}" ] || { python3 -m venv "$work/deck" && "$work/deck/bin/pip" -q install python-pptx==1.0.2; export DECK_PYTHON="$work/deck/bin/python"; }
export LIVE_BASE=http://127.0.0.1:18090 S3_BASE=http://127.0.0.1:19000
case "${E2E_SUITE:-all}" in
  main) node run.cjs ;;
  features) node features.cjs ;;
  browsers) node ../browsers/run.cjs ;;
  attack) node attack.cjs ;;
  *) node run.cjs; node features.cjs ;;
esac

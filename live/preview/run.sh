#!/usr/bin/env bash
# A local preview of the live studio on a real LiveKit server: one room, one link per person.
# Needs Docker, cargo and ffmpeg. Stop it with live/preview/stop.sh. Test credentials only.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
e2e="$here/../tests/e2e"
out="${PREVIEW_DIR:-$here/.preview}"
mkdir -p "$out/samples" "$out/data"
cd "$e2e"
[ -f "$PWD/certs/server.key" ] || { mkdir -p certs; openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj "/CN=rtmp" -addext "subjectAltName=DNS:rtmp" -keyout certs/server.key -out certs/server.crt 2>/dev/null; chmod 644 certs/server.key; }
[ -f models/ggml-base.en.bin ] || { mkdir -p models; curl -fsSL -o models/ggml-base.en.bin https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin; }
[ "$(shasum -a 1 models/ggml-base.en.bin | cut -d' ' -f1)" = 137c40403d78fd54d454da0f9bd998f78703390c ]
spool=$(mktemp -d); chmod 777 "$spool"; export SLIDES_SPOOL=$spool
docker compose up -d --quiet-pull
for _ in $(seq 1 60); do curl -sf -o /dev/null http://127.0.0.1:17880/ && docker compose exec -T s3 sh -c 'echo "s3.bucket.create -name recordings" | weed shell' >/dev/null 2>&1 && curl -sf -o /dev/null http://127.0.0.1:19000/recordings && break; sleep 1; done

secret=e2e-service-secret-e2e-service-secret-e2e
export LIVE_SERVICE_SECRET=$secret
export LIVE_TICKET_KEY=e2e-ticket-key-e2e-ticket-key-e2e-ticket
export LIVEKIT_API_KEY=devkey LIVEKIT_API_SECRET=e2e-livekit-secret-e2e-livekit-secret-e2e
export LIVE_BIND=0.0.0.0:18090 LIVE_PUBLIC_URL=http://127.0.0.1:18090
export LIVE_INTERNAL_URL=http://host.docker.internal:18090 LIVE_RTMP_ALLOW_PRIVATE=1
export LIVE_CAPTIONS_URL=http://127.0.0.1:18095 RECORDING_S3_INTERNAL_ENDPOINT=http://127.0.0.1:19000
export LIVE_SLIDES_DIR=$spool LIVE_INGRESS_URL=rtmp://127.0.0.1:12935/x
export LIVE_TRANSLATE_URL=http://127.0.0.1:15000 LIVE_TRANSLATE_LANGUAGES=es
export LIVEKIT_URL=http://127.0.0.1:17880 LIVEKIT_PUBLIC_URL=ws://127.0.0.1:17880
export RECORDING_S3_BUCKET=recordings RECORDING_S3_REGION=us-east-1 RECORDING_S3_ENDPOINT=http://s3:9000
export RECORDING_S3_ACCESS_KEY=e2e-access-key RECORDING_S3_SECRET_KEY=e2e-secret-key-e2e-secret-key RECORDING_S3_FORCE_PATH_STYLE=1
export RECORDING_PUBLIC_BASE_URL=http://127.0.0.1:19000/recordings
[ -f "$out/admin-password" ] || openssl rand -base64 15 > "$out/admin-password"
export LIVE_ADMIN_EMAIL=owner@example.com LIVE_ADMIN_PASSWORD="$(cat "$out/admin-password")"
export LIVE_DB_PATH="$out/data/live.sqlite3" LIVE_UPLOAD_DIR="$out/data/uploads"
(cd ../.. && cargo build --quiet)
if [ -f "$out/service.pid" ]; then kill "$(cat "$out/service.pid")" 2>/dev/null || true; fi
# A fresh start each time: VisionCast's side below is new, and the two have to match.
rm -f "$out/data/live.sqlite3" "$out/data/live.sqlite3-wal" "$out/data/live.sqlite3-shm"
nohup ../../target/debug/visioncast-live > "$out/service.log" 2>&1 &
echo $! > "$out/service.pid"
for _ in $(seq 1 30); do curl -sf -o /dev/null http://127.0.0.1:18090/healthz && break; sleep 0.5; done

# Things to upload in the room.
ffmpeg -y -loglevel error -f lavfi -i 'color=c=black:s=1920x1080,format=rgba,geq=r=194:g=70:b=46:a=if(between(X\,72\,1847)*between(Y\,72\,1007)\,0\,255)' -frames:v 1 "$out/samples/frame.png"
ffmpeg -y -loglevel error -f lavfi -i 'color=c=0x1C1A17:s=1920x1080,format=rgba' -frames:v 1 "$out/samples/background.png"
ffmpeg -y -loglevel error -f lavfi -i 'testsrc=duration=60:size=1280x720:rate=30' -f lavfi -i 'sine=frequency=330:duration=60' -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest -movflags +faststart "$out/samples/video.mp4"
printf '%%PDF-1.4\n%% A stand-in for a slide deck.\n' > "$out/samples/deck.pdf"

api() { curl -sf -X "$1" -H "Authorization: Bearer $secret" -H 'Content-Type: application/json' ${3:+-d "$3"} "http://127.0.0.1:18090/v1$2"; }
field() { python3 -c "import json,sys; print(json.loads(sys.argv[1])[sys.argv[2]])" "$1" "$2"; }

# The VisionCast studio, with an administrator and a session whose room is on this service.
root=$(cd ../../.. && pwd)
export PREVIEW_DIR="$out" LIVE_STACK_URL=http://127.0.0.1:18090
if [ -f "$out/studio.pid" ]; then kill "$(cat "$out/studio.pid")" 2>/dev/null || true; fi
rm -f "$out/studio.sqlite3"
seeded=$("$root/.venv-local/bin/python" "$here/studio_preview.py" --seed | tail -1)
room=$(field "$seeded" room); password=$(field "$seeded" password); session=$(field "$seeded" session); account=$(field "$seeded" account)
nohup "$root/.venv-local/bin/python" "$here/studio_preview.py" > "$out/studio.log" 2>&1 &
echo $! > "$out/studio.pid"
for _ in $(seq 1 40); do curl -sf -o /dev/null http://127.0.0.1:18100/healthz && break; sleep 0.5; done

person() {
  body=$(printf '{"role":"%s","name":"%s","title":"%s","key":"preview-%s"}' "$1" "$2" "$3" "$1")
  field "$(api POST "/rooms/$room/crew" "$body")" url
}
host=$(person host "Maya Host" "CEO"); producer=$(person producer "Pat Producer" ""); guest=$(person guest "Gus Guest" "VP Marketing")
a1=$(field "$(api POST "/rooms/$room/tickets" '{"identity":"preview-attendee-1"}')" joinUrl)
a2=$(field "$(api POST "/rooms/$room/tickets" '{"identity":"preview-attendee-2"}')" joinUrl)
cat > "$out/links.txt" <<LINKS
Live settings    http://127.0.0.1:18090/admin   $LIVE_ADMIN_EMAIL / $LIVE_ADMIN_PASSWORD
VisionCast       http://127.0.0.1:18100/studio/   admin / $password
Session hosting  http://127.0.0.1:18100/studio/sessions/$session/hosting
Attendee address http://127.0.0.1:18090/$(field "$(api GET "/rooms/$room")" slug)
Host             $host
Producer         $producer
Guest            $guest
Attendee 1       $a1
Attendee 2       $a2
Samples          $out/samples
Test platforms   Server rtmp://rtmp:1935/live with any key. Watch at http://127.0.0.1:18888/live/<key>/
LINKS
cat "$out/links.txt"

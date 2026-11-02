#!/usr/bin/env bash
# Writes deploy/live/.env with new secrets. Run once on the video server:
#   deploy/live/setup.sh live.example.com rtc.example.com turn.example.com you@example.com
# Secrets go to the file, never to the screen. An existing .env is left alone.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
if [ $# -ne 4 ]; then
  echo "Usage: $0 <service host, e.g. live.example.com> <media host, e.g. rtc.example.com> <TURN host, e.g. turn.example.com> <first admin's email>" >&2
  exit 2
fi
env_file="$here/.env"
if [ -e "$env_file" ]; then
  echo "$env_file already exists; leaving it as it is." >&2
else
  (umask 077; : > "$env_file")
  cat > "$env_file" <<ENV
LIVE_HOST=$1
RTC_HOST=$2
TURN_HOST=$3
LIVEKIT_API_KEY=vc$(openssl rand -hex 8)
LIVEKIT_API_SECRET=$(openssl rand -hex 32)
LIVE_SERVICE_SECRET=$(openssl rand -hex 32)
LIVE_TICKET_KEY=$(openssl rand -hex 32)
# The first owner, created once. Change this password after you first sign in.
LIVE_ADMIN_EMAIL=$4
LIVE_ADMIN_PASSWORD=$(openssl rand -base64 18)
# Recording: uncomment and fill in. Without a bucket, rooms work and recording stays off.
# RECORDING_S3_BUCKET=
# RECORDING_S3_REGION=
# RECORDING_S3_ENDPOINT=
# RECORDING_S3_ACCESS_KEY=
# RECORDING_S3_SECRET_KEY=
# RECORDING_S3_FORCE_PATH_STYLE=1
# RECORDING_PUBLIC_BASE_URL=
# Optional parts: COMPOSE_PROFILES=encoder,translate
# RTMP input, for OBS, vMix or a hardware encoder. Open port 1935 too.
# LIVE_INGRESS_URL=rtmp://$1/x
# Captions in other languages, separated by commas.
# LIVE_TRANSLATE_URL=http://127.0.0.1:5000
# LIVE_TRANSLATE_LANGUAGES=es,fr
ENV
fi
# Back to normal: only the secrets are private. Whisper's container has to be able to read its model.
umask 022
# Whisper's model for the captions: about 150 MB, checked against whisper.cpp's published hash.
model="$here/models/ggml-base.en.bin"
if [ -z "${SKIP_MODEL:-}" ] && [ ! -f "$model" ]; then
  mkdir -p "$here/models"
  curl -fsSL -o "$model.part" https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin
  [ "$(shasum -a 1 "$model.part" 2>/dev/null || sha1sum "$model.part")" != "" ]
  echo "137c40403d78fd54d454da0f9bd998f78703390c  $model.part" | { sha1sum -c --quiet - 2>/dev/null || shasum -a 1 -c --quiet -; }
  mv "$model.part" "$model"
fi
host=$(sed -n 's/^LIVE_HOST=//p' "$env_file")
rtc=$(sed -n 's/^RTC_HOST=//p' "$env_file")
turn=$(sed -n 's/^TURN_HOST=//p' "$env_file")
cat <<NEXT

Next:
1. DNS: point $host, $rtc and $turn at this server, DNS only.
2. Firewall: allow 80/tcp, 443/tcp, 7881/tcp, 3478/udp, 30000-40000/udp and 50000-60000/udp,
   and 1935/tcp if you turn on RTMP input.
   Do this before starting it: certificates are only asked for when Traefik starts.
3. For recording, fill in the RECORDING_S3_* lines in $env_file.
   Then start it: cd $here && docker compose up -d --build
4. On your VisionCast server, add $host to WEBHOOK_HOST_ALLOWLIST and restart.
5. In the studio, connect a LiveKit account: service host $host, and for the
   service secret the value of LIVE_SERVICE_SECRET in $env_file.
6. Before a session, check it's big enough: deploy/live/size-check.sh <attendees>
7. Settings are at https://$host/admin. Sign in as LIVE_ADMIN_EMAIL, with LIVE_ADMIN_PASSWORD
   from $env_file, then add everyone else under People. Locked out? Run:
   docker compose exec live visioncast-live reset-admin you@example.com
NEXT

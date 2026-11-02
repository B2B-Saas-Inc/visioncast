#!/usr/bin/env bash
# The whole system on one server: the studio, your own live rooms, and your brand on both.
#
#   deploy/quickstart/install.sh \
#     --studio events.example.com --live live.example.com --rtc rtc.example.com --turn turn.example.com \
#     --email you@example.com --name "Acme Events" --accent "#0b5a8a" --logo /path/to/logo.png
#
# Before you run it: a server of its own with Docker and Compose, all four names pointing at it (DNS
# only), and its firewall open on 80/tcp, 443/tcp, 7881/tcp, 3478/udp, 30000-40000/udp and
# 50000-60000/udp. Certificates are asked for as soon as it starts, so that has to come first.
#
# Secrets go to .env files and never to the screen. Run it again any time to bring things up to date:
# .env files are kept, and your brand isn't touched after the first time. To change a hostname, edit
# the .env files. Pass --version to use a published release instead of building from this checkout.
#
# --local tries it on your own machine: no real certificates, so browsers will warn. --no-captions
# skips the 150 MB speech model and the whisper build.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
studio="" live="" rtc="" turn="" email="" name="" accent="" logo="" local_only="" captions=1 version="" owner=""
while [ $# -gt 0 ]; do
  case "$1" in
    --studio) studio=$2; shift 2 ;;
    --live) live=$2; shift 2 ;;
    --rtc) rtc=$2; shift 2 ;;
    --turn) turn=$2; shift 2 ;;
    --email) email=$2; shift 2 ;;
    --name) name=$2; shift 2 ;;
    --accent) accent=$2; shift 2 ;;
    --logo) logo=$2; shift 2 ;;
    --version) version=$2; shift 2 ;;
    --image-owner) owner=$2; shift 2 ;;
    --local) local_only=1; shift ;;
    --no-captions) captions=""; shift ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done
for pair in "--studio:$studio" "--live:$live" "--rtc:$rtc" "--turn:$turn" "--email:$email"; do
  [ -n "${pair#*:}" ] || { echo "Missing ${pair%%:*}. See the top of $0." >&2; exit 2; }
done
for tool in docker openssl curl sed; do
  command -v "$tool" >/dev/null || { echo "$tool isn't installed." >&2; exit 1; }
done
docker compose version >/dev/null 2>&1 || { echo "Docker Compose isn't installed." >&2; exit 1; }
if [ -n "$logo" ]; then
  logo=$(cd "$(dirname "$logo")" && pwd)/$(basename "$logo")
  [ -f "$logo" ] || { echo "There's no file at $logo" >&2; exit 2; }
fi

live_env="$root/deploy/live/.env"
studio_env="$root/.env"

# Running it again with different names would leave half the system on the old ones.
already() { # file, variable, wanted, flag
  [ -e "$1" ] || return 0
  local have
  have=$(sed -n "s/^$2=//p" "$1" | head -1)
  if [ -n "$have" ] && [ "$have" != "$3" ]; then
    echo "$1 says $2=$have, but you passed $4 $3. Change the file if you meant to, then run this again." >&2
    exit 2
  fi
}
already "$live_env" LIVE_HOST "$live" --live
already "$live_env" RTC_HOST "$rtc" --rtc
already "$live_env" TURN_HOST "$turn" --turn
already "$live_env" STUDIO_HOST "$studio" --studio
if [ -z "$local_only" ] && [ -e "$live_env" ] && grep -q '^TLS_RESOLVER=$' "$live_env"; then
  echo "$live_env was made with --local, so it has no real certificates. Delete it, or those lines, to install for real." >&2
  exit 2
fi

# ---- the studio's settings. The file is private; the name is quoted so that # and $ survive.
if [ ! -e "$studio_env" ]; then
  (umask 077; : > "$studio_env")
  cat > "$studio_env" <<ENV
DJANGO_SECRET_KEY=$(openssl rand -hex 40)
DJANGO_DEBUG=0
DJANGO_ALLOWED_HOSTS=$studio
CSRF_TRUSTED_ORIGINS=https://$studio
DJANGO_TRUST_X_FORWARDED_PROTO=1
PUBLIC_BASE_URL=https://$studio
POSTGRES_HOST=db
POSTGRES_PORT=5432
POSTGRES_DB=webinars
POSTGRES_USER=webinars
POSTGRES_PASSWORD=$(openssl rand -hex 24)
POSTGRES_SSLMODE=prefer
INTEGRATION_MASTER_KEY=$(openssl rand -base64 32 | tr "+/" "-_")
WEBINAR_ALLOWED_ORIGINS=https://$studio
WEBHOOK_HOST_ALLOWLIST=$live
RATE_LIMIT_ENABLED=1
RATE_LIMIT_TRUST_X_FORWARDED_FOR=1
EMAIL_DELIVERY=disabled
DEFAULT_FROM_EMAIL=webinars@$studio
ENV
fi
[ -z "$version" ] || grep -q '^WEBINAR_IMAGE=' "$studio_env" || cat >> "$studio_env" <<ENV
WEBINAR_IMAGE=ghcr.io/${owner:-b2b-saas-inc}/visioncast:$version
WEBINAR_PULL_POLICY=never
ENV

# ---- the live server's settings
if [ -n "$captions" ]; then
  "$root/deploy/live/setup.sh" "$live" "$rtc" "$turn" "$email" >/dev/null
else
  SKIP_MODEL=1 "$root/deploy/live/setup.sh" "$live" "$rtc" "$turn" "$email" >/dev/null
fi
grep -q '^STUDIO_HOST=' "$live_env" || echo "STUDIO_HOST=$studio" >> "$live_env"
if [ -n "$local_only" ] && ! grep -q '^TLS_RESOLVER=' "$live_env"; then printf 'TLS_RESOLVER=\nLIVEKIT_USE_EXTERNAL_IP=false\n' >> "$live_env"; fi
[ -z "$version" ] || grep -q '^LIVE_VERSION=' "$live_env" || printf 'LIVE_VERSION=%s\nIMAGE_OWNER=%s\n' "$version" "${owner:-b2b-saas-inc}" >> "$live_env"

# ---- bring it all up
live_compose=(docker compose --env-file "$live_env" -f "$root/deploy/live/compose.yaml")
studio_compose=(docker compose -f "$root/compose.yaml")
services=(traefik redis livekit egress live slides)
[ -z "$captions" ] || services+=(captions)
# The optional parts, if they've been turned on in .env.
profiles=$(sed -n 's/^COMPOSE_PROFILES=//p' "$live_env")
case ",$profiles," in *,encoder,*) services+=(ingress) ;; esac
case ",$profiles," in *,translate,*) services+=(translate) ;; esac
echo "Starting the live server. The first time takes a while."
if [ -n "$version" ]; then
  "${live_compose[@]}" pull "${services[@]}"
  "${live_compose[@]}" up -d "${services[@]}"
else
  "${live_compose[@]}" up -d --build "${services[@]}"
fi
echo "Starting the studio."
if [ -n "$version" ]; then
  docker pull "ghcr.io/${owner:-b2b-saas-inc}/visioncast:$version"
else
  "${studio_compose[@]}" build --quiet migrate
fi
"${studio_compose[@]}" up -d db
"${studio_compose[@]}" run --rm migrate
"${studio_compose[@]}" up -d web worker
"${studio_compose[@]}" run --rm web manage sync_roles >/dev/null

# ---- the first owner, and the studio joined to the live server. Secrets go by name through the
# environment, so they're never on a command line for anyone on this machine to read.
admin_file="$root/deploy/quickstart/.first-admin"
if [ ! -e "$admin_file" ]; then
  BOOTSTRAP_ADMIN_PASSWORD=$(openssl rand -base64 18)
  export BOOTSTRAP_ADMIN_PASSWORD
  "${studio_compose[@]}" run --rm -e BOOTSTRAP_ADMIN_PASSWORD web manage bootstrap_admin --username owner --email "$email" >/dev/null
  (umask 077; printf '%s\n' "$BOOTSTRAP_ADMIN_PASSWORD" > "$admin_file")
  unset BOOTSTRAP_ADMIN_PASSWORD
fi
LIVE_SERVICE_SECRET=$(sed -n 's/^LIVE_SERVICE_SECRET=//p' "$live_env")
export LIVE_SERVICE_SECRET
"${studio_compose[@]}" run --rm -e LIVE_SERVICE_SECRET web manage connect_live_server --host "$live" --name "Live" >/dev/null

# ---- your brand, on both, the first time only. After that it's yours to change on the Brand page.
brand_file="$root/deploy/quickstart/.brand-applied"
if [ ! -e "$brand_file" ] && [ -n "$logo$accent$name" ]; then
  logo_file=/dev/null; [ -z "$logo" ] || logo_file=$logo
  BRAND_SAVE_NAME=$name BRAND_SAVE_ACCENT=$accent "${studio_compose[@]}" run --rm -T -e BRAND_SAVE_NAME -e BRAND_SAVE_ACCENT web manage shell -c "
import os, sys
from webinars import brand
data = sys.stdin.buffer.read()
brand.save(name=os.environ['BRAND_SAVE_NAME'], accent=os.environ['BRAND_SAVE_ACCENT'], logo=data or None)
" < "$logo_file" >/dev/null
  # Straight to the live server on this machine, so it doesn't wait on DNS or certificates. The
  # header goes through a file, so the secret isn't on a command line.
  to_live() {
    printf 'header = "Authorization: Bearer %s"\n' "$LIVE_SERVICE_SECRET" | curl -skf --retry 10 --retry-connrefused --retry-delay 3 --resolve "$live:443:127.0.0.1" --config - "$@" >/dev/null
  }
  if [ -n "$accent" ] && ! to_live -X PUT -H 'Content-Type: application/json' -d "{\"accent\":\"$accent\",\"stage\":\"\",\"mode\":\"\",\"font\":\"\"}" "https://$live/v1/brand"; then
    echo "The live server didn't take your colour. Set it on the studio's Brand page once it's up." >&2
  fi
  if [ -n "$logo" ] && ! to_live -X PUT --data-binary "@$logo" "https://$live/v1/brand/logo"; then
    echo "The live server didn't take your logo: it has to be a PNG of at most 2 MB. Set it on the studio's Brand page once it's up." >&2
  fi
  : > "$brand_file"
fi
unset LIVE_SERVICE_SECRET

cat <<DONE

It's running.

  Studio        https://$studio/studio/   owner, password in $admin_file
  Live settings https://$live/admin       $email, password is LIVE_ADMIN_PASSWORD in $live_env

Change both passwords when you first sign in. Keep the two .env files safe: they hold your database
password and the keys everything is encrypted with. See docs/branding.md for backups, and what to put
in front of the sign-in pages.

Then, in the studio, create an event and a session, open its hosting page, and choose the Live
account. Your brand is under Workspace, Brand.

Check it from another machine: deploy/live/verify-public.sh $live $rtc $turn
If certificates are missing because a port was closed at the start: docker compose --env-file $live_env -f $root/deploy/live/compose.yaml restart traefik
DONE

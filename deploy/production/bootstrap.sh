#!/usr/bin/env bash
# VisionCast production bootstrap: Docker Compose (web + worker + PostgreSQL), nginx + TLS,
# backups and retention cron on one Ubuntu 24.04 host behind Cloudflare.
#
#   bootstrap.sh --dry-run [params]        print every step and the rendered files (secrets redacted)
#   bootstrap.sh [params]                  apply (run as root on the host)
#   ssh root@<ip> 'DOMAIN=events.growthcast.app ACME_EMAIL=... AGE_RECIPIENT=age1... bash -s' < bootstrap.sh
#
# Parameters (flag, or environment variable of the same name; non-secret values are saved
# to /srv/visioncast/bootstrap.conf and reused by later runs, so a re-run needs none):
#   --domain DOMAIN                     platform host (required), e.g. events.growthcast.app
#   --site-origins LIST                 WEBINAR_ALLOWED_ORIGINS; THANK_YOU_REDIRECT_HOSTS derives from it
#                                       (default https://growthcast.app,https://www.growthcast.app)
#   --public-event-url-template URL     PUBLIC_EVENT_URL_TEMPLATE (default https://growthcast.app/events/{slug})
#   --image REF                         WEBINAR_IMAGE: empty = build webinar-platform:<version> from the
#                                       uploaded release source; ghcr.io/... = pull with the token in
#                                       <secrets>/ghcr-token; any other ref must already exist locally
#   --ghcr-username NAME                username for `docker login ghcr.io` (default visioncast-deploy)
#   --smtp-host HOST --smtp-port N --smtp-username U --smtp-starttls 0|1 --smtp-ssl 0|1
#                                       EMAIL_DELIVERY=smtp when --smtp-host is set; the password is
#                                       read from <secrets>/smtp-password (never a flag)
#   --default-from-email ADDR           DEFAULT_FROM_EMAIL (default no-reply@DOMAIN)
#   --webhook-host-allowlist LIST       WEBHOOK_HOST_ALLOWLIST (default empty)
#   --acme-email ADDR                   Let's Encrypt account email (required for the certbot step)
#   --age-recipient age1...             backup encryption recipient (required for the cron step)
#   --admin-username NAME --admin-email ADDR   first administrator (default admin / ACME email)
#   --agent-key 0|1                     also create an API key "agent" with scope config_read (default 0)
#   --web-concurrency N                 gunicorn workers (default 2 for a 1 vCPU / 2 GB droplet)
#   --purge-days N                      purge_personal_data --older-than-days (default 30)
#   --backup-keep-days N                encrypted dump retention on the host (default 14)
#   --origin-gate 0|1                   404 for /admin/ and /studio/ unless the request came through
#                                       Cloudflare (default 1; set 0 when not behind Cloudflare)
# Run control:
#   --steps LIST                        subset of: preflight,dirs,release,env,image,compose,cloudflare,
#                                       nginx,certbot,cron,status,verify (default: all)
#   --base DIR                          install root (default /srv/visioncast; another value skips the
#                                       root/OS checks: rehearsals)
#   --secrets-dir DIR                   default /root/visioncast-secrets (or <base>/secrets when --base is set)
#   --compose-project NAME              COMPOSE_PROJECT_NAME override (rehearsals; default from compose.yaml)
#   --skip-admin                        do not create the administrator (one exists, secret file lost)
#   --self-signed                       rehearsals only: self-signed certificate instead of certbot
#   --release-tar PATH                  default <base>/release.tar (+ optional .sha256 next to it)
#
# Secrets never appear on a command line or in output. Generated secrets live in
# <release>/.env (0600) and in <secrets>/ (0700): integration-master-key, admin-password,
# proxy-api-key, agent-api-key. Re-runs keep existing secrets, the database and the image.
set -Eeuo pipefail
umask 022

# --- Parameters: environment > saved bootstrap.conf > built-in default; flags override all ---
PARAM_NAMES="DOMAIN SITE_ORIGINS PUBLIC_EVENT_URL_TEMPLATE WEBINAR_IMAGE GHCR_USERNAME SMTP_HOST SMTP_PORT SMTP_USERNAME SMTP_STARTTLS SMTP_SSL DEFAULT_FROM_EMAIL WEBHOOK_HOST_ALLOWLIST ACME_EMAIL AGE_RECIPIENT ADMIN_USERNAME ADMIN_EMAIL AGENT_KEY WEB_CONCURRENCY PURGE_DAYS BACKUP_KEEP_DAYS ORIGIN_GATE"
ENV_SET=" "
for k in $PARAM_NAMES; do
    if [[ -n ${!k+x} ]]; then ENV_SET="$ENV_SET$k "; else printf -v "$k" '%s' ''; fi
done
BASE=${BASE:-/srv/visioncast}
for arg in "$@"; do case $arg in --base=*) BASE=${arg#--base=} ;; esac; done
prev=""; for arg in "$@"; do [[ $prev == --base ]] && BASE=$arg; prev=$arg; done
if [[ -r $BASE/bootstrap.conf ]]; then
    while IFS='=' read -r k v; do
        [[ " $PARAM_NAMES " == *" $k "* ]] || continue
        [[ $ENV_SET == *" $k "* ]] || printf -v "$k" '%s' "$v"
    done < <(grep -E '^[A-Z_]+=' "$BASE/bootstrap.conf")
fi
: "${SITE_ORIGINS:=https://growthcast.app,https://www.growthcast.app}"
[[ -n $PUBLIC_EVENT_URL_TEMPLATE ]] || PUBLIC_EVENT_URL_TEMPLATE='https://growthcast.app/events/{slug}'   # braces: not via ${:=}
: "${GHCR_USERNAME:=visioncast-deploy}"
: "${SMTP_PORT:=587}"
: "${SMTP_STARTTLS:=1}"
: "${SMTP_SSL:=0}"
: "${ADMIN_USERNAME:=admin}"
: "${AGENT_KEY:=0}"
: "${WEB_CONCURRENCY:=2}"
: "${PURGE_DAYS:=30}"
: "${BACKUP_KEEP_DAYS:=14}"
: "${ORIGIN_GATE:=1}"

SECRETS_DIR=${SECRETS_DIR:-}
COMPOSE_PROJECT=${COMPOSE_PROJECT:-}
RELEASE_TAR=${RELEASE_TAR:-}
STEPS=${STEPS:-preflight,dirs,release,env,image,compose,cloudflare,nginx,certbot,cron,status,verify}
DRY_RUN=0
SKIP_ADMIN=0
SELF_SIGNED=0

NGINX_CONF_DIR=${NGINX_CONF_DIR:-/etc/nginx/conf.d}
NGINX_SNIPPETS=${NGINX_SNIPPETS:-/etc/nginx/snippets}
CRON_FILE=${CRON_FILE:-/etc/cron.d/visioncast}
LOGROTATE_FILE=${LOGROTATE_FILE:-/etc/logrotate.d/visioncast}
LOG_DIR=${LOG_DIR:-/var/log/visioncast}
ACME_ROOT=${ACME_ROOT:-/var/www/visioncast-acme}
LETSENCRYPT_DIR=${LETSENCRYPT_DIR:-/etc/letsencrypt}
PROXY_KEY_NAME=growthcast-site
AGENT_KEY_NAME=agent

usage() { sed -n '2,52p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
    case $1 in
        --domain) DOMAIN=$2; shift ;;
        --site-origins) SITE_ORIGINS=$2; shift ;;
        --public-event-url-template) PUBLIC_EVENT_URL_TEMPLATE=$2; shift ;;
        --image) WEBINAR_IMAGE=$2; shift ;;
        --ghcr-username) GHCR_USERNAME=$2; shift ;;
        --smtp-host) SMTP_HOST=$2; shift ;;
        --smtp-port) SMTP_PORT=$2; shift ;;
        --smtp-username) SMTP_USERNAME=$2; shift ;;
        --smtp-starttls) SMTP_STARTTLS=$2; shift ;;
        --smtp-ssl) SMTP_SSL=$2; shift ;;
        --default-from-email) DEFAULT_FROM_EMAIL=$2; shift ;;
        --webhook-host-allowlist) WEBHOOK_HOST_ALLOWLIST=$2; shift ;;
        --acme-email) ACME_EMAIL=$2; shift ;;
        --age-recipient) AGE_RECIPIENT=$2; shift ;;
        --admin-username) ADMIN_USERNAME=$2; shift ;;
        --admin-email) ADMIN_EMAIL=$2; shift ;;
        --agent-key) AGENT_KEY=$2; shift ;;
        --web-concurrency) WEB_CONCURRENCY=$2; shift ;;
        --purge-days) PURGE_DAYS=$2; shift ;;
        --backup-keep-days) BACKUP_KEEP_DAYS=$2; shift ;;
        --origin-gate) ORIGIN_GATE=$2; shift ;;
        --steps) STEPS=$2; shift ;;
        --base) BASE=$2; shift ;;
        --base=*) BASE=${1#--base=} ;;
        --secrets-dir) SECRETS_DIR=$2; shift ;;
        --compose-project) COMPOSE_PROJECT=$2; shift ;;
        --release-tar) RELEASE_TAR=$2; shift ;;
        --dry-run) DRY_RUN=1 ;;
        --skip-admin) SKIP_ADMIN=1 ;;
        --self-signed) SELF_SIGNED=1 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1 (see --help)" ;;
    esac
    shift
done

STANDARD_BASE=1; [[ $BASE == /srv/visioncast ]] || STANDARD_BASE=0
[[ -n $SECRETS_DIR ]] || { if (( STANDARD_BASE )); then SECRETS_DIR=/root/visioncast-secrets; else SECRETS_DIR=$BASE/secrets; fi; }
[[ -n $RELEASE_TAR ]] || RELEASE_TAR=$BASE/release.tar
[[ -n $DEFAULT_FROM_EMAIL ]] || DEFAULT_FROM_EMAIL="no-reply@$DOMAIN"
[[ -n $ADMIN_EMAIL ]] || ADMIN_EMAIL=${ACME_EMAIL:-admin@$DOMAIN}
CONF_FILE=$BASE/bootstrap.conf
APP_DIR=$BASE/app
CURRENT=$APP_DIR/current
BACKUP_DIR=$BASE/backups
CERT_DIR=$LETSENCRYPT_DIR/live/$DOMAIN
[[ -z $COMPOSE_PROJECT ]] || export COMPOSE_PROJECT_NAME=$COMPOSE_PROJECT

# --- Helpers ------------------------------------------------------------------------------
log()  { printf '%s\n' "$*"; }
CURRENT_STEP=start
step() { CURRENT_STEP=$1; printf '\n== %s\n' "$1"; }
want() { [[ ",$STEPS," == *",$1,"* ]]; }
on_error() {
    printf '\nFAILED during step "%s" (line %s). Nothing after this point ran; re-run after fixing the cause\n' "$CURRENT_STEP" "$1" >&2
    printf '(every step is idempotent: existing secrets, the database and the image are kept).\n' >&2
}
trap 'on_error $LINENO' ERR
# Arguments are echoed: never pass a secret to run().
run() {
    if (( DRY_RUN )); then log "  would run: $*"; else log "  + $*"; "$@"; fi
}
# install_text DEST MODE < content : write when different (dry run: print the content).
install_text() {
    local dest=$1 mode=$2 tmp
    tmp=$(mktemp)
    cat > "$tmp"
    if (( DRY_RUN )); then
        log "  would install $dest (mode $mode):"
        sed 's/^/    | /' "$tmp"
    elif [[ -f $dest ]] && cmp -s "$tmp" "$dest" && [[ $(stat -c '%a' "$dest" 2>/dev/null || stat -f '%Lp' "$dest") == "${mode#0}" ]]; then
        log "  unchanged: $dest"
    else
        mkdir -p "$(dirname "$dest")"
        install -m "$mode" "$tmp" "$dest"
        log "  + installed $dest (mode $mode)"
    fi
    rm -f "$tmp"
}
# render TEMPLATE KEY=VALUE... : substitute @@KEY@@ placeholders (values must not contain '|').
render() {
    local tmpl=$1 expr="" kv; shift
    for kv in "$@"; do
        [[ ${kv#*=} != *"|"* && ${kv#*=} != *"&"* ]] || die "render: value for ${kv%%=*} contains '|' or '&'"
        expr="$expr; s|@@${kv%%=*}@@|${kv#*=}|g"
    done
    sed "${expr#; }" "$tmpl"
    if sed "${expr#; }" "$tmpl" | grep -q '@@[A-Z_]*@@'; then die "render: unreplaced placeholder in $tmpl"; fi
}
http_code() { curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$@" || true; }
sha256() { if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }
compose() { docker compose "$@"; }
manage() { docker compose run --rm -T web manage "$@"; }
# capture_secret FILE MARKER_REGEX CMD... : CMD prints a secret on the line after MARKER;
# that line goes straight into a new 0600 FILE. Nothing from CMD's stdout is shown.
capture_secret() {
    local file=$1 marker=$2 out; shift 2
    if [[ -s $file ]]; then log "  exists, unchanged: $file"; return 0; fi
    if (( DRY_RUN )); then log "  would create (value never shown): $file  via: $*"; return 0; fi
    log "  + $* -> $file"
    out=$(mktemp "$SECRETS_DIR/.capture.XXXXXX"); chmod 600 "$out"
    if ! "$@" > "$out"; then rm -f "$out"; die "command failed: $*"; fi
    ( umask 077; awk -v m="$marker" '$0 ~ m {getline; print; exit}' "$out" > "$file" )
    rm -f "$out"
    [[ -s $file ]] || { rm -f "$file"; die "no secret found in the output of: $*"; }
    chmod 600 "$file"
    log "  saved (value not shown): $file"
}

# Where the templates and helpers are: next to this script, else inside the unpacked release.
SRC_DIR=""
if [[ -n ${BASH_SOURCE[0]:-} && ${BASH_SOURCE[0]} != "bash" && -f $(dirname "${BASH_SOURCE[0]}")/nginx-site.conf.tmpl ]]; then
    SRC_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
fi

cd /
if (( DRY_RUN )); then log "MODE: DRY RUN (nothing is changed; rendered files are shown with secrets redacted)"; else log "MODE: APPLY"; fi
log "steps: $STEPS"

# --- preflight ----------------------------------------------------------------------------
step "preflight"
[[ -n $DOMAIN ]] || die "--domain is required"
[[ $DOMAIN =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$ ]] || die "--domain must be a lowercase DNS name"
[[ $SITE_ORIGINS =~ ^https://[^,[:space:]]+(,https://[^,[:space:]]+)*$ ]] || die "--site-origins must be comma-separated https origins"
[[ -z $PUBLIC_EVENT_URL_TEMPLATE || $PUBLIC_EVENT_URL_TEMPLATE == https://* ]] || die "--public-event-url-template must be https"
[[ $AGENT_KEY == 0 || $AGENT_KEY == 1 ]] || die "--agent-key must be 0 or 1"
[[ $ORIGIN_GATE == 0 || $ORIGIN_GATE == 1 ]] || die "--origin-gate must be 0 or 1"
[[ $WEB_CONCURRENCY =~ ^[1-9][0-9]?$ && $PURGE_DAYS =~ ^[0-9]+$ && $BACKUP_KEEP_DAYS =~ ^[0-9]+$ ]] || die "numeric parameter out of range"
[[ -z $AGE_RECIPIENT || $AGE_RECIPIENT =~ ^age1[a-z0-9]+$ ]] || die "--age-recipient must be an age public key (age1...)"
for v in SITE_ORIGINS PUBLIC_EVENT_URL_TEMPLATE WEBINAR_IMAGE SMTP_HOST SMTP_USERNAME DEFAULT_FROM_EMAIL WEBHOOK_HOST_ALLOWLIST ADMIN_EMAIL; do
    [[ ${!v} =~ ^[A-Za-z0-9_./:,{}=@+%!*~^-]*$ ]] || die "$v contains characters that .env cannot carry safely"
done
command -v python3 >/dev/null || die "python3 is required"
if (( ! DRY_RUN )) && (( STANDARD_BASE )); then
    [[ $EUID -eq 0 ]] || die "run as root (or use --dry-run)"
    os=$(. /etc/os-release && printf '%s %s' "$ID" "$VERSION_ID")
    [[ $os == "ubuntu 24.04" ]] || die "expected Ubuntu 24.04, found: $os"
fi
if (( ! DRY_RUN )); then
    if want image || want compose; then command -v docker >/dev/null || die "docker is required for the image/compose steps"; fi
    if want nginx || want certbot; then command -v nginx >/dev/null || die "nginx is required for the nginx/certbot steps"; fi
    if want certbot && (( ! SELF_SIGNED )); then
        command -v certbot >/dev/null || die "certbot is required"
        [[ -n $ACME_EMAIL ]] || die "--acme-email is required for the certbot step"
    fi
    if want cron; then
        [[ -n $AGE_RECIPIENT ]] || die "--age-recipient is required for the cron step (age-keygen on your workstation; keep the identity off-host)"
        command -v age >/dev/null || die "age is required (apt install age)"
    fi
fi
# Where the per-release files come from.
if [[ -z $SRC_DIR ]]; then
    if [[ -f $CURRENT/deploy/production/nginx-site.conf.tmpl ]]; then SRC_DIR=$CURRENT/deploy/production
    elif [[ -f $RELEASE_TAR ]]; then SRC_DIR=""   # resolved after the release step unpacks it
    else die "cannot find deploy/production/ templates: run from the directory or upload $RELEASE_TAR first"; fi
fi
log "  domain $DOMAIN; base $BASE; secrets $SECRETS_DIR; templates ${SRC_DIR:-<from release tar>}"

# --- dirs ---------------------------------------------------------------------------------
if want dirs; then
    step "dirs"
    run install -d -m 0755 "$BASE" "$APP_DIR"
    run install -d -m 0700 "$SECRETS_DIR" "$BACKUP_DIR"
    if (( STANDARD_BASE )); then
        run install -d -m 0755 "$ACME_ROOT"
        run install -d -m 0750 "$LOG_DIR"
    fi
fi

# --- release: unpack /srv/visioncast/release.tar into app/<version>, link current --------------
VERSION=""
RELEASE_DIR=""
if want release; then
    step "release"
    [[ -f $RELEASE_TAR ]] || die "$RELEASE_TAR not found (deploy/production/package-release.sh, then upload it)"
    VERSION=$(tar -xOf "$RELEASE_TAR" VERSION | tr -d '[:space:]')
    [[ $VERSION =~ ^[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?$ ]] || die "VERSION inside the tarball is not a version: '$VERSION'"
    SHA=$(sha256 "$RELEASE_TAR")
    if [[ -f $RELEASE_TAR.sha256 ]]; then
        WANT=$(cut -d' ' -f1 < "$RELEASE_TAR.sha256")
        [[ $SHA == "$WANT" ]] || die "SHA-256 mismatch: $RELEASE_TAR vs $RELEASE_TAR.sha256"
        log "  checksum verified: $SHA"
    else
        log "  no $RELEASE_TAR.sha256 next to the tarball; SHA-256 $SHA (compare with package-release.sh output)"
    fi
    RELEASE_DIR=$APP_DIR/$VERSION
    if [[ -f $RELEASE_DIR/.release-sha256 && $(cat "$RELEASE_DIR/.release-sha256") == "$SHA" ]]; then
        log "  already unpacked: $RELEASE_DIR"
    elif (( DRY_RUN )); then
        log "  would unpack $RELEASE_TAR into $RELEASE_DIR (keeping an existing .env)"
    else
        tmp="$RELEASE_DIR.tmp.$$"
        rm -rf "$tmp"; install -d -m 0755 "$tmp"
        tar --no-same-owner -xf "$RELEASE_TAR" -C "$tmp"
        [[ -f $tmp/compose.yaml && -f $tmp/Dockerfile && -f $tmp/.dockerignore && -f $tmp/.env.example && -x $tmp/deploy/upgrade.sh ]] \
            || die "tarball is incomplete (compose.yaml, Dockerfile, .dockerignore, .env.example, deploy/upgrade.sh)"
        printf '%s\n' "$SHA" > "$tmp/.release-sha256"
        chmod -R go-w "$tmp"
        if [[ -d $RELEASE_DIR ]]; then
            [[ ! -f $RELEASE_DIR/.env ]] || cp -p "$RELEASE_DIR/.env" "$tmp/.env"
            mv "$RELEASE_DIR" "$RELEASE_DIR.replaced-$(date -u +%Y%m%dT%H%M%SZ)"
        fi
        mv "$tmp" "$RELEASE_DIR"
        log "  + unpacked $RELEASE_DIR"
    fi
    if [[ -L $CURRENT ]]; then
        cur=$(readlink "$CURRENT")
        if [[ $cur == "$RELEASE_DIR" || $cur == "$VERSION" ]]; then log "  current -> $VERSION"
        else log "  NOTE: current -> $cur, tarball is $VERSION. Upgrades of a live install go through update.sh (backup, migrate, then switch); this run keeps current unchanged."; fi
    else
        run ln -sfn "$RELEASE_DIR" "$CURRENT"
    fi
    [[ -n $SRC_DIR ]] || SRC_DIR=$RELEASE_DIR/deploy/production
fi
if [[ -z $RELEASE_DIR ]]; then
    [[ -L $CURRENT ]] || die "no release installed yet; include the release step"
    RELEASE_DIR=$(cd "$CURRENT" && pwd -P)
    VERSION=$(tr -d '[:space:]' < "$RELEASE_DIR/VERSION")
fi
[[ -d $SRC_DIR ]] || die "templates directory missing: $SRC_DIR"
ENV_FILE=$RELEASE_DIR/.env

# Image reference (decided before the env step so .env can pin it).
IMAGE=$WEBINAR_IMAGE
IMAGE_MODE=local
if [[ -z $IMAGE ]]; then IMAGE="webinar-platform:$VERSION"; IMAGE_MODE=build
elif [[ $IMAGE == ghcr.io/* ]]; then IMAGE_MODE=pull; fi
[[ $IMAGE != *:latest && $IMAGE == *:* ]] || die "WEBINAR_IMAGE must be pinned to a version tag (never latest): $IMAGE"

# --- env --------------------------------------------------------------------------------------
site_hosts() { printf '%s' "$SITE_ORIGINS" | tr ',' '\n' | sed -E 's#^https?://##; s#[/:].*$##' | paste -sd, -; }
write_params() {
    cat <<EOF
DJANGO_DEBUG=0
DJANGO_ALLOWED_HOSTS=$DOMAIN
CSRF_TRUSTED_ORIGINS=https://$DOMAIN
DJANGO_TRUST_X_FORWARDED_PROTO=1
DJANGO_HSTS_SECONDS=31536000
PUBLIC_BASE_URL=https://$DOMAIN
PUBLIC_EVENT_URL_TEMPLATE=$PUBLIC_EVENT_URL_TEMPLATE
WEBINAR_ALLOWED_ORIGINS=$SITE_ORIGINS
THANK_YOU_REDIRECT_HOSTS=$(site_hosts)
WEBHOOK_HOST_ALLOWLIST=$WEBHOOK_HOST_ALLOWLIST
CAPABILITY_COOKIE_SECURE=1
RATE_LIMIT_ENABLED=1
RATE_LIMIT_TRUST_X_FORWARDED_FOR=1
RATE_LIMIT_TRUSTED_PROXY_COUNT=1
WEB_CONCURRENCY=$WEB_CONCURRENCY
EMAIL_DELIVERY=$([[ -n $SMTP_HOST ]] && echo smtp || echo disabled)
DEFAULT_FROM_EMAIL=$DEFAULT_FROM_EMAIL
SMTP_HOST=$SMTP_HOST
SMTP_PORT=$SMTP_PORT
SMTP_USERNAME=$SMTP_USERNAME
SMTP_STARTTLS=$SMTP_STARTTLS
SMTP_SSL=$SMTP_SSL
WEBINAR_IMAGE=$IMAGE
WEBINAR_PULL_POLICY=never
EOF
}
if want env; then
    step "env ($ENV_FILE)"
    PARAMS=$(mktemp); write_params > "$PARAMS"
    SMTP_PW=()
    if [[ -n $SMTP_HOST ]]; then
        if [[ -s $SECRETS_DIR/smtp-password ]]; then SMTP_PW=(--smtp-password-file "$SECRETS_DIR/smtp-password")
        else (( DRY_RUN )) && log "  NOTE: $SECRETS_DIR/smtp-password missing; SMTP_PASSWORD would stay empty" || die "--smtp-host is set but $SECRETS_DIR/smtp-password is missing (mode 600, one line)"; fi
    fi
    if (( DRY_RUN )); then
        log "  would $([[ -f $ENV_FILE ]] && echo update || echo create) $ENV_FILE (mode 0600). Rendered (secrets redacted):"
        python3 -I "$SRC_DIR/render_env.py" print --example "$SRC_DIR/../../.env.example" --params "$PARAMS" ${SMTP_PW[@]+"${SMTP_PW[@]}"} --existing "$ENV_FILE" | sed 's/^/    | /'
        log "  would save the master key copy to $SECRETS_DIR/integration-master-key (0600)"
    else
        if [[ -f $ENV_FILE ]]; then
            python3 -I "$SRC_DIR/render_env.py" update --example "$RELEASE_DIR/.env.example" --out "$ENV_FILE" --params "$PARAMS" ${SMTP_PW[@]+"${SMTP_PW[@]}"}
        else
            python3 -I "$SRC_DIR/render_env.py" create --example "$RELEASE_DIR/.env.example" --out "$ENV_FILE" --params "$PARAMS" ${SMTP_PW[@]+"${SMTP_PW[@]}"}
        fi
        python3 -I "$SRC_DIR/render_env.py" check --path "$ENV_FILE" --params "$PARAMS"
        python3 -I "$SRC_DIR/render_env.py" copy-secret --path "$ENV_FILE" --key INTEGRATION_MASTER_KEY --to "$SECRETS_DIR/integration-master-key"
        log "  REMINDER: copy $SECRETS_DIR/integration-master-key into your password manager / secret store OFF this host."
        log "            Backups are useless for destination credentials without it, and it must never sit next to the dumps."
    fi
    rm -f "$PARAMS"
    # Persist the non-secret parameters for re-runs and for the cron helpers.
    install_text "$CONF_FILE" 0600 <<EOF
# VisionCast bootstrap parameters (no secrets). Written by deploy/production/bootstrap.sh; re-runs read it.
BASE=$BASE
DOMAIN=$DOMAIN
SITE_ORIGINS=$SITE_ORIGINS
PUBLIC_EVENT_URL_TEMPLATE=$PUBLIC_EVENT_URL_TEMPLATE
WEBINAR_IMAGE=$WEBINAR_IMAGE
GHCR_USERNAME=$GHCR_USERNAME
SMTP_HOST=$SMTP_HOST
SMTP_PORT=$SMTP_PORT
SMTP_USERNAME=$SMTP_USERNAME
SMTP_STARTTLS=$SMTP_STARTTLS
SMTP_SSL=$SMTP_SSL
DEFAULT_FROM_EMAIL=$DEFAULT_FROM_EMAIL
WEBHOOK_HOST_ALLOWLIST=$WEBHOOK_HOST_ALLOWLIST
ACME_EMAIL=$ACME_EMAIL
AGE_RECIPIENT=$AGE_RECIPIENT
ADMIN_USERNAME=$ADMIN_USERNAME
ADMIN_EMAIL=$ADMIN_EMAIL
AGENT_KEY=$AGENT_KEY
WEB_CONCURRENCY=$WEB_CONCURRENCY
PURGE_DAYS=$PURGE_DAYS
BACKUP_KEEP_DAYS=$BACKUP_KEEP_DAYS
ORIGIN_GATE=$ORIGIN_GATE
EOF
fi

# --- image ------------------------------------------------------------------------------------
if want image; then
    step "image ($IMAGE, mode $IMAGE_MODE)"
    if (( ! DRY_RUN )) && docker image inspect "$IMAGE" >/dev/null 2>&1; then
        log "  present locally: $IMAGE ($(docker image inspect --format '{{.Id}}' "$IMAGE" | cut -c8-19))"
    else
        case $IMAGE_MODE in
            build)
                log "  building from $RELEASE_DIR (deploy/build-image.sh --allow-dirty: the release tree is not a git checkout, so the revision label is 'unknown'; RELEASE records the commit)"
                if (( DRY_RUN )); then log "  would run (in $RELEASE_DIR): deploy/build-image.sh --allow-dirty"
                else ( cd "$RELEASE_DIR" && run deploy/build-image.sh --allow-dirty ); fi ;;
            pull)
                TOKEN=$SECRETS_DIR/ghcr-token
                (( DRY_RUN )) || [[ -s $TOKEN ]] || die "$TOKEN missing: put a GitHub token with read:packages there (mode 600) or build from source (--image '')"
                log "  + docker login ghcr.io -u $GHCR_USERNAME --password-stdin < $TOKEN (token never on a command line)"
                (( DRY_RUN )) || docker login ghcr.io -u "$GHCR_USERNAME" --password-stdin < "$TOKEN" >/dev/null
                run docker pull "$IMAGE"
                run docker logout ghcr.io ;;
            local) die "image $IMAGE is not present locally (pass a ghcr.io/... reference or leave --image empty to build)" ;;
        esac
    fi
    (( DRY_RUN )) || docker image inspect "$IMAGE" >/dev/null 2>&1 || die "image $IMAGE still missing after $IMAGE_MODE"
fi

# --- compose: migrate, check, roles, admin, keys, up ----------------------------------------------
if want compose; then
    step "compose first run ($RELEASE_DIR, project ${COMPOSE_PROJECT_NAME:-webinar-platform})"
    if (( DRY_RUN )); then
        log "  would run (in $RELEASE_DIR): docker compose config -q; run --rm migrate; manage check; manage sync_roles"
        log "  would create (values never shown): $SECRETS_DIR/admin-password (bootstrap_admin --username $ADMIN_USERNAME --email $ADMIN_EMAIL)"
        log "  would create (values never shown): $SECRETS_DIR/proxy-api-key (create_api_key --name $PROXY_KEY_NAME --scope proxy)"
        [[ $AGENT_KEY != 1 ]] || log "  would create (values never shown): $SECRETS_DIR/agent-api-key (create_api_key --name $AGENT_KEY_NAME --scope config_read)"
        log "  would run: docker compose up -d; up -d --wait web worker; GET http://127.0.0.1:8000/healthz (Host: $DOMAIN)"
    else
        cd "$RELEASE_DIR"
        [[ -f $ENV_FILE ]] || die "$ENV_FILE missing; include the env step"
        run docker compose config -q
        run docker compose run --rm -T migrate
        run docker compose run --rm -T web manage check
        run docker compose run --rm -T web manage sync_roles
        if (( SKIP_ADMIN )); then log "  --skip-admin: administrator not created"
        else capture_secret "$SECRETS_DIR/admin-password" '^Generated password' docker compose run --rm -T web manage bootstrap_admin --username "$ADMIN_USERNAME" --email "$ADMIN_EMAIL"; fi
        capture_secret "$SECRETS_DIR/proxy-api-key" '^Raw key' docker compose run --rm -T web manage create_api_key --name "$PROXY_KEY_NAME" --scope proxy
        [[ $AGENT_KEY != 1 ]] || capture_secret "$SECRETS_DIR/agent-api-key" '^Raw key' docker compose run --rm -T web manage create_api_key --name "$AGENT_KEY_NAME" --scope config_read
        run docker compose up -d
        run docker compose up -d --wait web worker
        code=000
        for _ in $(seq 1 30); do
            code=$(http_code http://127.0.0.1:8000/healthz -H "Host: $DOMAIN" -H "X-Forwarded-Proto: https")
            [[ $code == 200 ]] && break; sleep 1
        done
        [[ $code == 200 ]] || die "loopback /healthz returned $code; see: docker compose logs web"
        log "  loopback /healthz: 200 ($(curl -s --max-time 5 -H "Host: $DOMAIN" -H 'X-Forwarded-Proto: https' http://127.0.0.1:8000/healthz))"
        docker compose ps
        cd /
    fi
fi

# --- cloudflare ranges -------------------------------------------------------------------------------
if want cloudflare; then
    step "cloudflare address ranges -> $NGINX_SNIPPETS"
    if (( DRY_RUN )); then
        tmpcf=$(mktemp -d); bash "$SRC_DIR/cloudflare-ips.sh" --out-dir "$tmpcf"
        log "  would install $NGINX_SNIPPETS/visioncast-cloudflare-realip.conf ($(grep -c set_real_ip_from "$tmpcf/visioncast-cloudflare-realip.conf") ranges) and visioncast-cloudflare-geo.conf"
        rm -rf "$tmpcf"
    else
        bash "$SRC_DIR/cloudflare-ips.sh" --out-dir "$NGINX_SNIPPETS"
    fi
fi

# --- nginx -----------------------------------------------------------------------------------------------
if want nginx; then
    step "nginx (port 80 now; HTTPS once $CERT_DIR exists)"
    if [[ $ORIGIN_GATE == 1 ]]; then
        install_text "$NGINX_SNIPPETS/visioncast-origin-gate.conf" 0644 <<'EOF'
# /admin/ and /studio/ are reachable only through Cloudflare (where Access enforces MFA).
# Anyone who finds the origin address gets 404. Generated by bootstrap.sh (--origin-gate 1).
if ($vc_from_cloudflare = 0) { return 404; }
EOF
    else
        install_text "$NGINX_SNIPPETS/visioncast-origin-gate.conf" 0644 <<'EOF'
# Origin gate disabled (bootstrap.sh --origin-gate 0): /admin/ and /studio/ answer to any peer.
EOF
    fi
    render "$SRC_DIR/nginx-site.conf.tmpl" "DOMAIN=$DOMAIN" "ACME_ROOT=$ACME_ROOT" | install_text "$NGINX_CONF_DIR/00-visioncast.conf" 0644
    if (( ! DRY_RUN )); then
        [[ ! -L /etc/nginx/sites-enabled/default ]] || run rm -f /etc/nginx/sites-enabled/default
        [[ ! -f $NGINX_CONF_DIR/default.conf ]] || run mv "$NGINX_CONF_DIR/default.conf" "$NGINX_CONF_DIR/default.conf.disabled"
        if [[ ! -f $CERT_DIR/fullchain.pem && -f $NGINX_CONF_DIR/visioncast-tls.conf ]]; then run rm -f "$NGINX_CONF_DIR/visioncast-tls.conf"; fi
        run nginx -t
        run systemctl enable --now nginx.service
        run systemctl reload nginx.service
    fi
fi

# --- certbot ------------------------------------------------------------------------------------------------
if want certbot; then
    step "certificate for $DOMAIN"
    if [[ -f $CERT_DIR/fullchain.pem ]]; then
        log "  present: $CERT_DIR (expires $(openssl x509 -enddate -noout -in "$CERT_DIR/fullchain.pem" 2>/dev/null | cut -d= -f2))"
    elif (( SELF_SIGNED )); then
        log "  --self-signed: rehearsal certificate (NOT for production)"
        run install -d -m 0700 "$CERT_DIR"
        (( DRY_RUN )) || openssl req -x509 -nodes -newkey rsa:2048 -days 7 -subj "/CN=$DOMAIN" \
            -keyout "$CERT_DIR/privkey.pem" -out "$CERT_DIR/fullchain.pem" >/dev/null 2>&1
    else
        resolved=$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk 'NR==1 {print $1}' || true)
        log "  $DOMAIN resolves to '${resolved:-nothing}' from this host"
        log "  HTTP-01 via webroot $ACME_ROOT. If the Cloudflare record is already PROXIED, keep 'Always Use HTTPS' OFF"
        log "  until this succeeds (Let's Encrypt must reach port 80 -> origin); simplest: issue while the record is DNS-only."
        if (( DRY_RUN )); then
            log "  would run: certbot certonly --webroot -w $ACME_ROOT -d $DOMAIN --non-interactive --agree-tos -m $ACME_EMAIL --keep-until-expiring"
        elif timeout 300 certbot certonly --webroot -w "$ACME_ROOT" -d "$DOMAIN" --non-interactive --agree-tos -m "$ACME_EMAIL" --keep-until-expiring; then
            log "  + certificate issued"
        else
            log "  WARNING: certbot failed. nginx keeps serving port 80 only. Fix DNS/Cloudflare (cloudflare.md, 'Issuing the certificate')"
            log "  and re-run: bootstrap.sh --steps certbot,nginx,status,verify"
        fi
    fi
    if (( DRY_RUN )); then
        log "  would install $LETSENCRYPT_DIR/renewal-hooks/deploy/visioncast-nginx-reload"
    elif [[ -d $LETSENCRYPT_DIR ]]; then
        install -D -m 0755 "$SRC_DIR/certbot-deploy-hook.sh" "$LETSENCRYPT_DIR/renewal-hooks/deploy/visioncast-nginx-reload"
    fi
fi

# --- nginx TLS server (after the certificate) -----------------------------------------------------------------
if want nginx; then
    step "nginx HTTPS server"
    if [[ -f $CERT_DIR/fullchain.pem ]] || (( DRY_RUN )); then
        render "$SRC_DIR/nginx-site-tls.conf.tmpl" "DOMAIN=$DOMAIN" "CERT_DIR=$CERT_DIR" "ACME_ROOT=$ACME_ROOT" | install_text "$NGINX_CONF_DIR/visioncast-tls.conf" 0644
        if (( ! DRY_RUN )); then run nginx -t; run systemctl reload nginx.service; fi
    else
        log "  skipped: no certificate yet"
    fi
fi

# --- cron + logrotate ----------------------------------------------------------------------------------------------
if want cron; then
    step "cron ($CRON_FILE) and logrotate ($LOGROTATE_FILE)"
    render "$SRC_DIR/cron.tmpl" "BASE=$BASE" "PURGE_DAYS=$PURGE_DAYS" | install_text "$CRON_FILE" 0644
    install_text "$LOGROTATE_FILE" 0644 < "$SRC_DIR/logrotate.conf"
    [[ -x $SRC_DIR/backup-nightly.sh && -x $SRC_DIR/cloudflare-ips.sh ]] || run chmod 0755 "$SRC_DIR/backup-nightly.sh" "$SRC_DIR/cloudflare-ips.sh"
    log "  backups: nightly 02:15 UTC -> $BACKUP_DIR (age-encrypted, $BACKUP_KEEP_DAYS days kept). Copy them off-host yourself."
fi

# --- status ---------------------------------------------------------------------------------------------------------
if want status; then
    step "status ($BASE/STATUS.md)"
    NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    IMAGE_ID="(not inspected)"; CERT_EXP="none"; COMPOSE_STATE="(not inspected)"; FIRST=""
    if (( ! DRY_RUN )); then
        command -v docker >/dev/null && docker image inspect "$IMAGE" >/dev/null 2>&1 && IMAGE_ID=$(docker image inspect --format '{{.Id}}' "$IMAGE")
        [[ ! -f $CERT_DIR/fullchain.pem ]] || CERT_EXP=$(openssl x509 -enddate -noout -in "$CERT_DIR/fullchain.pem" 2>/dev/null | cut -d= -f2)
        command -v docker >/dev/null && COMPOSE_STATE=$(cd "$RELEASE_DIR" 2>/dev/null && docker compose ps --format '{{.Service}}={{.Status}}' 2>/dev/null | paste -sd' ' - || true)
        [[ ! -f $BASE/STATUS.md ]] || FIRST=$(sed -n 's/^- First bootstrap: //p' "$BASE/STATUS.md" | head -n 1)
    fi
    FIRST=${FIRST:-$NOW}
    install_text "$BASE/STATUS.md" 0644 <<EOF
# VisionCast production host — $DOMAIN

Written by deploy/production/bootstrap.sh. No secrets: values live only in $RELEASE_DIR/.env and $SECRETS_DIR/.

- First bootstrap: $FIRST
- Last bootstrap run: $NOW
- Release: $VERSION in $RELEASE_DIR (current -> $(readlink "$CURRENT" 2>/dev/null || echo "$RELEASE_DIR"))
- Release manifest: $(tr '\n' ' ' < "$RELEASE_DIR/RELEASE" 2>/dev/null || echo "none")
- Image: $IMAGE ($IMAGE_MODE) id $IMAGE_ID
- Compose: ${COMPOSE_STATE:-not running}
- Certificate: $CERT_DIR, expires $CERT_EXP (certbot renew timer + deploy hook reloads nginx)
- Cloudflare ranges: $(sed -n 's/^# Generated \(.*\) by .*/\1/p' "$NGINX_SNIPPETS/visioncast-cloudflare-geo.conf" 2>/dev/null || echo "not installed") (weekly refresh via cron)
- Origin gate for /admin/ and /studio/: $ORIGIN_GATE
- Site origins: $SITE_ORIGINS; event page template: $PUBLIC_EVENT_URL_TEMPLATE
- Email: $([[ -n $SMTP_HOST ]] && echo "smtp via $SMTP_HOST:$SMTP_PORT as $SMTP_USERNAME" || echo "disabled"); from $DEFAULT_FROM_EMAIL
- Webhook host allowlist: ${WEBHOOK_HOST_ALLOWLIST:-(empty: no destination can be enabled)}
- Cron: backup 02:15 UTC (age-encrypted, keep $BACKUP_KEEP_DAYS days), purge 03:15 UTC (older than $PURGE_DAYS days), check Mon 04:15 UTC, Cloudflare ranges Mon 04:45 UTC
- Secret files (names only): $SECRETS_DIR/{integration-master-key,admin-password,proxy-api-key$([[ $AGENT_KEY == 1 ]] && echo ",agent-api-key")}
- Administrator username: $ADMIN_USERNAME (password in $SECRETS_DIR/admin-password; change it at first login)
EOF
fi

# --- verify (loopback) --------------------------------------------------------------------------------------------------
if want verify && (( ! DRY_RUN )); then
    step "verify (loopback)"
    failures=0
    expect() {  # expect CODE URL [curl args...]
        local want=$1 url=$2; shift 2
        local got; got=$(http_code "$url" "$@")
        if [[ $got == "$want" ]]; then log "  ok   $want $url"; else log "  FAIL $got (want $want) $url"; failures=$((failures + 1)); fi
    }
    expect 200 http://127.0.0.1:8000/healthz -H "Host: $DOMAIN" -H "X-Forwarded-Proto: https"
    if command -v nginx >/dev/null && systemctl is-active --quiet nginx 2>/dev/null; then
        expect 301 "http://$DOMAIN/healthz" --resolve "$DOMAIN:80:127.0.0.1"
        expect 000 "http://127.0.0.1/"      # unknown Host on port 80: connection closed (444)
        if [[ -f $CERT_DIR/fullchain.pem ]]; then
            k=(); (( SELF_SIGNED )) && k=(-k)
            expect 200 "https://$DOMAIN/healthz" --resolve "$DOMAIN:443:127.0.0.1" ${k[@]+"${k[@]}"}
            expect 200 "https://$DOMAIN/api/v1/openapi.json" --resolve "$DOMAIN:443:127.0.0.1" ${k[@]+"${k[@]}"}
            if [[ $ORIGIN_GATE == 1 ]]; then
                expect 404 "https://$DOMAIN/admin/login/" --resolve "$DOMAIN:443:127.0.0.1" ${k[@]+"${k[@]}"}
                expect 404 "https://$DOMAIN/studio/login/" --resolve "$DOMAIN:443:127.0.0.1" ${k[@]+"${k[@]}"}
            fi
            expect 401 "https://$DOMAIN/mcp" --resolve "$DOMAIN:443:127.0.0.1" ${k[@]+"${k[@]}"} -X POST -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"initialize"}'
        fi
    fi
    if command -v ss >/dev/null; then
        listeners=$(ss -ltnH 'sport = :8000' | awk '{print $4}' | sort -u | tr '\n' ' ')
        if [[ $listeners == "127.0.0.1:8000 " ]]; then log "  ok   gunicorn published on 127.0.0.1:8000 only"; else log "  FAIL port 8000 listeners: $listeners"; failures=$((failures + 1)); fi
    fi
    (( failures == 0 )) || die "$failures verification check(s) failed"
fi

CURRENT_STEP="done"
log ""
if (( DRY_RUN )); then
    log "Dry run complete. Nothing was changed."
else
    log "Bootstrap complete for https://$DOMAIN (release $VERSION, image $IMAGE)."
    log "Next: Cloudflare (deploy/production/cloudflare.md), then deploy/production/verify.sh from your workstation."
    log "Secret files (values not shown): $SECRETS_DIR/integration-master-key (copy off-host NOW), admin-password, proxy-api-key"
fi

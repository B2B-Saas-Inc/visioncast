#!/usr/bin/env bash
# VisionCast public demo: native Ubuntu 24.04 + systemd deployment on the dedicated,
# owner-approved droplet "visioncast-demo" (64.225.16.137). See deploy/demo/README.md.
#
#   deploy.sh                      dry run (default): verify identity, report, change nothing
#   deploy.sh --apply              make the changes
#   deploy.sh --apply --installer pip
#                                  build the venv with python3-venv + pip instead of uv
#
# Options:
#   --apply              perform changes (otherwise read-only)
#   --installer uv|pip   venv tool (default uv; uv must already be at --uv PATH)
#   --uv PATH            uv binary (default /usr/local/bin/uv)
#
# Secrets are generated on the host into root-only files and are never printed:
#   /etc/visioncast/visioncast.env            Django/DB/Fernet secrets
#   /root/visioncast-secrets/admin-password   first Administrator password
#   /root/visioncast-secrets/proxy-api-key    proxy-scoped API key for Vercel
#
# Every step is idempotent; re-running after a failure continues where it stopped.
# A failed run leaves the droplet running and billable (see README "Rollback").
set -Eeuo pipefail
umask 022

# --- Deployment constants (verified against droplet metadata before any change) ------
EXPECTED_DROPLET_ID=612305850
EXPECTED_HOSTNAME=visioncast-demo
EXPECTED_IPV4=64.225.16.137
PUBLIC_HOST=64-225-16-137.sslip.io
OWNER_PORT=8081

SVC_USER=visioncast
BASE=/srv/visioncast
APP=$BASE/app
VENV=$BASE/venv
STATIC=$BASE/static
DEPLOY=$BASE/deploy
ENV_DIR=/etc/visioncast
ENV_FILE=$ENV_DIR/visioncast.env
SECRETS_DIR=/root/visioncast-secrets
ADMIN_SECRET=$SECRETS_DIR/admin-password
PROXY_SECRET=$SECRETS_DIR/proxy-api-key
ADMIN_USERNAME=demo-owner
ADMIN_EMAIL=owner@visioncast-demo.invalid
PROXY_KEY_NAME=visioncast-vercel
ACME_ROOT=/var/www/visioncast-acme
CERT_DIR=/etc/letsencrypt/live/$PUBLIC_HOST
MANAGE=/usr/local/sbin/visioncast-manage
APT_PACKAGES=(postgresql python3-venv nginx certbot)
SWAPFILE=/swapfile

SRC_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

APPLY=0
INSTALLER=uv
UV=/usr/local/bin/uv
CURRENT_STEP=start

usage() { sed -n '2,21p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
    case $1 in
        --apply) APPLY=1 ;;
        --installer|--uv)
            [[ $# -ge 2 ]] || { echo "$1 needs a value" >&2; exit 2; }
            if [[ $1 == --installer ]]; then INSTALLER=$2; else UV=$2; fi
            shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done
[[ $INSTALLER == uv || $INSTALLER == pip ]] || { echo "--installer must be uv or pip" >&2; exit 2; }

# --- Helpers ---------------------------------------------------------------------------
log()  { printf '%s\n' "$*"; }
step() { CURRENT_STEP=$1; printf '\n== %s\n' "$1"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
# Arguments are echoed: never pass a secret to run().
run() {
    if (( APPLY )); then
        log "  + $*"
        "$@"
    else
        log "  would run: $*"
    fi
}
on_error() {
    printf '\nFAILED during step "%s" (line %s). Nothing after this point ran.\n' "$CURRENT_STEP" "$1" >&2
    printf 'The droplet keeps running and is billed until the owner destroys it.\n' >&2
    printf 'Fix the cause and re-run, or stop services per deploy/demo/README.md "Rollback".\n' >&2
}
trap 'on_error $LINENO' ERR

# install_file SRC DEST MODE: copy as root:root when the content or mode differs.
CHANGED_UNITS=0
install_file() {
    local src=$1 dest=$2 mode=$3
    [[ -f $src ]] || die "missing source file $src"
    if [[ -f $dest ]] && cmp -s "$src" "$dest" && [[ $(stat -c '%a %U %G' "$dest") == "${mode#0} root root" ]]; then
        log "  unchanged: $dest"
        return 0
    fi
    [[ $dest == /etc/systemd/system/* ]] && CHANGED_UNITS=1
    run install -D -o root -g root -m "$mode" "$src" "$dest"
}

http_code() {  # http_code URL [curl args...] -> status code (000 on failure)
    local url=$1; shift
    curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$@" "$url" || true
}

psql_q() {  # read-only query as the postgres superuser over the local socket
    runuser -u postgres -- psql -X -q -v ON_ERROR_STOP=1 -tAc "$1"
}

cd /
if (( APPLY )); then log "MODE: APPLY (changes will be made)"; else log "MODE: DRY RUN (no changes; pass --apply to deploy)"; fi

# --- 1. Identity: refuse to touch any host but the approved droplet ----------------------
step "1/14 verify host identity"
[[ $EUID -eq 0 ]] || die "run as root"
os=$(. /etc/os-release && printf '%s %s' "$ID" "$VERSION_ID")
[[ $os == "ubuntu 24.04" ]] || die "expected Ubuntu 24.04, found: $os"
command -v curl >/dev/null || die "curl not found"
md() { curl -fsS --connect-timeout 3 --max-time 5 "http://169.254.169.254/metadata/v1/$1"; }
md_id=$(md id) || die "DigitalOcean metadata service unreachable; not the expected droplet"
md_host=$(md hostname) || die "metadata hostname unavailable"
md_ipv4=$(md interfaces/public/0/ipv4/address) || die "metadata public IPv4 unavailable"
[[ $md_id == "$EXPECTED_DROPLET_ID" ]] || die "droplet ID mismatch: metadata says $md_id, expected $EXPECTED_DROPLET_ID"
[[ $md_host == "$EXPECTED_HOSTNAME" ]] || die "metadata hostname is '$md_host', expected '$EXPECTED_HOSTNAME'"
[[ $(hostname) == "$EXPECTED_HOSTNAME" ]] || die "local hostname is '$(hostname)', expected '$EXPECTED_HOSTNAME'"
[[ $md_ipv4 == "$EXPECTED_IPV4" ]] || die "public IPv4 is '$md_ipv4', expected '$EXPECTED_IPV4'"
[[ $PUBLIC_HOST == "${EXPECTED_IPV4//./-}.sslip.io" ]] || die "PUBLIC_HOST does not encode EXPECTED_IPV4"
log "  droplet $md_id, hostname $md_host, IPv4 $md_ipv4: ok"

# --- 2. Uploaded source and deploy bundle ------------------------------------------------
step "2/14 check uploaded source"
for f in manage.py config/settings.py config/wsgi.py requirements.txt \
         webinars/management/commands/seed_public_demo.py webinars/api/auth.py; do
    [[ -f $APP/$f ]] || die "missing $APP/$f (upload the source first)"
done
grep -q -- '--hash=sha256:' "$APP/requirements.txt" || die "$APP/requirements.txt is not hash-locked"
for f in gunicorn.conf.py bootstrap_demo.py write_env.py visioncast-manage certbot-deploy-hook.sh \
         systemd/visioncast-web.service systemd/visioncast-migrate.service systemd/visioncast-collectstatic.service \
         nginx/visioncast.conf nginx/visioncast-tls.conf nginx/visioncast-proxy.conf; do
    [[ -f $SRC_DIR/$f ]] || die "deploy bundle incomplete: $SRC_DIR/$f"
done
# Symlinks are only acceptable when they stay inside the app tree.
while IFS= read -r -d '' link; do
    target=$(realpath -m -- "$link")
    [[ $target == "$APP"/* ]] || die "symlink leaves $APP: $link"
done < <(find "$APP" -type l -print0)
/usr/bin/python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' || die "python3 >= 3.12 required"
log "  source ok: $APP"

# --- 3. Packages ---------------------------------------------------------------------------
step "3/14 apt packages: ${APT_PACKAGES[*]}"
missing=()
for p in "${APT_PACKAGES[@]}"; do
    dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q 'install ok installed' || missing+=("$p")
done
if (( ${#missing[@]} )); then
    run timeout 600 apt-get update
    run env DEBIAN_FRONTEND=noninteractive timeout 900 apt-get install -y --no-install-recommends "${missing[@]}"
else
    log "  all installed"
fi

# --- 4. Swap (2 GB, only when the host has none) --------------------------------------------
step "4/14 swap"
if [[ -n $(swapon --show --noheadings) ]]; then
    log "  swap already active; leaving it unchanged"
elif [[ -e $SWAPFILE ]]; then
    die "$SWAPFILE exists but is not active swap; inspect it manually"
else
    avail_mb=$(df --output=avail -B1M / | tail -n 1 | tr -d ' ')
    (( avail_mb > 4096 )) || die "less than 4 GB free on /; not creating swap"
    run fallocate -l 2G "$SWAPFILE"
    run chmod 600 "$SWAPFILE"
    run mkswap "$SWAPFILE"
    run swapon "$SWAPFILE"
    if ! grep -qE "^$SWAPFILE[[:space:]]" /etc/fstab; then
        if (( APPLY )); then printf '%s none swap sw 0 0\n' "$SWAPFILE" >> /etc/fstab; log "  + added $SWAPFILE to /etc/fstab"
        else log "  would append '$SWAPFILE none swap sw 0 0' to /etc/fstab"; fi
    fi
fi

# --- 5. Service user (no login, no sudo) -----------------------------------------------------
step "5/14 service user $SVC_USER"
if id -u "$SVC_USER" >/dev/null 2>&1; then
    groups=" $(id -nG "$SVC_USER") "
    for g in sudo admin root adm wheel; do
        [[ $groups != *" $g "* ]] || die "$SVC_USER is in privileged group '$g'"
    done
    log "  exists, unprivileged"
else
    run useradd --system --user-group --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin "$SVC_USER"
fi

# --- 6. Directories and file ownership ---------------------------------------------------------
step "6/14 directories"
run install -d -o root -g root -m 0755 "$BASE" "$DEPLOY" "$ACME_ROOT"
run install -d -o "$SVC_USER" -g "$SVC_USER" -m 0755 "$STATIC"
run install -d -o root -g root -m 0700 "$ENV_DIR" "$SECRETS_DIR"
# Code is root-owned and read-only to the service user.
run chown -R -h root:root "$APP"
run chmod -R go-w "$APP"
install_file "$SRC_DIR/gunicorn.conf.py" "$DEPLOY/gunicorn.conf.py" 0644
install_file "$SRC_DIR/bootstrap_demo.py" "$DEPLOY/bootstrap_demo.py" 0644
install_file "$SRC_DIR/visioncast-manage" "$MANAGE" 0700

# --- 7. Python virtualenv (hash-locked) ----------------------------------------------------------
step "7/14 virtualenv ($INSTALLER)"
REQ=$APP/requirements.txt
want=$(sha256sum "$REQ" | cut -d' ' -f1)
stamp=$VENV/.visioncast-requirements.sha256
if [[ -x $VENV/bin/python ]]; then
    [[ -f $stamp && $(cat "$stamp") == "$want" ]] \
        || die "$VENV exists but was built from a different requirements.txt. Stop visioncast-web, move $VENV aside and re-run."
    log "  up to date"
else
    if [[ $INSTALLER == uv ]]; then
        [[ -x $UV ]] || die "uv not found at $UV. Upload a checksum-verified uv binary there, pass --uv PATH, or use --installer pip."
        run "$UV" venv --python /usr/bin/python3 "$VENV"
        run timeout 900 "$UV" pip install --no-cache --python "$VENV/bin/python" \
            --require-hashes --only-binary :all: -r "$REQ"
    else
        run /usr/bin/python3 -m venv "$VENV"
        run timeout 900 "$VENV/bin/pip" install --no-cache-dir --disable-pip-version-check \
            --require-hashes --only-binary=:all: -r "$REQ"
    fi
    run "$VENV/bin/python" -c 'import django, gunicorn, psycopg, cryptography, whitenoise, PIL'
    if (( APPLY )); then printf '%s\n' "$want" > "$stamp"; fi
fi
run "$VENV/bin/python" -m compileall -q "$APP/config" "$APP/webinars"

# --- 8. PostgreSQL: local socket, peer authentication ---------------------------------------------
step "8/14 PostgreSQL role and database '$SVC_USER'"
if command -v psql >/dev/null; then
    run systemctl enable --now postgresql.service
    if systemctl is-active --quiet postgresql.service && role=$(psql_q "SELECT rolsuper FROM pg_roles WHERE rolname = '$SVC_USER'"); then
        if [[ -z $role ]]; then
            run runuser -u postgres -- createuser --no-superuser --no-createdb --no-createrole "$SVC_USER"
        elif [[ $role != f ]]; then
            die "PostgreSQL role $SVC_USER is a superuser; refusing"
        else
            log "  role exists (not superuser)"
        fi
        owner=$(psql_q "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = '$SVC_USER'")
        if [[ -z $owner ]]; then
            run runuser -u postgres -- createdb --owner="$SVC_USER" "$SVC_USER"
        elif [[ $owner != "$SVC_USER" ]]; then
            die "database $SVC_USER is owned by '$owner'"
        else
            log "  database exists, owned by $SVC_USER"
        fi
    else
        (( ! APPLY )) || die "PostgreSQL is not running or cannot be queried as postgres"
        log "  PostgreSQL not running yet; would create role and database $SVC_USER"
    fi
else
    (( ! APPLY )) || die "psql missing after package install"
    log "  PostgreSQL not installed yet; would create role and database $SVC_USER"
fi

# --- 9. Environment file (secrets generated on the host, never printed) ----------------------------
step "9/14 environment file $ENV_FILE"
if [[ -f $ENV_FILE ]]; then
    /usr/bin/python3 -I "$SRC_DIR/write_env.py" check --path "$ENV_FILE" --host "$PUBLIC_HOST"
else
    run /usr/bin/python3 -I "$SRC_DIR/write_env.py" create --path "$ENV_FILE" --host "$PUBLIC_HOST"
fi

# --- 10. systemd units -------------------------------------------------------------------------------
step "10/14 systemd units"
for unit in visioncast-web.service visioncast-migrate.service visioncast-collectstatic.service; do
    install_file "$SRC_DIR/systemd/$unit" "/etc/systemd/system/$unit" 0644
done
if (( CHANGED_UNITS )); then run systemctl daemon-reload; fi

# --- 11. Django: migrate, static, checks, admin, proxy key, demo seed ---------------------------------
step "11/14 Django: migrate, collectstatic, bootstrap, seed"
if (( APPLY )); then
    run systemctl start visioncast-migrate.service
    run systemctl start visioncast-collectstatic.service
    run "$MANAGE" check --deploy
    run "$MANAGE" --bootstrap preflight

    # create_secret FILE CMD...: CMD writes one secret to stdout, which goes straight into
    # a new 0600 file (noclobber). The value is never shown; the file is removed on failure.
    create_secret() {
        local file=$1; shift
        if [[ -s $file ]]; then log "  exists, unchanged: $file"; return 0; fi
        log "  + $* > $file"
        if ! ( umask 077; set -o noclobber; "$@" > "$file" ); then
            rm -f -- "$file"
            die "could not create $file"
        fi
        [[ -s $file ]] || { rm -f -- "$file"; die "$file is empty"; }
        chmod 600 "$file"
        log "  saved (value not shown): $file"
    }
    create_secret "$ADMIN_SECRET" "$MANAGE" --bootstrap create-admin --username "$ADMIN_USERNAME" --email "$ADMIN_EMAIL"
    create_secret "$PROXY_SECRET" "$MANAGE" --bootstrap create-proxy-key --name "$PROXY_KEY_NAME"

    run "$MANAGE" seed_public_demo --dry-run --allow-registration
    run "$MANAGE" seed_public_demo --allow-registration
elif [[ -x $MANAGE && -f $ENV_FILE && -x $VENV/bin/python ]] && systemctl is-active --quiet postgresql.service; then
    log "  installed: read-only checks"
    "$MANAGE" check --deploy
    # --plan makes migrate print and return without migrating or emitting signals.
    if "$MANAGE" migrate --plan --check >/dev/null 2>&1; then
        "$MANAGE" seed_public_demo --dry-run --allow-registration
    else
        log "  migrations pending: would run visioncast-migrate.service, then seed"
    fi
else
    log "  would run: systemctl start visioncast-migrate.service visioncast-collectstatic.service"
    log "  would run: $MANAGE check --deploy; $MANAGE --bootstrap preflight"
    log "  would create (values never shown): $ADMIN_SECRET, $PROXY_SECRET"
    log "  would run: $MANAGE seed_public_demo --dry-run --allow-registration, then without --dry-run"
fi

# --- 12. Web service ------------------------------------------------------------------------------------
step "12/14 visioncast-web.service"
run systemctl enable visioncast-web.service
run systemctl restart visioncast-web.service
if (( APPLY )); then
    healthy=0
    for _ in $(seq 1 30); do
        code=$(http_code http://127.0.0.1:8000/healthz -H "Host: $PUBLIC_HOST" -H "X-Forwarded-Proto: https")
        if [[ $code == 200 ]]; then healthy=1; break; fi
        sleep 1
    done
    (( healthy )) || die "web health check failed (last status $code); see: journalctl -u visioncast-web -n 100"
    log "  loopback /healthz: 200"
fi

# --- 13. nginx + TLS -----------------------------------------------------------------------------------
step "13/14 nginx and TLS for $PUBLIC_HOST"
if [[ -L /etc/nginx/sites-enabled/default ]]; then
    run rm -f /etc/nginx/sites-enabled/default   # symlink only; sites-available/default is kept
fi
install_file "$SRC_DIR/nginx/visioncast-proxy.conf" /etc/nginx/snippets/visioncast-proxy.conf 0644
install_file "$SRC_DIR/nginx/visioncast.conf" /etc/nginx/conf.d/00-visioncast.conf 0644
run nginx -t
run systemctl enable nginx.service
run systemctl reload-or-restart nginx.service
if [[ -f $CERT_DIR/fullchain.pem ]]; then
    log "  certificate present: $CERT_DIR"
else
    resolved=$(getent ahostsv4 "$PUBLIC_HOST" | awk 'NR==1 {print $1}' || true)
    if [[ $resolved != "$EXPECTED_IPV4" ]]; then
        (( ! APPLY )) || die "$PUBLIC_HOST resolves to '$resolved', expected $EXPECTED_IPV4"
        log "  WARNING: $PUBLIC_HOST resolves to '$resolved', expected $EXPECTED_IPV4; --apply would stop here"
    fi
    # No account email: --register-unsafely-without-email. Bounded by timeout.
    run timeout 300 certbot certonly --webroot -w "$ACME_ROOT" -d "$PUBLIC_HOST" \
        --non-interactive --agree-tos --register-unsafely-without-email
fi
install_file "$SRC_DIR/certbot-deploy-hook.sh" /etc/letsencrypt/renewal-hooks/deploy/visioncast-nginx-reload 0755
install_file "$SRC_DIR/nginx/visioncast-tls.conf" /etc/nginx/conf.d/visioncast-tls.conf 0644
run nginx -t
run systemctl reload nginx.service

# --- 14. Verify ------------------------------------------------------------------------------------------
step "14/14 verify"
if (( APPLY )); then
    failures=0
    expect() {  # expect CODE URL [curl args...]
        local want=$1 url=$2; shift 2
        local got; got=$(http_code "$url" "$@")
        if [[ $got == "$want" ]]; then log "  ok   $want $url"; else log "  FAIL $got (want $want) $url"; failures=$((failures + 1)); fi
    }
    pub=(--resolve "$PUBLIC_HOST:443:127.0.0.1")
    expect 200 "https://$PUBLIC_HOST/healthz" "${pub[@]}"
    expect 200 "https://$PUBLIC_HOST/api/v1/events?when=upcoming" "${pub[@]}"
    for path in / /studio/ /studio/login/ /admin/ /admin/login/ /mcp /api/v1/manage/events /developers/ /webinar-api/profile /w/demo/; do
        expect 404 "https://$PUBLIC_HOST$path" "${pub[@]}"
    done
    expect 301 "http://$PUBLIC_HOST/healthz" --resolve "$PUBLIC_HOST:80:127.0.0.1"
    expect 200 "http://localhost:$OWNER_PORT/studio/login/" --resolve "localhost:$OWNER_PORT:127.0.0.1"
    listeners=$(ss -ltnH 'sport = :8000' | awk '{print $4}' | sort -u | tr '\n' ' ')
    if [[ $listeners == "127.0.0.1:8000 " ]]; then log "  ok   gunicorn listens on 127.0.0.1:8000 only"
    else log "  FAIL gunicorn listeners: $listeners"; failures=$((failures + 1)); fi
    if systemctl list-unit-files 'visioncast-worker*' --no-legend | grep -q .; then
        log "  FAIL a worker unit exists"; failures=$((failures + 1))
    fi
    (( failures == 0 )) || die "$failures verification check(s) failed"
fi

CURRENT_STEP=done
log ""
if (( APPLY )); then
    log "Deployed. Backend: https://$PUBLIC_HOST"
    log "Root-only secret files (values not shown):"
    log "  $ENV_FILE"
    log "  $ADMIN_SECRET   (Administrator '$ADMIN_USERNAME', owner tunnel: http://localhost:$OWNER_PORT/studio/)"
    log "  $PROXY_SECRET   (Vercel env VISIONCAST_PROXY_KEY)"
    log "Vercel env VISIONCAST_API_BASE=https://$PUBLIC_HOST"
else
    log "Dry run complete. Nothing was changed. Re-run with --apply to deploy."
fi

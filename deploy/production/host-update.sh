#!/usr/bin/env bash
# Host side of an update (sent by deploy/production/update.sh over ssh; run as root):
# unpack the uploaded release next to the live one, carry .env forward, build or pull the
# image, run deploy/upgrade.sh (backup -> migrate -> restart -> sync_roles -> health) from
# the NEW tree, then point `current` at it. Nothing in the old tree is deleted.
#
#   host-update.sh [--image REF] [--dry-run] [--skip-backup]
#
# Rollback (no schema change): ln -sfn /srv/visioncast/app/<previous> /srv/visioncast/app/current
#   && cd /srv/visioncast/app/current && docker compose up -d web worker
# (the previous tree's .env still pins the previous image). Schema changes: docs/operations.md §5.
set -Eeuo pipefail
umask 022

BASE=${BASE:-/srv/visioncast}
SECRETS_DIR=${SECRETS_DIR:-}
RELEASE_TAR=${RELEASE_TAR:-$BASE/release.tar}
IMAGE=${WEBINAR_IMAGE:-}
GHCR_USERNAME=${GHCR_USERNAME:-visioncast-deploy}
# Host steps re-run from the new tree after the switch (rehearsals override this).
POST_STEPS=${POST_STEPS:-preflight,cloudflare,nginx,cron,status,verify}
DRY_RUN=0
SKIP_BACKUP=()
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '%s\n' "$*"; }
run() { if (( DRY_RUN )); then log "  would run: $*"; else log "  + $*"; "$@"; fi; }
sha256() { if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }
# set_conf KEY VALUE: replace or append in bootstrap.conf (portable, keeps mode 0600).
set_conf() {
    local tmp; tmp=$(mktemp "$BASE/.conf.XXXXXX")
    if grep -q "^$1=" "$CONF"; then sed "s|^$1=.*|$1=$2|" "$CONF" > "$tmp"; else cat "$CONF" > "$tmp"; printf '%s=%s\n' "$1" "$2" >> "$tmp"; fi
    chmod 600 "$tmp" && mv "$tmp" "$CONF"
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --image) IMAGE=$2; shift ;;
        --dry-run) DRY_RUN=1 ;;
        --skip-backup) SKIP_BACKUP=(--skip-backup) ;;
        -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
    shift
done
if [[ $BASE == /srv/visioncast ]]; then
    [[ $EUID -eq 0 ]] || die "run as root"
    SECRETS_DIR=${SECRETS_DIR:-/root/visioncast-secrets}
else
    SECRETS_DIR=${SECRETS_DIR:-$BASE/secrets}     # rehearsal layout (bootstrap.sh --base)
fi
CONF=$BASE/bootstrap.conf
[[ -r $CONF ]] || die "$CONF missing: this host was not bootstrapped"
if [[ -z $IMAGE ]]; then IMAGE=$(sed -n 's/^WEBINAR_IMAGE=//p' "$CONF" | tail -n 1); fi
[[ -L $BASE/app/current ]] || die "$BASE/app/current missing"
OLD_DIR=$(cd "$BASE/app/current" && pwd -P)
OLD_VERSION=$(tr -d '[:space:]' < "$OLD_DIR/VERSION")
[[ -f $RELEASE_TAR ]] || die "$RELEASE_TAR not found"
VERSION=$(tar -xOf "$RELEASE_TAR" VERSION | tr -d '[:space:]')
SHA=$(sha256 "$RELEASE_TAR")
if [[ -f $RELEASE_TAR.sha256 ]]; then
    [[ $SHA == "$(cut -d' ' -f1 < "$RELEASE_TAR.sha256")" ]] || die "SHA-256 mismatch for $RELEASE_TAR"
fi
NEW_DIR=$BASE/app/$VERSION
log "== update $OLD_VERSION ($OLD_DIR) -> $VERSION ($NEW_DIR), tarball sha256 $SHA"
if (( ! DRY_RUN )) && command -v flock >/dev/null; then
    exec 9>"$BASE/.update.lock"
    flock -n 9 || die "another update is running"
fi

log "== unpack"
if [[ -f $NEW_DIR/.release-sha256 && $(cat "$NEW_DIR/.release-sha256") == "$SHA" ]]; then
    log "  already unpacked (same checksum): $NEW_DIR"
elif (( DRY_RUN )); then
    log "  would unpack into $NEW_DIR and copy $OLD_DIR/.env forward"
else
    tmp="$NEW_DIR.tmp.$$"; rm -rf "$tmp"; install -d -m 0755 "$tmp"
    tar --no-same-owner -xf "$RELEASE_TAR" -C "$tmp"
    [[ -f $tmp/compose.yaml && -x $tmp/deploy/upgrade.sh && -f $tmp/deploy/production/bootstrap.sh ]] || die "tarball incomplete"
    printf '%s\n' "$SHA" > "$tmp/.release-sha256"
    chmod -R go-w "$tmp"
    if [[ -d $NEW_DIR ]]; then mv "$NEW_DIR" "$NEW_DIR.replaced-$(date -u +%Y%m%dT%H%M%SZ)"; fi
    mv "$tmp" "$NEW_DIR"
    log "  + unpacked $NEW_DIR"
fi
if [[ $NEW_DIR != "$OLD_DIR" && ! -f $NEW_DIR/.env ]]; then
    run cp -p "$OLD_DIR/.env" "$NEW_DIR/.env"
fi
chmod 0755 "$NEW_DIR/deploy/production/"*.sh 2>/dev/null || true

log "== image $IMAGE"
if [[ -z $IMAGE ]]; then
    IMAGE="webinar-platform:$VERSION"
    if docker image inspect "$IMAGE" >/dev/null 2>&1; then log "  present: $IMAGE"
    else ( cd "$NEW_DIR" && run deploy/build-image.sh --allow-dirty ); fi
elif [[ $IMAGE == ghcr.io/* ]]; then
    if docker image inspect "$IMAGE" >/dev/null 2>&1; then log "  present: $IMAGE"
    else
        [[ -s $SECRETS_DIR/ghcr-token ]] || die "$SECRETS_DIR/ghcr-token missing"
        log "  + docker login ghcr.io -u $GHCR_USERNAME --password-stdin < $SECRETS_DIR/ghcr-token"
        (( DRY_RUN )) || docker login ghcr.io -u "$GHCR_USERNAME" --password-stdin < "$SECRETS_DIR/ghcr-token" >/dev/null
        run docker pull "$IMAGE"
        run docker logout ghcr.io
    fi
else
    docker image inspect "$IMAGE" >/dev/null 2>&1 || die "image $IMAGE not present locally"
fi
[[ $IMAGE != *:latest ]] || die "refusing an unpinned image tag"

log "== deploy/upgrade.sh from $NEW_DIR (backup first, then migrate, restart, sync_roles, health)"
if (( DRY_RUN )); then
    log "  would run: cd $NEW_DIR && deploy/upgrade.sh --image $IMAGE --backup-dir $BASE/backups ${SKIP_BACKUP[*]:-}"
    log "  would run: ln -sfn $NEW_DIR $BASE/app/current; manage check; bootstrap.sh --steps $POST_STEPS"
    log "Dry run complete; nothing changed."
    exit 0
fi
( cd "$NEW_DIR" && deploy/upgrade.sh --image "$IMAGE" --backup-dir "$BASE/backups" ${SKIP_BACKUP[@]+"${SKIP_BACKUP[@]}"} )
( cd "$NEW_DIR" && docker compose run --rm -T web manage check )
ln -sfn "$NEW_DIR" "$BASE/app/current"
log "  current -> $NEW_DIR"
# Refresh host files that live in the release tree (templates, cron paths, STATUS); no secrets touched.
set_conf WEBINAR_IMAGE "$IMAGE"
bash "$NEW_DIR/deploy/production/bootstrap.sh" --base "$BASE" --secrets-dir "$SECRETS_DIR" --steps "$POST_STEPS"
log ""
log "Update complete: $OLD_VERSION -> $VERSION on $IMAGE. Previous tree kept at $OLD_DIR."
log "Rollback (no schema change): ln -sfn $OLD_DIR $BASE/app/current && cd $OLD_DIR && docker compose up -d web worker"

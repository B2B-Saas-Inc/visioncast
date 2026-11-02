#!/usr/bin/env bash
# Updates this live server to a release tag. Run it on the server, from the checkout:
#   deploy/live/update.sh v0.4.5-pre
# Keeps deploy/live/.env, the data volumes and the captions model. Takes a backup first when
# backups are set up (backup.sh), rebuilds the images from the tag, restarts what changed, waits
# for the service to answer, and prints how to go back.
set -euo pipefail

# Everything runs from main(): the checkout below replaces this file while it runs.
main() {
    local tag=${1:-}
    [[ $tag =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$ ]] || die "usage: deploy/live/update.sh <release tag, e.g. v0.4.5-pre>"
    local here repo previous host
    here=$(cd "$(dirname "$0")" && pwd)
    repo=$(cd "$here/../.." && pwd)
    cd "$repo"
    [[ -f deploy/live/.env ]] || die "deploy/live/.env is missing: run deploy/live/setup.sh first"
    [[ -z $(git status --porcelain --untracked-files=no) ]] || die "the checkout has local changes; this script only moves between release tags"
    previous=$(git describe --tags --exact-match 2>/dev/null || git rev-parse --short HEAD)
    host=$(sed -n 's/^LIVE_HOST=//p' deploy/live/.env)

    if [[ -x deploy/live/backup.sh ]] && grep -q '^LIVE_BACKUP_AGE_RECIPIENT=age1' deploy/live/.env; then
        log "backup before the update"
        deploy/live/backup.sh
    else
        log "no backup taken (backups aren't set up: see deploy/live/backup.sh)"
    fi

    log "fetching $tag (now on $previous)"
    git fetch --quiet --depth 1 origin tag "$tag"
    git -c advice.detachedHead=false checkout --quiet "$tag"

    cd deploy/live
    log "building images"
    docker compose build --quiet
    docker compose pull --ignore-buildable --quiet
    log "restarting what changed"
    docker compose up -d --remove-orphans

    log "waiting for https://$host/healthz"
    local i
    for i in $(seq 1 30); do
        if curl -fsS --max-time 5 --resolve "$host:443:127.0.0.1" "https://$host/healthz" >/dev/null 2>&1; then
            log "updated: $previous -> $tag"
            log "to go back: deploy/live/update.sh $previous"
            return 0
        fi
        sleep 2
    done
    docker compose ps
    die "the service didn't answer within a minute. To go back: deploy/live/update.sh $previous"
}

log() { printf '%s %s\n' "$(date -u +%H:%M:%S)" "$*"; }
die() { printf 'update.sh: %s\n' "$*" >&2; exit 1; }

main "$@"

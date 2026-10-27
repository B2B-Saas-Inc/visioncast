#!/usr/bin/env bash
# In-place upgrade of a Compose deployment: backup -> image -> migrate -> restart -> sync_roles -> health.
#
#   deploy/upgrade.sh --image webinar-platform:0.2.0        use an image built by deploy/build-image.sh
#   deploy/upgrade.sh --image ghcr.io/<owner>/<repo>:0.2.0  use a pulled image (docker pull it first)
#   deploy/upgrade.sh                                       rebuild webinar-platform:local from this tree
#   deploy/upgrade.sh --skip-backup ...                     only when a backup was just taken by other means
#
# Options:
#   --image REF       image to deploy; must already exist locally (never pulled or built here)
#   --backup-dir DIR  where deploy/backup.sh writes the pre-upgrade dump (default ./backups)
#   --skip-backup     do not take a backup first (not recommended)
#
# The chosen image is exported as WEBINAR_IMAGE for every docker compose call, so the
# `migrate`, `web` and `worker` services all run the same version. Put the same value
# in .env afterwards so later `docker compose` commands keep using it (the script
# prints the line). Rollback: docs/operations.md section 5.
set -Eeuo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

IMAGE=""
BACKUP_DIR="$ROOT/backups"
BACKUP=1
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
usage() { sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
    case $1 in
        --image|--backup-dir)
            [[ $# -ge 2 ]] || die "$1 needs a value"
            if [[ $1 == --image ]]; then IMAGE=$2; else BACKUP_DIR=$2; fi
            shift ;;
        --skip-backup) BACKUP=0 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
    shift
done
command -v docker >/dev/null || die "docker not found"

PREVIOUS=$(docker compose ps --format '{{.Image}}' web 2>/dev/null | head -n 1 || true)
if [[ -n $IMAGE ]]; then
    docker image inspect "$IMAGE" >/dev/null 2>&1 || die "image $IMAGE is not present locally (deploy/build-image.sh or docker pull it first)"
    export WEBINAR_IMAGE=$IMAGE
    export WEBINAR_PULL_POLICY=never
    NEW_VERSION=$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.version"}}' "$IMAGE")
    printf '== target image %s (label version: %s)\n' "$IMAGE" "${NEW_VERSION:-unlabelled}"
else
    printf '== building webinar-platform:local from %s\n' "$ROOT"
    docker compose build migrate
    export WEBINAR_IMAGE=${WEBINAR_IMAGE:-webinar-platform:local}
fi
printf '   previously running: %s\n' "${PREVIOUS:-nothing}"

if (( BACKUP )); then
    printf '== backup before migrate\n'
    "$ROOT/deploy/backup.sh" "$BACKUP_DIR"
else
    printf '== backup skipped (--skip-backup)\n'
fi

printf '== migrate\n'
docker compose run --rm migrate

printf '== restart web and worker on %s\n' "$WEBINAR_IMAGE"
docker compose up -d web worker

printf '== sync_roles\n'
docker compose run --rm web manage sync_roles

printf '== wait for healthchecks\n'
docker compose up -d --wait web worker
docker compose ps web worker
docker compose exec -T web python manage.py show_version --verbose

printf '\nUpgrade complete. Keep this image selected for later compose commands by setting in .env:\n'
printf '  WEBINAR_IMAGE=%s\n' "$WEBINAR_IMAGE"
[[ -z $IMAGE ]] || printf '  WEBINAR_PULL_POLICY=never\n'
[[ -z $PREVIOUS ]] || printf 'Rollback (no schema change): WEBINAR_IMAGE=%s docker compose up -d web worker\n' "$PREVIOUS"

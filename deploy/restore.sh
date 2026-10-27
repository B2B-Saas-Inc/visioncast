#!/usr/bin/env bash
# Restore a deploy/backup.sh dump into the Compose database, migrate, start web/worker.
#
#   deploy/restore.sh backups/webinars-<ts>.dump          restore into an EMPTY database
#   deploy/restore.sh --force backups/webinars-<ts>.dump  drop and recreate a non-empty database first
#   deploy/restore.sh --no-start FILE                     restore + migrate, leave web/worker stopped
#
# Decrypt an age-encrypted dump first:  age -d -i <identity> -o restore.dump webinars-<ts>.dump.age
#
# Steps: stop web/worker -> ensure db is healthy -> refuse unless the target database
# is empty (or --force: drop + recreate) -> pg_restore --exit-on-error -> `migrate`
# with the image in WEBINAR_IMAGE (default webinar-platform:local) -> `up -d web worker`.
# Honours COMPOSE_PROJECT_NAME / COMPOSE_FILE / .env like every docker compose call.
#
# The .env in use must carry the INTEGRATION_MASTER_KEY that was current when the
# dump was taken (or list it in INTEGRATION_MASTER_KEY_PREVIOUS); without it the
# schema and people restore but destination secrets cannot be decrypted.
set -Eeuo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

FORCE=0
START=1
DUMP=""
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
usage() { sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
    case $1 in
        --force) FORCE=1 ;;
        --no-start) START=0 ;;
        -h|--help) usage; exit 0 ;;
        -*) die "unknown option: $1" ;;
        *) [[ -z $DUMP ]] || die "only one dump file"; DUMP=$1 ;;
    esac
    shift
done
[[ -n $DUMP ]] || { usage >&2; exit 2; }
[[ -r $DUMP ]] || die "cannot read $DUMP"
[[ $(head -c 5 "$DUMP") == "PGDMP" ]] || die "$DUMP is not a custom-format pg_dump archive (encrypted? see --help)"
command -v docker >/dev/null || die "docker not found"

printf '== stop web and worker\n'
docker compose stop web worker >/dev/null 2>&1 || true

printf '== start db and wait until healthy\n'
docker compose up -d --wait db

printf '== inspect target database\n'
# All SQL runs inside the db container with its own POSTGRES_* values; nothing is echoed.
EXISTS=$(docker compose exec -T db sh -c 'exec psql -X -tA -U "$POSTGRES_USER" -d postgres -c "SELECT count(*) FROM pg_database WHERE datname = '"'"'$POSTGRES_DB'"'"'"')
TABLES=0
if [[ $EXISTS == 1 ]]; then
    TABLES=$(docker compose exec -T db sh -c 'exec psql -X -tA -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname NOT IN ('"'"'pg_catalog'"'"','"'"'information_schema'"'"')"')
fi
if [[ $EXISTS == 1 && $TABLES != 0 ]]; then
    (( FORCE )) || die "target database is not empty ($TABLES tables). Re-run with --force to drop and recreate it, or restore into a fresh volume."
    printf '== --force: dropping and recreating the database (%s tables)\n' "$TABLES"
    docker compose exec -T db sh -c 'psql -X -q -U "$POSTGRES_USER" -d postgres -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '"'"'$POSTGRES_DB'"'"' AND pid <> pg_backend_pid()" >/dev/null && dropdb -U "$POSTGRES_USER" "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'
elif [[ $EXISTS != 1 ]]; then
    printf '== creating empty database\n'
    docker compose exec -T db sh -c 'exec createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'
else
    printf '   database exists and is empty\n'
fi

printf '== pg_restore (exit on first error)\n'
docker compose exec -T db sh -c 'exec pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --exit-on-error' < "$DUMP"
MIGRATIONS=$(docker compose exec -T db sh -c 'exec psql -X -tA -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT count(*) FROM django_migrations"')
printf '   restored; %s migration rows present\n' "$MIGRATIONS"

printf '== migrate with image %s\n' "${WEBINAR_IMAGE:-webinar-platform:local}"
docker compose run --rm migrate

if (( START )); then
    printf '== start web and worker\n'
    docker compose up -d web worker
    printf 'Restore complete. Check: docker compose ps  (web/worker healthy), then the admin outbox.\n'
else
    printf 'Restore complete; web and worker left stopped (--no-start).\n'
fi
printf 'If erasure requests were processed after this backup was taken, re-run purge_personal_data for them.\n'

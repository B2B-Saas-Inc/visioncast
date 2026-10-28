#!/usr/bin/env bash
# Workstation side of an update: upload a release tarball (package-release.sh) to the host
# and run deploy/upgrade.sh there through host-update.sh (backup first). Also the way to
# upload the FIRST release before bootstrap.sh (--upload-only).
#
#   deploy/production/update.sh --host 203.0.113.10 --tarball dist/visioncast-0.3.1.tar --dry-run
#   deploy/production/update.sh --host 203.0.113.10 --tarball dist/visioncast-0.3.1.tar
#   deploy/production/update.sh --host ... --tarball ... --image ghcr.io/b2b-saas-inc/visioncast:0.3.1
#   deploy/production/update.sh --host ... --tarball ... --upload-only
#
# Options:
#   --host HOST        droplet address or ssh alias (required)
#   --user USER        ssh user (default root; a sudo user works: commands run via sudo)
#   --tarball PATH     release tarball; its .sha256 next to it is uploaded too and verified on the host
#   --image REF        image for the new release (default: what bootstrap.conf on the host pins;
#                      empty there = build from the uploaded source)
#   --dry-run          upload nothing; show what would happen on the host (host-update.sh --dry-run
#                      runs there only if the tarball is already uploaded)
#   --upload-only      upload the tarball and stop (first install, then run bootstrap.sh)
#   --skip-backup      pass through to deploy/upgrade.sh (not recommended)
#
# Nothing secret travels: the tarball is source only. On macOS the upload uses
# COPYFILE_DISABLE=1 and --no-xattrs so no AppleDouble ._* files are created.
set -Eeuo pipefail

SRC_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
HOST=""
USER_NAME=root
TARBALL=""
IMAGE=""
DRY_RUN=0
UPLOAD_ONLY=0
SKIP_BACKUP=""
REMOTE_TAR=/srv/visioncast/release.tar
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
usage() { sed -n '2,23p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
    case $1 in
        --host) HOST=$2; shift ;;
        --user) USER_NAME=$2; shift ;;
        --tarball) TARBALL=$2; shift ;;
        --image) IMAGE=$2; shift ;;
        --dry-run) DRY_RUN=1 ;;
        --upload-only) UPLOAD_ONLY=1 ;;
        --skip-backup) SKIP_BACKUP=--skip-backup ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
    shift
done
[[ -n $HOST ]] || die "--host is required"
[[ -n $TARBALL ]] || die "--tarball is required"
[[ -f $TARBALL ]] || die "tarball not found: $TARBALL"
[[ -f $TARBALL.sha256 ]] || die "$TARBALL.sha256 missing (package-release.sh writes it)"
[[ -z $IMAGE || $IMAGE == *:* ]] || die "--image must carry a tag"
[[ $IMAGE != *:latest ]] || die "never deploy :latest"
VERSION=$(tar -xOf "$TARBALL" VERSION | tr -d '[:space:]')
SHA=$(cut -d' ' -f1 < "$TARBALL.sha256")
if command -v sha256sum >/dev/null; then LOCAL=$(sha256sum "$TARBALL" | cut -d' ' -f1); else LOCAL=$(shasum -a 256 "$TARBALL" | cut -d' ' -f1); fi
[[ $SHA == "$LOCAL" ]] || die "local checksum mismatch: $TARBALL vs $TARBALL.sha256"

SSH=(ssh -o BatchMode=yes "$USER_NAME@$HOST")
SUDO=""; [[ $USER_NAME == root ]] || SUDO="sudo "
printf '== release %s (%s bytes, sha256 %s) -> %s@%s:%s\n' "$VERSION" "$(wc -c < "$TARBALL" | tr -d ' ')" "$SHA" "$USER_NAME" "$HOST" "$REMOTE_TAR"

if (( DRY_RUN )); then
    printf '== DRY RUN: would upload the tarball and its .sha256, then run on the host:\n'
    printf '   %sbash -s -- %s%s < %s/host-update.sh\n' "$SUDO" "${IMAGE:+--image $IMAGE }" "${SKIP_BACKUP:-}" "$SRC_DIR"
    printf '== asking the host for its own dry run (works only if a release is already uploaded there)\n'
    "${SSH[@]}" "${SUDO}bash -s -- --dry-run ${IMAGE:+--image $IMAGE}" < "$SRC_DIR/host-update.sh" || printf '   (host dry run not available yet)\n'
    exit 0
fi

printf '== upload\n'
# tar over ssh keeps modes and avoids scp quirks; COPYFILE_DISABLE/--no-xattrs stop macOS metadata.
( cd "$(dirname "$TARBALL")" && COPYFILE_DISABLE=1 tar --no-xattrs -cf - "$(basename "$TARBALL")" "$(basename "$TARBALL").sha256" 2>/dev/null \
    || tar -cf - "$(basename "$TARBALL")" "$(basename "$TARBALL").sha256" ) \
  | "${SSH[@]}" "${SUDO}sh -c 'umask 022 && mkdir -p /srv/visioncast && cd /srv/visioncast && tar -xf - && mv -f $(basename "$TARBALL") release.tar && mv -f $(basename "$TARBALL").sha256 release.tar.sha256 && sed -i \"s/ .*/  release.tar/\" release.tar.sha256 && sha256sum -c release.tar.sha256'"
printf '   uploaded and verified on the host\n'

if (( UPLOAD_ONLY )); then
    printf 'Upload only. Next on the host: bash /srv/visioncast/... (see README.md "First install").\n'
    exit 0
fi

printf '== host-update.sh on %s\n' "$HOST"
"${SSH[@]}" "${SUDO}bash -s -- ${IMAGE:+--image $IMAGE }${SKIP_BACKUP:-}" < "$SRC_DIR/host-update.sh"
printf '\nDone. Verify from here: deploy/production/verify.sh --domain <domain> --expect-version %s\n' "$VERSION"

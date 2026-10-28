#!/usr/bin/env bash
# Build the release source tarball for the production host from a git tag.
#
#   deploy/production/package-release.sh v0.3.0-pre            -> dist/visioncast-0.3.0-pre.tar (+ .sha256)
#   deploy/production/package-release.sh --ref main            any ref (branch, commit); the version is
#                                                              read from VERSION at that ref
#   deploy/production/package-release.sh v0.3.0 --out DIR      write into DIR
#
# `git archive` of an allowlist only (nothing untracked or ignored can enter): manage.py,
# VERSION, config/, webinars/, docker/, requirements*.txt, LICENSE, README.md, the docs
# allowlist from .dockerignore, compose.yaml, Dockerfile, .dockerignore, .env.example,
# deploy/backup.sh, deploy/restore.sh, deploy/upgrade.sh, deploy/build-image.sh and
# deploy/production/. A RELEASE manifest (version, ref, commit, date) is added at the
# root. Prints the SHA-256; bootstrap.sh verifies it from the .sha256 file.
#
# A tag whose tree predates deploy/production/ (for example v0.3.0-pre) cannot be
# packaged: the host needs these scripts. Tag a release that contains them first.
set -Eeuo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$ROOT"
REF=""
OUT_DIR="$ROOT/dist"
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
usage() { sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
    case $1 in
        --ref) REF=$2; shift ;;
        --out) OUT_DIR=$2; shift ;;
        -h|--help) usage; exit 0 ;;
        -*) die "unknown option: $1" ;;
        *) [[ -z $REF ]] || die "one ref only"; REF=$1 ;;
    esac
    shift
done
[[ -n $REF ]] || { usage >&2; exit 2; }
git rev-parse --verify --quiet "$REF^{commit}" >/dev/null || die "unknown ref: $REF"
COMMIT=$(git rev-parse "$REF^{commit}")
if git rev-parse --verify --quiet "refs/tags/$REF" >/dev/null; then KIND=tag; else KIND=ref; fi

VERSION=$(git show "$COMMIT:VERSION" | tr -d '[:space:]')
[[ $VERSION =~ ^[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?$ ]] || die "VERSION at $REF is not a semantic version: '$VERSION'"
if [[ $KIND == tag && $REF != "v$VERSION" ]]; then die "tag $REF does not match VERSION $VERSION (expected v$VERSION)"; fi

# Allowlist: required paths, then the docs re-included by .dockerignore.
REQUIRED=(manage.py VERSION config webinars docker requirements.txt requirements-dev.txt LICENSE README.md
          compose.yaml Dockerfile .dockerignore .env.example
          deploy/backup.sh deploy/restore.sh deploy/upgrade.sh deploy/build-image.sh deploy/production)
DOCS=()
while IFS= read -r line; do
    DOCS+=("${line#!}")
done < <(git show "$COMMIT:.dockerignore" | grep -E '^!docs/.*\.md$')
PATHS=()
for p in "${REQUIRED[@]}" ${DOCS[@]+"${DOCS[@]}"}; do
    git cat-file -e "$COMMIT:$p" 2>/dev/null || die "$p is missing at $REF (a tag before deploy/production/ existed cannot be packaged)"
    PATHS+=("$p")
done

mkdir -p "$OUT_DIR"
TAR="$OUT_DIR/visioncast-$VERSION.tar"
# --add-file (git >= 2.30) stores the file under its basename: RELEASE at the archive root.
REL_DIR=$(mktemp -d)
{
    printf 'version=%s\n' "$VERSION"
    printf 'ref=%s\n' "$REF"
    printf 'commit=%s\n' "$COMMIT"
    printf 'created=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'packaged_by=deploy/production/package-release.sh\n'
} > "$REL_DIR/RELEASE"
git archive --format=tar --add-file="$REL_DIR/RELEASE" -o "$TAR" "$COMMIT" -- "${PATHS[@]}"
rm -rf "$REL_DIR"

if command -v sha256sum >/dev/null; then SHA=$(sha256sum "$TAR" | cut -d' ' -f1); else SHA=$(shasum -a 256 "$TAR" | cut -d' ' -f1); fi
printf '%s  %s\n' "$SHA" "$(basename "$TAR")" > "$TAR.sha256"
COUNT=$(tar -tf "$TAR" | grep -vc '/$')
SIZE=$(wc -c < "$TAR" | tr -d ' ')
printf 'release:  %s (%s files, %s bytes)\n' "$TAR" "$COUNT" "$SIZE"
printf 'sha256:   %s  (%s)\n' "$SHA" "$TAR.sha256"
printf 'source:   %s %s @ %s\n' "$KIND" "$REF" "$COMMIT"
tar -tf "$TAR" | grep -qx 'RELEASE' || die "RELEASE manifest missing from the archive"
tar -tf "$TAR" | grep -qx 'deploy/production/bootstrap.sh' || die "deploy/production/bootstrap.sh missing from the archive"
if tar -tf "$TAR" | grep -qE '^\.env$|(^|/)\._|^(tests|sdk|site|preview|examples|\.git|\.github|\.claude)/'; then die "unexpected path inside the archive"; fi
printf 'upload:   deploy/production/update.sh --host <ip> --tarball %s   (first install: see README.md)\n' "$TAR"

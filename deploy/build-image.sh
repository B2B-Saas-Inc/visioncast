#!/usr/bin/env bash
# Build the production image with version tags and OCI labels.
#
#   deploy/build-image.sh                 build webinar-platform:<version> and :<version>-<commit>
#   deploy/build-image.sh --save          also write artifacts/<name>-<version>.tar and its SHA-256
#   deploy/build-image.sh --name wdist-webinar-platform
#                                         tag under another repository name (test runs)
#
# Options:
#   --name NAME         image repository (default webinar-platform; env IMAGE_NAME)
#   --platform P        e.g. linux/amd64 (default: the host platform; cross-builds use emulation)
#   --save              docker save the image to artifacts/ and record its SHA-256
#   --allow-dirty       build an uncommitted tree (revision is suffixed "-dirty")
#   --no-cache          pass --no-cache to docker build
#
# Inputs that make the build deterministic: the VERSION file, the commit, the
# digest-pinned base image (Dockerfile ARG PYTHON_IMAGE) and the hash-locked
# requirements.txt (pip verifies every hash while building). Docker image IDs are
# still not bit-for-bit reproducible across hosts (layer timestamps), so the
# evidence file records the ID of the image that was actually built and tested.
#
# Writes artifacts/image-<version>.sha256 (artifacts/ is gitignored) with the image
# ID, tags, labels and, with --save, the tarball checksum. Prints the same values.
# Never pushes. Publishing to a registry needs the owner's approval
# (.github/workflows/release.yml is prepared but no image has been published).
set -Eeuo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

NAME=${IMAGE_NAME:-webinar-platform}
PLATFORM=""
SAVE=0
ALLOW_DIRTY=0
NO_CACHE=()          # ${arr[@]+"${arr[@]}"} below: bash 3.2 (macOS) + set -u reject empty arrays

usage() { sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
    case $1 in
        --name|--platform)
            [[ $# -ge 2 ]] || die "$1 needs a value"
            if [[ $1 == --name ]]; then NAME=$2; else PLATFORM=$2; fi
            shift ;;
        --save) SAVE=1 ;;
        --allow-dirty) ALLOW_DIRTY=1 ;;
        --no-cache) NO_CACHE=(--no-cache) ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
    shift
done

command -v docker >/dev/null || die "docker not found"
[[ -f VERSION ]] || die "VERSION file missing"
VERSION=$(tr -d '[:space:]' < VERSION)
[[ $VERSION =~ ^[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?$ ]] || die "VERSION '$VERSION' is not a semantic version"

if git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    REVISION=$(git -C "$ROOT" rev-parse HEAD)
    SHORT=$(git -C "$ROOT" rev-parse --short=12 HEAD)
    if [[ -n $(git -C "$ROOT" status --porcelain --untracked-files=no) ]]; then
        (( ALLOW_DIRTY )) || die "working tree has uncommitted changes; commit them or pass --allow-dirty"
        REVISION="$REVISION-dirty"
        SHORT="$SHORT-dirty"
    fi
else
    (( ALLOW_DIRTY )) || die "not a git checkout; pass --allow-dirty to build without a revision"
    REVISION="unknown"
    SHORT="unknown"
fi
CREATED=$(date -u +%Y-%m-%dT%H:%M:%SZ)
SOURCE=$(git -C "$ROOT" remote get-url origin 2>/dev/null | sed -E 's#^git@github.com:#https://github.com/#; s#\.git$##' || true)
SOURCE=${SOURCE:-https://github.com/B2B-Saas-Inc/visioncast}

TAG_VERSION="$NAME:$VERSION"
TAG_REVISION="$NAME:$VERSION-$SHORT"
PLATFORM_ARGS=()
[[ -n $PLATFORM ]] && PLATFORM_ARGS=(--platform "$PLATFORM")

printf 'Building %s (%s) from %s\n' "$TAG_VERSION" "$TAG_REVISION" "$REVISION"
docker build ${NO_CACHE[@]+"${NO_CACHE[@]}"} ${PLATFORM_ARGS[@]+"${PLATFORM_ARGS[@]}"} \
    --target production \
    --build-arg "WEBINAR_VERSION=$VERSION" \
    --build-arg "WEBINAR_REVISION=$REVISION" \
    --build-arg "WEBINAR_CREATED=$CREATED" \
    --build-arg "WEBINAR_SOURCE=$SOURCE" \
    -t "$TAG_VERSION" -t "$TAG_REVISION" \
    "$ROOT"

IMAGE_ID=$(docker image inspect --format '{{.Id}}' "$TAG_VERSION")
IMAGE_ARCH=$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$TAG_VERSION")
LABEL_VERSION=$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.version"}}' "$TAG_VERSION")
LABEL_REVISION=$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$TAG_VERSION")
LABEL_CREATED=$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.created"}}' "$TAG_VERSION")
BASE_PIN=$(sed -n 's/^ARG PYTHON_IMAGE=//p' Dockerfile | head -n 1)
REPO_DIGESTS=$(docker image inspect --format '{{join .RepoDigests ","}}' "$TAG_VERSION")
[[ $LABEL_VERSION == "$VERSION" && $LABEL_REVISION == "$REVISION" ]] || die "label mismatch after build"

mkdir -p artifacts
EVIDENCE="artifacts/image-$VERSION.sha256"
{
    printf 'name=%s\n' "$NAME"
    printf 'version=%s\n' "$VERSION"
    printf 'revision=%s\n' "$REVISION"
    printf 'created=%s\n' "$CREATED"
    printf 'platform=%s\n' "$IMAGE_ARCH"
    printf 'tags=%s,%s\n' "$TAG_VERSION" "$TAG_REVISION"
    printf 'image_id=%s\n' "$IMAGE_ID"
    printf 'repo_digests=%s\n' "${REPO_DIGESTS:-none (not pushed)}"
    printf 'base_image=%s\n' "$BASE_PIN"
    printf 'label.created=%s\n' "$LABEL_CREATED"
} > "$EVIDENCE"

if (( SAVE )); then
    TAR="artifacts/$NAME-$VERSION.tar"
    docker save -o "$TAR" "$TAG_VERSION" "$TAG_REVISION"
    if command -v sha256sum >/dev/null; then TAR_SHA=$(sha256sum "$TAR" | cut -d' ' -f1)
    else TAR_SHA=$(shasum -a 256 "$TAR" | cut -d' ' -f1); fi
    printf 'tar=%s\ntar_sha256=%s\n' "$TAR" "$TAR_SHA" >> "$EVIDENCE"
fi

printf '\nImage evidence (%s):\n' "$EVIDENCE"
cat "$EVIDENCE"
printf '\nDeploy with:  WEBINAR_IMAGE=%s docker compose run --rm migrate && WEBINAR_IMAGE=%s docker compose up -d\n' "$TAG_VERSION" "$TAG_VERSION"
printf 'Not pushed. Publishing needs owner approval (see README "Publishing the image").\n'

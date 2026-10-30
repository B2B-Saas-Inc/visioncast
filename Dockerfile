# syntax=docker/dockerfile:1.7
#
# Single image for the web process and the durable worker.
#
# Base image pinned by digest (README "Supply chain notes" explains how to refresh):
#   docker buildx imagetools inspect python:3.12-slim-bookworm   # index digest below
# The digest is the multi-platform index, so the same pin resolves on linux/amd64
# and linux/arm64. Override with --build-arg PYTHON_IMAGE=... only to roll the pin.
#
# Build with deploy/build-image.sh: it passes the OCI label build args below
# (version from the VERSION file, git revision, creation time) and tags the image
# webinar-platform:<version> and webinar-platform:<version>-<git-sha>. A plain
# `docker compose build` works too but leaves the labels empty.
#
# The build context is restricted by .dockerignore (allow-list): only
# manage.py, VERSION, config/, webinars/, docker/, requirements*.txt, LICENSE and
# the allowlisted public guides (README.md, selected docs/*.md) are sent.

# python:3.12-slim-bookworm, resolved 2026-10-27 (image created 2026-10-26T22:46:30Z)
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:6dd8b6dab6b3adc7812bab69eca5dbdc1a5cc690ad28306d234ffb4ef8ac9753

# ---------------------------------------------------------------------------
# builder: build wheels for every dependency
# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build
COPY requirements.txt ./
RUN pip wheel --require-hashes --only-binary=:all: --wheel-dir /wheels -r requirements.txt

# ---------------------------------------------------------------------------
# runtime: installs only the prebuilt wheels, runs as uid 10001
# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

# OCI labels (https://github.com/opencontainers/image-spec/blob/main/annotations.md).
# Supplied by deploy/build-image.sh and .github/workflows/release.yml; empty otherwise.
ARG WEBINAR_VERSION=""
ARG WEBINAR_REVISION=""
ARG WEBINAR_CREATED=""
ARG WEBINAR_SOURCE="https://github.com/B2B-Saas-Inc/visioncast"
LABEL org.opencontainers.image.title="VisionCast" \
      org.opencontainers.image.description="VisionCast: the open-source headless CMS for webinars. Self-hosted, single-tenant (Django + PostgreSQL); one image for web, worker and management commands." \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${WEBINAR_VERSION}" \
      org.opencontainers.image.revision="${WEBINAR_REVISION}" \
      org.opencontainers.image.created="${WEBINAR_CREATED}" \
      org.opencontainers.image.source="${WEBINAR_SOURCE}" \
      org.opencontainers.image.base.name="docker.io/library/python:3.12-slim-bookworm"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    WEBINAR_BUILD_REVISION="${WEBINAR_REVISION}" \
    WEBINAR_BUILD_CREATED="${WEBINAR_CREATED}"

RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid 10001 --home-dir /app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY requirements.txt /tmp/requirements.txt
COPY --from=builder /wheels /wheels
RUN pip install --require-hashes --no-index --find-links=/wheels -r /tmp/requirements.txt \
 && rm -rf /wheels /tmp/requirements.txt

COPY --chown=root:root manage.py VERSION ./
COPY --chown=root:root config/ ./config/
COPY --chown=root:root webinars/ ./webinars/
COPY --chown=root:root docker/entrypoint.sh docker/web_healthcheck.py docker/worker_healthcheck.py ./docker/
COPY --chown=root:root LICENSE ./
# Guides served read-only as MCP resources (webinars/mcp_resources.GUIDES; keep in sync
# with the .dockerignore allowlist). Public documentation only, no secrets.
COPY --chown=root:root README.md ./
COPY --chown=root:root docs/implementation-guide.md docs/operations.md docs/integration.md docs/mcp.md docs/management-api.md \
     docs/api-reference.md docs/webhooks.md docs/field-dictionary.md docs/field-mapping.md \
     docs/waiting-room.md docs/studio.md docs/email.md docs/browser-integration.md \
     docs/native-integrations.md docs/security.md docs/tickets.md docs/hosting.md docs/kits.md ./docs/

# BUILD-ONLY collectstatic. The values below exist solely so that settings can
# be imported during this single RUN step; they are not persisted into the
# image environment (RUN-scoped variables, not ENV) and are never used at
# runtime. DJANGO_DEBUG=1 here only relaxes production-only checks while
# collecting static files; the runtime container never sets it. The Fernet
# key is an all-zero placeholder that never encrypts real data. No database
# connection is made by collectstatic.
RUN DJANGO_SECRET_KEY=build-only-not-secret-collectstatic-placeholder-0000000000 \
    DJANGO_DEBUG=1 \
    DJANGO_ALLOWED_HOSTS=localhost \
    INTEGRATION_MASTER_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA= \
    POSTGRES_HOST=build-only-unused \
    POSTGRES_DB=build-only-unused \
    POSTGRES_USER=build-only-unused \
    POSTGRES_PASSWORD=build-only-unused \
    python manage.py collectstatic --noinput \
 && chmod 0755 /app/docker/entrypoint.sh \
 && python -m compileall -q /app/config /app/webinars

USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "/app/docker/web_healthcheck.py"]

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["web"]

# ---------------------------------------------------------------------------
# test: runtime + pytest. Tests and pyproject.toml are bind-mounted at run time
# (see the `test` profile in compose.yaml); they are never baked into images.
# ---------------------------------------------------------------------------
FROM runtime AS test
USER root
COPY requirements.txt requirements-dev.txt /tmp/req/
RUN pip install -r /tmp/req/requirements-dev.txt && rm -rf /tmp/req
USER 10001:10001

# ---------------------------------------------------------------------------
# production (default target): identical to `runtime`. Declared last so a plain
# `docker build .` never produces the test image.
# ---------------------------------------------------------------------------
FROM runtime AS production

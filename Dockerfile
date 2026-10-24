# syntax=docker/dockerfile:1.7
#
# Single image for the web process and the durable worker.
#
# Base image is pinned by tag. For reproducible/supply-chain-safe builds, pin
# by digest as well, e.g.
#   docker buildx imagetools inspect python:3.12-slim-bookworm
#   --build-arg PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:<digest>
# The digest was not recorded here because it could not be resolved offline.
#
# The build context is restricted by .dockerignore (allow-list): only
# manage.py, config/, webinars/, docker/, requirements*.txt, LICENSE and the
# allowlisted public guides (README.md, selected docs/*.md) are sent.

ARG PYTHON_IMAGE=python:3.12-slim-bookworm

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

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid 10001 --home-dir /app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY requirements.txt /tmp/requirements.txt
COPY --from=builder /wheels /wheels
RUN pip install --require-hashes --no-index --find-links=/wheels -r /tmp/requirements.txt \
 && rm -rf /wheels /tmp/requirements.txt

COPY --chown=root:root manage.py ./
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
     docs/native-integrations.md docs/security.md ./docs/

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

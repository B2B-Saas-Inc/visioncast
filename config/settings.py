"""Django settings for the webinar platform.

Everything is configured from environment variables. Production (``DJANGO_DEBUG``
unset/false) is fail-closed: missing secrets or hosts raise ``ImproperlyConfigured``
at start-up instead of silently falling back to insecure defaults.
"""
import os
import secrets
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Environment parsing helpers
# ---------------------------------------------------------------------------

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def env_str(name, default=""):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip()


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ImproperlyConfigured(f"{name} must be a boolean (1/0, true/false, yes/no, on/off).")


def env_int(name, default):
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value.strip())
    except ValueError as exc:
        raise ImproperlyConfigured(f"{name} must be an integer.") from exc


def env_bounded_int(name, default, minimum, maximum):
    """Whole number in ``[minimum, maximum]``; unset or empty uses ``default``.

    Only plain decimal digits are accepted (no sign, underscores or fractions), so a
    typo fails start-up instead of being clamped or silently reinterpreted.
    """
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    value = value.strip()
    if not (value.isascii() and value.isdigit()) or not minimum <= int(value) <= maximum:
        raise ImproperlyConfigured(f"{name} must be a whole number from {minimum} to {maximum} (got {value[:20]!r}).")
    return int(value)


def env_list(name, default=None):
    value = os.environ.get(name)
    if value is None:
        return list(default or [])
    return [item.strip() for item in value.split(",") if item.strip()]


def env_int_list(name, default=None):
    items = env_list(name, None) if os.environ.get(name) is not None else None
    if items is None:
        return list(default or [])
    try:
        return [int(item) for item in items]
    except ValueError as exc:
        raise ImproperlyConfigured(f"{name} must be a comma-separated list of integers.") from exc


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

DEBUG = env_bool("DJANGO_DEBUG", False)

# Test-only switch: run against SQLite. Results obtained this way are NOT evidence
# of PostgreSQL behaviour (locking, SKIP LOCKED, constraints) and must never be
# used in production.
TEST_SQLITE = env_bool("DJANGO_TEST_SQLITE", False)

# Production = not DEBUG and not the SQLite test harness.
_RELAXED = DEBUG or TEST_SQLITE


def _required(name, value):
    if not value and not _RELAXED:
        raise ImproperlyConfigured(f"{name} must be set when DJANGO_DEBUG is false.")
    return value


SECRET_KEY = env_str("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    _required("DJANGO_SECRET_KEY", SECRET_KEY)
    # Development/test only: ephemeral random key (sessions do not survive restarts).
    SECRET_KEY = "dev-insecure-" + secrets.token_urlsafe(48)
elif len(SECRET_KEY) < 50 and not _RELAXED:
    raise ImproperlyConfigured("DJANGO_SECRET_KEY must be at least 50 characters.")

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS")
if not ALLOWED_HOSTS:
    _required("DJANGO_ALLOWED_HOSTS", ALLOWED_HOSTS)
    ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]", "testserver"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "webinars.apps.WebinarsConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

POSTGRES_PASSWORD = env_str("POSTGRES_PASSWORD")

if TEST_SQLITE:
    # Tests only. NOT PostgreSQL evidence.
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": env_str("DJANGO_TEST_SQLITE_PATH", str(BASE_DIR / "test.sqlite3")),
        }
    }
else:
    _required("POSTGRES_PASSWORD", POSTGRES_PASSWORD)
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "HOST": env_str("POSTGRES_HOST", "db"),
            "PORT": env_str("POSTGRES_PORT", "5432"),
            "NAME": env_str("POSTGRES_DB", "webinars"),
            "USER": env_str("POSTGRES_USER", "webinars"),
            "PASSWORD": POSTGRES_PASSWORD,
            "CONN_MAX_AGE": 60,
            "CONN_HEALTH_CHECKS": True,
            "OPTIONS": {"sslmode": env_str("POSTGRES_SSLMODE", "prefer")},
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# ---------------------------------------------------------------------------
# Auth / passwords
# ---------------------------------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# ---------------------------------------------------------------------------
# I18N / time
# ---------------------------------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
FORMS_URLFIELD_ASSUME_HTTPS = True


# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------

STATIC_URL = "/static/"
STATIC_ROOT = env_str("DJANGO_STATIC_ROOT", "/app/staticfiles")
# SDK lives in webinars/static/webinar-sdk; sdk/src links to this single source.
# App-directory discovery also works in the bounded preview source mounts.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if TEST_SQLITE
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    },
}


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

if env_bool("DJANGO_TRUST_X_FORWARDED_PROTO", False):
    # Only enable behind a reverse proxy that overwrites X-Forwarded-Proto.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", not _RELAXED)
SECURE_REDIRECT_EXEMPT = [r"^healthz$"]
SESSION_COOKIE_SECURE = env_bool("DJANGO_SESSION_COOKIE_SECURE", not DEBUG)
CSRF_COOKIE_SECURE = env_bool("DJANGO_CSRF_COOKIE_SECURE", not DEBUG)
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = True
SESSION_COOKIE_AGE = 8 * 60 * 60  # admin sessions: 8 hours
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SECURE_HSTS_SECONDS = env_int("DJANGO_HSTS_SECONDS", 0 if DEBUG else 31536000)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("DJANGO_HSTS_INCLUDE_SUBDOMAINS", False)
SECURE_HSTS_PRELOAD = env_bool("DJANGO_HSTS_PRELOAD", False)
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS") or env_list("CSRF_TRUSTED_ORIGINS")

DATA_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024  # 1 MB
DATA_UPLOAD_MAX_NUMBER_FIELDS = 1000
# The only file upload is the studio event cover (webinars/covers.py): in memory only,
# at most covers.MAX_UPLOAD_BYTES per file and one file per request.
FILE_UPLOAD_HANDLERS = ["webinars.covers.BoundedMemoryUploadHandler"]
DATA_UPLOAD_MAX_NUMBER_FILES = 1


# ---------------------------------------------------------------------------
# Logging (stdout, no request bodies)
# ---------------------------------------------------------------------------

LOG_LEVEL = env_str("DJANGO_LOG_LEVEL", "INFO").upper()
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "jsonish": {
            "format": '{"ts": "%(asctime)s", "level": "%(levelname)s", "logger": "%(name)s", "msg": "%(message)s"}',
        },
    },
    "handlers": {
        "stdout": {"class": "logging.StreamHandler", "stream": "ext://sys.stdout", "formatter": "jsonish"},
    },
    "root": {"handlers": ["stdout"], "level": LOG_LEVEL},
    "loggers": {
        "django": {"handlers": ["stdout"], "level": LOG_LEVEL, "propagate": False},
        # django.request logs include the request object only in `extra`; the
        # formatter above does not render it, so bodies/headers are never printed.
        "django.request": {"handlers": ["stdout"], "level": "WARNING", "propagate": False},
        "django.security": {"handlers": ["stdout"], "level": "WARNING", "propagate": False},
        "webinars": {"handlers": ["stdout"], "level": LOG_LEVEL, "propagate": False},
    },
}


# ---------------------------------------------------------------------------
# Application settings
# ---------------------------------------------------------------------------

PUBLIC_BASE_URL = env_str("PUBLIC_BASE_URL", "http://localhost:8000" if _RELAXED else "")
if not PUBLIC_BASE_URL and not _RELAXED:
    raise ImproperlyConfigured("PUBLIC_BASE_URL must be set when DJANGO_DEBUG is false.")
# Optional public event page for calendar entries, e.g. https://www.example.com/webinars/{slug}
# ({slug}, {occurrence_id}; https; placeholders after the host only). Unset or invalid falls
# back to the JSON event API URL; system check webinars.E101. See webinars/calendar_links.py.
PUBLIC_EVENT_URL_TEMPLATE = env_str("PUBLIC_EVENT_URL_TEMPLATE")
WEBINAR_ALLOWED_ORIGINS = env_list("WEBINAR_ALLOWED_ORIGINS")
# Direct mode: point registration calendar.icsUrl at /webinar-api/calendar.ics (same origin).
# Set false only for an edge that routes /api/v1/occurrences/ and relies on the old link.
WEBINAR_DIRECT_ICS_REWRITE = env_bool("WEBINAR_DIRECT_ICS_REWRITE", True)
THANK_YOU_REDIRECT_HOSTS = env_list("THANK_YOU_REDIRECT_HOSTS")
WEBHOOK_HOST_ALLOWLIST = env_list("WEBHOOK_HOST_ALLOWLIST")
# Native integrations send credentials only to hosts pinned in code (the vendor's official API
# host, or a typed host that must end in the vendor's domain). Those pinned hosts are allowed
# without a WEBHOOK_HOST_ALLOWLIST entry unless an operator turns this off; the allowlist then
# governs generic webhooks and self-hosted receivers only.
NATIVE_VENDOR_HOSTS_IMPLICIT = env_bool("NATIVE_VENDOR_HOSTS_IMPLICIT", True)
WEBHOOK_ALLOWED_PORTS = env_int_list("WEBHOOK_ALLOWED_PORTS", [443])
WEBHOOK_TIMEOUT_SECONDS = env_int("WEBHOOK_TIMEOUT_SECONDS", 10)
# Hard deadline for one whole send (DNS, connect, TLS, request, response). At run time
# webinars/delivery.py also caps it at the remaining outbox lease minus a safety margin.
WEBHOOK_TOTAL_TIMEOUT_SECONDS = env_bounded_int("WEBHOOK_TOTAL_TIMEOUT_SECONDS", 30, 5, 600)

INTEGRATION_MASTER_KEY = env_str("INTEGRATION_MASTER_KEY")
_required("INTEGRATION_MASTER_KEY", INTEGRATION_MASTER_KEY)
INTEGRATION_MASTER_KEY_PREVIOUS = env_list("INTEGRATION_MASTER_KEY_PREVIOUS")

# Must equal the first-party proxy's PROXY_SESSION_COOKIE (sdk/proxy default "webinar_session").
CAPABILITY_COOKIE_NAME = env_str("CAPABILITY_COOKIE_NAME", "webinar_session")
CAPABILITY_COOKIE_SECURE = env_bool("CAPABILITY_COOKIE_SECURE", True)
CAPABILITY_TTL_DAYS = env_int("CAPABILITY_TTL_DAYS", 180)

REMINDER_OFFSETS_MINUTES = env_int_list("REMINDER_OFFSETS_MINUTES", [1440, 60])
REMINDER_LATE_GRACE_MINUTES = env_int("REMINDER_LATE_GRACE_MINUTES", 15)

OUTBOX_LEASE_SECONDS = env_int("OUTBOX_LEASE_SECONDS", 120)
OUTBOX_BATCH_SIZE = env_int("OUTBOX_BATCH_SIZE", 20)
WORKER_HEARTBEAT_FILE = env_str("WORKER_HEARTBEAT_FILE", "/tmp/worker-heartbeat")
IDEMPOTENCY_TTL_HOURS = env_int("IDEMPOTENCY_TTL_HOURS", 72)
OUTBOX_PAYLOAD_RETENTION_DAYS = env_int("OUTBOX_PAYLOAD_RETENTION_DAYS", 30)


# ---------------------------------------------------------------------------
# Studio (marketer UI at /studio/) and team invitations
# ---------------------------------------------------------------------------

LOGIN_URL = "/studio/login/"
LOGIN_REDIRECT_URL = "/studio/"
LOGOUT_REDIRECT_URL = "/studio/login/"
INVITATION_TTL_HOURS = env_int("INVITATION_TTL_HOURS", 72)


# ---------------------------------------------------------------------------
# Outgoing email (waiting-room verification codes, team invitations)
#
# Disabled by default: messages are dropped (``webinars.mail.DisabledBackend``)
# and nothing leaves the process. Set EMAIL_DELIVERY=smtp plus the SMTP_* values
# to send for real; credentials are read from the environment only and never
# rendered, logged or stored. See docs/email.md.
# ---------------------------------------------------------------------------

EMAIL_DELIVERY = env_str("EMAIL_DELIVERY", "disabled").lower()
DEFAULT_FROM_EMAIL = env_str("DEFAULT_FROM_EMAIL", "webinars@localhost")
EMAIL_SUBJECT_PREFIX = ""
if EMAIL_DELIVERY == "smtp":
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_HOST = env_str("SMTP_HOST")
    EMAIL_PORT = env_int("SMTP_PORT", 587)
    EMAIL_HOST_USER = env_str("SMTP_USERNAME")
    EMAIL_HOST_PASSWORD = env_str("SMTP_PASSWORD")
    EMAIL_USE_TLS = env_bool("SMTP_STARTTLS", True)
    EMAIL_USE_SSL = env_bool("SMTP_SSL", False)
    EMAIL_TIMEOUT = env_int("SMTP_TIMEOUT_SECONDS", 10)
    if not EMAIL_HOST:
        raise ImproperlyConfigured("EMAIL_DELIVERY=smtp requires SMTP_HOST.")
    if EMAIL_USE_TLS and EMAIL_USE_SSL:
        raise ImproperlyConfigured("Set only one of SMTP_STARTTLS and SMTP_SSL.")
    if not (EMAIL_USE_TLS or EMAIL_USE_SSL) and not _RELAXED:
        raise ImproperlyConfigured("SMTP delivery requires SMTP_STARTTLS=1 or SMTP_SSL=1 in production.")
elif EMAIL_DELIVERY == "disabled":
    EMAIL_BACKEND = "webinars.mail.DisabledBackend"
else:
    raise ImproperlyConfigured("EMAIL_DELIVERY must be 'disabled' or 'smtp'.")


# ---------------------------------------------------------------------------
# Hosted waiting room (docs/waiting-room.md)
# ---------------------------------------------------------------------------

WAITING_ROOM_VERIFICATION_TTL_MINUTES = env_int("WAITING_ROOM_VERIFICATION_TTL_MINUTES", 15)
WAITING_ROOM_MAX_CODE_ATTEMPTS = env_int("WAITING_ROOM_MAX_CODE_ATTEMPTS", 5)
# Codes sent per (occurrence, email) per verification TTL window; extra requests get the
# same generic response but no new email.
WAITING_ROOM_MAX_CODES_PER_EMAIL = env_int("WAITING_ROOM_MAX_CODES_PER_EMAIL", 3)
# Platform-wide caps per TTL window (the identity cookie is platform-wide): codes per
# address across all rooms, and wrong guesses per (browser, address) across all rooms.
# Upper bounds keep worst-case guesses per address small against the 6-digit code space.
WAITING_ROOM_MAX_CODES_PER_EMAIL_GLOBAL = env_bounded_int("WAITING_ROOM_MAX_CODES_PER_EMAIL_GLOBAL", 10, 1, 50)
WAITING_ROOM_MAX_GUESSES_PER_EMAIL = env_bounded_int("WAITING_ROOM_MAX_GUESSES_PER_EMAIL", 10, 1, 50)
# Basic per-client-IP limits (per process, see webinars/ratelimit.py). Production
# deployments must also rate-limit /w/ and /mcp at the reverse proxy.
RATE_LIMIT_ENABLED = env_bool("RATE_LIMIT_ENABLED", True)
# Only trust X-Forwarded-For when a reverse proxy overwrites it.
RATE_LIMIT_TRUST_X_FORWARDED_FOR = env_bool("RATE_LIMIT_TRUST_X_FORWARDED_FOR", False)
# Reverse proxies in front of Django that each append to X-Forwarded-For; the client
# address is the Nth entry from the right. Ignored unless the flag above is on.
RATE_LIMIT_TRUSTED_PROXY_COUNT = env_bounded_int("RATE_LIMIT_TRUSTED_PROXY_COUNT", 1, 1, 10)
WAITING_ROOM_POLL_SECONDS = env_int("WAITING_ROOM_POLL_SECONDS", 15)

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "webinars-default",
        "OPTIONS": {"MAX_ENTRIES": 10000},
    }
}

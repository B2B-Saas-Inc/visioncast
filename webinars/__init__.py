"""Webinar platform application package.

``__version__`` is read from the ``VERSION`` file at the repository root (copied
into the container image as ``/app/VERSION``). It is reported by ``GET /healthz``
(``"version"``), ``manage.py show_version`` and the MCP ``serverInfo``; the image build
stamps the same value into the ``org.opencontainers.image.version`` label
(``deploy/build-image.sh``). A release bumps ``VERSION`` and tags ``v<VERSION>``.
"""
from pathlib import Path

_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"


def _read_version() -> str:
    try:
        value = _VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0+unknown"
    return value or "0.0.0+unknown"


__version__ = _read_version()

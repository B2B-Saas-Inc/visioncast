"""Web healthcheck: GET http://127.0.0.1:8000/healthz with a valid Host header.

The Host header is the first entry of DJANGO_ALLOWED_HOSTS (leading "." of a
subdomain wildcard removed) so Django's host validation accepts the request.
``X-Forwarded-Proto: https`` is sent so the check is not redirected when the
deployment trusts that header (DJANGO_TRUST_X_FORWARDED_PROTO=1). Redirects
are never followed: anything other than HTTP 200 is unhealthy.
Standard library only.
"""
import os
import sys
import urllib.error
import urllib.request


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _host() -> str:
    for entry in (os.environ.get("DJANGO_ALLOWED_HOSTS") or "").split(","):
        entry = entry.strip().lstrip(".")
        if entry and entry != "*":
            return entry
    return "localhost"


def main() -> int:
    request = urllib.request.Request(
        "http://127.0.0.1:8000/healthz",
        headers={"Host": _host(), "X-Forwarded-Proto": "https"},
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=4) as response:
            return 0 if response.status == 200 else 1
    except (urllib.error.URLError, OSError) as exc:
        print(f"unhealthy: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

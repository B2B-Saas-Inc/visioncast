"""Environment configuration for operational limits that callers previously read only
through ``getattr`` code defaults: RATE_LIMIT_TRUSTED_PROXY_COUNT,
WAITING_ROOM_MAX_CODES_PER_EMAIL_GLOBAL, WAITING_ROOM_MAX_GUESSES_PER_EMAIL and
WEBHOOK_TOTAL_TIMEOUT_SECONDS (docs/operational-settings.md).

Whole-module checks execute config/settings.py in a fresh namespace with ``runpy`` and
``DJANGO_TEST_SQLITE=1`` (relaxed mode, so no secrets are needed). That never touches the
settings Django has already loaded. No database or network access.
"""
import re
import runpy
from pathlib import Path

import pytest
from django.core.exceptions import ImproperlyConfigured

from config.settings import env_bounded_int

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_FILE = ROOT / "config" / "settings.py"

# name -> (default, minimum, maximum, caller module that reads it)
OPERATIONAL = {
    "RATE_LIMIT_TRUSTED_PROXY_COUNT": (1, 1, 10, "webinars/ratelimit.py"),
    "WAITING_ROOM_MAX_CODES_PER_EMAIL_GLOBAL": (10, 1, 50, "webinars/waiting_room.py"),
    "WAITING_ROOM_MAX_GUESSES_PER_EMAIL": (10, 1, 50, "webinars/waiting_room.py"),
    "WEBHOOK_TOTAL_TIMEOUT_SECONDS": (30, 5, 600, "webinars/delivery.py"),
}

VAR = "TEST_OPERATIONAL_SETTING"


@pytest.fixture
def clean_env(monkeypatch):
    for name in OPERATIONAL:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DJANGO_TEST_SQLITE", "1")
    return monkeypatch


def _load_settings():
    return runpy.run_path(str(SETTINGS_FILE))


# ---------------------------------------------------------------- env_bounded_int


def test_unset_or_blank_uses_default(monkeypatch):
    monkeypatch.delenv(VAR, raising=False)
    assert env_bounded_int(VAR, 7, 1, 10) == 7
    for blank in ("", "   "):
        monkeypatch.setenv(VAR, blank)
        assert env_bounded_int(VAR, 7, 1, 10) == 7


@pytest.mark.parametrize("raw,expected", [("1", 1), ("10", 10), (" 4 ", 4), ("004", 4)])
def test_valid_values_inclusive_bounds(monkeypatch, raw, expected):
    monkeypatch.setenv(VAR, raw)
    assert env_bounded_int(VAR, 7, 1, 10) == expected


@pytest.mark.parametrize("raw", ["0", "11", "-1", "+2", "1_0", "1.5", "1e1", "abc", "٣", "99999999999999999999"])
def test_invalid_values_rejected_with_clear_message(monkeypatch, raw):
    monkeypatch.setenv(VAR, raw)
    with pytest.raises(ImproperlyConfigured) as exc:
        env_bounded_int(VAR, 7, 1, 10)
    message = str(exc.value)
    assert VAR in message
    assert "from 1 to 10" in message


def test_error_message_truncates_echoed_value(monkeypatch):
    monkeypatch.setenv(VAR, "x" * 500)
    with pytest.raises(ImproperlyConfigured) as exc:
        env_bounded_int(VAR, 7, 1, 10)
    assert "x" * 21 not in str(exc.value)


# ---------------------------------------------------------------- config/settings.py


def test_settings_defaults_match_previous_code_defaults(clean_env):
    loaded = _load_settings()
    for name, (default, _lo, _hi, _caller) in OPERATIONAL.items():
        assert loaded[name] == default, name


@pytest.mark.parametrize("name", sorted(OPERATIONAL))
def test_settings_accept_bounds_from_environment(clean_env, name):
    _default, lo, hi, _caller = OPERATIONAL[name]
    for value in (lo, hi):
        clean_env.setenv(name, str(value))
        assert _load_settings()[name] == value


@pytest.mark.parametrize("name", sorted(OPERATIONAL))
def test_settings_reject_out_of_range_and_malformed(clean_env, name):
    _default, lo, hi, _caller = OPERATIONAL[name]
    for raw in (str(lo - 1), str(hi + 1), "ten", "-1"):
        clean_env.setenv(name, raw)
        with pytest.raises(ImproperlyConfigured, match=name):
            _load_settings()


@pytest.mark.parametrize("name", sorted(OPERATIONAL))
def test_caller_fallback_default_matches_settings_default(name):
    """Callers keep a getattr fallback; it must agree with the settings default."""
    default, _lo, _hi, caller = OPERATIONAL[name]
    source = (ROOT / caller).read_text(encoding="utf-8")
    found = re.findall(rf'getattr\(settings, "{name}", (\d+)\)', source)
    assert found, f"{caller} no longer reads {name}"
    assert {int(v) for v in found} == {default}


@pytest.mark.parametrize("name", sorted(OPERATIONAL))
def test_env_example_documents_setting(name):
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    default = OPERATIONAL[name][0]
    assert f"# {name}={default}" in text


# ---------------------------------------------------------------- callers honour the setting


def test_trusted_proxy_count_reaches_rate_limiter(settings):
    from webinars import ratelimit

    settings.RATE_LIMIT_TRUSTED_PROXY_COUNT = 3
    assert ratelimit._trusted_proxy_count() == 3

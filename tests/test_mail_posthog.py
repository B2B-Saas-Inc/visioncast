"""EMAIL_DELIVERY=posthog: the platform's own messages through PostHog transactional email.

No network: urlopen is replaced. Nothing here was checked against a real PostHog project."""
import io
import json
import urllib.error

import pytest
from django.core import mail as django_mail

from webinars import mail

KEY = "phs_SecretProjectKeyValue_1234567890"
TEMPLATE = "01963d8a-6f2c-7d4e-9b1a-3c5e8f2a7b10"


@pytest.fixture
def posthog(settings):
    settings.EMAIL_BACKEND = mail.POSTHOG_BACKEND
    settings.POSTHOG_EMAIL_HOST = "us.posthog.com"
    settings.POSTHOG_EMAIL_API_KEY = KEY
    settings.POSTHOG_EMAIL_TEMPLATE_ID = TEMPLATE
    settings.EMAIL_TIMEOUT = 5
    return settings


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def capture(monkeypatch, *, status=202, body=None):
    calls = []

    def urlopen(request, timeout):
        calls.append({"url": request.full_url, "headers": dict(request.header_items()), "json": json.loads(request.data), "timeout": timeout})
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, "refused", {}, io.BytesIO(json.dumps(body or {}).encode()))
        return Response(json.dumps(body if body is not None else {"message_id": "m-1", "replayed": False}).encode())

    monkeypatch.setattr(mail.urllib.request, "urlopen", urlopen)
    return calls


def test_mode_is_posthog(posthog):
    assert mail.delivery_mode() == "posthog"


def test_send_shape(posthog, monkeypatch):
    calls = capture(monkeypatch)
    assert mail.send("grace@example.com", "Your waiting room code: 123456", "Hi,\n\nYour code is 123456.\n\nOpen https://events.example.com/w/x/")
    [call] = calls
    assert call["url"] == "https://us.posthog.com/api/projects/@current/messaging/send/email/"
    headers = {k.lower(): v for k, v in call["headers"].items()}
    assert headers["authorization"] == f"Bearer {KEY}" and headers["idempotency-key"].startswith("visioncast-")
    body = call["json"]
    assert body["template_id"] == TEMPLATE and body["to"] == "grace@example.com" and body["distinct_id"] == "grace@example.com"
    assert body["message_data"]["subject"] == "Your waiting room code: 123456"
    assert body["message_data"]["html"].startswith("<p>Hi,</p>") and '<a href="https://events.example.com/w/x/">' in body["message_data"]["html"]


@pytest.mark.parametrize("status,body,code", [(422, {"code": "recipient_suppressed"}, "recipient_suppressed"), (401, {}, "http_401"), (500, {"code": "bad code!"}, "http_500")])
def test_refusals_report_a_code_and_never_the_message(posthog, monkeypatch, caplog, status, body, code):
    capture(monkeypatch, status=status, body=body)
    with pytest.raises(mail.PostHogEmailError) as err:
        mail.posthog_send("grace@example.com", "Code 123456", "Your code is 123456")
    assert err.value.code == code and "123456" not in str(err.value) and "grace" not in str(err.value)
    # mail.send swallows it and logs the type only.
    assert mail.send("grace@example.com", "Code 123456", "Your code is 123456") is False
    assert "123456" not in caplog.text and "grace@" not in caplog.text


def test_a_response_without_an_id_is_a_failure(posthog, monkeypatch):
    capture(monkeypatch, body={"replayed": False})
    assert mail.send("grace@example.com", "s", "b") is False


def test_one_call_per_recipient_and_fail_silently(posthog, monkeypatch):
    calls = capture(monkeypatch, status=422, body={"code": "recipient_unreachable"})
    message = django_mail.EmailMessage("s", "b", to=["a@example.com", "b@example.com"])
    assert mail.PostHogBackend(fail_silently=True).send_messages([message]) == 0 and len(calls) == 2

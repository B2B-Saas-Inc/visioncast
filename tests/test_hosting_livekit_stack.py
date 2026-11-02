"""VisionCast against the real thing: the adapter, the waiting room and the attendance import
talking to a running visioncast-live, a real LiveKit server, Egress and storage, with real
browsers. Skipped unless ``live/tests/e2e/run-visioncast.sh`` started the stack.
"""
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path

import pytest

from tests.test_waiting_room import _use_cookie, registered_viewer, room_occurrence
from webinars import attendance_import, crypto
from webinars.hosting import base, service
from webinars.models import HostedSession, HostingAccount

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.skipif(not os.environ.get("LIVE_STACK_URL"), reason="needs live/tests/e2e/run-visioncast.sh"),
]

HOST = "live.example.com"


def _local(url):
    return url.replace(f"https://{HOST}", os.environ["LIVE_STACK_URL"])


def transport(method, url, body, headers, *, allowed_hosts):
    assert HOST in allowed_hosts
    request = urllib.request.Request(_local(url), data=body or None, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read(), None, False
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), None, False


def _until(what, fn, seconds=90):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(1)
    raise AssertionError(f"Timed out waiting for {what}")


def _host_user():
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user("maya", "maya@example.com", "x" * 20, first_name="Maya", last_name="Host")


def test_a_session_from_room_to_applied_attendance(client, monkeypatch, settings):
    settings.WEBHOOK_HOST_ALLOWLIST = [HOST]
    monkeypatch.setattr(base, "default_transport", transport)
    account = HostingAccount.objects.create(
        provider="livekit", name="stack", enabled=True, config={"service_host": HOST},
        credentials_encrypted=crypto.encrypt_secret(json.dumps({"service_secret": os.environ["LIVE_SERVICE_SECRET"]})))
    occ = room_occurrence(starts_in=timedelta(minutes=5), join_url="")

    # The Administrator's preview, then create.
    assert not service.preview(occ, account, "create")["problems"]
    service.execute(occ, account, "create")
    assert HostedSession.objects.get(occurrence=occ).state == HostedSession.State.LINKED

    # Brand and destinations are the live server's own; the session's address came from VisionCast.
    room = HostedSession.objects.get(occurrence=occ).external_id
    def settings_call(path, body):
        headers = {"Authorization": f"Bearer {os.environ['LIVE_SERVICE_SECRET']}", "Content-Type": "application/json"}
        status, raw, _, _ = transport("PUT", f"https://{HOST}/v1{path}", json.dumps(body).encode(), headers, allowed_hosts={HOST})
        assert status == 200, raw
    settings_call("/brand", {"accent": "#1565c0", "stage": "", "mode": "", "font": ""})
    settings_call(f"/rooms/{room}/destinations", {"destinations": [{"platform": "youtube", "server": "rtmp://rtmp:1935/live", "key": "from-the-studio", "armed": True}]})

    # The attendee is admitted by the real waiting room and sent to their own link.
    reg, token, _cap = registered_viewer(occ)
    _use_cookie(client, token)
    entered = client.post(f"/w/{occ.waiting_room_code}/enter")
    assert entered.status_code == 303 and entered["Location"].startswith(f"https://{HOST}/join/a.")

    # A real host and attendee on those links, with a recording in between.
    subprocess.run(
        ["node", "visioncast.cjs"], check=True, cwd=Path(__file__).parent.parent / "live/tests/e2e", timeout=240,
        env={**os.environ, "HOST_URL": _local(service.host_room_link(occ, user=_host_user())), "JOIN_URL": _local(entered["Location"])})

    # LiveKit's webhooks reached the service, and the import matches on the registration ID.
    adapter = service.adapter_for(account)
    session = HostedSession.objects.get(occurrence=occ)

    def attendance_done():
        data = adapter.fetch_attendance(occ, account.config, service.load_secrets(account), session.external_id,
                                        transport=transport).data
        return data.get("complete") and data.get("rows")

    _until("attendance", attendance_done)
    imp = attendance_import.pull_from_provider(occ)
    row = imp.rows.get()
    assert (row.match, row.registration_id) == ("matched", reg.pk)
    assert imp.complete and row.duration_seconds > 0 and not row.email_key
    attendance_import.apply_import(imp, expected_state=attendance_import.import_state(imp))
    imp.refresh_from_db()
    assert imp.applied_count == 1

    # The recording finished, and the studio would link to it.
    def finished():
        return [r for r in service.fetch_recordings(occ) if r["status"] == "complete"]

    recording = _until("the recording", finished)[0]
    assert recording["durationSeconds"] >= 5 and recording["sizeBytes"] > 0
    assert recording["publicUrl"].startswith("https://cdn.example.com/recordings/")

    # Its captions land next to it, for everyone and per speaker, as SRT and transcript.
    def with_captions():
        return [r for r in service.fetch_recordings(occ) if r["captions"]]

    files = _until("the caption files", with_captions, seconds=60)[0]["captions"]
    assert {("Everyone", "srt"), ("Everyone", "txt"), ("Maya Host", "srt"), ("Maya Host", "txt")} == {(c["speaker"], c["kind"]) for c in files}

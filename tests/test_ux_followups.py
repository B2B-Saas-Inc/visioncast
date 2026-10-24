"""UX follow-ups: configurable registration button text, uploaded cover images,
host-count wording and waiting-room discoverability."""
import io

import pytest
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from PIL import Image

from tests.conftest import JOIN_URL, WAITING_URL, make_event, make_form, make_occurrence
from webinars import covers, services
from webinars.models import CoverImage, Event, EventRevision, Host
from webinars.roles import ADMINISTRATOR, ANALYST, EDITOR

pytestmark = pytest.mark.django_db

PASSWORD = "studio-pass-Quiet-Harbor-7"


def client_for(django_user_model, role, **client_kw):
    user = django_user_model.objects.create_user(f"u-{role.lower()}", f"{role.lower()}@example.com", PASSWORD)
    user.groups.add(Group.objects.get(name=role))
    c = Client(**client_kw)
    c.force_login(user)
    return c


def image_bytes(fmt="PNG", size=(64, 48), exif=None):
    img = Image.new("RGB", size, (200, 80, 40))
    out = io.BytesIO()
    kwargs = {"exif": exif} if exif is not None else {}
    img.save(out, fmt, **kwargs)
    return out.getvalue()


def upload(data, name="cover.png", content_type="image/png"):
    return SimpleUploadedFile(name, data, content_type=content_type)


def edit_post(event, **overrides):
    data = services.event_form_data(event)
    post = {k: v for k, v in data.items() if k not in ("learning_outcomes", "hosts", "topics", "visibility")}
    post = {k: ("" if v is None else v) for k, v in post.items()}
    post["hosts"] = [str(pk) for pk in data["hosts"]]
    post["topics"] = [str(pk) for pk in data["topics"]]
    post["learning_outcomes"] = list(event.learning_outcomes or [])
    post.update(overrides)
    return post


@pytest.fixture
def event(db):
    return make_event(make_form(), slug="ux-event", visibility="public")


# ---------------------------------------------------------------------------
# Registration button text (CTA)
# ---------------------------------------------------------------------------


def test_cta_defaults_are_backward_compatible(client, event):
    make_occurrence(event)
    body = client.get(f"/api/v1/events/{event.slug}").json()
    assert body["cta"] == {"register": "Register", "confirm": "Confirm my RSVP"}


def test_cta_edited_visually_propagates_to_api_revision_preview_and_webhook(django_user_model, client, event):
    occ = make_occurrence(event)
    editor = client_for(django_user_model, EDITOR)
    resp = editor.post(
        f"/studio/events/{event.pk}/",
        edit_post(event, register_label="  Save my seat ", confirm_label="Yes, keep my seat"),
    )
    assert resp.status_code == 302, resp.content
    event.refresh_from_db()
    assert (event.register_label, event.confirm_label) == ("Save my seat", "Yes, keep my seat")
    rev = EventRevision.objects.get(event=event, revision=event.revision)
    assert rev.snapshot["registerLabel"] == "Save my seat"
    assert rev.snapshot["confirmLabel"] == "Yes, keep my seat"
    assert client.get(f"/api/v1/events/{event.slug}").json()["cta"] == {
        "register": "Save my seat", "confirm": "Yes, keep my seat"
    }
    preview = editor.get(f"/studio/events/{event.pk}/preview").content.decode()
    assert "Save my seat</button>" in preview and "Yes, keep my seat" in preview

    from webinars.outbox import _event_group

    assert _event_group(occ, False)["cta"]["register"] == "Save my seat"


@pytest.mark.parametrize(
    "label",
    ["x" * 61, "<b>Go</b>", "Line\nbreak", "tab\there", "back`tick"],
)
def test_cta_rejects_unsafe_or_long_text(django_user_model, event, label):
    editor = client_for(django_user_model, EDITOR)
    before = event.revision
    resp = editor.post(f"/studio/events/{event.pk}/", edit_post(event, register_label=label))
    assert resp.status_code == 400
    event.refresh_from_db()
    assert event.revision == before and event.register_label == ""


def test_cta_management_api_round_trip(client, event):
    from tests.conftest import make_api_key

    key = make_api_key(("content_write",), name="cms")
    auth = {"Authorization": f"Bearer {key}"}
    resp = client.patch(
        f"/api/v1/manage/events/{event.pk}", data={"confirmLabel": "Count me in"}, content_type="application/json", headers=auth
    )
    assert resp.status_code == 200, resp.content
    assert resp.json()["confirmLabel"] == "Count me in"
    bad = client.patch(
        f"/api/v1/manage/events/{event.pk}", data={"registerLabel": "<script>"}, content_type="application/json", headers=auth
    )
    assert bad.status_code == 400


def test_openapi_documents_cta_and_cover():
    from webinars.openapi import build_spec

    schemas = build_spec()["components"]["schemas"]
    assert "cta" in schemas["PublicEvent"]["properties"]
    managed = schemas["ManagedEvent"]["properties"]
    assert {"registerLabel", "confirmLabel", "coverImageId"} <= set(managed)
    assert schemas["EventUpdate"]["properties"]["registerLabel"]["maxLength"] == 60


# ---------------------------------------------------------------------------
# Cover uploads
# ---------------------------------------------------------------------------


def test_upload_png_reencodes_and_propagates(django_user_model, client, event):
    occ = make_occurrence(event)
    editor = client_for(django_user_model, EDITOR)
    resp = editor.post(
        f"/studio/events/{event.pk}/", edit_post(event, hero_image_url="", cover_upload=upload(image_bytes()))
    )
    assert resp.status_code == 302, resp.content
    event.refresh_from_db()
    cover = event.cover_image
    assert cover is not None and cover.content_type == "image/png" and (cover.width, cover.height) == (64, 48)
    url = f"https://webinars.example.com/media/covers/{cover.pk}.png"
    assert client.get(f"/api/v1/events/{event.slug}").json()["heroImageUrl"] == url
    rev = EventRevision.objects.get(event=event, revision=event.revision)
    assert rev.snapshot["heroImageUrl"] == url and rev.snapshot["coverImageId"] == str(cover.pk)

    from webinars.outbox import _event_group

    assert _event_group(occ, False)["heroImageUrl"] == url
    assert f"/media/covers/{cover.pk}.png" in editor.get(f"/studio/events/{event.pk}/preview").content.decode()

    served = client.get(f"/media/covers/{cover.pk}.png")
    assert served.status_code == 200
    assert served["Content-Type"] == "image/png"
    assert served["X-Content-Type-Options"] == "nosniff"
    assert "sandbox" in served["Content-Security-Policy"]
    assert "immutable" in served["Cache-Control"]
    Image.open(io.BytesIO(served.content)).verify()
    assert client.get(f"/media/covers/{cover.pk}.jpg").status_code == 404
    assert client.post(f"/media/covers/{cover.pk}.png").status_code == 405


@pytest.mark.parametrize("fmt,ctype", [("JPEG", "image/jpeg"), ("PNG", "image/png"), ("WEBP", "image/webp")])
def test_upload_strips_metadata(event, fmt, ctype):
    exif = Image.Exif()
    exif[0x013B] = "Secret Author"  # Artist
    data = image_bytes(fmt, exif=exif.tobytes())
    assert b"Secret Author" in data
    content_type, encoded, _w, _h = covers.process(data)
    assert content_type == ctype
    assert b"Secret Author" not in encoded and b"Exif" not in encoded


@pytest.mark.parametrize(
    "data,name,ctype",
    [
        (b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', "x.svg", "image/svg+xml"),
        (b"\x89PNG\r\n\x1a\n" + b"not really a png" * 10, "x.png", "image/png"),
        (b"GIF89a" + b"\x00" * 50, "x.gif", "image/gif"),
        (b"<html><body>hi</body></html>", "x.png", "image/png"),
        (b"", "x.png", "image/png"),
    ],
)
def test_upload_rejects_svg_malformed_and_spoofed(django_user_model, event, data, name, ctype):
    editor = client_for(django_user_model, EDITOR)
    before = event.revision
    resp = editor.post(
        f"/studio/events/{event.pk}/", edit_post(event, hero_image_url="", cover_upload=upload(data, name, ctype))
    )
    assert resp.status_code == 400
    assert CoverImage.objects.count() == 0
    event.refresh_from_db()
    assert event.revision == before and event.cover_image_id is None


def test_upload_rejects_oversized_bytes_and_pixels(django_user_model, event, monkeypatch):
    editor = client_for(django_user_model, EDITOR)
    big = image_bytes() + b"\x00" * (covers.MAX_UPLOAD_BYTES + 10)
    resp = editor.post(f"/studio/events/{event.pk}/", edit_post(event, hero_image_url="", cover_upload=upload(big)))
    assert resp.status_code == 400
    with pytest.raises(Exception):
        covers.process(image_bytes(size=(covers.MAX_DIMENSION + 1, 20)))
    assert CoverImage.objects.count() == 0


def test_upload_and_url_together_is_ambiguous(django_user_model, event):
    editor = client_for(django_user_model, EDITOR)
    resp = editor.post(
        f"/studio/events/{event.pk}/",
        edit_post(event, hero_image_url="https://cdn.example.com/hero.png", cover_upload=upload(image_bytes())),
    )
    assert resp.status_code == 400
    assert b"Choose one" in resp.content
    assert CoverImage.objects.count() == 0


def test_no_orphan_cover_when_event_validation_fails(django_user_model, event):
    editor = client_for(django_user_model, EDITOR)
    resp = editor.post(
        f"/studio/events/{event.pk}/",
        edit_post(event, hero_image_url="", public_title="", cover_upload=upload(image_bytes())),
    )
    assert resp.status_code == 400
    assert CoverImage.objects.count() == 0


def test_remove_cover_then_url_is_allowed_and_url_rejected_while_cover_present(django_user_model, event):
    editor = client_for(django_user_model, EDITOR)
    editor.post(f"/studio/events/{event.pk}/", edit_post(event, hero_image_url="", cover_upload=upload(image_bytes())))
    event.refresh_from_db()
    old = event.cover_image
    assert old is not None
    blocked = editor.post(f"/studio/events/{event.pk}/", edit_post(event, hero_image_url="https://cdn.example.com/h.png"))
    assert blocked.status_code == 400
    event.refresh_from_db()
    resp = editor.post(f"/studio/events/{event.pk}/", edit_post(event, remove_cover="on"))
    assert resp.status_code == 302
    event.refresh_from_db()
    assert event.cover_image_id is None
    # Old revisions keep resolving: the cover row is immutable and kept.
    assert CoverImage.objects.filter(pk=old.pk).exists()


def test_upload_permission_denied_for_analyst(django_user_model, event):
    analyst = client_for(django_user_model, ANALYST)
    resp = analyst.post(f"/studio/events/{event.pk}/", edit_post(event, hero_image_url="", cover_upload=upload(image_bytes())))
    assert resp.status_code in (403, 302)
    assert CoverImage.objects.count() == 0


def test_upload_requires_csrf(django_user_model, event):
    editor = client_for(django_user_model, EDITOR, enforce_csrf_checks=True)
    resp = editor.post(f"/studio/events/{event.pk}/", edit_post(event, hero_image_url="", cover_upload=upload(image_bytes())))
    assert resp.status_code == 403
    assert CoverImage.objects.count() == 0


# ---------------------------------------------------------------------------
# Host wording and waiting-room discoverability
# ---------------------------------------------------------------------------


def _room_html(client, occ):
    from webinars import waiting_room

    waiting_room.configure(occ, enabled=True)
    occ.refresh_from_db()
    waiting_room.set_admission(occ, "hold")
    return client.get(f"/w/{occ.waiting_room_code}/").content.decode()


def test_waiting_room_host_wording_singular_and_plural(client, event):
    occ = make_occurrence(event)
    event.hosts.add(Host.objects.create(name="Maya", slug="maya"))
    assert "The host is holding the doors" in _room_html(client, occ)
    event.hosts.add(Host.objects.create(name="Lee", slug="lee"))
    assert "The hosts are holding the doors" in client.get(f"/w/{occ.waiting_room_code}/").content.decode()


def test_preview_host_heading_counts(django_user_model, event):
    editor = client_for(django_user_model, EDITOR)
    event.hosts.add(Host.objects.create(name="Maya", slug="maya"))
    assert "Your host</h2>" in editor.get(f"/studio/events/{event.pk}/preview").content.decode()
    event.hosts.add(Host.objects.create(name="Lee", slug="lee"))
    assert "Your hosts</h2>" in editor.get(f"/studio/events/{event.pk}/preview").content.decode()


@pytest.mark.parametrize("role", [ANALYST, EDITOR, ADMINISTRATOR])
def test_event_editor_links_waiting_room_without_leaking_private_urls(django_user_model, event, role):
    occ = make_occurrence(event)
    c = client_for(django_user_model, role)
    body = c.get(f"/studio/events/{event.pk}/").content.decode()
    assert "Waiting room &amp; admission" in body
    assert f"/studio/sessions/{occ.pk}/#room-title" in body
    assert JOIN_URL not in body and WAITING_URL not in body
    session = c.get(f"/studio/sessions/{occ.pk}/").content.decode()
    assert "Waiting room &amp; admission" in session
    if role == ANALYST:
        assert JOIN_URL not in session


def test_public_event_api_never_exposes_private_links(client, event):
    make_occurrence(event)
    body = client.get(f"/api/v1/events/{event.slug}").content.decode()
    assert JOIN_URL not in body and WAITING_URL not in body
    assert Event.objects.get(pk=event.pk).cover_image_id is None

"""Deleting hosts from the studio and the management API (Administrator / content_write only)."""
import pytest
from django.contrib.auth.models import Group
from django.test import Client

from tests.conftest import make_api_key, make_event, make_form
from tests.test_management_api import BASE, call
from webinars.models import AuditLog, Host

pytestmark = pytest.mark.django_db


def _host(slug="maya"):
    return Host.objects.create(name="Maya Okonkwo", slug=slug, title="Group PM")


def _client(django_user_model, role=None, superuser=False):
    if superuser:
        user = django_user_model.objects.create_superuser("root", "root@example.com", "pw-Quiet-Harbor-7")
    else:
        user = django_user_model.objects.create_user(role.lower(), f"{role}@example.com", "pw-Quiet-Harbor-7")
        user.groups.add(Group.objects.get(name=role))
    c = Client()
    c.force_login(user)
    return c


def test_studio_admin_deletes_unused_host(django_user_model):
    host = _host()
    admin = _client(django_user_model, "Administrator")
    page = admin.get(f"/studio/hosts/{host.slug}/")
    assert page.status_code == 200 and b"Delete host" in page.content
    resp = admin.post(f"/studio/hosts/{host.slug}/delete/", follow=True)
    assert resp.status_code == 200 and resp.redirect_chain[-1][0] == "/studio/hosts/"
    assert not Host.objects.filter(pk=host.pk).exists()
    assert AuditLog.objects.filter(action="host.deleted", object_id=str(host.pk)).exists()


def test_studio_delete_detaches_draft_events_only(django_user_model):
    host = _host()
    draft = make_event(make_form(), slug="draft-ev", visibility="draft")
    draft.hosts.add(host)
    admin = _client(django_user_model, "Administrator")
    assert admin.post(f"/studio/hosts/{host.slug}/delete/").status_code == 302
    draft.refresh_from_db()
    assert not Host.objects.filter(pk=host.pk).exists() and draft.hosts.count() == 0


@pytest.mark.parametrize("visibility", ["public", "unlisted"])
def test_studio_refuses_host_on_published_event(django_user_model, visibility):
    host = _host()
    live = make_event(make_form(), slug="live-ev", visibility=visibility)
    live.hosts.add(host)
    admin = _client(django_user_model, "Administrator")
    page = admin.get(f"/studio/hosts/{host.slug}/")
    assert b"1 published event" in page.content and b"Delete host</button>" not in page.content
    resp = admin.post(f"/studio/hosts/{host.slug}/delete/", follow=True)
    assert resp.redirect_chain[-1][0] == f"/studio/hosts/{host.slug}/"
    assert b"published event" in resp.content
    assert Host.objects.filter(pk=host.pk).exists() and live.hosts.count() == 1


@pytest.mark.parametrize("role", ["Editor", "Analyst"])
def test_studio_other_roles_cannot_delete(django_user_model, role):
    host = _host()
    c = _client(django_user_model, role)
    page = c.get(f"/studio/hosts/{host.slug}/")
    assert page.status_code == 200 and b"Delete host" not in page.content
    assert c.post(f"/studio/hosts/{host.slug}/delete/").status_code == 403
    assert Host.objects.filter(pk=host.pk).exists()


def test_studio_delete_is_post_only(django_user_model):
    host = _host()
    admin = _client(django_user_model, "Administrator")
    assert admin.get(f"/studio/hosts/{host.slug}/delete/").status_code == 405
    assert Host.objects.filter(pk=host.pk).exists()


def test_api_delete_host(client):
    key = make_api_key(("content_write",), name="cms")
    host = _host()
    live = make_event(make_form(), slug="live-ev")
    live.hosts.add(host)
    resp = call(client, "DELETE", f"{BASE}/hosts/{host.slug}", key)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "conflict"
    live.hosts.remove(host)
    assert call(client, "DELETE", f"{BASE}/hosts/{host.slug}", key).status_code == 204
    assert not Host.objects.filter(pk=host.pk).exists()
    assert call(client, "DELETE", f"{BASE}/hosts/{host.slug}", key).status_code == 404
    assert AuditLog.objects.filter(action="host.deleted", object_id=str(host.pk)).exists()


def test_api_delete_host_needs_content_scope(client):
    host = _host()
    assert call(client, "DELETE", f"{BASE}/hosts/{host.slug}", make_api_key(("attendance_write",), name="a")).status_code == 403
    assert call(client, "DELETE", f"{BASE}/hosts/{host.slug}", None).status_code == 401
    assert Host.objects.filter(pk=host.pk).exists()

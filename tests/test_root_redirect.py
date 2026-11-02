import pytest
from django.contrib.auth import get_user_model

pytestmark = pytest.mark.django_db


def test_bare_domain_sends_signed_out_visitors_to_studio_sign_in(client):
    first = client.get("/")
    assert first.status_code == 302 and first["Location"] == "/studio/"
    assert client.get("/studio/")["Location"].startswith("/studio/login/")


def test_bare_domain_takes_someone_signed_in_to_the_studio(client):
    user = get_user_model().objects.create_superuser("owner", "owner@example.com", "x-long-password-1")
    client.force_login(user)
    assert client.get("/", follow=True).redirect_chain[0] == ("/studio/", 302)
    assert client.get("/", follow=True).status_code == 200


def test_bare_domain_refuses_posts(client):
    assert client.post("/").status_code == 405

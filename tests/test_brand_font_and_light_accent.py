import pytest
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile

from webinars import brand

pytestmark = pytest.mark.django_db

WOFF2 = b"wOF2" + b"\x00" * 60


@pytest.fixture(autouse=True)
def fresh_cache():
    cache.clear()
    yield
    cache.clear()


def test_a_light_brand_colour_is_accepted_with_dark_button_text_and_a_readable_text_shade(admin_client, client):
    assert admin_client.post("/studio/brand/", {"name": "Acme Events", "accent": "#ff6b4a"}).status_code == 200
    css = client.get("/brand/brand.css").content.decode()
    assert "--coral:#ff6b4a" in css and f"--on-accent:{brand.DARK_TEXT}" in css
    dark = css.split("--coral-dark:")[1].split(";")[0]
    assert brand._contrast(dark, "#ffffff") >= 4.5


def test_a_dark_brand_colour_keeps_white_button_text(admin_client, client):
    admin_client.post("/studio/brand/", {"name": "A", "accent": "#9e321b"})
    assert "--on-accent:#ffffff" in client.get("/brand/brand.css").content.decode()


def test_a_font_is_served_as_a_font_and_used_everywhere(admin_client, client):
    resp = admin_client.post("/studio/brand/", {"name": "A", "accent": "#9e321b", "font": SimpleUploadedFile("brand.woff2", WOFF2, "font/woff2")})
    assert resp.status_code == 200
    served = client.get("/brand/font?v=1")
    assert served.status_code == 200 and served["Content-Type"] == "font/woff2" and served.content == WOFF2
    assert served["X-Content-Type-Options"] == "nosniff" and "sandbox" in served["Content-Security-Policy"]
    css = client.get("/brand/brand.css").content.decode()
    assert "@font-face" in css and "--ui:\"Brand Font\"" in css and "--font-ui:\"Brand Font\"" in css
    # Pages link the stylesheet with a version that changes with the font, so browsers fetch the new one.
    page = client.get("/w/nope0000/").content.decode()
    assert "/brand/brand.css?v=" in page
    admin_client.post("/studio/brand/", {"name": "A", "accent": "#9e321b", "remove_font": "1"})
    assert client.get("/brand/font").status_code == 404 and "@font-face" not in client.get("/brand/brand.css").content.decode()


def test_a_font_is_refused_when_it_isnt_one_or_is_too_big(admin_client):
    for data in (b"<svg onload=alert(1)>", b"wOF2" + b"\x00" * (brand.MAX_FONT_BYTES + 1)):
        resp = admin_client.post("/studio/brand/", {"font": SimpleUploadedFile("x.woff2", data, "font/woff2")})
        assert resp.status_code == 400 and b'id="brand-font-err"' in resp.content

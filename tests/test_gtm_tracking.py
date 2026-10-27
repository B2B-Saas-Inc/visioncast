"""Studio "Tracking & GTM": site-wide GTM install tag (route, RBAC, validation, secrets, determinism)."""
import html
import json
from pathlib import Path

import pytest
from django.contrib.auth.models import Group
from django.test import Client

from tests.conftest import JOIN_URL, WAITING_URL, make_api_key, make_event, make_form, make_occurrence
from webinars.roles import ADMINISTRATOR, ANALYST, EDITOR
from webinars.studio import views_tracking as vt

pytestmark = pytest.mark.django_db

PASSWORD = "studio-pass-Quiet-Harbor-7"
URL = "/studio/tracking/"
GOLDEN = Path(__file__).resolve().parents[1] / "sdk" / "test" / "fixtures" / "gtm-tag.html"
GOLDEN_PARAMS = {"script_src": "https://www.example.com/assets/webinar-form.js"}


def client_for(django_user_model, role=None, superuser=False):
    name = "root" if superuser else f"u-{role.lower()}"
    if superuser:
        user = django_user_model.objects.create_superuser(name, "root@example.com", PASSWORD)
    else:
        user = django_user_model.objects.create_user(name, f"{name}@example.com", PASSWORD)
        user.groups.add(Group.objects.get(name=role))
    c = Client()
    c.force_login(user)
    return c


def _block(response, element_id):
    body = response.content.decode()
    marker = f'<code id="{element_id}">'
    start = body.index(marker) + len(marker)
    return html.unescape(body[start:body.index("</code>", start)])


def _snippet(response):
    return _block(response, "gtm-snippet")


# --- route and RBAC ---------------------------------------------------------


def test_anonymous_redirected_to_login():
    resp = Client().get(URL)
    assert resp.status_code == 302
    assert "/studio/login/" in resp["Location"]


@pytest.mark.parametrize("role", [ANALYST, EDITOR])
def test_non_settings_roles_forbidden_and_unlisted(django_user_model, role):
    c = client_for(django_user_model, role)
    assert c.get(URL).status_code == 403
    assert "/studio/tracking/" not in c.get("/studio/").content.decode()


def test_administrator_and_superuser_can_open(django_user_model):
    for c in (client_for(django_user_model, ADMINISTRATOR), client_for(django_user_model, superuser=True)):
        resp = c.get(URL)
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "Tracking &amp; GTM" in body
        assert "Nothing is installed automatically" in body
        assert "PROXY_API_KEY" in body
        assert "Embed on your site" in body
        # Review items: functional vs consent-gated copy, referrer, SPA after-render, Unlimited, CSP, embed conflict.
        assert "functional, not consent-gated" in body
        assert "of the page and of the referrer" in body
        assert "after</strong> the view containing the form has rendered" in body
        assert "<em>Unlimited</em>" in body
        assert "Content Security Policy" in body
        assert "the proxy path set here is ignored" in body
        assert "not</strong> an automatic integration" in body


def test_navigation_lists_page_for_administrator(django_user_model):
    body = client_for(django_user_model, ADMINISTRATOR).get("/studio/").content.decode()
    assert 'href="/studio/tracking/"' in body


def test_post_not_allowed_nothing_is_persisted(django_user_model):
    c = client_for(django_user_model, ADMINISTRATOR)
    resp = c.post(URL, {"proxy_base": "/x"})
    assert resp.status_code == 405
    assert "GET" in resp["Allow"]
    assert '"proxyBase": "/x"' not in _snippet(c.get(URL))


# --- the generated tag -----------------------------------------------------


def test_default_tag_uses_platform_script_default_proxy_and_consent_key(django_user_model):
    resp = client_for(django_user_model, ADMINISTRATOR).get(URL)
    tag = _snippet(resp)
    assert tag == vt.build_tag(vt.defaults())
    assert f'"src": "{vt.sdk_script_url()}"' in tag
    assert '"proxyBase": "/webinar-api"' in tag
    assert '"consentKey": "webinarConsent"' in tag
    assert "{{" not in tag and "}}" not in tag, "no GTM variable substitution points"
    assert tag.count("<script") == 1 and tag.count("</script") == 1
    assert "webinarFormGtmInstall(window, document," in tag
    for forbidden in ("eval(", "new Function", "innerHTML", "document.write"):
        assert forbidden not in tag
    # The inlined helper is byte-identical to the public static file's marked body.
    assert vt.loader_body() in tag
    static_text = vt.LOADER_PATH.read_text(encoding="utf-8")
    assert vt.loader_body() in static_text


def test_tag_matches_golden_fixture_shared_with_node_and_browser_tests(django_user_model):
    resp = client_for(django_user_model, ADMINISTRATOR).get(URL, GOLDEN_PARAMS)
    assert resp.status_code == 200
    assert _snippet(resp) == GOLDEN.read_text(encoding="utf-8").rstrip("\n")


def test_deterministic_for_identical_inputs(django_user_model):
    c = client_for(django_user_model, ADMINISTRATOR)
    params = {**GOLDEN_PARAMS, "proxy_base": "/forms/api/", "consent_key": "cmp_state", "consent_mode": "cmp"}
    first, second = _snippet(c.get(URL, params)), _snippet(c.get(URL, params))
    assert first == second
    assert '"proxyBase": "/forms/api"' in first  # trailing slash dropped, as the SDK does
    assert '"consentKey": "cmp_state"' in first


def test_always_denied_mode_writes_null_key(django_user_model):
    resp = client_for(django_user_model, ADMINISTRATOR).get(URL, {"consent_mode": "denied", "consent_key": "anything"})
    tag = _snippet(resp)
    assert '"consentKey": null' in tag
    assert "anything" not in tag
    assert "{{" not in tag
    assert 'id="gtm-bridge"' not in resp.content.decode(), "no CMP bridge example when consent is always denied"


def test_bridge_example_uses_literal_values_and_the_configured_key(django_user_model):
    c = client_for(django_user_model, ADMINISTRATOR)
    resp = c.get(URL, {"consent_key": "siteConsent"})
    example = _block(resp, "gtm-bridge")
    assert example == vt.build_bridge_example({**vt.defaults(), "consent_key": "siteConsent"})
    assert "siteConsent: { analytics: 'granted', advertising: 'denied' }" in example
    assert 'event: "webinar_consent_update"' in example
    assert "{{" not in example
    assert example.count("<script") == 1 and example.count("</script") == 1
    assert "Replace 'granted' / 'denied'" in html.unescape(resp.content.decode())


def test_tag_and_page_contain_no_secrets_or_private_links(django_user_model):
    make_api_key()
    occ = make_occurrence(make_event(make_form(version="gtm-v1")))
    resp = client_for(django_user_model, ADMINISTRATOR).get(URL)
    body = resp.content.decode()
    assert "wbk_" not in body
    assert JOIN_URL not in body and WAITING_URL not in body
    assert str(occ.pk) not in _snippet(resp)
    assert "@" not in _snippet(resp)


# --- validation ------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "javascript:alert(1)",
        "data:text/javascript,alert(1)",
        "http://cdn.example.com/webinar-form.js",
        "https://user:pw@cdn.example.com/webinar-form.js",
        "https://cdn.example.com@evil.example/webinar-form.js",
        "https://cdn.example.com/webinar-form.js?x=</script>",
        "https://cdn.example.com/webinar-form.js#frag",
        "https://cdn.example.com/../webinar-form.js",
        "https://cdn.example.com/js//webinar-form.js",
        "https://cdn.example.com",
        "//cdn.example.com/webinar-form.js",
        "/\\evil.example/webinar-form.js",
        "/assets/../webinar-form.js",
        "/assets//webinar-form.js",
        "/assets/webinar-form.js//",
        "///evil.example/webinar-form.js",
        "/assets/webinar form.js",
        "/assets/<script>.js",
        "/assets/<!--.js",
        "https://cdn.example.com/webinar-form.js</script><script>alert(1)</script>",
        "x" * 600,
        "",
    ],
)
def test_script_src_rejected(django_user_model, bad):
    with pytest.raises(ValueError):
        vt.validate_script_src(bad)
    resp = client_for(django_user_model, ADMINISTRATOR).get(URL, {"script_src": bad})
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "Please fix" in body
    tag = _snippet(resp)
    assert f'"src": "{vt.sdk_script_url()}"' in tag, "falls back to the platform script"
    assert "javascript:" not in tag and "evil.example" not in tag
    assert tag.count("</script") == 1
    assert 'id="custom-host-notice"' not in body, "fallback to the platform host shows no custom-host notice"


@pytest.mark.parametrize(
    "good",
    [
        "/assets/webinar-form.js",
        "/static/webinar-sdk/webinar-form.js",
        "https://cdn.example.com/js/webinar-form.js",
        "https://www.example.com:8443/webinar-form.js",
        "http://localhost:8000/static/webinar-sdk/webinar-form.js",
    ],
)
def test_script_src_accepted(good):
    assert vt.validate_script_src(good) == good


def test_custom_script_host_is_called_out_visibly(django_user_model):
    c = client_for(django_user_model, ADMINISTRATOR)
    body = c.get(URL, {"script_src": "https://cdn.example.com/js/webinar-form.js"}).content.decode()
    assert 'id="custom-host-notice"' in body
    assert "loads the script from cdn.example.com, not from this platform" in body
    assert "Only use a host you control" in body
    body = c.get(URL, {"script_src": "/assets/webinar-form.js"}).content.decode()
    assert "loads the script from this site, not from this platform" in body
    body = c.get(URL).content.decode()
    assert 'id="custom-host-notice"' not in body
    body = c.get(URL, {"script_src": vt.sdk_script_url()}).content.decode()
    assert 'id="custom-host-notice"' not in body


@pytest.mark.parametrize(
    "bad",
    ["", "webinar-api", "//evil.example", "/\\evil", "/webinar-api/../x", "/a/./b", "/a//b", "https://x.example/p", "/api?x=1", "/" + "a" * 200],
)
def test_proxy_base_rejected(django_user_model, bad):
    with pytest.raises(ValueError):
        vt.validate_proxy_base(bad)
    tag = _snippet(client_for(django_user_model, ADMINISTRATOR).get(URL, {"proxy_base": bad}))
    assert '"proxyBase": "/webinar-api"' in tag


@pytest.mark.parametrize(
    "bad",
    ["", "{{x}}", 'a"b', "a</script>", "1abc", "a-b", "a b", "a.b", "x" * 65, "a\nb", "__proto__", "constructor", "prototype", "hasOwnProperty", "event", "wébinar"],
)
def test_consent_keys_rejected(django_user_model, bad):
    with pytest.raises(ValueError):
        vt.validate_consent_key(bad)
    tag = _snippet(client_for(django_user_model, ADMINISTRATOR).get(URL, {"consent_key": bad}))
    assert '"consentKey": "webinarConsent"' in tag
    assert tag.count("</script") == 1
    assert "<script>alert" not in tag


@pytest.mark.parametrize("good", ["webinarConsent", "consent_state", "_cmp", "x" * 64, "Consent2"])
def test_consent_keys_accepted(good):
    assert vt.validate_consent_key(good) == good


def test_script_json_never_closes_the_script_element():
    out = vt.script_json({"a": "</script><!--", "b": "  é"})
    assert "</" not in out and "<!--" not in out
    assert " " not in out and "é" not in out
    assert out == '{"a": "<\\/script><\\u0021--", "b": "\\u2028\\u2029\\u00e9"}'
    assert json.loads(out) == {"a": "</script><!--", "b": "  é"}, "still valid JSON after escaping"


def test_unknown_consent_mode_rejected(django_user_model):
    resp = client_for(django_user_model, ADMINISTRATOR).get(URL, {"consent_mode": "granted"})
    assert "Choose one of the listed options" in resp.content.decode()
    assert '"consentKey": "webinarConsent"' in _snippet(resp)


def test_values_are_escaped_in_the_page(django_user_model):
    payload = '"><img src=x onerror=alert(1)>'
    resp = client_for(django_user_model, ADMINISTRATOR).get(URL, {"consent_key": payload, "proxy_base": payload, "script_src": payload})
    body = resp.content.decode()
    # Invalid values are never echoed (fields fall back to defaults), so neither a raw nor an
    # escaped copy of the payload may appear; the three error summaries must.
    assert "<img src=x" not in body
    assert "onerror=alert(1)" not in body
    assert "these 3 problems" in body
    for field in ("f-consent_key", "f-proxy_base", "f-script_src"):
        assert f'href="#{field}"' in body


def test_session_embed_page_links_here(django_user_model):
    occ = make_occurrence(make_event(make_form(version="gtm-v2")))
    body = client_for(django_user_model, ADMINISTRATOR).get(f"/studio/sessions/{occ.pk}/embed/").content.decode()
    assert 'href="/studio/tracking/"' in body

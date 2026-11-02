"""This deployment's brand: name, colour, logo and font (docs/branding.md).

Set in the studio under Brand, or first from ``BRAND_NAME`` and ``BRAND_ACCENT`` in the environment.
Saving passes the colour, logo and font on to any live server connected here, so the rooms match. Logos
are decoded and re-encoded as PNG, so nothing but pixels is ever served from this origin. A font is
checked by its first bytes and served as a font, never as anything a browser would run.
"""
import hashlib
import io
import logging
import os
import re

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import DatabaseError
from django.http import Http404, HttpResponse
from django.templatetags.static import static
from django.urls import reverse
from PIL import Image

from webinars.models import HostingAccount, StudioBrand

logger = logging.getLogger(__name__)

DEFAULT_NAME = "VisionCast"
MAX_LOGO_BYTES = 2 * 1024 * 1024
MAX_LOGO_SIDE = 1024
MAX_LOGO_PIXELS = 16_000_000
MAX_FONT_BYTES = 2 * 1024 * 1024
# Text on the accent: white, or this when white would be hard to read (GrowthCast-style coral).
DARK_TEXT = "#1f1d1a"
CACHE_KEY = "studio-brand"
_COLOUR = re.compile(r"^#[0-9a-fA-F]{6}$")


def _env_accent():
    value = os.environ.get("BRAND_ACCENT", "")
    try:
        return valid_accent(value)
    except ValidationError:
        return ""


def current():
    """``{name, accent, logoVersion}``, cached for a moment: it's on every page. If the database
    can't be read, the page still renders, with the defaults."""
    found = cache.get(CACHE_KEY)
    if found is not None:
        return found
    try:
        row = StudioBrand.objects.defer("logo").order_by("pk").first()
    except DatabaseError:
        row = None
    found = {
        "name": (row.name if row else "") or os.environ.get("BRAND_NAME", "") or DEFAULT_NAME,
        "accent": (row.accent if row else "") or _env_accent(),
        "logoVersion": row.logo_version if row else "",
        "fontVersion": row.font_version if row else "",
    }
    cache.set(CACHE_KEY, found, 30)
    return found


def context(request):
    """Template context: ``brand.name``, ``brand.logo`` and, when a colour is set, ``brand.css``."""
    now = current()
    version = hashlib.sha256(f"{now['accent']}|{now.get('fontVersion', '')}".encode()).hexdigest()[:12]
    return {"brand": {
        "name": now["name"],
        "logo": reverse("brand-logo") + f"?v={now['logoVersion']}" if now["logoVersion"] else static("brand/visioncast-mark.svg"),
        "css": reverse("brand-css") + f"?v={version}" if now["accent"] or now.get("fontVersion") else "",
        "custom_logo": bool(now["logoVersion"]),
        "custom_font": bool(now.get("fontVersion")),
    }}


def _luminance(hex_colour):
    def channel(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _mix(hex_colour, other, share):
    parts = [int(hex_colour[i:i + 2], 16) for i in (1, 3, 5)]
    to = [int(other[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(a + (b - a) * share):02x}" for a, b in zip(parts, to))


def _contrast(a, b):
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def on_accent(accent):
    """Text on the accent: white when it reads well, dark otherwise, so light brand colours work."""
    return "#ffffff" if _contrast(accent, "#ffffff") >= 4.5 or _contrast(accent, "#ffffff") >= _contrast(accent, DARK_TEXT) else DARK_TEXT


def text_accent(accent):
    """The accent darkened until it reads as text on white (links, eyebrows, badges)."""
    colour = accent
    for _ in range(20):
        if _contrast(colour, "#ffffff") >= 4.5:
            return colour
        colour = _mix(colour, "#000000", 0.1)
    return colour


def valid_accent(value):
    """Any colour works: button text turns dark on light colours, and text uses a darker shade."""
    if not value:
        return ""
    if not _COLOUR.match(value):
        raise ValidationError({"accent": ["Use a colour like #c2462e."]})
    return value.lower()


def font_mime(data):
    """What a font file really is, from its first bytes, or None."""
    head = bytes(data[:4])
    return {b"wOF2": "font/woff2", b"wOFF": "font/woff", b"OTTO": "font/otf", b"\x00\x01\x00\x00": "font/ttf", b"true": "font/ttf"}.get(head)


def clean_font(data):
    if len(data) > MAX_FONT_BYTES:
        raise ValidationError({"font": ["The font is larger than 2 MB."]})
    if font_mime(data) is None:
        raise ValidationError({"font": ["Use a WOFF2, WOFF, TTF or OTF font file."]})
    return bytes(data)


def clean_logo(data):
    if len(data) > MAX_LOGO_BYTES:
        raise ValidationError({"logo": ["The logo is larger than 2 MB."]})
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValidationError({"logo": ["The logo has to be a PNG."]})
    try:
        image = Image.open(io.BytesIO(data))
        # The header says how big it is. Checked before it's decoded, so a small file can't fill memory.
        if image.size[0] * image.size[1] > MAX_LOGO_PIXELS:
            raise ValueError("too big")
        image.load()
    except Exception:
        raise ValidationError({"logo": ["That file couldn't be read as an image, or it's over 16 megapixels."]})
    image = image.convert("RGBA")
    # Live servers take at most 2 MB, and rebuilding a logo can make it bigger.
    for side in (MAX_LOGO_SIDE, 512, 256):
        image.thumbnail((side, side))
        out = io.BytesIO()
        image.save(out, format="PNG", optimize=True)
        if out.tell() <= MAX_LOGO_BYTES:
            return out.getvalue()
    raise ValidationError({"logo": ["The logo is too detailed. Try a simpler one."]})


def save(*, name, accent, logo=None, remove_logo=False, font=None, remove_font=False, user=None, transport=None):
    """Saves the brand and passes it on to connected live servers. Returns warnings to show."""
    from webinars.audit import audit

    name = (name or "").strip()[:80]
    accent = valid_accent((accent or "").strip())
    cleaned = clean_logo(logo) if logo else None
    font = clean_font(font) if font else None
    row = StudioBrand.objects.order_by("pk").first() or StudioBrand()
    row.name, row.accent = name, accent
    if cleaned:
        row.logo = cleaned
        row.logo_version = hashlib.sha256(cleaned).hexdigest()[:12]
    elif remove_logo:
        row.logo = None
        row.logo_version = ""
    if font:
        row.font = font
        row.font_version = hashlib.sha256(font).hexdigest()[:12]
    elif remove_font:
        row.font = None
        row.font_version = ""
    row.save()
    cache.delete(CACHE_KEY)
    audit("studio.brand_changed", user=user)
    return _push(accent, cleaned, remove_logo, transport, font=font, remove_font=remove_font, has_font=bool(row.font_version))


def _push(accent, logo, remove_logo, transport, *, font=None, remove_font=False, has_font=False):
    from webinars.hosting import base, service

    warnings = []
    for account in HostingAccount.objects.filter(provider="livekit", enabled=True):
        try:
            current_theme = service.live_brand(account, transport=transport)
            theme = {k: current_theme.get(k, "") for k in ("accent", "stage", "mode", "font")}
            theme["accent"] = accent
            if has_font:
                theme["font"] = "custom"
            elif remove_font and theme.get("font") == "custom":
                theme["font"] = ""
            service.update_live_brand(account, theme, transport=transport)
            adapter = service.adapter_for(account)
            secrets = service.load_secrets(account)
            if logo or remove_logo:
                outcome = adapter.put_brand_logo(account.config or {}, secrets, logo, transport=transport or base.default_transport)
                if outcome.kind != "succeeded":
                    raise ValidationError(outcome.error)
            if font or remove_font:
                outcome = adapter.put_brand_font(account.config or {}, secrets, font, transport=transport or base.default_transport)
                if outcome.kind != "succeeded":
                    raise ValidationError(outcome.error)
        except ValidationError:
            warnings.append(f"Saved here, but the live server {account.name} didn't take it. Save again once it's reachable.")
    return warnings


def logo_response(request):
    row = StudioBrand.objects.order_by("pk").first()
    if row is None or not row.logo:
        raise Http404
    response = HttpResponse(bytes(row.logo), content_type="image/png")
    response["Cache-Control"] = "public, max-age=86400" if request.GET.get("v") else "public, max-age=60"
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def font_response(request):
    row = StudioBrand.objects.order_by("pk").first()
    if row is None or not row.font:
        raise Http404
    data = bytes(row.font)
    response = HttpResponse(data, content_type=font_mime(data) or "application/octet-stream")
    response["Cache-Control"] = "public, max-age=86400" if request.GET.get("v") else "public, max-age=60"
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def css_response(request):
    # Straight from the database: this answer is cached for a day, so it can't be a stale one.
    row = StudioBrand.objects.defer("logo", "font").order_by("pk").first()
    accent = (row.accent if row else "") or _env_accent()
    body = ""
    if accent:
        # The darker shade carries text and hover states, so a light brand colour still reads on white.
        dark = _mix(accent, "#000000", 0.15)
        if _contrast(dark, "#ffffff") < 4.5:
            dark = text_accent(accent)
        body = (":root{"
                f"--coral-600:{accent};--coral-700:{dark};--coral-500:{_mix(accent, '#ffffff', 0.12)};"
                f"--coral-100:{_mix(accent, '#ffffff', 0.8)};--coral-50:{_mix(accent, '#ffffff', 0.93)};"
                f"--coral:{accent};--coral-dark:{dark};--coral-soft:{_mix(accent, '#ffffff', 0.93)};"
                f"--on-accent:{on_accent(accent)};--accent-hover:{_mix(accent, '#000000', 0.1)};"
                "}")
    if row is not None and row.font_version:
        # One file, used at every weight: a variable font covers them all.
        family = '"Brand Font"'
        body += ("@font-face{font-family:\"Brand Font\";"
                 f"src:url(\"{reverse('brand-font')}?v={row.font_version}\");font-weight:100 900;font-display:swap}}"
                 f":root{{--ui:{family},system-ui,sans-serif;--display:{family},system-ui,sans-serif;"
                 f"--font-ui:{family},system-ui,sans-serif;--font-display:{family},system-ui,sans-serif}}")
    response = HttpResponse(body, content_type="text/css")
    response["Cache-Control"] = "public, max-age=86400" if request.GET.get("v") else "public, max-age=60"
    response["X-Content-Type-Options"] = "nosniff"
    return response

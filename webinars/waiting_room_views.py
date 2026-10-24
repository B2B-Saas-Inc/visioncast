"""Public hosted waiting room views (/w/<code>/...). Logic lives in webinars.waiting_room.

Pages are rendered by this app, so POSTs use Django's CSRF tokens. Every response is
``no-store``. The provider join link appears only in the Location header of the
303 returned by ``room_enter`` for an admitted registrant.
"""
import secrets

from django.conf import settings
from django.http import Http404, HttpResponse, HttpResponseRedirect, JsonResponse
from django.template.response import TemplateResponse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from webinars import identity, ratelimit
from webinars import waiting_room as wr
from webinars.forms_schema import public_schema
from webinars.mail import delivery_mode

TEMPLATE = "waiting_room/room.html"


def _no_store(response):
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "same-origin"
    return response


def _occ_or_404(code):
    occ = wr.occurrence_for_code(code)
    if occ is None:
        raise Http404("Waiting room not found")
    return occ


def _room_redirect(occ, step=None):
    path = wr.room_path(occ)
    return _no_store(HttpResponseRedirect(path + (f"?step={step}" if step else ""), status=303))


def _too_many(request, occ=None):
    response = TemplateResponse(request, "waiting_room/message.html", {
        "occ": occ, "title": "Please wait a moment",
        "message": "Too many attempts from this connection. Wait a few minutes and try again.",
    }, status=429)
    return _no_store(response)


def _pending_cookie(response, token):
    response.set_cookie(
        wr.PENDING_COOKIE, token, max_age=wr.PENDING_COOKIE_MAX_AGE, path="/w/",
        secure=bool(getattr(settings, "CAPABILITY_COOKIE_SECURE", True)), httponly=True, samesite="Lax",
    )


def _fields_for(occ, cap):
    schema = public_schema(occ.event.form_version)
    missing = set(wr.missing_field_keys(occ, cap))
    return schema, [f for f in schema["fields"] if f["key"] in missing]


def _render(request, occ, *, step=None, errors=None, values=None, status=200, email_error=None):
    cap = identity.resolve_capability(request)
    payload = wr.status_payload(occ, cap)
    view = "unknown"
    fields, schema = [], None
    if cap is not None:
        if payload["viewer"]["registered"]:
            view = "registered"
        else:
            view = "complete"
            schema, fields = _fields_for(occ, cap)
    elif step == "code" and request.COOKIES.get(wr.PENDING_COOKIE):
        view = "code"
    ctx = {
        "occ": occ,
        "event": occ.event,
        "status": payload,
        "view": view,
        "fields": fields,
        "sms": schema["sms"] if schema else None,
        "errors": errors or {},
        "values": values or {},
        "email_error": email_error,
        "idempotency_key": "wr-" + secrets.token_hex(16),
        "email_disabled": delivery_mode() == "disabled",
        "poll_seconds": payload["pollAfterSeconds"],
        "hosts": list(occ.event.hosts.all()),
    }
    return _no_store(TemplateResponse(request, TEMPLATE, ctx, status=status))


@require_GET
def room(request, code):
    occ = _occ_or_404(code)
    return _render(request, occ, step=request.GET.get("step"))


@require_GET
def room_status(request, code):
    occ = _occ_or_404(code)
    if not ratelimit.hit(request, "wr_status"):
        return _no_store(JsonResponse({"error": {"code": "rate_limited", "message": "Slow down."}}, status=429))
    return _no_store(JsonResponse(wr.status_payload(occ, identity.resolve_capability(request))))


@require_POST
def room_identify(request, code):
    occ = _occ_or_404(code)
    if not ratelimit.hit(request, "wr_identify"):
        return _too_many(request, occ)
    token = request.COOKIES.get(wr.PENDING_COOKIE) or wr.new_pending_token()
    from django.core.exceptions import ValidationError

    try:
        wr.request_verification(occ, request.POST.get("email", ""), token)
    except ValidationError as exc:
        message = exc.message_dict.get("email", ["Enter a valid email address."])[0]
        return _render(request, occ, email_error=message, values={"email": request.POST.get("email", "")}, status=400)
    response = _room_redirect(occ, "code")
    _pending_cookie(response, token)
    return response


@require_POST
def room_verify(request, code):
    occ = _occ_or_404(code)
    if not ratelimit.hit(request, "wr_verify"):
        return _too_many(request, occ)
    try:
        verified = wr.verify_code(occ, request.COOKIES.get(wr.PENDING_COOKIE), request.POST.get("code", ""))
    except wr.VerificationFailed as exc:
        return _render(request, occ, step="code", errors={"code": exc.message}, status=400)
    response = _room_redirect(occ)
    identity.set_capability_cookie(response, verified.token)
    response.delete_cookie(wr.PENDING_COOKIE, path="/w/")
    return response


@require_http_methods(["GET", "POST"])
def room_verify_link(request, code, token):
    """Emailed one-time link. GET only shows a confirm page; the CSRF-protected POST spends it.

    In the browser that asked for the code (matching pending cookie) the page is a
    plain Continue. In any other browser the link may have been planted by someone
    else (login CSRF: the attacker's own address, so the victim's later input lands on
    the attacker's record); the page shows the address being verified and asks
    "This is me, continue" or "Not me". Nothing is submitted automatically.
    """
    occ = _occ_or_404(code)
    pending = request.COOKIES.get(wr.PENDING_COOKIE)
    if request.method == "GET":
        check = wr.inspect_link(occ, token, pending)
        ctx = {
            "occ": occ,
            "event": occ.event,
            "usable": check.usable,
            "same_browser": check.same_browser,
            "email": check.email if check.usable else "",
        }
        return _no_store(TemplateResponse(request, "waiting_room/confirm.html", ctx, status=200 if check.usable else 410))
    if not ratelimit.hit(request, "wr_verify"):
        return _too_many(request, occ)
    same_browser = wr.inspect_link(occ, token, pending).same_browser
    try:
        verified = wr.verify_link(occ, token)
    except wr.VerificationFailed as exc:
        return _no_store(TemplateResponse(request, "waiting_room/confirm.html",
                                          {"occ": occ, "event": occ.event, "usable": False, "message": exc.message}, status=410))
    response = _room_redirect(occ)
    identity.set_capability_cookie(response, verified.token)
    if same_browser:
        response.delete_cookie(wr.PENDING_COOKIE, path="/w/")
    return response


@require_POST
def room_register(request, code):
    occ = _occ_or_404(code)
    if not ratelimit.hit(request, "wr_register"):
        return _too_many(request, occ)
    cap = identity.resolve_capability(request)
    if cap is None:
        return _room_redirect(occ)
    _schema, fields = _fields_for(occ, cap)
    profile, custom, values = {}, {}, {}
    for f in fields:
        if f["type"] == "checkbox":
            raw = request.POST.get(f["key"]) == "on"
            values[f["key"]] = raw
            if raw:
                custom[f["key"]] = True
            continue
        raw = (request.POST.get(f["key"]) or "").strip()
        values[f["key"]] = raw
        if not raw:
            continue
        if f["custom"]:
            custom[f["key"]] = raw
        else:
            profile[f["key"]] = raw
    consents = {}
    form = occ.event.form_version
    if form.sms_disclosure_version and any(f["key"] == "phone" for f in fields):
        consents["sms"] = {"granted": request.POST.get("sms_consent") == "on", "disclosureVersion": form.sms_disclosure_version}
    key = request.POST.get("idempotency_key") or ("wr-" + secrets.token_hex(16))
    result = wr.register_in_room(occ, cap, profile=profile, custom_fields=custom, consents=consents, idempotency_key=key)
    if result.status_code in (200, 201):
        return _room_redirect(occ)
    body = result.body or {}
    errors = dict(body.get("fieldErrors") or {})
    if not errors:
        errors["_"] = (body.get("error") or {}).get("message", "We couldn't save that. Try again.")
    return _render(request, occ, errors=errors, values=values, status=result.status_code)


@require_POST
def room_enter(request, code):
    occ = _occ_or_404(code)
    if not ratelimit.hit(request, "wr_enter"):
        return _too_many(request, occ)
    admission = wr.admit(occ, identity.resolve_capability(request))
    if not admission.admitted:
        return _room_redirect(occ)
    response = HttpResponse(status=303)
    response["Location"] = admission.join_url
    response = _no_store(response)
    response["Referrer-Policy"] = "no-referrer"
    return response


@require_POST
def room_reset(request, code):
    occ = _occ_or_404(code)
    cap = identity.resolve_capability(request)
    if cap is not None:
        identity.revoke_capability(cap, "user_reset")
    response = _room_redirect(occ)
    identity.clear_capability_cookie(response)
    response.delete_cookie(wr.PENDING_COOKIE, path="/w/")
    return response

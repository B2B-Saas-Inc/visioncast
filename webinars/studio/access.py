"""Studio authentication, authorization and shared page context.

* Anonymous users are redirected to ``/studio/login/``.
* A *studio user* is an active user who is a superuser or belongs to one of the
  role groups (Administrator, Editor, Analyst). Anyone else gets 403.
* ``studio_view(perm=..., write_perm=...)``: ``perm`` is required for every method;
  ``write_perm`` additionally for POST (so Analysts get read-only pages). Every POST
  is protected by Django's CSRF middleware (views are never ``csrf_exempt``).
* ``superuser_only`` guards team management (roles.py: only superusers manage users).
"""
from functools import wraps

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.template.response import TemplateResponse

from webinars.roles import ADMINISTRATOR, ANALYST, EDITOR

ROLE_NAMES = (ADMINISTRATOR, EDITOR, ANALYST)
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def is_studio_user(user):
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return False
    if user.is_superuser:
        return True
    return user.groups.filter(name__in=ROLE_NAMES).exists()


def role_label(user):
    if user.is_superuser:
        return "Superuser"
    names = set(user.groups.values_list("name", flat=True))
    for role in ROLE_NAMES:
        if role in names:
            return role
    return "No role"


def can(user, *perms):
    return all(user.has_perm(p) for p in perms)


def forbidden(request, message="You don't have permission to do that."):
    response = TemplateResponse(request, "studio/403.html", {"message": message, **page_context(request)}, status=403)
    return response


def studio_view(perm=None, write_perm=None, methods=("GET", "POST")):
    """Decorator for studio views. ``perm``/``write_perm`` may be a string or tuple."""

    def as_tuple(value):
        if value is None:
            return ()
        return (value,) if isinstance(value, str) else tuple(value)

    read_perms, write_perms = as_tuple(perm), as_tuple(write_perm)

    def decorator(func):
        @wraps(func)
        def view(request, *args, **kwargs):
            if not getattr(request.user, "is_authenticated", False):
                return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
            if not is_studio_user(request.user):
                return forbidden(request, "Your account has no studio role yet. Ask a superuser to assign one.")
            if request.method not in methods and request.method not in ("HEAD", "OPTIONS"):
                response = forbidden(request, "That action isn't available here.")
                response.status_code = 405
                response["Allow"] = ", ".join(methods)
                return response
            if read_perms and not can(request.user, *read_perms):
                return forbidden(request)
            if request.method not in SAFE_METHODS and write_perms and not can(request.user, *write_perms):
                return forbidden(request, "Your role can view this page but not change it.")
            return func(request, *args, **kwargs)

        return view

    return decorator


def superuser_only(func):
    @wraps(func)
    def view(request, *args, **kwargs):
        if not getattr(request.user, "is_authenticated", False):
            return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
        if not (request.user.is_active and request.user.is_superuser):
            return forbidden(request, "Only superusers manage the team.")
        return func(request, *args, **kwargs)

    return view


def nav_items(request):
    """Sidebar sections filtered by permission: ``[{title, items: [{label, url, icon, active}]}]``."""
    from webinars.mail import preview_mailbox_enabled

    user = request.user
    sections = [
        ("Overview", [("Dashboard", "studio:dashboard", "home", "/studio/$")]),
        (
            "Content",
            [
                item
                for item, ok in (
                    (("Events", "studio:events", "calendar", "/studio/(events|sessions)"), user.has_perm("webinars.view_event")),
                    (("Registration forms", "studio:forms", "form", "/studio/forms"), user.has_perm("webinars.view_formversion")),
                    (("Hosts & topics", "studio:hosts", "people", "/studio/(hosts|topics)"), user.has_perm("webinars.view_host")),
                )
                if ok
            ],
        ),
        (
            "Insights",
            [
                item
                for item, ok in ((("Reports", "studio:reports", "chart", "/studio/reports"), user.has_perm("webinars.view_reports")),)
                if ok
            ],
        ),
        (
            "Connect",
            [
                item
                for item, ok in (
                    (("Integrations", "studio:integrations", "plug", "/studio/integrations"), user.has_perm("webinars.view_destination")),
                    (("Deliveries", "studio:deliveries", "send", "/studio/deliveries"), user.has_perm("webinars.view_outboxmessage")),
                    (("API keys", "studio:api-keys", "key", "/studio/api-keys"), user.has_perm("webinars.view_apikey")),
                )
                if ok
            ],
        ),
        (
            "Workspace",
            [
                item
                for item, ok in (
                    (("Team", "studio:team", "users", "/studio/team"), user.is_superuser or user.has_perm("auth.view_user")),
                    (
                        ("Preview mailbox", "studio:mailbox", "inbox", "/studio/mailbox"),
                        preview_mailbox_enabled() and user.is_superuser,
                    ),
                    (("Your account", "studio:account", "user", "/studio/account"), True),
                )
                if ok
            ],
        ),
    ]
    import re

    from django.urls import NoReverseMatch, reverse

    result = []
    for title, items in sections:
        entries = []
        for label, url_name, icon, pattern in items:
            try:
                url = reverse(url_name)
            except NoReverseMatch:
                continue
            entries.append(
                {"label": label, "url": url, "icon": icon, "active": bool(re.match(pattern, request.path))}
            )
        if entries:
            result.append({"title": title, "items": entries})
    return result


def page_context(request):
    """Context every studio template needs (base.html)."""
    from webinars.mail import delivery_mode

    user = request.user
    if not getattr(user, "is_authenticated", False):
        return {"studio_nav": [], "studio_role": None, "email_mode": delivery_mode()}
    return {
        "studio_nav": nav_items(request) if is_studio_user(user) else [],
        "studio_role": role_label(user),
        "email_mode": delivery_mode(),
    }


def render(request, template, context=None, status=200):
    """TemplateResponse with the shared studio context merged in."""
    ctx = page_context(request)
    ctx.update(context or {})
    return TemplateResponse(request, template, ctx, status=status)


def no_store(response):
    """For responses that reveal a secret once (API keys, signing secrets, invite links)."""
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"
    return response

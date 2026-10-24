"""Role -> permission mapping.

Used by migration ``0002_roles`` (with historical apps) and the ``sync_roles``
management command (with the live app registry). ``apply_roles`` is idempotent:
each group's permission set is replaced with exactly the mapping below.
Migrations only apply the mapping once, so after an upgrade that changes it,
run ``manage.py sync_roles`` (docs/operations.md) to remove stale grants.

* Administrator – everything in the webinars app except the preview mailbox,
  read-only users/groups, reports. User/group/permission management and the
  preview mailbox are reserved for superusers (bootstrap admin).
* Editor – events/occurrences/series/hosts/topics/forms, attendance entries. No PII models.
* Analyst – read-only catalogue and registrations (PII masked in admin), reports.
"""

ADMINISTRATOR = "Administrator"
EDITOR = "Editor"
ANALYST = "Analyst"

# Custom permission (not tied to a model Meta so the schema stays unchanged);
# attached to the Registration content type.
REPORTS_PERMISSION = ("webinars", "registration", "view_reports", "Can view reports")

WEBINARS_MODELS = [
    "host",
    "topic",
    "formversion",
    "event",
    "eventrevision",
    "series",
    "occurrence",
    "person",
    "browsercapability",
    "organizationexternalid",
    "registration",
    "idempotencyrecord",
    "consentrecord",
    "attributiontouch",
    "attendanceevent",
    "reminderschedule",
    "destination",
    "outboxmessage",
    "apikey",
    "auditlog",
    "checkin",
    "waitingroomverification",
]
# ``previewmessage`` is deliberately absent: the preview mailbox holds invitation
# accept links and registrants' codes/magic links, so it is superuser-only
# (``views_settings._mailbox_allowed``). No role is granted any preview-message permission.
# Team membership is superuser-only (see module docstring): Administrators may only view invitations.
VIEW_ONLY_FOR_ADMINISTRATOR = ["invitation"]

_ALL_ACTIONS = ("add", "change", "delete", "view")


def _perms(model, actions, app_label="webinars"):
    return [f"{app_label}.{action}_{model}" for action in actions]


def _administrator():
    perms = []
    for model in WEBINARS_MODELS:
        perms += _perms(model, _ALL_ACTIONS)
    for model in VIEW_ONLY_FOR_ADMINISTRATOR:
        perms += _perms(model, ("view",))
    # Read-only for users/groups: change_user/change_group would let an
    # Administrator tick is_superuser, grant user_permissions, reset the
    # superuser's password or widen another role. Only superusers manage them.
    perms += _perms("user", ("view",), "auth")
    perms += _perms("group", ("view",), "auth")
    perms.append("webinars.view_reports")
    return perms


def _editor():
    perms = []
    perms += _perms("event", ("add", "change", "view"))
    perms += _perms("eventrevision", ("view",))
    for model in ("series", "occurrence", "host", "topic", "formversion"):
        perms += _perms(model, ("add", "change", "view"))
    perms += _perms("attendanceevent", ("add", "view"))
    perms += _perms("checkin", ("view",))
    return perms


def _analyst():
    perms = []
    for model in (
        "event", "occurrence", "series", "host", "topic", "formversion", "registration", "attendanceevent", "checkin",
    ):
        perms += _perms(model, ("view",))
    perms.append("webinars.view_reports")
    return perms


ROLE_PERMISSIONS = {
    ADMINISTRATOR: _administrator(),
    EDITOR: _editor(),
    ANALYST: _analyst(),
}


def ensure_custom_permissions(apps=None):
    """Create the ``webinars.view_reports`` permission if missing."""
    if apps is None:
        from django.apps import apps as global_apps

        apps = global_apps
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")
    app_label, model, codename, name = REPORTS_PERMISSION
    ct, _ = ContentType.objects.get_or_create(app_label=app_label, model=model)
    perm, created = Permission.objects.get_or_create(content_type=ct, codename=codename, defaults={"name": name})
    if not created and perm.name != name:
        perm.name = name
        perm.save(update_fields=["name"])
    return perm


def _model_exists(apps, app_label, codename):
    """True when the model behind a default ``<action>_<model>`` permission is registered."""
    action, _, model = codename.partition("_")
    if action not in _ALL_ACTIONS or not model:
        return True  # custom permission: always expected
    try:
        apps.get_model(app_label, model)
    except LookupError:
        return False
    return True


def apply_roles(apps=None):
    """Create/update the role groups. Returns ``{group_name: permission_count}``.

    Raises ``LookupError`` if any expected permission does not exist (run
    ``migrate`` first so default model permissions are created).
    """
    if apps is None:
        from django.apps import apps as global_apps

        apps = global_apps
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    ensure_custom_permissions(apps)

    result = {}
    for group_name, perm_names in ROLE_PERMISSIONS.items():
        wanted = set(perm_names)
        perms = []
        missing = []
        for full in sorted(wanted):
            app_label, codename = full.split(".", 1)
            perm = Permission.objects.filter(content_type__app_label=app_label, codename=codename).first()
            if perm is None:
                if not _model_exists(apps, app_label, codename):
                    # Historical app registry (an early migration such as 0002_roles):
                    # this model is created by a later migration, which re-applies roles.
                    continue
                missing.append(full)
            else:
                perms.append(perm)
        if missing:
            raise LookupError(f"Missing permissions for role {group_name}: {', '.join(missing)}")
        group, _ = Group.objects.get_or_create(name=group_name)
        group.permissions.set(perms)
        result[group_name] = len(perms)
    return result

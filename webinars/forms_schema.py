"""Form builder definitions, layout validation and the public form schema.

A form version's *membership* lives in the existing columns (``required_fields``,
``optional_fields``: canonical keys; ``custom_fields``: bare custom keys). The studio
builder adds ``field_layout`` presentation metadata in display order::

    {"key": "companySize", "label": "Company size", "help": "", "type": "select",
     "options": ["1-10", "11-50"]}

``key`` is a canonical key or ``custom.<key>``. Custom fields are always optional
(the existing schema validators only accept canonical keys as required). When a
field has ``options`` the registration service only accepts one of them
(``normalization.normalize_submission``).
"""
import re

from django.core.exceptions import ValidationError

from webinars.normalization import CANONICAL_FIELDS, CUSTOM_PREFIX

SCHEMA_VERSION = "1"
CUSTOM_KEY_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,63}$")
MAX_FIELDS = 40
MAX_OPTIONS = 50
MAX_LABEL = 120
MAX_HELP = 300
MAX_OPTION = 100

DEFAULT_LABELS = {
    "email": "Work email",
    "phone": "Mobile phone",
    "firstName": "First name",
    "lastName": "Last name",
    "name": "Full name",
    "company": "Company",
    "jobTitle": "Job title",
    "companySize": "Company size",
}
AUTOCOMPLETE = {
    "email": "email",
    "phone": "tel",
    "firstName": "given-name",
    "lastName": "family-name",
    "name": "name",
    "company": "organization",
    "jobTitle": "organization-title",
}
CANONICAL_TYPES = {"email": "email", "phone": "tel"}
CUSTOM_TYPES = ("text", "textarea", "select", "checkbox")


def default_type(key, options=None):
    if key in CANONICAL_TYPES:
        return CANONICAL_TYPES[key]
    if key == "companySize":
        return "select" if options else "text"
    return "text"


def humanize(key):
    bare = key[len(CUSTOM_PREFIX):] if key.startswith(CUSTOM_PREFIX) else key
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", bare).replace("_", " ").strip()
    return words[:1].upper() + words[1:] if words else key


def _clean_options(raw, where):
    if raw in (None, ""):
        return []
    if not isinstance(raw, list) or not all(isinstance(o, str) for o in raw):
        raise ValidationError(f"{where}: options must be a list of text values.")
    options = []
    for option in raw:
        option = option.strip()
        if not option:
            continue
        if len(option) > MAX_OPTION:
            raise ValidationError(f"{where}: options must be at most {MAX_OPTION} characters.")
        if option in options:
            raise ValidationError(f"{where}: option “{option}” is listed twice.")
        options.append(option)
    if len(options) > MAX_OPTIONS:
        raise ValidationError(f"{where}: at most {MAX_OPTIONS} options.")
    return options


def build_definition(rows):
    """Turn builder rows into FormVersion column values.

    ``rows``: ordered list of dicts with ``key`` (canonical or ``custom.<key>``),
    ``label``, ``help``, ``required`` (bool), ``type`` (custom fields only) and
    ``options`` (list of str). Raises ``ValidationError`` with a dict of
    ``{row_index: message}`` so the builder can show errors next to the row.
    """
    errors = {}
    required, optional, custom, layout = [], [], [], []
    seen = set()
    if not rows:
        raise ValidationError({"_": "Add at least one field. Email is required for registration."})
    if len(rows) > MAX_FIELDS:
        raise ValidationError({"_": f"A form can have at most {MAX_FIELDS} fields."})
    for index, row in enumerate(rows):
        key = (row.get("key") or "").strip()
        label = (row.get("label") or "").strip()
        help_text = (row.get("help") or "").strip()
        is_required = bool(row.get("required"))
        where = label or key or f"Field {index + 1}"
        try:
            if key.startswith(CUSTOM_PREFIX):
                bare = key[len(CUSTOM_PREFIX):]
                if not CUSTOM_KEY_RE.match(bare):
                    raise ValidationError(
                        "Custom field keys start with a letter and use only letters, digits and underscores."
                    )
                if bare in CANONICAL_FIELDS:
                    raise ValidationError(f"“{bare}” is a standard field; pick it from the list instead.")
                if is_required:
                    raise ValidationError("Custom fields are always optional.")
                field_type = row.get("type") or "text"
                if field_type not in CUSTOM_TYPES:
                    raise ValidationError("Choose a field type.")
            elif key in CANONICAL_FIELDS:
                field_type = None
            else:
                raise ValidationError("Choose a field.")
            if key in seen:
                raise ValidationError("This field is already on the form.")
            if not label:
                raise ValidationError("Every field needs a label.")
            if len(label) > MAX_LABEL:
                raise ValidationError(f"Labels must be at most {MAX_LABEL} characters.")
            if len(help_text) > MAX_HELP:
                raise ValidationError(f"Help text must be at most {MAX_HELP} characters.")
            options = _clean_options(row.get("options"), where)
            if key.startswith(CUSTOM_PREFIX):
                if field_type == "select" and not options:
                    raise ValidationError("Dropdowns need at least one option.")
                if field_type != "select":
                    options = []
            elif key != "companySize" and options:
                raise ValidationError("Only Company size and custom dropdowns can have options.")
        except ValidationError as exc:
            errors[index] = exc.messages[0]
            continue
        seen.add(key)
        entry = {"key": key, "label": label, "help": help_text, "options": options}
        if key.startswith(CUSTOM_PREFIX):
            entry["type"] = field_type
            custom.append(key[len(CUSTOM_PREFIX):])
        else:
            (required if is_required else optional).append(key)
        layout.append(entry)
    if "email" not in required and not errors:
        errors["_"] = "Email must be on the form and required: it is how registrations are de-duplicated."
    if errors:
        raise ValidationError(errors)
    return {
        "required_fields": required,
        "optional_fields": optional,
        "custom_fields": custom,
        "field_layout": layout,
    }


def validate_layout(layout, required, optional, custom):
    """Validate a stored/submitted ``field_layout`` against the membership columns."""
    if layout in (None, ""):
        return []
    if not isinstance(layout, list):
        raise ValidationError("Layout must be a list.")
    rows = []
    for entry in layout:
        if not isinstance(entry, dict):
            raise ValidationError("Layout entries must be objects.")
        key = entry.get("key")
        rows.append(
            {
                "key": key,
                "label": entry.get("label"),
                "help": entry.get("help"),
                "required": key in (required or []),
                "type": entry.get("type"),
                "options": entry.get("options"),
            }
        )
    definition = build_definition(rows)
    expected = set(required or []) | set(optional or []) | {CUSTOM_PREFIX + c for c in (custom or [])}
    listed = {e["key"] for e in definition["field_layout"]}
    if listed != expected:
        raise ValidationError("The layout must list exactly the form's fields.")
    return definition["field_layout"]


def rows_for(form):
    """Builder rows for an existing form version (layout order, then any unlisted fields)."""
    required = list(form.required_fields or [])
    optional = list(form.optional_fields or [])
    custom = [CUSTOM_PREFIX + c for c in (form.custom_fields or [])]
    by_key = {e.get("key"): e for e in (form.field_layout or []) if isinstance(e, dict)}
    order = [e.get("key") for e in (form.field_layout or []) if isinstance(e, dict)]
    for key in required + optional + custom:
        if key not in order:
            order.append(key)
    rows = []
    for key in order:
        if key not in required and key not in optional and key not in custom:
            continue
        entry = by_key.get(key, {})
        options = list(entry.get("options") or [])
        rows.append(
            {
                "key": key,
                "label": entry.get("label") or DEFAULT_LABELS.get(key) or humanize(key),
                "help": entry.get("help") or "",
                "required": key in required,
                "type": entry.get("type") or default_type(key, options),
                "options": options,
                "custom": key.startswith(CUSTOM_PREFIX),
            }
        )
    return rows


def option_sets(form):
    """``{key: set(options)}`` for fields restricted to listed options."""
    result = {}
    layout = getattr(form, "field_layout", None) or []
    if not isinstance(layout, list):
        return result
    for entry in layout:
        if isinstance(entry, dict) and entry.get("options"):
            result[entry.get("key")] = set(entry["options"])
    return result


def cta_labels(event):
    """``{"register": str, "confirm": str}`` button labels for ``event`` with defaults resolved."""
    return event.cta


def public_schema(form):
    """Canonical, PII-free description of a form version for renderers and agents."""
    fields = []
    for row in rows_for(form):
        key = row["key"]
        field_type = row["type"] if row["custom"] else default_type(key, row["options"])
        fields.append(
            {
                "key": key,
                "name": key,
                "type": field_type,
                "label": row["label"],
                "help": row["help"] or None,
                "required": row["required"],
                "options": row["options"] or None,
                "autocomplete": AUTOCOMPLETE.get(key),
                "custom": row["custom"],
            }
        )
    sms = None
    if form.sms_disclosure_version:
        sms = {"disclosureVersion": form.sms_disclosure_version, "disclosureText": form.sms_disclosure_text}
    return {
        "schemaVersion": SCHEMA_VERSION,
        "formVersion": form.version,
        "description": form.description,
        "fields": fields,
        "fieldMapping": dict(form.field_mapping or {}),
        "defaultPhoneRegion": form.default_phone_region or None,
        "sms": sms,
        "isActive": form.is_active,
    }

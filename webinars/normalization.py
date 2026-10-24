"""Canonical profile fields, alias resolution and conservative email/phone normalisation.

Rules:
* Keys resolve in order: canonical camelCase key, the form's explicit
  ``field_mapping``, then ``COMMON_ALIASES``. Anything else is an
  "unknown field" error; there is no fuzzy matching.
* Two keys resolving to the same canonical field with different values is a
  ``conflict`` error.
* Blank / whitespace / ``None`` values are dropped, so they can never erase
  stored data.
* ``name`` is never split into ``firstName``/``lastName`` (or joined).
"""
import math
import re
from dataclasses import dataclass, field

CANONICAL_FIELDS = ["email", "phone", "firstName", "lastName", "name", "company", "jobTitle", "companySize"]

CANONICAL_TO_MODEL = {
    "email": "email",
    "phone": "phone",
    "firstName": "first_name",
    "lastName": "last_name",
    "name": "name",
    "company": "company",
    "jobTitle": "job_title",
    "companySize": "company_size",
}

COMMON_ALIASES = {
    "first_name": "firstName",
    "last_name": "lastName",
    "job_title": "jobTitle",
    "company_size": "companySize",
    "email_address": "email",
    "phone_number": "phone",
    "full_name": "name",
    "company_name": "company",
}

# Maximum lengths mirror the Person model columns.
MAX_LENGTHS = {
    "email": 254,
    "phone": 20,
    "firstName": 150,
    "lastName": 150,
    "name": 300,
    "company": 300,
    "jobTitle": 300,
    "companySize": 64,
}

CUSTOM_PREFIX = "custom."
CUSTOM_MAX_LENGTH = 1000
MAX_KEYS = 100

# Explicit calling codes; only used when the form supplies a default region.
CALLING_CODES = {
    "US": "1",
    "CA": "1",
    "GB": "44",
    "IE": "353",
    "AU": "61",
    "DE": "49",
    "FR": "33",
    "NL": "31",
    "ES": "34",
    "IT": "39",
    "IN": "91",
    "NZ": "64",
    "SG": "65",
}
_NANP_REGIONS = {"US", "CA"}


class FieldError(Exception):
    """A single field failed validation. ``code`` is a stable machine-readable code."""

    def __init__(self, message, code="invalid"):
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass
class Normalized:
    fields: dict = field(default_factory=dict)
    custom: dict = field(default_factory=dict)
    errors: dict = field(default_factory=dict)
    error_codes: dict = field(default_factory=dict)

    @property
    def ok(self):
        return not self.errors


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------

# RFC 5322 dot-atom subset: atext runs separated by single dots.
_ATEXT = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
_LOCAL_RE = re.compile(rf"^{_ATEXT}(?:\.{_ATEXT})*$")
_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def _normalize_domain(domain):
    if not domain or len(domain) > 253:
        raise FieldError("Enter a valid email address.")
    labels = domain.split(".")
    if len(labels) < 2:
        raise FieldError("Enter a valid email address.")
    out = []
    for label in labels:
        if not label:
            raise FieldError("Enter a valid email address.")
        try:
            ascii_label = label.encode("idna").decode("ascii").lower()
        except UnicodeError:
            raise FieldError("Enter a valid email address.")
        if not _LABEL_RE.match(ascii_label):
            raise FieldError("Enter a valid email address.")
        out.append(ascii_label)
    if out[-1].isdigit():
        raise FieldError("Enter a valid email address.")
    result = ".".join(out)
    if len(result) > 253:
        raise FieldError("Enter a valid email address.")
    return result


def normalize_email(value):
    """Trim, validate conservatively and lower-case/IDNA-encode the domain.

    The local part keeps its case; no plus-tag or dot stripping is performed.
    """
    if not isinstance(value, str):
        raise FieldError("Email must be a string.", code="invalid_type")
    value = value.strip()
    if not value:
        raise FieldError("Email is required.", code="required")
    if len(value) > 254:
        raise FieldError("Email is too long.", code="too_long")
    if value.count("@") != 1:
        raise FieldError("Enter a valid email address.")
    local, domain = value.split("@")
    if not local or len(local) > 64 or not _LOCAL_RE.match(local):
        raise FieldError("Enter a valid email address.")
    result = f"{local}@{_normalize_domain(domain)}"
    if len(result) > 254:
        raise FieldError("Email is too long.", code="too_long")
    return result


def email_key(email):
    """Case-insensitive dedupe key: casefolded local part + lower-case domain.

    This is for deduplication only; it never strips plus tags or dots and
    never authorises access to an existing profile.
    """
    local, _, domain = email.rpartition("@")
    return f"{local.casefold()}@{domain.lower()}"


# --------------------------------------------------------------------------
# Phone
# --------------------------------------------------------------------------

_PHONE_ALLOWED_RE = re.compile(r"^\+?[0-9 ().\-]+$")
_PHONE_STRIP_RE = re.compile(r"[ ().\-]")


def normalize_phone(value, region):
    """Return an E.164 number or raise ``FieldError``. Never guesses a country."""
    if not isinstance(value, str):
        raise FieldError("Phone must be a string.", code="invalid_type")
    value = value.strip()
    if not value:
        raise FieldError("Phone is required.", code="required")
    if len(value) > 40 or not _PHONE_ALLOWED_RE.match(value):
        raise FieldError("Enter a valid phone number.")
    international = value.startswith("+")
    digits = _PHONE_STRIP_RE.sub("", value.lstrip("+"))
    if not digits.isdigit():
        raise FieldError("Enter a valid phone number.")

    if not international:
        region = (region or "").strip().upper()
        if not region:
            raise FieldError("Phone must be in international format (+country code)", code="needs_country")
        code = CALLING_CODES.get(region)
        if code is None:
            raise FieldError("Phone must be in international format (+country code)", code="needs_country")
        if region in _NANP_REGIONS:
            if len(digits) == 11 and digits.startswith("1"):
                digits = digits[1:]
            if len(digits) != 10 or digits[0] in "01" or digits[3] in "01":
                raise FieldError("Enter a valid phone number.")
        else:
            if digits.startswith("00"):
                # International dialling prefix without '+': do not guess.
                raise FieldError("Phone must be in international format (+country code)", code="needs_country")
            if digits.startswith("0"):
                digits = digits[1:]
            if not digits or digits[0] == "0":
                raise FieldError("Enter a valid phone number.")
        digits = code + digits

    if not 8 <= len(digits) <= 15 or digits[0] == "0":
        raise FieldError("Enter a valid phone number.")
    return "+" + digits


# --------------------------------------------------------------------------
# Submission
# --------------------------------------------------------------------------


def _has_control_chars(s):
    return any(ord(c) < 32 or 0x7F <= ord(c) < 0xA0 or c in "  " for c in s)


def _normalize_text(canonical, value):
    value = value.strip()
    if _has_control_chars(value):
        raise FieldError("Contains invalid characters.")
    if len(value) > MAX_LENGTHS[canonical]:
        raise FieldError(f"Must be at most {MAX_LENGTHS[canonical]} characters.", code="too_long")
    return value


def _resolve_key(key, form_mapping):
    if key in CANONICAL_FIELDS:
        return key
    if key in form_mapping:
        target = form_mapping[key]
        return target if target in CANONICAL_FIELDS else None
    return COMMON_ALIASES.get(key)


def _add_error(result, key, message, code):
    if key not in result.errors:
        result.errors[key] = message
        result.error_codes[key] = code


def normalize_submission(profile, custom, form):
    """Normalise a browser/API submission against ``form`` (a ``FormVersion``)."""
    result = Normalized()
    profile = {} if profile is None else profile
    custom = {} if custom is None else custom
    if not isinstance(profile, dict):
        _add_error(result, "profile", "Profile must be an object.", "invalid_type")
        profile = {}
    if not isinstance(custom, dict):
        _add_error(result, "customFields", "Custom fields must be an object.", "invalid_type")
        custom = {}
    if len(profile) > MAX_KEYS or len(custom) > MAX_KEYS:
        _add_error(result, "profile", "Too many fields.", "too_many")
        return result

    form_mapping = getattr(form, "field_mapping", None) or {}
    if not isinstance(form_mapping, dict):
        form_mapping = {}
    region = getattr(form, "default_phone_region", "") or None

    for key, raw in profile.items():
        if not isinstance(key, str):
            continue
        canonical = _resolve_key(key, form_mapping)
        if canonical is None:
            _add_error(result, key, "Unknown field.", "unknown_field")
            continue
        if raw is None:
            continue
        if not isinstance(raw, str):
            _add_error(result, canonical, "Must be a string.", "invalid_type")
            continue
        if not raw.strip():
            continue
        try:
            if canonical == "email":
                value = normalize_email(raw)
            elif canonical == "phone":
                value = normalize_phone(raw, region)
            else:
                value = _normalize_text(canonical, raw)
        except FieldError as exc:
            _add_error(result, canonical, exc.message, exc.code)
            continue
        existing = result.fields.get(canonical)
        if existing is not None and existing != value:
            # Conflicts override earlier per-field errors so the client sees the cause.
            result.errors[canonical] = "Conflicting values were supplied for this field."
            result.error_codes[canonical] = "conflict"
            continue
        result.fields[canonical] = value

    for canonical, code in result.error_codes.items():
        if code == "conflict":
            result.fields.pop(canonical, None)

    allowed_custom = set(getattr(form, "custom_fields", None) or [])
    for key, raw in custom.items():
        if not isinstance(key, str):
            continue
        bare = key[len(CUSTOM_PREFIX):] if key.startswith(CUSTOM_PREFIX) else key
        ns_key = CUSTOM_PREFIX + bare
        if bare not in allowed_custom:
            _add_error(result, ns_key, "Unknown custom field.", "unknown_field")
            continue
        if raw is None:
            continue
        if isinstance(raw, bool):
            value = raw
        elif isinstance(raw, (int, float)):
            if isinstance(raw, float) and not math.isfinite(raw):
                _add_error(result, ns_key, "Must be a finite number.", "invalid_type")
                continue
            value = raw
        elif isinstance(raw, str):
            value = raw.strip()
            if not value:
                continue
            if len(value) > CUSTOM_MAX_LENGTH:
                _add_error(result, ns_key, f"Must be at most {CUSTOM_MAX_LENGTH} characters.", "too_long")
                continue
            if any(ord(c) < 32 and c not in "\n\t" for c in value):
                _add_error(result, ns_key, "Contains invalid characters.", "invalid")
                continue
        else:
            _add_error(result, ns_key, "Must be a string, number or boolean.", "invalid_type")
            continue
        existing = result.custom.get(ns_key)
        if existing is not None and existing != value:
            result.errors[ns_key] = "Conflicting values were supplied for this field."
            result.error_codes[ns_key] = "conflict"
            result.custom.pop(ns_key, None)
            continue
        if result.error_codes.get(ns_key) == "conflict":
            continue
        result.custom[ns_key] = value

    _enforce_options(result, form)
    return result


def _enforce_options(result, form):
    """Fields the studio builder restricted to listed options accept only those values."""
    from webinars.forms_schema import option_sets

    for key, allowed in option_sets(form).items():
        if key in result.fields:
            value, store = result.fields[key], result.fields
        elif key in result.custom:
            value, store = result.custom[key], result.custom
        else:
            continue
        if not isinstance(value, str) or value not in allowed:
            store.pop(key, None)
            _add_error(result, key, "Choose one of the listed options.", "invalid_choice")

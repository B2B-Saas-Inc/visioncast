"""Registration service: idempotency, dedupe, identity rules, consent, attribution, outbox.

See docs/architecture.md "Identity rules". Summary of what this module guarantees:

* The registration (and its outbox rows) is durably committed before a success
  response is produced; the response is stored in ``IdempotencyRecord`` so a retry of
  the same intent replays it and a different payload with the same key gets 409.
* An unknown browser that submits an existing person's email is linked to that person
  only for per-occurrence dedupe; the person's profile is never read back or updated,
  the submission is kept in ``Registration.submitted_profile`` and no capability is issued.
* Client consent claims are stored as ``client_claim`` with ``server_verified=False``.
* Responses never contain profile data, join links or waiting-room links.
"""
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import timezone as dt_timezone

from django.db import IntegrityError, transaction
from django.utils import timezone

from webinars import attribution as attribution_mod
from webinars import calendar_links, identity, normalization, outbox, scheduling
from webinars.audit import audit
from webinars.models import (
    AttributionTouch,
    ConsentRecord,
    Event,
    IdempotencyRecord,
    Occurrence,
    OrganizationExternalId,
    Person,
    Registration,
)

SCHEMA_VERSION = "1"
ALLOWED_TOP_LEVEL = {
    "schemaVersion",
    "occurrenceId",
    "formVersion",
    "idempotencyKey",
    "profile",
    "organization",
    "consents",
    "attribution",
    "customFields",
}
CONSENT_PURPOSES = ("sms", "analytics", "advertising")
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_\-:.]{8,128}$")
NAMESPACE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
MAX_EXTERNAL_IDS = 20
MAX_EXTERNAL_ID_VALUE = 256


@dataclass
class RegistrationResult:
    status_code: int
    body: dict
    set_token: str | None = None
    clear_cookie: bool = False


class _Abort(Exception):
    def __init__(self, result):
        super().__init__(result.body.get("error", {}).get("code", "abort"))
        self.result = result


def error_body(code, message, fields=None):
    err = {"code": code, "message": message}
    if fields:
        err["fields"] = fields
        # Top-level mirror read by the browser SDK (sdk/src/webinar-form.js errorFieldKeys).
        return {"error": err, "fieldErrors": dict(fields)}
    return {"error": err}


def _error(status, code, message, fields=None):
    return RegistrationResult(status_code=status, body=error_body(code, message, fields))


# ---------------------------------------------------------------------------
# Envelope validation
# ---------------------------------------------------------------------------


def validate_envelope(payload):
    """Return a dict of field errors (empty when the envelope is structurally valid)."""
    if not isinstance(payload, dict):
        return {"body": "Request body must be a JSON object."}
    errors = {}
    unknown = sorted(set(payload) - ALLOWED_TOP_LEVEL)
    for key in unknown:
        errors[str(key)[:64]] = "Unknown field."

    if payload.get("schemaVersion") != SCHEMA_VERSION:
        errors["schemaVersion"] = "Must be \"1\"."

    occ_id = payload.get("occurrenceId")
    if not isinstance(occ_id, str):
        errors["occurrenceId"] = "Required."
    else:
        try:
            uuid.UUID(occ_id)
        except ValueError:
            errors["occurrenceId"] = "Must be a UUID."

    fv = payload.get("formVersion")
    if not isinstance(fv, str) or not fv or len(fv) > 64:
        errors["formVersion"] = "Required."

    ik = payload.get("idempotencyKey")
    if not isinstance(ik, str) or not IDEMPOTENCY_KEY_RE.match(ik):
        errors["idempotencyKey"] = "Must be 8-128 characters of A-Z a-z 0-9 _ - : ."

    if not isinstance(payload.get("profile"), dict):
        errors["profile"] = "Must be an object."

    if "customFields" in payload and not isinstance(payload["customFields"], dict):
        errors["customFields"] = "Must be an object."

    if "attribution" in payload and not isinstance(payload["attribution"], dict):
        errors["attribution"] = "Must be an object."

    org = payload.get("organization")
    if org is not None:
        if not isinstance(org, dict) or set(org) - {"externalIds"}:
            errors["organization"] = "Must be an object with only externalIds."
        else:
            ids = org.get("externalIds", [])
            if not isinstance(ids, list) or len(ids) > MAX_EXTERNAL_IDS:
                errors["organization.externalIds"] = f"Must be a list of at most {MAX_EXTERNAL_IDS} items."
            else:
                for i, item in enumerate(ids):
                    if (
                        not isinstance(item, dict)
                        or set(item) != {"namespace", "value"}
                        or not isinstance(item["namespace"], str)
                        or not NAMESPACE_RE.match(item["namespace"])
                        or not isinstance(item["value"], str)
                        or not item["value"].strip()
                        or len(item["value"]) > MAX_EXTERNAL_ID_VALUE
                    ):
                        errors[f"organization.externalIds.{i}"] = "Invalid namespace/value."

    consents = payload.get("consents")
    if consents is not None:
        if not isinstance(consents, dict):
            errors["consents"] = "Must be an object."
        else:
            for key, entry in consents.items():
                if key not in CONSENT_PURPOSES:
                    errors[f"consents.{str(key)[:32]}"] = "Unknown consent purpose."
                    continue
                if not isinstance(entry, dict) or not isinstance(entry.get("granted"), bool):
                    errors[f"consents.{key}"] = "Must be an object with boolean granted."
                    continue
                if key == "sms" and "disclosureVersion" in entry and not isinstance(entry["disclosureVersion"], (str, type(None))):
                    errors["consents.sms.disclosureVersion"] = "Must be a string."
                # Any other keys (e.g. a client-claimed "verified") are ignored.
    return errors


def request_hash(payload, scope):
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(f"{scope}\n{canonical}".encode("utf-8")).hexdigest()


# The stored hash covers the payload only; the browser context is checked separately
# against the stored registration's owner (see ``_replay``). Records written before this
# change used ``"anon"`` or the capability id as scope and are still accepted.
PAYLOAD_SCOPE = "payload-v2"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def register(*, payload, capability, api_key, trusted=False, allow_started=False):
    """``allow_started`` is used only by the hosted waiting room (``webinars.waiting_room``):
    it accepts a session that has started but not ended, for late walk-ins whose
    mailbox was verified. Public APIs never set it."""
    errors = validate_envelope(payload)
    if errors:
        return _error(400, "validation_error", "The request is invalid.", errors)

    if capability is not None and (capability.revoked_at is not None or capability.person.erased_at is not None):
        capability = None

    legacy_scope = str(capability.id) if capability is not None else "anon"
    req_hash = request_hash(payload, PAYLOAD_SCOPE)
    legacy_hash = request_hash(payload, legacy_scope)
    key = payload["idempotencyKey"]

    for attempt in range(2):
        try:
            with transaction.atomic():
                record = (
                    IdempotencyRecord.objects.select_for_update(of=("self",))
                    .select_related("registration", "registration__person")
                    .filter(key=key)
                    .first()
                )
                if record is not None:
                    return _replay(record, req_hash, legacy_hash, capability)
                result, registration = _process(payload, capability, api_key, trusted, allow_started)
                IdempotencyRecord.objects.create(
                    key=key,
                    request_hash=req_hash,
                    status_code=result.status_code,
                    response_body=result.body,
                    registration=registration,
                    caller_capability_id=capability.id if capability is not None else None,
                )
                return result
        except _Abort as abort:
            return abort.result
        except IntegrityError:
            # Concurrent insert (same idempotency key, same email, same occurrence).
            # The transaction was rolled back; re-run once so the winner's rows are seen.
            if attempt == 0:
                continue
            raise
    raise RuntimeError("unreachable")  # pragma: no cover


def _conflict():
    return _error(409, "idempotency_conflict", "This idempotency key was already used for a different request.")


def _replay(record, req_hash, legacy_hash, capability):
    """Replay a stored response for the same payload, never into another person's context.

    The stored body is uniform (no profile, identity or private links) and a replay never
    issues a real capability: knowing the idempotency key and payload does not prove the
    caller owns the person, so a lost capability cookie is not recovered (that browser
    stays unrecognised until it registers again or uses "Not you?").

    * A browser holding a capability may replay if the original request was made with
      that same capability (e.g. it registered someone else, rule 6) or the stored
      registration belongs to that capability's person (e.g. the anon request succeeded,
      the response was lost and the retry now carries the issued cookie).
    * A cookie-less retry replays the body with an unbound decoy cookie, so Set-Cookie
      stays uniform.
    """
    if record.request_hash not in (req_hash, legacy_hash):
        return _conflict()
    registration = record.registration
    if capability is not None:
        same_caller = record.caller_capability_id is not None and record.caller_capability_id == capability.id
        own_registration = registration is not None and registration.person_id == capability.person_id
        if not (same_caller or own_registration):
            return _conflict()
        return RegistrationResult(status_code=record.status_code, body=record.response_body)
    return RegistrationResult(status_code=record.status_code, body=record.response_body, set_token=identity.unbound_token())


# ---------------------------------------------------------------------------
# Core processing (runs inside transaction.atomic; raise _Abort to roll back)
# ---------------------------------------------------------------------------


def _abort(status, code, message, fields=None):
    raise _Abort(_error(status, code, message, fields))


def _load_occurrence(payload, allow_started=False):
    occ = (
        Occurrence.objects.select_related("event", "event__form_version")
        .filter(pk=payload["occurrenceId"])
        .first()
    )
    if occ is None or occ.event.visibility not in (Event.Visibility.PUBLIC, Event.Visibility.UNLISTED):
        _abort(404, "not_found", "Occurrence not found.")
    if payload["formVersion"] != occ.event.form_version.version:
        _abort(400, "validation_error", "The form version does not match this event.", {"formVersion": "Mismatch."})
    now = timezone.now()
    started = occ.starts_at <= now
    late_walk_in = allow_started and occ.waiting_room_enabled and occ.ends_at > now
    if (
        occ.status != Occurrence.Status.SCHEDULED
        or not occ.registration_open
        or (started and not late_walk_in)
    ):
        _abort(410, "registration_closed", "Registration for this session is closed.")
    return occ


def _resolve_identity(capability, submitted_email):
    """Return (person|None, verified: bool, issue_cookie: bool).

    ``person is None`` means "create a new person from the submission".
    """
    cap_person = None
    if capability is not None:
        cap_person = Person.objects.select_for_update().get(pk=capability.person_id)
        if cap_person.erased_at is not None:
            cap_person = None

    sub_key = normalization.email_key(submitted_email) if submitted_email else None

    if cap_person is not None:
        if sub_key is None or sub_key == cap_person.email_key:
            return cap_person, True, False  # rule 5
        if cap_person.email_key is None and not Person.objects.filter(email_key=sub_key).exists():
            return cap_person, True, False  # capability person without email adds one
        # rule 6: registering someone else; never rebind the browser's cookie.

    if sub_key is None:
        _abort(400, "validation_error", "Email is required.", {"email": "Required."})

    existing = Person.objects.select_for_update().filter(email_key=sub_key).first()
    if existing is not None:
        return existing, False, False  # rule 3 (and rule 6 for an existing email)
    return None, True, capability is None  # rule 4 (rule 6: new person, no cookie)


def _check_required(form, person, verified, normalized):
    submitted = set(normalized.fields)
    for k in normalized.custom:
        submitted.add(k)
        submitted.add(k[len("custom."):] if k.startswith("custom.") else k)
    # Existing values only count when the identity is verified (capability-bound person).
    person_missing = None
    if verified and person is not None:
        person_missing = set(identity.missing_fields(person, form))
    missing = []
    for k in form.required_fields or []:
        if not isinstance(k, str) or k in submitted:
            continue
        if person_missing is not None and k not in person_missing:
            continue
        missing.append(k)
    if missing:
        _abort(400, "validation_error", "Some required fields are missing.", {k: "Required." for k in missing})


def _apply_fields(person, normalized):
    changed = False
    for canon, value in normalized.fields.items():
        if value in (None, ""):
            continue
        attr = normalization.CANONICAL_TO_MODEL.get(canon)
        if not attr:
            continue
        if canon == "email":
            new_key = normalization.email_key(value)
            if person.email != value or person.email_key != new_key:
                person.email = value
                person.email_key = new_key
                changed = True
            continue
        if getattr(person, attr) != value:
            setattr(person, attr, value)
            changed = True
    custom = dict(person.custom_fields or {})
    for k, v in normalized.custom.items():
        if v in (None, "") or custom.get(k) == v:
            continue
        custom[k] = v
        changed = True
    person.custom_fields = custom
    return changed


def _validate_consents(consents, form, person, verified, normalized):
    sms = consents.get("sms")
    if sms and sms["granted"]:
        fields = {}
        if not form.sms_disclosure_version or sms.get("disclosureVersion") != form.sms_disclosure_version:
            fields["consents.sms.disclosureVersion"] = "Does not match the current SMS disclosure."
        phone = normalized.fields.get("phone") or (person.phone if (verified and person is not None) else "")
        if not phone:
            fields["phone"] = "A valid phone number is required for SMS consent."
        if fields:
            _abort(400, "validation_error", "SMS consent is invalid.", fields)
        return phone
    return ""


def _record_consents(consents, form, person, registration, sms_phone, verified=True):
    """Append consent evidence.

    Unverified submissions (unknown browser using an existing person's email) record
    nothing against that person: neither grants nor withdrawals (the SDK sends
    ``granted:false`` defaults, so a withdrawal would be a false record). The claim is kept
    only in ``Registration.submitted_profile["consentClaims"]``.
    """
    if not verified:
        return
    now = timezone.now()
    for purpose in CONSENT_PURPOSES:
        entry = consents.get(purpose)
        if entry is None:
            continue
        granted = bool(entry["granted"])
        previous = ConsentRecord.objects.filter(person=person, purpose=purpose).order_by("-recorded_at", "-id").first()
        kwargs = dict(
            person=person,
            registration=registration,
            purpose=purpose,
            granted=granted,
            source=ConsentRecord.Source.CLIENT_CLAIM,
            server_verified=False,  # never trust client verification claims
            recorded_at=now,
        )
        if purpose == "sms":
            kwargs["disclosure_version"] = form.sms_disclosure_version if granted else (entry.get("disclosureVersion") or "")[:64]
            if granted:
                kwargs["disclosure_text_snapshot"] = form.sms_disclosure_text
                kwargs["phone_snapshot"] = sms_phone
        if not granted and previous is not None and previous.granted:
            kwargs["withdrawn_at"] = now
        ConsentRecord.objects.create(**kwargs)


def _record_org_ids(person, ids, trusted):
    source = OrganizationExternalId.Source.TRUSTED_API if trusted else OrganizationExternalId.Source.BROWSER
    for item in ids:
        OrganizationExternalId.objects.get_or_create(
            person=person,
            namespace=item["namespace"],
            value=item["value"].strip(),
            source=source,
            defaults={"verified": bool(trusted)},
        )


def _iso(dt):
    return dt.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_response_body(occ):
    event = occ.event
    if event.thank_you_mode == Event.ThankYouMode.REDIRECT and event.thank_you_url:
        thank_you = {"mode": "redirect", "redirectUrl": event.thank_you_url}
    else:
        thank_you = {"mode": "message", "message": event.thank_you_message or "Thanks — you're registered."}
    # Absolute Google/Outlook links (null when cancelled or ended); icsUrl stays relative for
    # the proxy/direct rewrite. Public fields only, never join or waiting-room links.
    web = calendar_links.calendar_links(occ)
    return {
        "status": "registered",
        "occurrenceId": str(occ.pk),
        "thankYou": thank_you,
        "calendar": {
            "title": event.public_title,
            "startsAt": _iso(occ.starts_at),
            "endsAt": _iso(occ.ends_at),
            "timezone": occ.timezone,
            "icsUrl": f"/api/v1/occurrences/{occ.pk}/calendar.ics",
            "googleUrl": web["google"],
            "outlookUrl": web["outlook"],
            "office365Url": web["office365"],
            "uid": occ.ics_uid,
            "sequence": occ.ics_sequence,
        },
    }


def _unverified_result(occ, capability):
    return RegistrationResult(
        status_code=201,
        body=build_response_body(occ),
        set_token=identity.unbound_token() if capability is None else None,
    )


def _process(payload, capability, api_key, trusted, allow_started=False):
    occ = _load_occurrence(payload, allow_started)
    form = occ.event.form_version

    normalized = normalization.normalize_submission(payload.get("profile") or {}, payload.get("customFields") or {}, form)
    if normalized.errors:
        _abort(400, "validation_error", "Some fields are invalid.", dict(normalized.errors))

    person, verified, issue_cookie = _resolve_identity(capability, normalized.fields.get("email"))
    _check_required(form, person, verified, normalized)

    consents = payload.get("consents") or {}
    sms_phone = _validate_consents(consents, form, person, verified, normalized)

    org_ids = (payload.get("organization") or {}).get("externalIds") or []
    profile_changed = False
    new_person = False
    if person is None:
        person = Person()
        _apply_fields(person, normalized)
        person.save()
        new_person = True
    elif verified:
        if _apply_fields(person, normalized):
            person.save()
            profile_changed = True

    submitted_profile = dict(normalized.fields)
    if consents and not verified:
        # Unverified claim evidence only; never applied to the existing person's consent state.
        submitted_profile["consentClaims"] = {
            p: {"granted": bool(e["granted"]), "disclosureVersion": (e.get("disclosureVersion") or "")[:64] or None}
            for p, e in consents.items()
        }
    if org_ids and not verified:
        submitted_profile["organization"] = {"externalIds": [{"namespace": i["namespace"], "value": i["value"].strip()} for i in org_ids]}
    if verified and org_ids:
        _record_org_ids(person, org_ids, trusted)

    registration = Registration.objects.select_for_update().filter(occurrence=occ, person=person).first()
    created = registration is None
    reactivated = False
    if created:
        registration = Registration.objects.create(
            occurrence=occ,
            person=person,
            form_version=form.version,
            identity_verified=verified,
            submitted_profile=submitted_profile,
            custom_fields=dict(normalized.custom),
        )
    elif verified:
        update_fields = ["updated_at", "form_version", "identity_verified", "submitted_profile", "custom_fields"]
        if registration.status == Registration.Status.CANCELLED:
            registration.status = Registration.Status.REGISTERED
            reactivated = True
            update_fields.append("status")
        registration.form_version = form.version
        registration.identity_verified = True
        registration.submitted_profile = submitted_profile
        registration.custom_fields = {**(registration.custom_fields or {}), **normalized.custom}
        registration.save(update_fields=update_fields)
    else:
        # Unverified duplicate: the existing registration is never reactivated or
        # mutated, and no consent, attribution, reminder or webhook is produced.
        audit(
            "registration.deduplicated",
            api_key=api_key,
            obj=registration,
            occurrence_id=str(occ.pk),
            person_id=str(person.pk),
            identity_verified=False,
            new_person=False,
            reactivated=False,
        )
        return _unverified_result(occ, capability), registration

    if consents:
        _record_consents(consents, form, person, registration, sms_phone, verified=verified)

    cleaned = attribution_mod.clean_attribution(payload.get("attribution") or {}, consents)
    touch_kwargs = dict(
        parameters=cleaned.get("parameters") or {},
        identifiers=cleaned.get("identifiers") or {},
        page_url=(cleaned.get("pageUrl") or "")[:2000],
        referrer=(cleaned.get("referrer") or "")[:2000],
    )
    # For an unverified submission this touch belongs only to the registration it just
    # created; it never replaces another registration's touch or the person's first touch.
    AttributionTouch.objects.create(
        person=person, registration=registration, touch_type=AttributionTouch.TouchType.REGISTRATION_TOUCH, **touch_kwargs
    )
    if verified and not AttributionTouch.objects.filter(person=person, touch_type=AttributionTouch.TouchType.FIRST_TOUCH).exists():
        AttributionTouch.objects.create(
            person=person, registration=registration, touch_type=AttributionTouch.TouchType.FIRST_TOUCH, **touch_kwargs
        )

    if created:
        scheduling.create_reminders(registration)
        outbox.enqueue_event("registration.created", registration=registration, occurrence=occ)
    else:
        if reactivated:
            try:
                with transaction.atomic():
                    scheduling.create_reminders(registration)
            except IntegrityError:
                pass  # reminder slots for this generation already exist
        if reactivated or profile_changed or (verified and consents):
            outbox.enqueue_event("registration.updated", registration=registration, occurrence=occ)

    set_token = identity.issue_capability(person) if (new_person and issue_cookie) else None
    if set_token is None and capability is None:
        # Cookie-less callers always get a Set-Cookie so its presence does not reveal
        # whether the email already exists. The decoy is random and never stored.
        set_token = identity.unbound_token()

    audit(
        "registration.created" if created else "registration.deduplicated",
        api_key=api_key,
        obj=registration,
        occurrence_id=str(occ.pk),
        person_id=str(person.pk),
        identity_verified=verified,
        new_person=new_person,
        reactivated=reactivated,
    )

    # Unverified submissions always get 201 so the status code does not disclose
    # whether that email was already registered for this occurrence.
    status_code = 201 if (created or not verified) else 200
    return RegistrationResult(status_code=status_code, body=build_response_body(occ), set_token=set_token), registration

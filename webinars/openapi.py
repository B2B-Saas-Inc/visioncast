"""OpenAPI 3.1 description of every HTTP API (served at /api/v1/openapi.json).

Built in code (not docs/) because the container image ships only webinars/ and
config/. Keep it in sync with webinars/urls.py; tests/test_mcp.py and
tests/test_forms_schema.py check selected paths exist.
"""

from webinars import attribution, registration
from webinars.api import management
from webinars.models import Event, Occurrence
from webinars.normalization import CANONICAL_FIELDS

ERR = {"$ref": "#/components/schemas/Error"}


def _ref(name):
    return {"$ref": f"#/components/schemas/{name}"}


def _json(schema, description="OK"):
    return {"description": description, "content": {"application/json": {"schema": schema}}}


def _errors(*codes):
    names = {400: "Validation error", 401: "Missing or invalid API key", 403: "Key lacks scope / origin rejected",
             404: "Not found", 405: "Method not allowed", 409: "Conflict", 410: "Closed", 413: "Too large",
             415: "Content-Type must be application/json", 429: "Rate limited"}
    return {str(c): _json(ERR, names.get(c, "Error")) for c in codes}


def _op(summary, tag, *, security=None, params=None, body=None, responses=None, description=None):
    op = {"summary": summary, "tags": [tag], "responses": responses or {"200": _json({"type": "object"})}}
    if description:
        op["description"] = description
    if security is not None:
        op["security"] = security
    if params:
        op["parameters"] = params
    if body is not None:
        op["requestBody"] = {"required": True, "content": {"application/json": {"schema": body}}}
    return op


def _path_param(name, fmt=None):
    schema = {"type": "string"}
    if fmt:
        schema["format"] = fmt
    return {"name": name, "in": "path", "required": True, "schema": schema}


def _q(name, schema, description="", required=False):
    return {"name": name, "in": "query", "required": required, "schema": schema, "description": description}


PROFILE_QUERY = [_q("occurrenceId", {"type": "string", "format": "uuid"}, required=True),
                 _q("formVersion", {"type": "string"}, "Must match the event's form version.", required=True)]
OCCURRENCE_QUERY = [_q("occurrenceId", {"type": "string", "format": "uuid"},
                       "The only accepted parameter (exactly once); anything else is a 400.", required=True)]
MY_REGISTRATION_NOTE = ("Status of this browser's registration for one public/unlisted occurrence, identified only by the "
                        "capability cookie (never an email or id). Unrecognised browsers and recognised people without a "
                        "registration get the same shape. thankYou/calendar are present only when registered.")
# GET on these management resources also accepts config_read (docs/management-api.md).
READ_NOTE = ("Scopes: content_write, or config_read for read-only access (GET only). Read-only responses have "
             "waitingRoom.url = null; provider join/waiting-room links are never returned to anyone.")


KEY = [{"apiKey": []}]
# The Webinar-Confirm-Token request header is described in PUBLISH_NOTE (operation
# parameters on templated paths are reserved for path parameters).
CONFIRMATION = {"type": "object", "properties": {"error": ERR, "preview": {"type": "object"}, "confirmToken": {"type": "string"}}}
PUBLISH_NOTE = ("Publication (a visibility change or a recording) needs the publication scope and two steps: the first "
                "request returns 428 with a side-effect-free preview and a confirmToken; repeat the identical request "
                "with header Webinar-Confirm-Token. The token proves only that the same key asked twice; it is not "
                "independent human approval.")
FORM = {"application/x-www-form-urlencoded": {"schema": {"type": "object"}}}


def _form_op(summary, fields, description, responses):
    return {
        "summary": summary, "tags": ["Waiting room"], "description": description,
        "requestBody": {"required": True, "content": {"application/x-www-form-urlencoded": {"schema": {
            "type": "object", "properties": {f: {"type": "string"} for f in fields + ["csrfmiddlewaretoken"]}}}}},
        "responses": responses,
    }


TICKETS_TAG = "Management (tickets)"
HOSTING_TAG = "Management (hosting)"
_OBJ = {"type": "object"}
_S = {"type": "string"}
_UUID_S = {"type": "string", "format": "uuid"}


def _tickets_and_hosting_paths():
    """Ticket (docs/tickets.md) and hosting-provider (docs/hosting.md) endpoints."""
    from webinars.api import hosting, tickets

    occ = [_path_param("id", "uuid")]
    std = _errors(400, 401, 403, 404, 409)
    hosting_note = ("Provisioning is never automatic: preview, then execute with Webinar-Confirm-Token (428 first). "
                    "Unknown outcomes stop for reconciliation; external sessions are never deleted. Private join/host "
                    "links are never returned (hasJoinUrl/hasHostUrl flags only).")
    return {
        "/api/v1/payments/stripe/webhook": {"post": {
            "summary": "Stripe webhook (signature-authenticated)", "tags": ["Tickets"], "security": [],
            "description": "Stripe-Signature over the raw body (5-minute tolerance), durable event-id dedupe. Fulfils only "
                           "checkout.session.completed / async_payment_succeeded with payment_status=paid and matching "
                           "order, session, amount, currency and mode. No API key, cookie or CSRF token.",
            "requestBody": {"required": True, "content": {"application/json": {"schema": _OBJ}}},
            "responses": {"200": _json({"type": "object", "properties": {"received": {"type": "boolean"}, "duplicate": {"type": "boolean"}}}),
                          "400": {"description": "Invalid signature, payload, mode or account"}, "413": {"description": "Too large"}}}},
        "/api/v1/orders/{id}": {"parameters": occ, "get": _op("Ticket order status", "Tickets", security=[],
            description="Authorised only by the statusToken returned with payment_required, sent in the Webinar-Order-Token "
                        "header (never the query string). Uniform 404 otherwise. Never contains personal data.",
            responses={"200": _json(_ref("TicketOrderStatus")), **_errors(404)})},
        "/tickets/return": {"get": {"summary": "Checkout return page (HTML; confirms nothing)", "tags": ["Tickets"], "security": [],
            "responses": {"200": {"description": "HTML", "content": {"text/html": {}}}}}},
        "/api/v1/manage/payments/account": {
            "get": _op("Stripe account (redacted)", TICKETS_TAG, security=KEY, description="Scope: payments_admin (superuser-granted).",
                       responses={"200": _json(_ref("PaymentAccount")), **_errors(401, 403)}),
            "patch": _op("Configure Stripe (set-only secrets)", TICKETS_TAG, security=KEY, body=tickets.PAYMENT_ACCOUNT_INPUT_SCHEMA,
                         responses={"200": _json(_ref("PaymentAccount")), **_errors(400, 401, 403)})},
        "/api/v1/manage/events/{id}/tickets": {"parameters": occ,
            "get": _op("Event ticket price", TICKETS_TAG, security=KEY, responses={"200": _json(_ref("EventTickets")), **_errors(401, 403, 404)}),
            "patch": _op("Set event ticket price (payments_admin)", TICKETS_TAG, security=KEY, body=tickets.EVENT_TICKETS_INPUT_SCHEMA,
                         responses={"200": _json(_ref("EventTickets")), **std})},
        "/api/v1/manage/occurrences/{id}/tickets": {"parameters": occ,
            "get": _op("Session seats and price", TICKETS_TAG, security=KEY, description="content_write, payments_admin or config_read (GET).",
                       responses={"200": _json(_ref("OccurrenceTickets")), **_errors(401, 403, 404)}),
            "patch": _op("Set capacity (content_write) / price (payments_admin)", TICKETS_TAG, security=KEY,
                         body=tickets.OCCURRENCE_TICKETS_INPUT_SCHEMA, responses={"200": _json(_ref("OccurrenceTickets")), **std})},
        "/api/v1/manage/occurrences/{id}/orders": {"parameters": occ, "get": _op("Ticket orders (no personal data)", TICKETS_TAG, security=KEY,
            responses={"200": _json({"type": "object", "properties": {"occurrenceId": _UUID_S, "counts": _OBJ,
                                     "results": {"type": "array", "items": _ref("TicketOrderAdmin")}}}), **_errors(401, 403, 404)})},
        "/api/v1/manage/orders/{id}/mark-refunded": {"parameters": occ, "post": _op("Record a refund made in Stripe", TICKETS_TAG, security=KEY,
            body=tickets.ORDER_ACTION_INPUT_SCHEMA, description="Bookkeeping only (no money moves); cancels the registration.",
            responses={"200": _json(_ref("TicketOrderAdmin")), **std})},
        "/api/v1/manage/orders/{id}/release": {"parameters": occ, "post": _op("Release an unpaid order needing reconciliation", TICKETS_TAG,
            security=KEY, body=tickets.ORDER_ACTION_INPUT_SCHEMA, description="Only after checking in Stripe that it was not paid.",
            responses={"200": _json(_ref("TicketOrderAdmin")), **std})},
        "/api/v1/manage/hosting/providers": {"get": _op("Hosting adapters and capabilities", HOSTING_TAG, security=KEY,
            description="Scope: hosting_provision or config_read. Distinguishes provisioning capabilities from manual links.",
            responses={"200": _json({"type": "object", "properties": {"results": {"type": "array", "items": _OBJ}}}), **_errors(401, 403)})},
        "/api/v1/manage/hosting/accounts": {
            "get": _op("Hosting accounts (redacted)", HOSTING_TAG, security=KEY,
                       responses={"200": _json({"type": "object", "properties": {"results": {"type": "array", "items": _ref("HostingAccount")}}}), **_errors(401, 403)}),
            "post": _op("Connect a hosting account (hosting_admin)", HOSTING_TAG, security=KEY, body=hosting.ACCOUNT_INPUT_SCHEMA,
                        responses={"201": _json(_ref("HostingAccount")), **_errors(400, 401, 403)})},
        "/api/v1/manage/hosting/accounts/{id}": {"parameters": occ,
            "get": _op("Hosting account", HOSTING_TAG, security=KEY, responses={"200": _json(_ref("HostingAccount")), **_errors(401, 403, 404)}),
            "patch": _op("Update hosting account (set-only credentials)", HOSTING_TAG, security=KEY, body=hosting.ACCOUNT_UPDATE_SCHEMA,
                         responses={"200": _json(_ref("HostingAccount")), **std})},
        "/api/v1/manage/occurrences/{id}/hosting": {"parameters": occ, "get": _op("Session provider link state", HOSTING_TAG, security=KEY,
            description=hosting_note, responses={"200": _json(_ref("HostedSession")), **_errors(401, 403, 404)})},
        "/api/v1/manage/occurrences/{id}/hosting/preview": {"parameters": occ, "post": _op("Preview a provider action (no side effects)",
            HOSTING_TAG, security=KEY, body=hosting.PREVIEW_INPUT_SCHEMA,
            responses={"200": _json({"type": "object", "properties": {"action": _S, "provider": _S, "plans": {"type": "array", "items": _OBJ},
                                     "problems": {"type": "array", "items": _S}}}), **std})},
        "/api/v1/manage/occurrences/{id}/hosting/execute": {"parameters": occ, "post": _op("Create / update / attach (confirmed)",
            HOSTING_TAG, security=KEY, body=hosting.PREVIEW_INPUT_SCHEMA, description=hosting_note,
            responses={"200": _json(_ref("HostedSession")), "428": _json(CONFIRMATION, "Confirmation required"), **std})},
        "/api/v1/manage/occurrences/{id}/hosting/reconcile": {"parameters": occ, "post": _op("Resolve an unknown outcome", HOSTING_TAG,
            security=KEY, body=hosting.RECONCILE_INPUT_SCHEMA, responses={"200": _json(_ref("HostedSession")), **std})},
        "/api/v1/manage/occurrences/{id}/hosting/registrants": {"parameters": occ, "get": _op(
            "Stalled Riverside registrants (IDs and state only)", HOSTING_TAG, security=KEY,
            description="Scope: hosting_provision or config_read. No names, emails or links; no provider call, no writes.",
            responses={"200": _json({"type": "object", "properties": {"occurrenceId": _UUID_S, "note": _S,
                                     "results": {"type": "array", "items": _ref("ProviderRegistrant")}}}), **_errors(401, 403, 404)})},
        "/api/v1/manage/occurrences/{id}/hosting/registrants/reconcile": {
            "parameters": occ, "post": _op(
                "Record a Riverside registrant outcome (confirmed)", HOSTING_TAG, security=KEY,
                body=hosting.REGISTRANT_RECONCILE_INPUT_SCHEMA,
                description="Scope: hosting_provision. First call returns 428 with a link-free preview and a single-use "
                            "confirmToken bound to this key, the exact body and the registrant's attempt/state; repeat with "
                            "Webinar-Confirm-Token. Other-occurrence registration IDs are 404; registrants not in "
                            "needs_reconciliation or non-Riverside are 409. Nothing is sent to Riverside; joinUrl is set-only.",
                responses={"200": _json(_ref("ProviderRegistrant")), "428": _json(CONFIRMATION, "Confirmation required"),
                           **std})},
        "/api/v1/manage/occurrences/{id}/hosting/manual-link": {"parameters": occ, "post": _op("Manual link (StreamYard; no outbound calls)",
            HOSTING_TAG, security=KEY, body=hosting.MANUAL_LINK_INPUT_SCHEMA, responses={"200": _json(_ref("HostedSession")), **std})},
        **_attendance_import_paths(),
    }


ATTENDANCE_TAG = "Management (attendance import)"


def _attendance_import_paths():
    """Attendance import endpoints (docs/hosting.md, "Attendance import")."""
    from webinars.api import attendance_imports as ai

    occ = [_path_param("id", "uuid")]
    imp = occ + [_path_param("importId", "uuid")]
    row = imp + [_path_param("rowId", "uuid")]
    std = _errors(400, 401, 403, 404, 409)
    note = ("Scope: attendance_write. The first call returns 428 with a preview and a single-use confirmToken bound to "
            "this key and the import's state; repeat with Webinar-Confirm-Token. ")
    base = "/api/v1/manage/occurrences/{id}/attendance/imports"
    return {
        base: {"parameters": occ,
            "get": _op("Attendance imports of a session", ATTENDANCE_TAG, security=KEY,
                       description="Scope: attendance_write or config_read. Counts and state only (no rows).",
                       responses={"200": _json({"type": "object", "properties": {"occurrenceId": _UUID_S,
                                                "results": {"type": "array", "items": _ref("AttendanceImport")}}}),
                                  **_errors(401, 403, 404)}),
            "post": _op("Create an import: provider pull or supplied rows", ATTENDANCE_TAG, security=KEY,
                        body=ai.CREATE_INPUT_SCHEMA,
                        description="Scope: attendance_write. source=provider makes one read-only, durable pull from the "
                                    "linked hosting provider (Zoom, Riverside; refused for providers without "
                                    "attendanceImport, and when the account is disabled). source=file stores the given "
                                    "rows (max 5000). Rows are matched (provider id, then email) and deduplicated; nothing "
                                    "is recorded until the import is applied. A failed pull is returned with state=failed "
                                    "(safe to retry).",
                        responses={"201": _json(_ref("AttendanceImport")), **std})},
        base + "/{importId}": {"parameters": imp,
            "get": _op("Attendance import with its rows (masked emails)", ATTENDANCE_TAG, security=KEY,
                       description="Scope: attendance_write or config_read. Never full emails or links.",
                       responses={"200": _json(_ref("AttendanceImport")), **_errors(401, 403, 404)})},
        base + "/{importId}/apply": {"parameters": imp,
            "post": _op("Apply an import (confirmed)", ATTENDANCE_TAG, security=KEY, body=ai.EMPTY_INPUT_SCHEMA,
                        description=note + "Records attended for every matched row whose registration is not already "
                                           "attended (attendance.recorded once per registration); re-applying records nothing new.",
                        responses={"200": _json(_ref("AttendanceImport")), "428": _json(CONFIRMATION, "Confirmation required"), **std})},
        base + "/{importId}/rows/{rowId}/resolve": {"parameters": row,
            "post": _op("Resolve an unmatched/ambiguous row", ATTENDANCE_TAG, security=KEY, body=ai.RESOLVE_INPUT_SCHEMA,
                        description="Scope: attendance_write. registrationId (active registration of this session: attendance "
                                    "is recorded at once) or ignore=true. Rows already matched, resolved or ignored are 409.",
                        responses={"200": _json({"type": "object", "properties": {"importId": _UUID_S, "row": _ref("AttendanceImportRow")}}),
                                   **std})},
        base + "/{importId}/no-shows": {"parameters": imp,
            "post": _op("Mark remaining registrations no_show (confirmed)", ATTENDANCE_TAG, security=KEY, body=ai.EMPTY_INPUT_SCHEMA,
                        description=note + "Only after the import is applied and the session has ended. Registrants who "
                                           "checked in through the waiting room are never marked no_show (reported as refused).",
                        responses={"200": _json(_ref("AttendanceImport")), "428": _json(CONFIRMATION, "Confirmation required"), **std})},
    }


def _tickets_and_hosting_schemas():
    from webinars.api import attendance_imports as ai
    from webinars.api import hosting

    n_int = {"type": ["integer", "null"]}
    n_str = {"type": ["string", "null"]}
    return {
        "TicketOrderStatus": {"type": "object", "properties": {"id": _UUID_S, "status": _S, "registered": {"type": "boolean"},
            "checkoutUrl": {"type": ["string", "null"], "format": "uri"}, "expiresAt": n_str, "amountMinor": {"type": "integer"},
            "currency": _S, "statusPath": _S, "statusToken": _S}},
        "PaymentRequired": {"type": "object", "description": "202 from registration endpoints for a paid session: a seat is held, "
                            "nothing is registered until Stripe's verified webhook.",
            "properties": {"status": {"const": "payment_required"}, "order": _ref("TicketOrderStatus")}},
        "VerificationRequired": {"type": "object", "description": "202 from registration endpoints for a paid session when the "
                                 "browser has no mailbox-verified identity for the submitted email. Identical for every email "
                                 "(no lookup result); nothing is held and Stripe is not called. Send the buyer to "
                                 "verification.verifyUrl (the hosted waiting room, usable any time before the session ends).",
            "properties": {"status": {"const": "verification_required"}, "occurrenceId": _UUID_S,
                           "verification": {"type": "object", "properties": {
                               "available": {"type": "boolean", "description": "false when the session has no waiting room or email delivery is disabled"},
                               "verifyUrl": {"type": ["string", "null"], "format": "uri"},
                               "message": _S}}}},
        "PaymentAccount": {"type": "object", "properties": {"configured": {"type": "boolean"}, "id": _UUID_S, "enabled": {"type": "boolean"},
            "mode": n_str, "secretKeySet": {"type": "boolean"}, "webhookSecretSet": {"type": "boolean"}, "expectedAccountId": _S,
            "webhookUrl": _S, "problems": {"type": "array", "items": _S}}},
        "EventTickets": {"type": "object", "properties": {"eventId": _UUID_S, "priceMinor": n_int, "currency": _S, "display": _S}},
        "OccurrenceTickets": {"type": "object", "properties": {"occurrenceId": _UUID_S, "capacity": n_int, "showRemaining": {"type": "boolean"},
            "remaining": n_int, "priceMinor": n_int, "currency": _S, "effectivePrice": {"type": ["object", "null"]},
            "paymentsAvailable": {"type": "boolean"}, "orderCounts": _OBJ}},
        "TicketOrderAdmin": {"type": "object", "properties": {"id": _UUID_S, "occurrenceId": _UUID_S, "status": _S, "mode": _S,
            "amountMinor": {"type": "integer"}, "currency": _S, "display": _S, "registrationId": n_str,
            "hasCheckoutSession": {"type": "boolean"}, "createdAt": n_str, "holdExpiresAt": n_str, "paidAt": n_str, "lastError": _S}},
        "HostingAccount": {"type": "object", "properties": {"id": _UUID_S, "provider": _S, "name": _S, "enabled": {"type": "boolean"},
            "config": _OBJ, "secretsSet": _OBJ, "capabilities": _OBJ, "contract": _S, "warnings": {"type": "array", "items": _S}}},
        "HostedSession": {"type": "object", "properties": {"provider": n_str, "accountId": n_str, "mode": _S, "state": _S,
            "externalId": n_str, "hasJoinUrl": {"type": "boolean"}, "hasHostUrl": {"type": "boolean"}, "embedUrlSet": {"type": "boolean"},
            "lastError": n_str, "lastSyncedAt": n_str, "outOfDate": {"type": "boolean"}, "capabilities": {"type": ["object", "null"]},
            "contract": n_str, "operations": {"type": "array", "items": _OBJ}}},
        "ProviderRegistrant": hosting.REGISTRANT_SCHEMA,
        "AttendanceImport": ai.IMPORT_SCHEMA,
        "AttendanceImportRow": ai.ROW_SCHEMA,
        "Availability": {"type": "object", "description": "Present only when the session has a seat limit.",
            "properties": {"remaining": {**n_int, "description": "null unless the organiser shows remaining seats"}, "soldOut": {"type": "boolean"}}},
        "PublicTicket": {"type": "object", "description": "Present only for paid sessions.",
            "properties": {"amountMinor": {"type": "integer"}, "currency": _S, "display": _S, "available": {"type": "boolean"}}},
    }


def build_spec():
    see_other = {"303": {"description": "See Other (back to the room)"}, "429": {"description": "Rate limited"}}
    paths = {
        "/api/v1/events": {"get": _op("List public events", "Public", security=[], params=[
            _q("q", {"type": "string"}, "Search title/summary"), _q("topic", {"type": "string"}), _q("host", {"type": "string"}),
            _q("when", {"type": "string", "enum": ["upcoming", "recorded"]}), _q("limit", {"type": "integer", "minimum": 1, "maximum": 50}),
            _q("offset", {"type": "integer", "minimum": 0})],
            responses={"200": _json({"type": "object", "properties": {"count": {"type": "integer"}, "limit": {"type": "integer"},
                                     "offset": {"type": "integer"}, "results": {"type": "array", "items": {"$ref": "#/components/schemas/PublicEvent"}}}}),
                       **_errors(400)})},
        "/api/v1/events/{slug}": {"get": _op("Get a public or unlisted event", "Public", security=[], params=[_path_param("slug")],
                                             responses={"200": _json({"$ref": "#/components/schemas/PublicEvent"}), **_errors(404)})},
        "/api/v1/occurrences/{id}/calendar.ics": {"get": _op("ICS calendar file", "Public", security=[], params=[_path_param("id", "uuid")],
            responses={"200": {"description": "text/calendar", "content": {"text/calendar": {"schema": {"type": "string"}}}}, **_errors(404)})},
        "/api/v1/forms/{version}": {"get": _op("Canonical registration form schema", "Public", security=[], params=[_path_param("version")],
            description="Field keys (canonical camelCase or custom.<key>), labels, types, options and SMS disclosure. No personal data.",
            responses={"200": _json({"$ref": "#/components/schemas/FormSchema"}), **_errors(404)})},
        "/api/v1/registrations": {"post": _op("Register (server-side or proxy)", "Registration", security=KEY,
            description="Scopes: register, proxy or trusted. Idempotent by idempotencyKey. Responses never contain profile data or join links.",
            body={"$ref": "#/components/schemas/RegistrationRequest"},
            responses={"201": _json({"$ref": "#/components/schemas/RegistrationResponse"}, "Registered"),
                       "200": _json({"$ref": "#/components/schemas/RegistrationResponse"}, "Updated"),
                       "202": _json({"oneOf": [_ref("PaymentRequired"), _ref("VerificationRequired")]},
                                    "Paid session: payment_required (seat held, pay at order.checkoutUrl; not registered yet) "
                                    "or verification_required (confirm the email first; nothing held)"),
                       "503": _json(ERR, "Paid session but ticket sales are not configured (payments_unavailable)"),
                       **_errors(400, 401, 403, 404, 409, 410, 413, 415, 429)})},
        "/api/v1/profile": {"get": _op("Progressive-profiling missing fields (proxy key)", "Registration", security=KEY,
            params=PROFILE_QUERY,
            responses={"200": _json(_ref("ProfileStatus")), **_errors(400, 401, 403, 404)})},
        "/api/v1/reset": {"post": _op("Forget this browser (proxy key)", "Registration", security=KEY,
                                      responses={"200": _json(_ref("ResetResponse")), **_errors(401, 403)})},
        "/webinar-api/profile": {"get": _op("Same-origin profile (cookie)", "Same-origin proxy", security=[],
            params=PROFILE_QUERY,
            description="Origin must be in WEBINAR_ALLOWED_ORIGINS.", responses={"200": _json(_ref("ProfileStatus")), **_errors(400, 403, 404)})},
        "/webinar-api/registrations": {"post": _op("Same-origin registration (cookie)", "Same-origin proxy", security=[],
            description="Requires Origin, JSON content type and X-Webinar-Request: 1 or the X-Webinar-CSRF double-submit header.",
            body={"$ref": "#/components/schemas/RegistrationRequest"},
            responses={"201": _json({"$ref": "#/components/schemas/RegistrationResponse"}),
                       "202": _json({"oneOf": [_ref("PaymentRequired"), _ref("VerificationRequired")]}, "Paid session (body passed through)"),
                       "502": _json({"type": "object"}, "Upstream 5xx (including payments_unavailable) is reported as bad_gateway"),
                       **_errors(400, 403, 409, 410)})},
        "/webinar-api/reset": {"post": _op("Same-origin reset", "Same-origin proxy", security=[], responses={"200": _json(_ref("ResetResponse")), **_errors(403)})},
        "/api/v1/my-registration": {"get": _op("Registration status for this browser (proxy key)", "Registration", security=KEY,
            params=OCCURRENCE_QUERY, description="Scope: proxy (the proxy forwards the capability cookie). " + MY_REGISTRATION_NOTE,
            responses={"200": _json(_ref("MyRegistration")), **_errors(400, 401, 403, 404, 405)})},
        "/webinar-api/my-registration": {"get": _op("Same-origin registration status (cookie)", "Same-origin proxy", security=[],
            params=OCCURRENCE_QUERY,
            description="Origin, if sent, must be in WEBINAR_ALLOWED_ORIGINS; Sec-Fetch-Site, if sent, must be same-origin; "
                        "requires X-Webinar-Request: 1 or the X-Webinar-CSRF double-submit header; rate limited. " + MY_REGISTRATION_NOTE,
            responses={"200": _json(_ref("MyRegistration")), **_errors(400, 403, 404, 405, 429)})},
        "/webinar-api/calendar.ics": {"get": _op("Same-origin ICS calendar file", "Same-origin proxy", security=[], params=OCCURRENCE_QUERY,
            description="Same file and visibility rule as /api/v1/occurrences/{id}/calendar.ics. Reads no cookie or key. "
                        "Direct-mode registration responses point calendar.icsUrl here unless WEBINAR_DIRECT_ICS_REWRITE=false.",
            responses={"200": {"description": "text/calendar", "content": {"text/calendar": {"schema": {"type": "string"}}}},
                       **_errors(400, 404, 405)})},
        "/healthz": {"get": _op("Health check", "Operations", security=[], responses={
            "200": _json({"type": "object", "properties": {"status": {"const": "ok"}}}),
            "503": _json({"type": "object", "properties": {"status": {"const": "unavailable"}}}, "Database unavailable")})},
    }
    mg = "Management (content_write)"
    paths.update({
        "/api/v1/manage/events": {
            "get": _op("List all events", mg, security=KEY, description="Newest first, at most 200; no pagination. Includes drafts. " + READ_NOTE,
                       responses={"200": _json(_ref("ManagedEventList")), **_errors(401, 403, 405)}),
            "post": _op("Create a draft event", mg, security=KEY, body={"$ref": "#/components/schemas/EventInput"},
                        responses={"201": _json({"$ref": "#/components/schemas/ManagedEvent"}), **_errors(400, 401, 403, 409)})},
        "/api/v1/manage/events/{id}": {
            "parameters": [_path_param("id", "uuid")],
            "get": _op("Get event", mg, security=KEY, description=READ_NOTE, responses={"200": _json({"$ref": "#/components/schemas/ManagedEvent"}), **_errors(401, 403, 404)}),
            "patch": _op("Update / publish event", mg, security=KEY, body={"$ref": "#/components/schemas/EventUpdate"},
                         description=PUBLISH_NOTE,
                         responses={"200": _json({"$ref": "#/components/schemas/ManagedEvent"}), **_errors(400, 401, 403, 404, 409),
                                    "428": _json(CONFIRMATION, "Visibility change needs confirmation")}),
            "delete": _op("Delete a draft event without registrations", mg, security=KEY, responses={"204": {"description": "Deleted"}, **_errors(401, 403, 404, 409)})},
        "/api/v1/manage/events/{id}/occurrences": {"parameters": [_path_param("id", "uuid")],
            "post": _op("Schedule a session", mg, security=KEY, body={"$ref": "#/components/schemas/OccurrenceInput"},
                        responses={"201": _json({"$ref": "#/components/schemas/ManagedOccurrence"}), **_errors(400, 401, 403, 404)})},
        "/api/v1/manage/occurrences/{id}": {"parameters": [_path_param("id", "uuid")],
            "get": _op("Get session", mg, security=KEY, description=READ_NOTE, responses={"200": _json({"$ref": "#/components/schemas/ManagedOccurrence"}), **_errors(401, 403, 404)}),
            "patch": _op("Update / reschedule session", mg, security=KEY, body={"$ref": "#/components/schemas/OccurrenceUpdate"},
                         responses={"200": _json({"$ref": "#/components/schemas/ManagedOccurrence"}), **_errors(400, 401, 403, 404, 409)})},
        "/api/v1/manage/occurrences/{id}/cancel": {"parameters": [_path_param("id", "uuid")],
            "post": _op("Cancel session", mg, security=KEY, body={"type": "object", "additionalProperties": False},
                        responses={"200": _json({"$ref": "#/components/schemas/ManagedOccurrence"}), **_errors(401, 403, 404)})},
        "/api/v1/manage/occurrences/{id}/recording": {"parameters": [_path_param("id", "uuid")],
            "post": _op("Publish recording", "Management (publication)", security=KEY, body={"type": "object", "required": ["recordingUrl"], "properties": {"recordingUrl": {"type": "string", "format": "uri"}}},
                        description="Requires the publication scope. " + PUBLISH_NOTE,
                        responses={"200": _json({"$ref": "#/components/schemas/ManagedOccurrence"}), **_errors(400, 401, 403, 404, 409),
                                   "428": _json(CONFIRMATION, "Needs confirmation")})},
        "/api/v1/manage/occurrences/{id}/waiting-room": {"parameters": [_path_param("id", "uuid")],
            "get": _op("Waiting room settings and state", mg, security=KEY, description=READ_NOTE, responses={"200": _json({"$ref": "#/components/schemas/WaitingRoom"}), **_errors(401, 403, 404)}),
            "patch": _op("Configure waiting room / live admission", mg, security=KEY,
                         body=_ref("WaitingRoomUpdate"),
                         responses={"200": _json({"$ref": "#/components/schemas/WaitingRoom"}), **_errors(400, 401, 403, 404, 409)})},
        "/api/v1/manage/registrations/{id}/attendance": {"parameters": [_path_param("id", "uuid")],
            "post": _op("Record attendance (attendance_write)", "Management (attendance_write)", security=KEY,
                        body={"$ref": "#/components/schemas/AttendanceInput"},
                        responses={"200": _json({"type": "object", "properties": {"registrationId": {"type": "string", "format": "uuid"}, "occurrenceId": {"type": "string", "format": "uuid"}, "attendance": {"type": "string"}}}),
                                   **_errors(400, 401, 403, 404, 409)})},
        "/api/v1/manage/hosts": {"post": _op("Create host", mg, security=KEY, body=_ref("HostInput"),
            responses={"201": _json(_ref("Host")), **_errors(400, 401, 403, 409)})},
        "/api/v1/manage/hosts/{slug}": {"parameters": [_path_param("slug")], "patch": _op("Update host", mg, security=KEY, body=_ref("HostUpdate"),
            responses={"200": _json(_ref("Host")), **_errors(400, 401, 403, 404, 409)})},
        "/api/v1/manage/topics": {"post": _op("Create topic", mg, security=KEY, body=_ref("TopicInput"),
            responses={"201": _json(_ref("Topic")), **_errors(400, 401, 403, 409)})},
        "/api/v1/manage/topics/{slug}": {"parameters": [_path_param("slug")], "patch": _op("Update topic", mg, security=KEY, body=_ref("TopicUpdate"),
            responses={"200": _json(_ref("Topic")), **_errors(400, 401, 403, 404, 409)})},
        "/api/v1/manage/forms": {"post": _op("Create form version (immutable)", mg, security=KEY, body=_ref("FormVersionInput"),
            description="requiredFields/optionalFields must be canonical keys; fieldMapping maps simple alias keys to canonical keys.",
            responses={"201": _json(_ref("ManagedFormVersion")), **_errors(400, 401, 403, 409)})},
        "/api/v1/manage/forms/{version}": {"parameters": [_path_param("version")], "patch": _op("Update description / isActive", mg, security=KEY,
            body=_ref("FormVersionUpdate"), description="Published form versions are immutable except description and isActive.",
            responses={"200": _json(_ref("ManagedFormVersion")), **_errors(400, 401, 403, 404)})},
    })
    paths.update(_tickets_and_hosting_paths())
    room = [_path_param("code")]
    paths.update({
        "/w/{code}/": {"parameters": room, "get": {"summary": "Waiting room page (HTML)", "tags": ["Waiting room"],
            "responses": {"200": {"description": "HTML", "content": {"text/html": {}}}, "404": {"description": "Unknown or disabled room"}}}},
        "/w/{code}/status": {"parameters": room, "get": {"summary": "Poll admission state", "tags": ["Waiting room"],
            "description": "Server-authoritative state for this browser. Never contains the join link or personal data.",
            "responses": {"200": _json({"$ref": "#/components/schemas/RoomStatus"}), "404": {"description": "Not found"}, "429": {"description": "Rate limited"}}}},
        "/w/{code}/identify": {"parameters": room, "post": _form_op("Request an emailed code", ["email"],
            "Identical 303 for known and unknown addresses; sets the wr_pending browser cookie. 400 only for invalid syntax.", see_other)},
        "/w/{code}/verify": {"parameters": room, "post": _form_op("Verify the 6-digit code", ["code"],
            "Single-use, expiring, bound to the requesting browser; max attempts. Success sets the capability cookie.", {**see_other, "400": {"description": "Generic failure"}})},
        "/w/{code}/verify/{token}": {"parameters": room + [_path_param("token")],
            "get": {"summary": "Confirm page for an emailed link (does not consume)", "tags": ["Waiting room"], "responses": {"200": {"description": "HTML"}, "410": {"description": "Expired or used"}}},
            "post": _form_op("Consume emailed link", [], "Single-use; sets the capability cookie.", {**see_other, "410": {"description": "Expired or used"}})},
        "/w/{code}/register": {"parameters": room, "post": _form_op("Complete registration inside the room", ["idempotency_key", "sms_consent"],
            "Verified/recognized browsers only; fields are the form's canonical keys and custom.<key>. Late walk-ins allowed until the room ends. "
            "Paid sessions: a mailbox-verified browser gets 303 to Stripe Checkout (any time before the room ends, not only "
            "after doors open); a recognised but unverified browser gets 202 with the email step (nothing held).",
            {"303": {"description": "Back to the room, or to Stripe Checkout (paid)"}, "429": {"description": "Rate limited"},
             "202": {"description": "HTML: confirm your email first (paid), or checkout still being prepared"},
             "400": {"description": "Field errors"}, "409": {"description": "HTML: sold out / checkout in progress / checkout failed"}})},
        "/w/{code}/enter": {"parameters": room, "post": _form_op("Enter the session", [],
            "When admitted: records one check-in (attendance.checked_in webhook) and returns 303 with the join link in Location only.", {"303": {"description": "To the session (admitted) or back to the room"}})},
        "/w/{code}/reset": {"parameters": room, "post": _form_op("Not you? Forget this browser", [], "Revokes this browser's capability.", see_other)},
        "/mcp": {"post": {"summary": "MCP (JSON-RPC 2.0, stateless Streamable HTTP)", "tags": ["MCP"], "security": KEY,
            "description": "initialize, notifications/initialized (202), ping, tools/list, tools/call. Protocol versions 2025-06-18, 2025-03-26, 2024-11-05. See /developers/mcp/.",
            "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object"}}}},
            "responses": {"200": _json({"type": "object", "properties": {"jsonrpc": {"const": "2.0"}, "id": {}, "result": {}, "error": {"type": "object"}}}),
                          "202": {"description": "Notification or client JSON-RPC response accepted"}, "400": {"description": "Parse/invalid request"},
                          "401": {"description": "Missing key"}, "403": {"description": "Origin not allowed"},
                          "405": {"description": "GET not supported (no SSE)"}, "413": {"description": "Body too large"},
                          "429": {"description": "Rate limited"}}}},
    })
    s = {"type": "string"}
    schemas = {
        "Error": {"type": "object", "properties": {"error": {"type": "object", "properties": {"code": s, "message": s, "fields": {"type": "object"}}},
                                                   "fieldErrors": {"type": "object"}}},
        "PublicOccurrence": {"type": "object", "properties": {"id": {"type": "string", "format": "uuid"}, "startsAt": s, "endsAt": s, "timezone": s,
            "status": s, "registrationOpen": {"type": "boolean"}, "recordingUrl": {"type": ["string", "null"]}, "calendarUrl": s,
            "calendarLinks": _ref("CalendarLinks"), "availability": _ref("Availability"), "ticket": _ref("PublicTicket")}},
        "CalendarLinks": {"type": "object", "additionalProperties": False, "required": ["ics", "google", "outlook", "office365"],
            "description": "Add-to-calendar links built from public fields only, never private join links. Web links are null "
                           "when the session is cancelled or has ended. The event URL in their details comes from "
                           "PUBLIC_EVENT_URL_TEMPLATE (else the JSON event API URL).",
            "properties": {"ics": {"type": "string", "format": "uri"}, "google": {"type": ["string", "null"], "format": "uri"},
                           "outlook": {"type": ["string", "null"], "format": "uri"}, "office365": {"type": ["string", "null"], "format": "uri"}}},
        "PublicEvent": {"type": "object", "properties": {"slug": s, "title": s, "summary": s, "heroImageUrl": {"type": ["string", "null"]},
            "aida": {"type": "object", "properties": {"attention": s, "interest": s, "decision": s, "action": s}},
            "learningOutcomes": {"type": "array", "items": s}, "hosts": {"type": "array", "items": {"type": "object"}},
            "topics": {"type": "array", "items": {"type": "object"}}, "formVersion": s,
            "cta": {"type": "object", "required": ["register", "confirm"], "description": "Registration button labels, defaults resolved.",
                    "properties": {"register": s, "confirm": s}},
            "occurrences": {"type": "array", "items": {"$ref": "#/components/schemas/PublicOccurrence"}}}},
        "FormField": {"type": "object", "properties": {"key": s, "name": s, "type": {"type": "string", "enum": ["text", "email", "tel", "textarea", "select", "checkbox"]},
            "label": s, "help": {"type": ["string", "null"]}, "required": {"type": "boolean"}, "options": {"type": ["array", "null"], "items": s},
            "autocomplete": {"type": ["string", "null"]}, "custom": {"type": "boolean"}}},
        "FormSchema": {"type": "object", "properties": {"schemaVersion": s, "formVersion": s, "description": s,
            "fields": {"type": "array", "items": {"$ref": "#/components/schemas/FormField"}}, "fieldMapping": {"type": "object"},
            "defaultPhoneRegion": {"type": ["string", "null"]}, "sms": {"type": ["object", "null"]}, "isActive": {"type": "boolean"}}},
        "RegistrationRequest": {"type": "object", "required": ["schemaVersion", "occurrenceId", "formVersion", "idempotencyKey", "profile"],
            "properties": {"schemaVersion": {"const": "1"}, "occurrenceId": {"type": "string", "format": "uuid"}, "formVersion": s,
                "idempotencyKey": {"type": "string", "pattern": "^[A-Za-z0-9_\\-:.]{8,128}$"}, "profile": _ref("RegistrationProfile"),
                "organization": {"type": "object", "additionalProperties": False, "properties": {"externalIds": {
                    "type": "array", "maxItems": registration.MAX_EXTERNAL_IDS, "items": {"type": "object", "additionalProperties": False,
                    "required": ["namespace", "value"], "properties": {"namespace": {"type": "string", "pattern": registration.NAMESPACE_RE.pattern},
                                                                       "value": {"type": "string", "minLength": 1, "maxLength": registration.MAX_EXTERNAL_ID_VALUE}}}}}},
                "consents": _ref("Consents"), "attribution": _ref("Attribution"),
                "customFields": {"type": "object", "description": "Keys are the form's customFields.", "additionalProperties": {"type": ["string", "null"]}}},
            "additionalProperties": False,
            "example": {"schemaVersion": "1", "occurrenceId": "3f1c2a9e-7b4d-4c1e-9a55-0d2b6f8e1a10", "formVersion": "f1",
                        "idempotencyKey": "idem-0123456789", "profile": {"email": "ada@example.com", "firstName": "Ada"},
                        "consents": {"sms": {"granted": False}, "analytics": {"granted": True}},
                        "attribution": {"parameters": {"utm_source": "newsletter"}, "pageUrl": "https://www.example.com/webinars/x"}}},
        "RegistrationProfile": {"type": "object", "description": "Canonical keys (or the form's fieldMapping aliases); values are strings. "
                                "Which keys are required depends on the form version.",
            "properties": {k: {"type": ["string", "null"]} for k in CANONICAL_FIELDS},
            "additionalProperties": {"type": ["string", "null"], "description": "fieldMapping alias"}},
        "Consent": {"type": "object", "required": ["granted"], "properties": {"granted": {"type": "boolean"}},
                    "description": "Other keys (e.g. a client-claimed verified flag) are ignored."},
        "Consents": {"type": "object", "additionalProperties": False, "properties": {
            "sms": {"allOf": [_ref("Consent")], "properties": {"disclosureVersion": {"type": ["string", "null"],
                    "description": "Must match the form's current smsDisclosureVersion when granted."}}},
            "analytics": _ref("Consent"), "advertising": _ref("Consent")}},
        "Attribution": {"type": "object", "description": "Descriptive only; unknown keys and values are dropped, and nothing is kept "
                        "without analytics or advertising consent. pageUrl/referrer are reduced to scheme, host and path.",
            "properties": {
                "parameters": {"type": "object", "description": "Kept keys: " + ", ".join(sorted(attribution.ALLOWED_PARAMETERS)),
                               "additionalProperties": {"type": "string", "maxLength": attribution.MAX_VALUE_LENGTH}},
                "identifiers": {"type": "object", "description": "Kept keys: " + ", ".join(sorted(attribution.IDENTIFIER_ALIASES)),
                                "additionalProperties": {"type": "string", "maxLength": attribution.MAX_VALUE_LENGTH}},
                "pageUrl": {"type": "string", "maxLength": attribution.MAX_URL_LENGTH},
                "referrer": {"type": "string", "maxLength": attribution.MAX_URL_LENGTH}}},
        "RegistrationResponse": {"type": "object", "required": ["status", "occurrenceId", "thankYou", "calendar"], "properties": {
            "status": {"type": "string", "examples": ["registered"]}, "occurrenceId": {"type": "string", "format": "uuid"},
            "thankYou": {"type": "object", "required": ["mode"], "properties": {"mode": {"type": "string", "enum": ["message", "redirect"]},
                         "message": s, "redirectUrl": {"type": "string", "format": "uri"}}},
            "calendar": _ref("RegistrationCalendar")}},
        "RegistrationCalendar": {"type": "object", "properties": {"title": s, "startsAt": {"type": "string", "format": "date-time"},
            "endsAt": {"type": "string", "format": "date-time"}, "timezone": s,
            "icsUrl": {"type": "string", "description": "Relative. /api/v1/occurrences/{id}/calendar.ics on key routes; "
                       "/webinar-api/calendar.ics?occurrenceId=<id> from the Node proxy and (by default) direct mode."},
            "googleUrl": {"type": ["string", "null"], "format": "uri"}, "outlookUrl": {"type": ["string", "null"], "format": "uri"},
            "office365Url": {"type": ["string", "null"], "format": "uri", "description": "Web links are null when cancelled or ended."},
            "uid": s, "sequence": {"type": "integer"}}},
        "MyRegistration": {"type": "object", "additionalProperties": False, "required": ["occurrenceId", "recognized", "registered", "status"],
            "properties": {"occurrenceId": {"type": "string", "format": "uuid"}, "recognized": {"type": "boolean"},
                           "registered": {"type": "boolean"},
                           "status": {"type": ["string", "null"], "description": "Registration status, or null when there is none."},
                           "thankYou": {"type": "object", "properties": {"mode": {"type": "string", "enum": ["message", "redirect"]},
                                        "message": s, "redirectUrl": {"type": "string", "format": "uri"}}},
                           "calendar": _ref("RegistrationCalendar")},
            "example": {"occurrenceId": "3f1c2a9e-7b4d-4c1e-9a55-0d2b6f8e1a10", "recognized": False, "registered": False, "status": None}},
        "ProfileStatus": {"type": "object", "required": ["recognized", "missingFields", "formVersion"], "properties": {
            "recognized": {"type": "boolean"}, "missingFields": {"type": "array", "items": s, "description": "Keys only; never values."},
            "formVersion": s}, "example": {"recognized": True, "missingFields": ["company"], "formVersion": "f1"}},
        "ResetResponse": {"type": "object", "properties": {"reset": {"const": True}}},
        # Input schemas come from the same builders the management API and MCP use.
        "EventInput": management.event_input_schema(create=True),
        "EventUpdate": management.event_input_schema(),
        "ManagedEvent": {"type": "object", "additionalProperties": False,
            "description": "Management projection: all copy fields, revision, occurrences (no private links, no person data).",
            "required": ["id", "revision", "slug", "visibility", "occurrences"],
            "properties": {
                "id": {"type": "string", "format": "uuid"}, "revision": {"type": "integer", "minimum": 1},
                "internalName": s, "slug": s, "publicTitle": s, "summary": s,
                "heroImageUrl": {"type": "string", "description": "Public cover URL: the uploaded cover when coverImageId is set, else the image URL."},
                "coverImageId": {"type": ["string", "null"], "format": "uuid", "description": "Studio-uploaded cover (read-only here)."},
                "registerLabel": s, "confirmLabel": s,
                "attention": s, "interest": s, "decision": s, "action": s,
                "learningOutcomes": {"type": "array", "items": s},
                "hosts": {"type": "array", "items": s, "description": "Host slugs, sorted."},
                "topics": {"type": "array", "items": s, "description": "Topic slugs, sorted."},
                "visibility": {"type": "string", "enum": list(Event.Visibility.values)},
                "formVersion": {"type": ["string", "null"]},
                "thankYouMode": {"type": "string", "enum": list(Event.ThankYouMode.values)},
                "thankYouUrl": s, "thankYouMessage": s,
                "createdAt": {"type": ["string", "null"], "format": "date-time"}, "updatedAt": {"type": ["string", "null"], "format": "date-time"},
                "occurrences": {"type": "array", "items": _ref("ManagedOccurrence")}},
            "example": {"id": "0b6c1d8e-2f3a-4b5c-9d7e-1a2b3c4d5e6f", "revision": 1, "slug": "intro", "publicTitle": "Intro",
                        "visibility": "draft", "formVersion": "f1", "hosts": [], "topics": [], "occurrences": []}},
        "ManagedEventList": {"type": "object", "required": ["results"], "properties": {
            "results": {"type": "array", "maxItems": 200, "items": _ref("ManagedEvent")}}},
        "OccurrenceInput": management.occurrence_input_schema(create=True),
        "OccurrenceUpdate": management.occurrence_input_schema(),
        "AttendanceInput": management.attendance_input_schema(),
        "WaitingRoomUpdate": management.waiting_room_input_schema(),
        "HostInput": management.input_schema(management.HOST_FIELDS, management.Host, required=("name", "slug"),
                                             extra={"imageUrl": {"description": "Public https URL."}}),
        "HostUpdate": management.input_schema(management.HOST_FIELDS, management.Host),
        "Host": {"type": "object", "additionalProperties": False, "required": list(management.HOST_FIELDS),
                 "properties": {k: s for k in management.HOST_FIELDS},
                 "example": {"slug": "ada", "name": "Ada Lovelace", "title": "Analyst", "bio": "", "imageUrl": ""}},
        "TopicInput": management.input_schema(management.TOPIC_FIELDS, management.Topic, required=("name", "slug")),
        "TopicUpdate": management.input_schema(management.TOPIC_FIELDS, management.Topic),
        "Topic": {"type": "object", "additionalProperties": False, "required": ["slug", "name"], "properties": {"slug": s, "name": s},
                  "example": {"slug": "analytics", "name": "Analytics"}},
        "FormVersionInput": management.input_schema(management.FORM_FIELDS, management.FormVersion, required=("version", "requiredFields"), extra={
            "version": {"pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"},
            "requiredFields": {"items": {"type": "string", "enum": list(CANONICAL_FIELDS)}},
            "optionalFields": {"items": {"type": "string", "enum": list(CANONICAL_FIELDS)}},
            "customFields": {"items": {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_]{0,63}$"}},
            "fieldMapping": {"additionalProperties": {"type": "string", "enum": list(CANONICAL_FIELDS)}},
            "defaultPhoneRegion": {"description": "ISO 3166-1 alpha-2, upper case, or empty."},
            "isActive": {"default": True}}),
        "FormVersionUpdate": {"type": "object", "additionalProperties": False, "properties": {
            "description": {"type": "string", "maxLength": 300}, "isActive": {"type": "boolean"}}},
        "ManagedFormVersion": {"type": "object", "additionalProperties": False, "required": list(management.FORM_FIELDS), "properties": {
            "version": s, "description": s, "requiredFields": {"type": "array", "items": s}, "optionalFields": {"type": "array", "items": s},
            "fieldMapping": {"type": "object", "additionalProperties": s}, "customFields": {"type": "array", "items": s},
            "defaultPhoneRegion": s, "smsDisclosureVersion": s, "smsDisclosureText": s, "isActive": {"type": "boolean"}},
            "example": {"version": "f1", "description": "", "requiredFields": ["email", "firstName"], "optionalFields": ["company"],
                        "fieldMapping": {}, "customFields": ["role"], "defaultPhoneRegion": "GB", "smsDisclosureVersion": "",
                        "smsDisclosureText": "", "isActive": True}},
        "ManagedOccurrence": {"type": "object", "additionalProperties": False,
            "description": "joinUrl/waitingRoomUrl are write-only; only hasJoinUrl/hasWaitingRoomUrl flags are returned.", "properties": {
            "id": {"type": "string", "format": "uuid"}, "eventId": {"type": "string", "format": "uuid"}, "seriesId": {"type": ["string", "null"]},
            "timezone": s, "localStart": s,
            "dstFold": {"type": ["integer", "null"], "enum": [0, 1, None]}, "durationMinutes": {"type": "integer"},
            "startsAt": s, "endsAt": s, "status": {"type": "string", "enum": list(Occurrence.Status.values)}, "registrationOpen": {"type": "boolean"},
            "hasJoinUrl": {"type": "boolean"}, "hasWaitingRoomUrl": {"type": "boolean"},
            "recordingUrl": {"type": ["string", "null"]}, "recordingPublishedAt": {"type": ["string", "null"]},
            "icsSequence": {"type": "integer"}, "scheduleGeneration": {"type": "integer"},
            "cancelledAt": {"type": ["string", "null"]}, "waitingRoom": {"$ref": "#/components/schemas/WaitingRoom"}}},
        "WaitingRoom": {"type": "object", "properties": {"enabled": {"type": "boolean"}, "url": {"type": ["string", "null"]},
            "admissionMode": {"type": "string", "enum": list(Occurrence.Admission.values)}, "admissionLeadMinutes": {"type": "integer"},
            "state": {"type": ["string", "null"], "enum": ["waiting", "open", "hold", "ended", "cancelled", "preparing", None]},
            "attendanceFinalizedAt": {"type": ["string", "null"]}}},
        "RoomStatus": {"type": "object", "properties": {"state": s, "serverTime": s, "startsAt": s, "endsAt": s, "opensAt": s,
            "secondsUntilOpen": {"type": ["integer", "null"]}, "viewer": {"type": "object", "properties": {"recognized": {"type": "boolean"}, "registered": {"type": "boolean"}}},
            "admit": {"type": "boolean"}, "enterPath": {"type": ["string", "null"]}, "pollAfterSeconds": {"type": ["integer", "null"]}}},
    }
    return {
        "openapi": "3.1.0",
        "info": {"title": "Webinar Platform API", "version": "1.0.0", "license": {"name": "Apache-2.0", "identifier": "Apache-2.0"},
                 "description": "Public, registration, management, waiting-room and MCP endpoints. Private join links are never returned by any endpoint; they appear only as the redirect target of an admitted waiting-room entry."},
        "servers": [{"url": "/"}],
        "security": KEY,
        "tags": [{"name": t} for t in ("Public", "Registration", "Same-origin proxy", mg, "Management (publication)", "Management (attendance_write)",
                                       TICKETS_TAG, HOSTING_TAG, "Tickets", "Waiting room", "MCP", "Operations")],
        "paths": paths,
        "components": {"securitySchemes": {"apiKey": {"type": "http", "scheme": "bearer", "description": "wbk_<prefix>_<secret>; also X-Api-Key header. "
                                                      "The same-origin proxy endpoints instead authenticate the first-party proxy with X-Webinar-Proxy-Key."}},
                       "schemas": {**schemas, **_tickets_and_hosting_schemas()}},
    }

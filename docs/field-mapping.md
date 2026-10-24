# Registration schema and field mapping guide

Applies to registration `schemaVersion: "1"` (see
[`registration.schema.json`](registration.schema.json)). Implementation:
`webinars/normalization.py`. Any change to these rules requires a new
`schemaVersion` or a new `FormVersion`, never a silent change to an existing one.

## Versioning

* **`schemaVersion`** versions the request envelope (`profile`, `consents`,
  `attribution`, …). Only `"1"` exists. Unknown versions are rejected.
* **`formVersion`** names a `FormVersion` row: which fields are required/optional,
  explicit alias mappings, allowed custom fields, the default phone region and the SMS
  disclosure version/text. Form versions are immutable in practice: to change a
  live form, create a new version and point the event at it, so historic
  registrations remain interpretable (each `Registration.form_version` records it).

## Canonical fields

| Canonical key | Stored as (`Person`) | Max length | Notes |
|---|---|---|---|
| `email` | `email` (+ `email_key`) | 254 | See email rules |
| `phone` | `phone` | 20 (E.164) | See phone rules |
| `firstName` | `first_name` | 150 | |
| `lastName` | `last_name` | 150 | |
| `name` | `name` | 300 | Full name, independent of first/last |
| `company` | `company` | 300 | |
| `jobTitle` | `job_title` | 300 | |
| `companySize` | `company_size` | 64 | Free text / band label chosen by the form |

HTML forms using the GTM script should use the canonical key as the input `name`
attribute (`<input name="firstName">`).

## Key resolution (no fuzzy matching)

Each key in `profile` resolves in this order, and only these:

1. A canonical key exactly as above (case-sensitive).
2. An explicit entry in the form's `FormVersion.field_mapping`, e.g.
   `{"work_email": "email", "organisation": "company"}`. The target must be canonical.
3. A built-in common alias:

   | Alias | Canonical |
   |---|---|
   | `first_name` | `firstName` |
   | `last_name` | `lastName` |
   | `job_title` | `jobTitle` |
   | `company_size` | `companySize` |
   | `email_address` | `email` |
   | `phone_number` | `phone` |
   | `full_name` | `name` |
   | `company_name` | `company` |

Anything else is rejected with `unknown_field` for that key. There is no case
folding, no edit-distance matching and no guessing (`Email`, `e-mail`, `fname` are
all unknown unless the form maps them explicitly).

**Conflicts**: if two keys resolve to the same canonical field with different
normalised values (e.g. `email` and `email_address` disagree), the request fails with
`400 validation_error` and `fields.<canonical> = "Conflicting values were supplied
for this field."`. Identical values after normalisation are accepted.

## Value rules

* Values must be strings (or `null`). Other types are `invalid_type`.
* Leading/trailing whitespace is trimmed; control characters are rejected.
* **Blank cannot erase**: `null`, `""` and whitespace-only values are dropped before
  processing, so they never overwrite or clear a stored value. There is no way to
  delete a profile value through registration; use the admin or the purge command.
* Non-blank values update the profile **only** when the request carries a valid
  capability for that same person (see identity rules in `architecture.md`).
  Otherwise the submission is kept only in `Registration.submitted_profile`.

### Email

* Exactly one `@`; local part is an RFC 5322 dot-atom (max 64 chars); domain has at
  least two labels, each a valid hostname label, IDNA-encoded and lower-cased; the
  TLD may not be all digits; total ≤ 254.
* The local part keeps its case and content. **No plus-tag stripping, no dot
  removal**, no provider-specific rewriting: `Ada.L+webinar@Example.COM` is stored
  as `Ada.L+webinar@example.com`.
* `email_key` (dedupe only) = case-folded local part + lower-case domain. A matching
  `email_key` dedupes registrations per occurrence; it never authorises reading or
  updating the existing person.

### Phone

* Output is always E.164 (`+` then 8–15 digits).
* Input starting with `+` is accepted as international (spaces, dots, dashes and
  parentheses are removed).
* National-format input is accepted **only** when the form supplies country context
  via `FormVersion.default_phone_region` and that region is supported (US, CA, GB,
  IE, AU, DE, FR, NL, ES, IT, IN, NZ, SG). Without it the error code is
  `needs_country`. `00`-prefixed numbers without `+` are not guessed.
* Region is never inferred from IP, locale, email domain or attribution.

### Names

`firstName`, `lastName` and `name` are independent. A `name` is **never split** into
first/last and first/last are **never joined** into `name`. If a form needs
`firstName`, a submitted `name` does not satisfy it.

## Custom fields

* Only keys listed in `FormVersion.custom_fields` are accepted; others are
  `unknown_field`.
* Submit either the bare key (`"role_focus"`) or the namespaced key
  (`"custom.role_focus"`); both are stored as `custom.role_focus`, and conflicting
  values between the two forms are rejected.
* Values may be string (≤ 1000 chars), finite number or boolean; blank strings and
  `null` are dropped.
* Custom fields can never shadow canonical fields because of the `custom.` namespace.

## `missingFields` semantics

`GET /webinar-api/profile` (→ `/api/v1/profile`) returns the form's fields (required
and optional canonical keys, plus `custom.<key>` entries) that the recognised profile
does not already hold:

* Unrecognised (no/invalid/revoked capability): every form field is listed, i.e. the
  full form is shown. A matching email never changes this.
* Recognised: only keys whose stored value is empty are listed. Values are never
  returned, only key names.
* Empty list: the browser hides/disables the completed fields and shows only a
  "Confirm my RSVP" button.
* `missingFields` is a UX hint, not a security boundary: the server re-validates
  `required_fields` on submission against the stored profile plus the submitted
  values.

## Errors

Field errors appear under `error.fields` keyed by canonical key, `custom.<key>`, or the
original unknown key, with stable internal codes such as `unknown_field`,
`invalid_type`, `conflict`, `too_long`, `needs_country`, `required` and `invalid`.
Messages never contain the submitted value.

## Outbound (webhook) field mapping

`FormVersion.field_mapping` above maps *incoming* form keys to canonical keys.
`Destination.field_mapping` is separate: it maps canonical payload paths to the
receiver's names for *outgoing* webhooks (details in [`webhooks.md`](webhooks.md)).

* Applied exactly once, at send time, from the canonical groups the destination may
  currently receive; results go in a top-level `mapped` object and the canonical
  groups are kept unchanged. Sources are canonical paths only (`profile.email`,
  `event.occurrence.startsAt`), never `mapped.*` or another target name, so
  `{"profile.email": "a", "a": "b"}` produces only `a`.
* Changing the mapping applies to messages already queued: their `mapped` object is
  rebuilt from the stored canonical data, not re-mapped.
* Mapped private links follow the link rules: `event.occurrence.joinUrl` /
  `waitingRoomUrl` map to `null` unless the destination has `include_private_links`
  and the occurrence's managed waiting room is off. Use
  `event.occurrence.managedRoomUrl` to map the stable room URL.

## Worked examples

| Submitted `profile` | Form mapping | Result |
|---|---|---|
| `{"first_name": "Ada", "firstName": "Ada"}` | – | `firstName = "Ada"` |
| `{"first_name": "Ada", "firstName": "Augusta"}` | – | 400, `firstName` conflict |
| `{"work_email": "ada@ex.com"}` | `{"work_email": "email"}` | `email` |
| `{"work_email": "ada@ex.com"}` | none | 400, `work_email` unknown |
| `{"name": "Ada Lovelace"}` | – | `name` only; `firstName`/`lastName` untouched |
| `{"company": "   "}` | – | ignored; stored company unchanged |
| `{"phone": "07700 900123"}` | region `GB` | `+447700900123` |
| `{"phone": "07700 900123"}` | no region | 400, `needs_country` |
| `{"email": "A.Lovelace+w@GMAIL.com"}` | – | `A.Lovelace+w@gmail.com` (no stripping) |

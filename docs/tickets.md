# Seats and paid tickets

Free registration with unlimited seats remains the default. Nothing changes unless a session gets a seat limit or a price.

## Seat limits (`webinars/capacity.py`)

- `Occurrence.capacity` set to null means unlimited. A seat counts as taken by a `registered` registration or by a ticket order in `hold`, `checkout_open` or `needs_reconciliation`.
- Every registration path counts seats under the occurrence row lock. That covers the public API, the same-origin proxy, trusted keys, the waiting room and paid fulfillment.
- When a session is full, the response is `409 sold_out` with a generic message. Re-submitting as an existing registrant still succeeds and takes no new seat.
- `showRemaining` publishes `availability.remaining`. Without it, only `availability.soldOut` is public. Both keys appear only on sessions that have a limit.

## Prices

- **Ordinary edits never touch prices:** the price columns are excluded from the event form, so studio edits, management `PATCH` and Django admin cannot clear or change a price. Use the price endpoints instead.
- **Deleting events or sessions:** events and sessions with ticket orders cannot be deleted (409). Orders are the financial record.
- A price is stored as integer minor units plus a lower-case ISO 4217 code from an explicit allowlist (`payments.CURRENCIES`).
- A session price overrides the event price. If both are null, the session is free.
- Every order snapshots its price, so later price changes never alter it.
- Changing a price needs `payments_admin` over the API, and an Administrator or superuser in the studio. Changing capacity needs `content_write` or an Editor.

## Stripe account

The studio page is **Payments** (superuser only). The API is `PATCH /api/v1/manage/payments/account` (`payments_admin`, superuser-granted).

- **Secrets:** the secret or restricted key and the webhook signing secret are set-only. They are Fernet-encrypted with `INTEGRATION_MASTER_KEY` and never rendered or returned.
- **Mode:** must match the key prefix (`sk_test_`/`rk_test_` or `sk_live_`/`rk_live_`). Webhooks must also match it (`livemode`).
- **`expectedAccountId`:** optional. If set, events carrying a different `account` are rejected.
- **Operator requirements:**
  - `api.stripe.com` on `WEBHOOK_HOST_ALLOWLIST`
  - an https `PUBLIC_BASE_URL`
  - `INTEGRATION_MASTER_KEY`
- **Optional settings:**
  - `STRIPE_API_VERSION` pins the `Stripe-Version` header. Unset, it uses the account default.
  - `TICKET_CHECKOUT_MINUTES` sets the checkout lifetime: at least 31, at most 1440.
- **If anything is missing:** paid sessions answer `503 payments_unavailable` ("Ticket sales are not configured"). They never fake a purchase.

## Flow

0. **Mailbox verification first (breaking change).** A seat is held only for a browser whose mailbox for the submitted email was proven by the waiting room's emailed code or link (`BrowserCapability.mailbox_verified_at`, migration 0009). A capability from an ordinary free registration is recognition, not proof, and does not count.
   - **Every other caller** (no cookie, an unverified cookie, or someone registering a different address) gets the same `202 {"status":"verification_required","occurrenceId","verification":{"available","verifyUrl","message"}}`. Cookie-less callers also get a decoy cookie. The answer depends only on the session's configuration, never on the email: no "already registered", "checkout in progress" or new-versus-known distinction, no hold and no Stripe call.
   - **`verifyUrl`** is the session's hosted waiting room (`/w/<code>/`). It emails the code, then runs the paid checkout for the now-verified browser. If the waiting room is disabled or email delivery is `disabled`, `available` is `false`, `verifyUrl` is `null` and the message tells the buyer to contact the organiser. **Paid sessions sold through the public API or SDK therefore need the waiting room enabled and SMTP (or preview) mail configured.**
   - `503 payments_unavailable` (no account configured) is still reported before verification, because it does not depend on the email.
   - **Free sessions are unchanged.**
1. **Registration:** the registration endpoints (and the waiting room) validate the form exactly as for free sessions. They then create a `TicketOrder` hold under the occurrence lock. No registration, consent, reminder or webhook is written yet.
2. **Checkout:** after commit, the server calls `POST https://api.stripe.com/v1/checkout/sessions`, with no database lock held.
   - **Request contents:** card only, quantity 1, `client_reference_id` and `metadata[order_id]`, and `expires_at` 31 minutes ahead.
   - **Idempotency:** the request uses `Idempotency-Key: order-<id>`. The exact request is persisted before sending.
   - **Response:** `202 {"status":"payment_required","order":{id,status,checkoutUrl,expiresAt,statusToken,...}}`.
   - **Order status:** `statusToken` is an opaque capability (only its SHA-256 is stored; it expires after 7 days). It authorises `GET /api/v1/orders/<id>` (header `Webinar-Order-Token`).
3. **Fulfillment:** only `POST /api/v1/payments/stripe/webhook` registers anyone. It is authenticated by its signature alone (CSRF-exempt, no key or cookie). It applies these checks:
   - `Stripe-Signature` must be valid over the raw body, within a 300-second tolerance.
   - Event ids are deduplicated durably (`StripeEventRecord`) in the same transaction.
   - The event must be `checkout.session.completed` or `checkout.session.async_payment_succeeded` with `payment_status=paid`.
   - The session id must match the order. If our create response was lost, a signed event can supply it.
   - `client_reference_id`, `metadata.order_id`, the amount, the currency, the mode and the livemode must all match.
   - On a mismatch, the order goes to `needs_reconciliation` and nobody is registered.
   - The success redirect (`/tickets/return`) reads and confirms nothing.
4. **Identity:** the stored envelope is processed by the same `registration._process` with the capability the browser had when the seat was held. Since step 0, that capability is always mailbox-verified for the buyer's email. Stripe data (email, metadata) is never used for identity.

## Buyer flow (end to end)

Covered by `tests/test_paid_room_flow.py` (server only; Stripe and mail mocked).

1. **First submit:** the buyer submits the embedded form (SDK, via the proxy) days before the session. The answer is `202 verification_required`. The SDK navigates to `verifyUrl`, the waiting room.
2. **Email step:** the room asks for the email. `POST /w/<code>/identify` emails a 6-digit code and a one-time link, with the same 303 for any address.
3. **Code:** `POST /w/<code>/verify` (or the link) replaces the browser's cookie with a mailbox-verified capability.
4. **Remaining fields:** the room shows the fields still missing. `POST /w/<code>/register` holds the seat and returns `303` to Stripe Checkout (`Referrer-Policy: no-referrer`). A reload or double submit returns the same order and Checkout.
   - A browser that is only recognised (for example from a free registration) is shown the email step (`202`, nothing held) instead of Checkout.
5. **Payment:** only the signed `paid` webhook registers the buyer. The return page confirms nothing.
6. **Admission:** the room shows "You're on the list". `POST /w/<code>/enter` gives the join link only once the doors open: automatically `admission_lead_minutes` before the start, or when staff choose Open.

**Lead time and price:**
- Steps 2–4 work whenever the room is not ended or cancelled. They do not wait for the doors to open, and work in `hold` too, so a buyer can verify and pay days ahead.
- The price is the session's effective price snapshotted on the order. No room state, mode or lead time skips Checkout.

**Polling:** the room page polls `/w/<code>/status` every `pollAfterSeconds`. The interval is capped at `WAITING_ROOM_POLL_SECONDS` (default 15) and is at least 2 seconds. Polling stops (`null`) once the room is ended or cancelled. The status body never contains the Checkout URL or the join link.

**Through the Node proxy:**
- A `202` registration body (`payment_required` or `verification_required`) is passed through unchanged. Only `calendar.icsUrl` is rewritten.
- Only the session cookie is forwarded and re-issued.
- An upstream `5xx`, including `503 payments_unavailable`, is reported as `502 bad_gateway`.

## Expiry, reconciliation and refunds

- **Seat release:** a seat is released only when Stripe says the session expired (a `checkout.session.expired` webhook, or a GET from the worker sweep), or when nothing was ever sent.
- **Unknown create outcome** (timeout, 5xx, 429, unparsable): the request is retried once with the same Idempotency-Key. After that, the order stays `needs_reconciliation`, with the seat still held.
  - Stripe's `409` (a request with that key is still in progress) leaves the order in `hold`.
  - A session Stripe did create is applied whenever it arrives, even if a concurrent retry already marked the order for reconciliation.
  - The worker replays the identical request with the same key every 2 minutes, before the hold expires, for up to 23 hours (Stripe dedupes).
  - After 23 hours, staff resolve it in the studio by checking Stripe ("Not paid: release seat").
- **Paid session without its webhook yet:** the order stays held until the signed webhook arrives. The sweep never fulfils.
- **Payment after release, for a cancelled or ended session, or for someone already registered:** the order becomes `refund_required`. Nobody is admitted and no money moves automatically. Staff refund in Stripe, then record it ("Refunded in Stripe" or `POST /api/v1/manage/orders/<id>/mark-refunded`). That cancels the registration if there is one.
- **Admission:** the waiting room never admits a registration whose order is `refund_required` or `refunded`. A paid waiting room cannot bypass Checkout: its registration goes through the same paid flow and redirects to Stripe.

## Abuse limits and retries

- **Seat holds need a verified mailbox** (Flow step 0), so throwaway emails cannot fill a session. Each verified person has at most one open hold per session.
- **Rate limits on new seat holds:**
  - Same-origin direct proxy and waiting room: at most 5 per client address per session every 10 minutes (`paid_hold`).
  - Through the Node proxy key: at most 5 per verified person, keyed by the capability cookie (`paid_hold`, `person:<id>`). A forwarded address header is never trusted for this.
  - Server-side API keys without a browser cookie: at most 300 per key per session every 10 minutes (`paid_hold_key`). Such callers still need a verified capability to hold a seat.
  - Over the limit, the answer is `429 rate_limited`. Free registrations are not affected.
- **One open hold per person and session.**
  - Any mailbox-verified browser of the same person (a reload, a new idempotency key, or a concurrent same-key retry that only sees the committed order) gets the existing order and its checkout back.
  - A hold not created by a mailbox-verified browser of that person (for example one made before this rule existed) is never adopted, and its stored envelope is never fulfilled for them. The answer is `409 checkout_in_progress`, without the link, until the hold expires.
- **Replays are bound to the caller.** Replaying an idempotency key from a different browser context is refused (`409`), the same rule as for free registrations.
  - **Checkout refused by Stripe:** this now answers **`409 checkout_failed`** (it was `502`), so the Node proxy passes the code through. The SDK rotates its idempotency key on `checkout_failed` and on `order_closed`.
  - An expired or closed order answers `409 order_closed` for its old key.
- **No charge without a way in.** A verified person who is already registered gets the free-path dedupe answer (`200`) and no checkout.
- **Order status:** `Webinar-Order-Token` is accepted as a header only, never in the query string.

## Browser SDK

`webinar-form.js` treats `202 payment_required` as "not registered yet":

- If `order.checkoutUrl` is an https URL on `checkout.stripe.com`, it navigates there.
- Otherwise it shows `paymentPreparing` and keeps the idempotency key, so submitting again replays the same order.
- It never shows the confirmation message, never fires the `webinar_registration` dataLayer event, and never renders calendar links before payment.
- `sold_out`, `checkout_in_progress`, `payments_unavailable`, `order_closed` and `checkout_failed` each map to their own message.
- **`202 verification_required`:**
  - It resets the idempotency key.
  - If `verification.available` is true and `verifyUrl` is an https `/w/<code>/` URL, it navigates there (`verificationRedirecting`).
  - Otherwise it shows `verificationUnavailable`.
  - It never shows a confirmation.

## Refunds, disputes and cancellations

- **Refunds started in Stripe:** a full `charge.refunded` for a fulfilled order records the refund. The order becomes `refunded`, and its registration is cancelled unless another fulfilled order pays for it.
  - Partial refunds are only audited; staff decide.
- **Disputes:** `charge.dispute.created` flags a fulfilled order `refund_required`, which stops waiting-room admission.
- **Webhook events to enable in Stripe:** `checkout.session.completed`, `checkout.session.async_payment_succeeded`, `checkout.session.expired`, `charge.refunded` and `charge.dispute.created`.
- **Cancelling a session:** flags its fulfilled orders `refund_required`. No money moves.
  - The worker sweep also closes open checkouts of a cancelled session without waiting for the hold to expire. An open Stripe session is expired with `POST .../expire`, and the seat is released on the expired event or the next GET. A hold that was never sent is released at once.
- **Refund matching for mismatched amounts:** when the session is ours (id, reference, metadata, mode and account all match) and only the amount or currency differs, the payment intent is recorded, so a later `charge.refunded` moves the order to `refunded`. A foreign session never links its intent.
- **`email_key` retention:** it is cleared together with the envelope whenever an order closes (expired, cancelled, refund required, fulfilled or refunded).
- **Which order decides admission:** only the latest order for a registration, and any fulfilled order admits. An old refund never blocks a later paid re-purchase.
- **Paid orders stay out of "release":**
  - An order Stripe reported as paid (`paidAt`), or a mismatched session that was paid, is never released as unpaid; it goes to `refund_required` instead.
  - Releasing a `needs_reconciliation` order by hand re-checks the session with Stripe first.
- **Account changes:** changing the Stripe mode, secret key or expected account is refused while any order is still held, in checkout or awaiting reconciliation. Open orders replay their checkout with the account's current key, so a change could create a session on another account or charge live money for a test order. Resolve open orders first; key rotation therefore needs a quiet moment.
- **Order records outlive people:** orders keep their amount, status and ids when a person is deleted (`SET_NULL`). Erasure clears the stored envelope and email key, including holds made before the person existed (matched by email key).
- **Fulfillment rule:** a paid hold is fulfilled while the session is scheduled and has not ended, even if the event was unpublished or registration was closed during checkout. The buyer has paid for a seat that was held for them.
- **Webhook destinations:** destinations whose link policy includes the join URL receive it with `registration.created`. For paid sessions that happens only after verified payment, but a later refund cannot recall a link that was already delivered. Prefer the waiting room for paid sessions.

## Not supported

Subscriptions, coupons and promotion codes, taxes, quantities above one, delayed or asynchronous payment methods (card only), automatic refunds, Stripe Connect and partial payments.

## Not verified

None of the Stripe contract details above have been run against a Stripe sandbox. That includes the field names, the 30-minute `expires_at` minimum, idempotency replay behaviour and the expire endpoint. Verify them before taking live payments.

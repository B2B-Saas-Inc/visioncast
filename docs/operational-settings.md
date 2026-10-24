# Operational settings

Four limits that code previously read only from Django settings defaults can now be set from the environment. Each is optional: unset or empty keeps the previous default, so existing deployments behave the same.

Values must be plain whole numbers (digits only: no sign, decimals, underscores or units) inside the range below. Anything else stops start-up with `ImproperlyConfigured`, naming the variable and the allowed range, for example:

```
WAITING_ROOM_MAX_GUESSES_PER_EMAIL must be a whole number from 1 to 50 (got '0').
```

Values are never clamped silently. Parsing is in `config/settings.py` (`env_bounded_int`).

| Variable | Default | Range | Read by |
|---|---|---|---|
| `RATE_LIMIT_TRUSTED_PROXY_COUNT` | 1 | 1–10 | `webinars/ratelimit.py` (`client_ip`) |
| `WAITING_ROOM_MAX_CODES_PER_EMAIL_GLOBAL` | 10 | 1–50 | `webinars/waiting_room.py` (`request_verification`) |
| `WAITING_ROOM_MAX_GUESSES_PER_EMAIL` | 10 | 1–50 | `webinars/waiting_room.py` (`verify_code`) |
| `WEBHOOK_TOTAL_TIMEOUT_SECONDS` | 30 | 5–600 | `webinars/delivery.py` (`_total_timeout`) |

## `RATE_LIMIT_TRUSTED_PROXY_COUNT`

This is the number of reverse proxies in front of Django that each append the address they received the request from to `X-Forwarded-For`. The rate-limit key is the Nth entry from the right. Everything further left is client-controlled and ignored. It only applies when `RATE_LIMIT_TRUST_X_FORWARDED_FOR=1`; otherwise `REMOTE_ADDR` is used and this value is ignored.

Set it to exactly the number of proxies you run. A value that is too high lets clients pick their own key by prepending entries. A value that is too low puts every client behind one proxy address. When a request carries fewer entries than configured, the leftmost entry is used. See [waiting-room.md](waiting-room.md#rate-limits).

## Waiting-room platform caps

Both are counted in the database per verification TTL window (`WAITING_ROOM_VERIFICATION_TTL_MINUTES`), so they apply across all workers:

- `WAITING_ROOM_MAX_CODES_PER_EMAIL_GLOBAL`: codes issued to one address across all rooms. When it is reached, `identify` returns the same generic response and sends no email.
- `WAITING_ROOM_MAX_GUESSES_PER_EMAIL`: wrong guesses by one browser for one address across all rooms. When it is reached, that browser's current code is burned without comparing. This is keyed by the guessing browser, so it never locks the address owner's own browser.

The per-room cap (`WAITING_ROOM_MAX_CODES_PER_EMAIL`) and per-code guesses (`WAITING_ROOM_MAX_CODE_ATTEMPTS`) still apply within these limits. The upper bound of 50 keeps the worst case per address per window small against the 6-digit code space. Raising either cap makes guessing easier, so only raise one if real users are being suppressed. A global code cap below the per-room cap is allowed (it is stricter) but makes the per-room cap redundant.

## `WEBHOOK_TOTAL_TIMEOUT_SECONDS`

This is the hard deadline for one whole webhook or native-integration send: DNS, connect, TLS, request and response. When it is reached, the child process is terminated and the attempt is recorded as `timeout` and retried. `WEBHOOK_TIMEOUT_SECONDS` still limits each socket operation inside that deadline.

At run time the deadline is also capped at `remaining outbox lease − 9 s` (`delivery.LEASE_SAFETY_SECONDS`). A value larger than `OUTBOX_LEASE_SECONDS − 9` therefore has no effect beyond that cap. Start-up does not reject it, because the default 30 combined with a short lease has always been valid. Keep `OUTBOX_LEASE_SECONDS` comfortably above this value plus 10. The minimum of 5 s avoids configurations where every send times out before a TLS handshake can complete.

## Tests

`tests/test_operational_settings.py` covers parsing, bounds, defaults, the `.env.example` entries, and agreement between each caller's `getattr` fallback and the settings default. **Not yet run.**

## Not changed

- The callers still keep their own `getattr` fallbacks and defensive conversion. For example, `ratelimit` still treats invalid values as 1 when settings are overridden in code.
- The existing `WAITING_ROOM_*`, `WEBHOOK_TIMEOUT_SECONDS` and `OUTBOX_*` variables keep their unbounded integer parsing (`env_int`).

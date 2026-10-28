# Cloudflare configuration for events.growthcast.app

Manual steps (dashboard) with `curl` equivalents for the owner of the `growthcast.app` zone.
Nothing here is automated by the scripts in this directory. Keep an API token in the
`CF_API_TOKEN` environment variable for the `curl` calls (never on a command line); it needs
`Zone:DNS:Edit`, `Zone:Zone Settings:Edit`, `Zone:Cache Rules:Edit`, `Zone:WAF:Edit` and, for
Access, `Access: Apps and Policies:Edit` on the account. `ZONE_ID` is the zone id shown on the
zone overview page.

Order: **1 → 2 (DNS only) → bootstrap.sh obtains the certificate → 2 (proxied) → 3 → 4 → 5 → 6 → 7**,
then `deploy/production/verify.sh --domain events.growthcast.app --expect-access`.

The origin (`bootstrap.sh`) assumes this setup: it restores the visitor address from
`CF-Connecting-IP` only for Cloudflare's published ranges and returns **404 for `/admin/` and
`/studio/` to any peer that is not Cloudflare** (origin gate), so Access cannot be bypassed by
talking to the droplet directly. Keep the proxy (orange cloud) on once the certificate exists.

## 1. Zone settings

| Setting | Value | Why |
|---|---|---|
| SSL/TLS → Overview → encryption mode | **Full (strict)** | Cloudflare validates the Let's Encrypt certificate on the origin; "Flexible" would send plain HTTP to the origin and break `DJANGO_TRUST_X_FORWARDED_PROTO`. |
| SSL/TLS → Edge Certificates → Always Use HTTPS | **On** (after the first certificate, see section 2) | HTTP → HTTPS at the edge; nginx also redirects. |
| SSL/TLS → Edge Certificates → Minimum TLS Version | TLS 1.2 | Matches the origin. |
| SSL/TLS → Edge Certificates → HSTS | leave **off** at the edge | Django already sends `Strict-Transport-Security: max-age=31536000` (`DJANGO_HSTS_SECONDS`). Enabling it at Cloudflare too is harmless; enabling `includeSubDomains`/preload is NOT (it would affect `growthcast.app`). |
| Network → WebSockets | off | Not used. |
| Speed → Optimization → Rocket Loader, Auto Minify, Mirage, Email Obfuscation | **off** | They rewrite HTML/JS; the studio and the GTM script must be served byte-for-byte. |
| Scrape Shield → Email Address Obfuscation | off | Same reason. |
| Security → Settings → Browser Integrity Check | on (default) | Fine for browsers; API clients send normal headers. |
| Security → Bots → Bot Fight Mode | **off** | It challenges server-to-server calls (`/api/v1/*` from the site proxy, Stripe, MCP clients). Use the WAF rules below instead. |

```sh
API=https://api.cloudflare.com/client/v4
H=(-H "Authorization: Bearer $CF_API_TOKEN" -H "Content-Type: application/json")
curl -sS -X PATCH "$API/zones/$ZONE_ID/settings/ssl"              "${H[@]}" --data '{"value":"strict"}'
curl -sS -X PATCH "$API/zones/$ZONE_ID/settings/min_tls_version"  "${H[@]}" --data '{"value":"1.2"}'
curl -sS -X PATCH "$API/zones/$ZONE_ID/settings/rocket_loader"    "${H[@]}" --data '{"value":"off"}'
curl -sS -X PATCH "$API/zones/$ZONE_ID/settings/email_obfuscation" "${H[@]}" --data '{"value":"off"}'
# after the certificate exists (section 2):
curl -sS -X PATCH "$API/zones/$ZONE_ID/settings/always_use_https" "${H[@]}" --data '{"value":"on"}'
```

## 2. DNS record and the first certificate

Create `A events → <droplet IPv4>` (from `provision.sh`). Two ways to get the first
Let's Encrypt certificate with HTTP-01:

**A. DNS only first (recommended, deterministic).** Create the record with the proxy **off**
(grey cloud), wait until `dig +short events.growthcast.app` returns the droplet address, run
`bootstrap.sh` (certbot talks to Let's Encrypt directly through port 80), then switch the record
to **Proxied** and turn on "Always Use HTTPS".

**B. Proxied from the start.** Works only while "Always Use HTTPS" is **off** and the SSL mode
is Full (strict): Let's Encrypt reaches `http://events.growthcast.app/.well-known/acme-challenge/…`
through Cloudflare, which forwards it to the origin's port 80. If the redirect is already on,
Cloudflare answers 301 → https → origin has no certificate yet → validation fails. Turn the
redirect on after the certificate exists. Renewals (every 60 days, certbot's systemd timer)
work in either state because the origin then has a valid certificate and both nginx server
blocks serve the challenge path.

```sh
curl -sS -X POST "$API/zones/$ZONE_ID/dns_records" "${H[@]}" \
  --data '{"type":"A","name":"events","content":"<droplet IPv4>","ttl":1,"proxied":false,"comment":"visioncast-events droplet"}'
# later, to proxy it (RECORD_ID from the response above or GET .../dns_records?name=events.growthcast.app):
curl -sS -X PATCH "$API/zones/$ZONE_ID/dns_records/$RECORD_ID" "${H[@]}" --data '{"proxied":true}'
```

No `AAAA` record: the nginx config listens on IPv6 but the droplet is created without IPv6.

## 3. Access (MFA in front of `/admin/` and `/studio/`) — checklist D3

Zero Trust → Access → Applications → **Add an application → Self-hosted**:

| Field | Value |
|---|---|
| Application name | VisionCast studio and admin |
| Session duration | 24 hours (studio sessions themselves expire after 8 hours of Django session age) |
| Application domain | `events.growthcast.app` path `admin` **and** a second domain entry `events.growthcast.app` path `studio` (one application, two public hostnames/paths; Access matches the path prefix, so `/admin/*` and `/studio/*` are covered) |
| Identity providers | One-time PIN (built in; sends a code to the e-mail) and/or Google (Zero Trust → Settings → Authentication → Login methods → Google, with the GrowthCast Workspace client id/secret) |
| Policy "Owner" | Action **Allow**; Include → **Emails** → the owner's address (and each marketer who gets a studio account). With Google as the only login method and "Require" → Login method → Google, the Google account's own MFA applies. |
| Policy "Everyone else" | not needed: Access denies by default |
| App Launcher visibility | off |
| HTTP-only cookie, Enable Binding Cookie | on |
| Cookie SameSite | Lax |

Access does **not** cover `/studio/login/` POSTs from scripts, `/mcp`, `/api/v1/*`, `/w/*` or
`/webinar-api/*`: those must stay reachable without a Cloudflare login. Do not add them to the
application. The studio's own login (username + password from
`/root/visioncast-secrets/admin-password`) still applies behind Access.

Cloudflare sends `Cf-Access-Jwt-Assertion` to the origin; the platform does not validate it
(not required: the origin gate makes the origin unreachable except through Cloudflare, and
a different Cloudflare zone cannot present our host name under Full (strict)). To close that
last theoretical path as well, enable **Authenticated Origin Pulls** (SSL/TLS → Origin Server)
and add `ssl_client_certificate` / `ssl_verify_client on` with Cloudflare's origin-pull CA in
`nginx-site-tls.conf.tmpl`; not done by default because it makes direct `verify.sh --origin`
checks impossible.

API (replace `$ACCOUNT_ID`):

```sh
curl -sS -X POST "$API/accounts/$ACCOUNT_ID/access/apps" "${H[@]}" --data '{
  "name": "VisionCast studio and admin", "type": "self_hosted",
  "self_hosted_domains": ["events.growthcast.app/admin", "events.growthcast.app/studio"],
  "session_duration": "24h", "http_only_cookie_attribute": true, "enable_binding_cookie": true,
  "app_launcher_visible": false,
  "policies": [{"name": "Owner", "decision": "allow", "include": [{"email": {"email": "owner@growthcast.app"}}]}]
}'
```

## 4. WAF rate-limiting rules — checklist D2

Security → WAF → Rate limiting rules. These mirror the nginx zones in
`nginx-site.conf.tmpl` (which stay in place as the second layer). Counting by IP; period 10 s
(the only period on the Free plan); action **Block** for the duration shown.

| # | Name | Expression (`http.host eq "events.growthcast.app" and …`) | Requests / 10 s per IP | Block for |
|---|---|---|---|---|
| 1 | registrations and reset | `(http.request.uri.path in {"/webinar-api/registrations" "/webinar-api/reset"} and http.request.method eq "POST")` | 10 | 1 min |
| 2 | profile lookups | `(http.request.uri.path in {"/webinar-api/profile" "/webinar-api/my-registration" "/webinar-api/calendar.ics"})` | 30 | 1 min |
| 3 | waiting room | `(starts_with(http.request.uri.path, "/w/"))` | 30 | 1 min |
| 4 | logins | `(http.request.uri.path in {"/studio/login/" "/admin/login/"} and http.request.method eq "POST")` | 5 | 10 min |
| 5 | MCP | `(http.request.uri.path eq "/mcp")` | 60 | 1 min |
| 6 | Stripe webhook | `(http.request.uri.path eq "/api/v1/payments/stripe/webhook")` | 100 | 10 s |

Plan limits: Free allows **1** rate-limiting rule, Pro 2, Business 5, Enterprise 100. On Free,
create one rule with the union of the paths 1–5 (`in {…}` plus `starts_with(…, "/w/")`) at
**20 requests / 10 s, block 1 min**, and rely on nginx for the finer limits; leave the Stripe
webhook out of it (Stripe retries, and nginx keeps its own generous zone). The
`verify.sh` burst check expects a 429 from either layer.

```sh
# Example: one combined rule (Free plan). ruleset phase http_ratelimit.
curl -sS -X PUT "$API/zones/$ZONE_ID/rulesets/phases/http_ratelimit/entrypoint" "${H[@]}" --data '{
  "rules": [{
    "description": "visioncast visitor endpoints",
    "expression": "(http.host eq \"events.growthcast.app\") and (starts_with(http.request.uri.path, \"/w/\") or http.request.uri.path in {\"/webinar-api/registrations\" \"/webinar-api/reset\" \"/webinar-api/profile\" \"/webinar-api/my-registration\" \"/webinar-api/calendar.ics\" \"/studio/login/\" \"/admin/login/\" \"/mcp\"})",
    "action": "block",
    "ratelimit": {"characteristics": ["ip.src", "cf.colo.id"], "period": 10, "requests_per_period": 20, "mitigation_timeout": 60}
  }]
}'
```

Also add a **custom WAF rule** (Security → WAF → Custom rules) that blocks requests to
`/admin/` and `/studio/` whose country or ASN you never expect, if the owner wants a
geographic fence; optional.

## 5. Cache rules

Caching → Cache Rules → create one rule **"visioncast bypass"**, action **Bypass cache**,
expression:

```
(http.host eq "events.growthcast.app" and (
  starts_with(http.request.uri.path, "/webinar-api/") or
  starts_with(http.request.uri.path, "/w/") or
  starts_with(http.request.uri.path, "/studio/") or
  starts_with(http.request.uri.path, "/admin/") or
  starts_with(http.request.uri.path, "/api/") or
  http.request.uri.path eq "/mcp" or
  http.request.uri.path eq "/healthz"))
```

Cloudflare caches by file extension by default, so these JSON/HTML responses would not be
cached anyway; the rule makes it explicit and survives future default changes.
`/static/*` (hashed whitenoise files, `Cache-Control: max-age=315360000, immutable`) may be
cached by Cloudflare; a second rule "Eligible for cache" on `starts_with(http.request.uri.path, "/static/")`
is optional.

```sh
curl -sS -X PUT "$API/zones/$ZONE_ID/rulesets/phases/http_request_cache_settings/entrypoint" "${H[@]}" --data '{
  "rules": [{"description": "visioncast bypass", "action": "set_cache_settings", "action_parameters": {"cache": false},
             "expression": "(http.host eq \"events.growthcast.app\" and (starts_with(http.request.uri.path, \"/webinar-api/\") or starts_with(http.request.uri.path, \"/w/\") or starts_with(http.request.uri.path, \"/studio/\") or starts_with(http.request.uri.path, \"/admin/\") or starts_with(http.request.uri.path, \"/api/\") or http.request.uri.path eq \"/mcp\" or http.request.uri.path eq \"/healthz\"))"}]
}'
```

## 6. Request headers reaching the origin

Cloudflare adds `CF-Connecting-IP` (the visitor), `X-Forwarded-For` (appends the visitor),
`X-Forwarded-Proto` and `CF-Ray`. nginx (`nginx-site-tls.conf.tmpl`) ignores what Cloudflare
sends in `X-Forwarded-For`/`X-Forwarded-Proto`: it trusts `CF-Connecting-IP` only from
Cloudflare's address ranges, then **overwrites** `X-Forwarded-For` with that one address and
`X-Forwarded-Proto` with `https`. That is why `.env` has `RATE_LIMIT_TRUSTED_PROXY_COUNT=1`
(exactly one trusted hop as seen by Django). Do not enable Cloudflare's "Remove visitor IP
headers" managed transform; it would hide `CF-Connecting-IP`.

The address ranges are refreshed weekly by cron (`cloudflare-ips.sh --reload`); if Cloudflare
announces new ranges before the next run, `sudo /srv/visioncast/app/current/deploy/production/cloudflare-ips.sh --reload`.

## 7. Optional: Cloudflare Origin CA instead of Let's Encrypt

With the record permanently proxied, a Cloudflare **Origin CA** certificate (15 years, trusted
only by Cloudflare) avoids ACME entirely: SSL/TLS → Origin Server → Create Certificate
(RSA, hostname `events.growthcast.app`), save the certificate as
`/etc/letsencrypt/live/events.growthcast.app/fullchain.pem` and the key as
`privkey.pem` (mode 600) on the droplet, then
`bootstrap.sh --steps nginx,status` (certbot step skipped because the files exist). Downsides:
`verify.sh --origin <ip>` and any direct-to-origin curl then fail TLS verification (expected),
and if the record is ever un-proxied the site shows an untrusted certificate. Keep certbot
(default) unless the ACME path proves troublesome.

## 8. Checklist mapping

| Release checklist | Where |
|---|---|
| D1 TLS + HTTP→HTTPS | section 1 (Full strict, Always Use HTTPS) + nginx port 80 redirect |
| D2 edge rate limits | section 4 + nginx zones |
| D3 MFA in front of `/admin/` and `/studio/` | section 3 (Access) + origin gate |
| D5 trusted forwarding depth | section 6 (`RATE_LIMIT_TRUSTED_PROXY_COUNT=1`) |

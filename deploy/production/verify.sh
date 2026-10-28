#!/usr/bin/env bash
# External verification of a production deployment, run from the workstation.
#
#   deploy/production/verify.sh --domain events.growthcast.app
#   deploy/production/verify.sh --domain events.growthcast.app --expect-access --expect-version 0.3.0-pre
#   deploy/production/verify.sh --domain events.growthcast.app --origin 203.0.113.10   # bypass Cloudflare
#
# Options:
#   --domain HOST        the platform host (required)
#   --expect-version V   /healthz must report this version
#   --expect-access      /admin/ and /studio/login/ must be intercepted by Cloudflare Access
#                        (302 to *.cloudflareaccess.com, or 401/403), not served
#   --origin IP          resolve the domain to this address (direct-to-origin checks before the
#                        record is proxied, and the origin gate: /admin/ must be 404 there)
#   --no-burst           skip the rate-limit burst on /webinar-api/profile
#   --burst N            number of rapid requests for the 429 check (default 45; the nginx zone
#                        allows 30/min + burst 20, Cloudflare's own rule may answer first)
#
# Prints a PASS/FAIL table and exits non-zero when anything failed. Needs curl and openssl.
# No secrets are used or printed.
set -Eeuo pipefail

DOMAIN=""
EXPECT_VERSION=""
EXPECT_ACCESS=0
ORIGIN=""
BURST=45
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
usage() { sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
    case $1 in
        --domain) DOMAIN=$2; shift ;;
        --expect-version) EXPECT_VERSION=$2; shift ;;
        --expect-access) EXPECT_ACCESS=1 ;;
        --origin) ORIGIN=$2; shift ;;
        --no-burst) BURST=0 ;;
        --burst) BURST=$2; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
    shift
done
[[ -n $DOMAIN ]] || die "--domain is required"
command -v curl >/dev/null || die "curl is required"
command -v openssl >/dev/null || die "openssl is required"

RESOLVE=()
[[ -z $ORIGIN ]] || RESOLVE=(--resolve "$DOMAIN:443:$ORIGIN" --resolve "$DOMAIN:80:$ORIGIN")
C=(curl -sS --max-time 15 ${RESOLVE[@]+"${RESOLVE[@]}"})
HDR=$(mktemp); BODY=$(mktemp)
trap 'rm -f "$HDR" "$BODY"' EXIT

RESULTS=()
FAIL=0
record() {  # record PASS|FAIL "check" "detail"
    RESULTS+=("$1|$2|$3")
    [[ $1 == PASS ]] || FAIL=$((FAIL + 1))
}
# fetch URL [curl args] -> status code; headers in $HDR (lowercased names), body in $BODY
fetch() {
    local url=$1; shift
    local code
    code=$("${C[@]}" -o "$BODY" -D "$HDR" -w '%{http_code}' "$@" "$url" 2>/dev/null) || code=000
    tr '[:upper:]' '[:lower:]' < "$HDR" > "$HDR.l" && mv "$HDR.l" "$HDR"
    printf '%s' "$code"
}
header() { sed -n "s/^$1: *//p" "$HDR" | tr -d '\r' | tail -n 1; }

# 1. HTTP -> HTTPS
code=$(fetch "http://$DOMAIN/healthz")
loc=$(header location)
if [[ ($code == 301 || $code == 308 || $code == 302) && $loc == https://$DOMAIN/* ]]; then record PASS "HTTP -> HTTPS redirect" "$code to $loc"
else record FAIL "HTTP -> HTTPS redirect" "status $code, location '${loc:-none}'"; fi

# 2. TLS chain verifies and certificate details
if [[ -n $ORIGIN ]]; then conn="$ORIGIN:443"; else conn="$DOMAIN:443"; fi
cert=$(openssl s_client -connect "$conn" -servername "$DOMAIN" </dev/null 2>/dev/null | openssl x509 -noout -issuer -enddate -subject 2>/dev/null || true)
verify=$("${C[@]}" -o /dev/null -w '%{ssl_verify_result}' "https://$DOMAIN/healthz" 2>/dev/null || echo 1)
if [[ $verify == 0 && -n $cert ]]; then record PASS "TLS chain verifies" "$(printf '%s' "$cert" | tr '\n' ';' | cut -c1-150)"
else
    record FAIL "TLS chain verifies" "ssl_verify_result=$verify ${cert:+; $(printf '%s' "$cert" | tr '\n' ';' | cut -c1-120)}; remaining checks run WITHOUT verification"
    C+=(--insecure)   # keep going so the table stays informative (self-signed / Origin CA at the origin)
fi
code=$(fetch "https://$DOMAIN/healthz")

# 3. HSTS
hsts=$(header strict-transport-security)
if [[ $hsts == *max-age=* ]]; then record PASS "HSTS header" "$hsts"; else record FAIL "HSTS header" "missing"; fi

# 4. /healthz version
if [[ $code == 200 ]] && grep -q '"status": *"ok"' "$BODY"; then
    ver=$(sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' "$BODY")
    if [[ -n $EXPECT_VERSION && $ver != "$EXPECT_VERSION" ]]; then record FAIL "/healthz version" "got '$ver', expected '$EXPECT_VERSION'"
    else record PASS "/healthz" "200, version ${ver:-?}"; fi
else record FAIL "/healthz" "status $code"; fi

# 5. Security headers (set by Django; Cloudflare passes them through)
missing=""
[[ $(header x-content-type-options) == nosniff ]] || missing="$missing x-content-type-options"
[[ $(header x-frame-options) == deny ]] || missing="$missing x-frame-options"
[[ -n $(header referrer-policy) ]] || missing="$missing referrer-policy"
srv=$(header server)
if [[ -z $missing ]]; then record PASS "security headers" "nosniff, X-Frame-Options DENY, referrer-policy; server='${srv:-}'"
else record FAIL "security headers" "missing:$missing"; fi
if [[ $srv == nginx/* ]]; then record FAIL "server header" "nginx version exposed ($srv)"; else record PASS "server header" "'${srv:-none}'"; fi

# 6. OpenAPI
code=$(fetch "https://$DOMAIN/api/v1/openapi.json")
if [[ $code == 200 ]] && grep -q '"openapi"' "$BODY"; then record PASS "OpenAPI" "200, $(grep -o '"/[^"]*":' "$BODY" | wc -l | tr -d ' ') paths"
else record FAIL "OpenAPI" "status $code"; fi

# 7. /studio/login/ and /admin/login/ (Access or origin gate)
access_ok() {  # status location -> 0 when intercepted by Cloudflare Access
    local st=$1 lo=$2
    [[ $st == 401 || $st == 403 ]] && return 0
    [[ ($st == 302 || $st == 303 || $st == 307) && $lo == *cloudflareaccess.com* ]] && return 0
    return 1
}
for path in /studio/login/ /admin/login/; do
    code=$(fetch "https://$DOMAIN$path"); loc=$(header location)
    if [[ -n $ORIGIN ]]; then
        if [[ $code == 404 ]]; then record PASS "$path direct to origin" "404 (origin gate: only Cloudflare peers may reach it)"
        else record FAIL "$path direct to origin" "status $code (expected 404 from the origin gate)"; fi
    elif (( EXPECT_ACCESS )); then
        if access_ok "$code" "$loc"; then record PASS "$path behind Access" "$code ${loc:+-> ${loc%%\?*}}"
        else record FAIL "$path behind Access" "status $code served without Access"; fi
    else
        if [[ $code == 200 ]] && grep -qi 'csrfmiddlewaretoken' "$BODY"; then record PASS "$path" "200 login form (enable Cloudflare Access, then re-run with --expect-access)"
        elif access_ok "$code" "$loc"; then record PASS "$path" "$code intercepted by Cloudflare Access"
        else record FAIL "$path" "status $code"; fi
    fi
done

# 8. /mcp without key
code=$(fetch "https://$DOMAIN/mcp" -X POST -H 'Content-Type: application/json' -H "Origin: https://$DOMAIN" \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"verify","version":"0"}}}')
if [[ $code == 401 ]]; then record PASS "/mcp without key" "401"; else record FAIL "/mcp without key" "status $code (expected 401)"; fi

# 9. Rate limit burst on /webinar-api/profile
if (( BURST > 0 )); then
    first=$(fetch "https://$DOMAIN/webinar-api/profile")
    got429=0; n=0
    while (( n < BURST )); do
        c=$("${C[@]}" -o /dev/null -w '%{http_code}' "https://$DOMAIN/webinar-api/profile" 2>/dev/null || echo 000)
        n=$((n + 1))
        if [[ $c == 429 ]]; then got429=1; break; fi
    done
    if [[ $first == 429 ]]; then record FAIL "rate limit /webinar-api/profile" "already 429 before the burst (blocked by an earlier run? wait a minute)"
    elif (( got429 )); then record PASS "rate limit /webinar-api/profile" "first $first, then 429 after $n rapid requests"
    else record FAIL "rate limit /webinar-api/profile" "no 429 within $BURST requests (first status $first)"; fi
else
    record PASS "rate limit burst" "skipped (--no-burst)"
fi

# 10. Unknown /webinar-api/ path is 404 at the edge
code=$(fetch "https://$DOMAIN/webinar-api/nothing-here")
if [[ $code == 404 ]]; then record PASS "/webinar-api/* unknown path" "404"; else record FAIL "/webinar-api/* unknown path" "status $code"; fi

printf '\n%-6s %-40s %s\n' RESULT CHECK DETAIL
printf '%-6s %-40s %s\n' ------ ---------------------------------------- ------
for r in "${RESULTS[@]}"; do
    IFS='|' read -r s c d <<< "$r"
    printf '%-6s %-40s %s\n' "$s" "$c" "$d"
done
printf '\n%s: %s check(s) failed\n' "$DOMAIN" "$FAIL"
(( FAIL == 0 ))

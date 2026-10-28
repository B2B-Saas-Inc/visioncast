#!/usr/bin/env bash
# Write nginx snippets with Cloudflare's published address ranges.
#
#   cloudflare-ips.sh                      refresh /etc/nginx/snippets/visioncast-cloudflare-*.conf
#   cloudflare-ips.sh --reload             ... and `nginx -t && systemctl reload nginx` when they changed
#   cloudflare-ips.sh --out-dir DIR        write into another directory (tests, dry runs)
#   cloudflare-ips.sh --offline            use the bundled cloudflare-ips.fallback instead of downloading
#
# Sources: https://www.cloudflare.com/ips-v4 and https://www.cloudflare.com/ips-v6 (the
# lists Cloudflare publishes for origin firewalls). Every line must be a plain CIDR;
# anything else aborts without touching the installed snippets. When the download fails
# the existing snippets are kept; when none exist yet, the bundled fallback list
# (dated inside the file) is installed with a warning.
#
# Output files (both read by nginx-site.conf.tmpl):
#   visioncast-cloudflare-realip.conf   set_real_ip_from <cidr>; ... real_ip_header CF-Connecting-IP;
#   visioncast-cloudflare-geo.conf      <cidr> 1;  (for the geo block that gates /admin/ and /studio/)
#
# bootstrap.sh runs this once; the installed cron entry (cron.tmpl) refreshes it weekly.
set -Eeuo pipefail

SRC_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
OUT_DIR=/etc/nginx/snippets
RELOAD=0
OFFLINE=0
die() { printf 'cloudflare-ips: ERROR: %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
    case $1 in
        --out-dir) [[ $# -ge 2 ]] || die "--out-dir needs a value"; OUT_DIR=$2; shift ;;
        --reload) RELOAD=1 ;;
        --offline) OFFLINE=1 ;;
        -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
    shift
done

V4_RE='^([0-9]{1,3}\.){3}[0-9]{1,3}/[0-9]{1,2}$'
V6_RE='^[0-9a-fA-F:]+/[0-9]{1,3}$'

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

SOURCE=""
if (( ! OFFLINE )); then
    if curl -fsS --max-time 20 -o "$TMP/v4" https://www.cloudflare.com/ips-v4 \
       && curl -fsS --max-time 20 -o "$TMP/v6" https://www.cloudflare.com/ips-v6; then
        SOURCE="https://www.cloudflare.com/ips-v4 + ips-v6"
    else
        printf 'cloudflare-ips: WARNING: download failed\n' >&2
    fi
fi
if [[ -z $SOURCE ]]; then
    if [[ -s $OUT_DIR/visioncast-cloudflare-geo.conf && ! $OFFLINE -eq 1 ]]; then
        printf 'cloudflare-ips: keeping the installed snippets unchanged\n'
        exit 0
    fi
    [[ -f $SRC_DIR/cloudflare-ips.fallback ]] || die "no download and no fallback list"
    grep -E "$V4_RE" "$SRC_DIR/cloudflare-ips.fallback" > "$TMP/v4" || true
    grep -E "$V6_RE" "$SRC_DIR/cloudflare-ips.fallback" > "$TMP/v6" || true
    SOURCE="bundled fallback $(sed -n 's/^# as of //p' "$SRC_DIR/cloudflare-ips.fallback" | head -n 1)"
    printf 'cloudflare-ips: WARNING: using the %s; refresh with network access soon\n' "$SOURCE" >&2
fi

# Normalise (CRLF, missing final newline, blank lines), then validate: only CIDRs, nothing
# else (a tampered or HTML response must never reach nginx).
awk '{ sub(/\r$/, ""); if ($0 != "") print }' "$TMP/v4" > "$TMP/v4.n"
awk '{ sub(/\r$/, ""); if ($0 != "") print }' "$TMP/v6" > "$TMP/v6.n"
V4_COUNT=0; V6_COUNT=0
while IFS= read -r line; do
    [[ $line =~ $V4_RE ]] || die "unexpected IPv4 line: ${line:0:40}"
    V4_COUNT=$((V4_COUNT + 1))
done < "$TMP/v4.n"
while IFS= read -r line; do
    [[ $line =~ $V6_RE ]] || die "unexpected IPv6 line: ${line:0:40}"
    V6_COUNT=$((V6_COUNT + 1))
done < "$TMP/v6.n"
(( V4_COUNT >= 5 && V6_COUNT >= 3 )) || die "list too short ($V4_COUNT IPv4, $V6_COUNT IPv6); refusing"
[[ $(wc -l < "$TMP/v4.n" | tr -d ' ') == "$V4_COUNT" && $(wc -l < "$TMP/v6.n" | tr -d ' ') == "$V6_COUNT" ]] || die "line count mismatch"

NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
{
    printf '# Cloudflare address ranges: trust CF-Connecting-IP only from these peers.\n'
    printf '# Generated %s by deploy/production/cloudflare-ips.sh from %s. Do not edit.\n' "$NOW" "$SOURCE"
    awk '{ print "set_real_ip_from " $0 ";" }' "$TMP/v4.n" "$TMP/v6.n"
    printf 'real_ip_header CF-Connecting-IP;\nreal_ip_recursive off;\n'
} > "$TMP/realip.conf"
{
    printf '# Cloudflare address ranges for `geo $realip_remote_addr $vc_from_cloudflare`.\n'
    printf '# Generated %s by deploy/production/cloudflare-ips.sh from %s. Do not edit.\n' "$NOW" "$SOURCE"
    awk '{ print $0 " 1;" }' "$TMP/v4.n" "$TMP/v6.n"
} > "$TMP/geo.conf"

mkdir -p "$OUT_DIR"
CHANGED=0
for pair in "realip.conf:visioncast-cloudflare-realip.conf" "geo.conf:visioncast-cloudflare-geo.conf"; do
    src=${pair%%:*}; dest=$OUT_DIR/${pair#*:}
    # Compare without the generation timestamp line.
    if [[ -f $dest ]] && diff -q <(grep -v '^# Generated' "$TMP/$src") <(grep -v '^# Generated' "$dest") >/dev/null; then
        continue
    fi
    install -m 0644 "$TMP/$src" "$dest"
    CHANGED=1
done
printf 'cloudflare-ips: %s IPv4 + %s IPv6 ranges from %s -> %s (%s)\n' "$V4_COUNT" "$V6_COUNT" "$SOURCE" "$OUT_DIR" \
    "$([[ $CHANGED -eq 1 ]] && echo updated || echo unchanged)"

if (( RELOAD && CHANGED )); then
    if nginx -t -q; then
        systemctl reload nginx
        printf 'cloudflare-ips: nginx reloaded\n'
    else
        die "nginx -t failed after the refresh; snippets left in place for inspection, nginx NOT reloaded"
    fi
fi

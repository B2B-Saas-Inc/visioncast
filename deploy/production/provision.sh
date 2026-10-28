#!/usr/bin/env bash
# Create the production droplet with doctl. DRY RUN by default: prints the exact
# `doctl compute droplet create` command and the monthly price, creates nothing.
#
#   deploy/production/provision.sh                      dry run
#   deploy/production/provision.sh --apply              create the droplet (a person runs this)
#
# Options (environment variables of the same name are accepted):
#   --name NAME        droplet name (default visioncast-events); refuses if it already exists
#   --region SLUG      default nyc3
#   --size SLUG        default s-1vcpu-2gb
#   --image SLUG       default ubuntu-24-04-x64
#   --ssh-key ID       existing DigitalOcean SSH key id (default 60221334)
#   --tags LIST        default visioncast,production
#   --user-data FILE   cloud-init user-data (default: cloud-init.yaml next to this script)
#
# The droplet has NO DigitalOcean backups add-on (the platform makes its own encrypted
# dumps, deploy/production/cron.tmpl) and monitoring enabled (free agent). Billing starts
# when the droplet exists and only stops when the owner destroys it.
set -Eeuo pipefail

SRC_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
NAME=${NAME:-visioncast-events}
REGION=${REGION:-nyc3}
SIZE=${SIZE:-s-1vcpu-2gb}
IMAGE=${IMAGE:-ubuntu-24-04-x64}
SSH_KEY=${SSH_KEY:-60221334}
TAGS=${TAGS:-visioncast,production}
USER_DATA=${USER_DATA:-$SRC_DIR/cloud-init.yaml}
APPLY=0

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
usage() { sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
    case $1 in
        --name) NAME=$2; shift ;;
        --region) REGION=$2; shift ;;
        --size) SIZE=$2; shift ;;
        --image) IMAGE=$2; shift ;;
        --ssh-key) SSH_KEY=$2; shift ;;
        --tags) TAGS=$2; shift ;;
        --user-data) USER_DATA=$2; shift ;;
        --apply) APPLY=1 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
    shift
done

command -v doctl >/dev/null || die "doctl not found (brew install doctl; doctl auth init)"
[[ -f $USER_DATA ]] || die "user-data file not found: $USER_DATA"
head -n 1 "$USER_DATA" | grep -q '^#cloud-config' || die "$USER_DATA does not start with #cloud-config"
[[ $NAME =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] || die "--name must be a DNS-safe label"
[[ $SSH_KEY =~ ^[0-9]+$ ]] || die "--ssh-key must be a numeric DigitalOcean key id"

printf '== doctl account\n'
doctl account get --format Email,Status,DropletLimit --no-header | sed 's/^/   /' || die "doctl is not authenticated (doctl auth init)"

printf '== SSH key %s\n' "$SSH_KEY"
KEY_LINE=$(doctl compute ssh-key get "$SSH_KEY" --format ID,Name,FingerPrint --no-header 2>/dev/null) || die "SSH key id $SSH_KEY not found in this account"
printf '   %s\n' "$KEY_LINE"

printf '== size %s in %s\n' "$SIZE" "$REGION"
SIZE_LINE=$(doctl compute size list --format Slug,Memory,VCPUs,Disk,PriceMonthly --no-header | awk -v s="$SIZE" '$1 == s') || true
[[ -n $SIZE_LINE ]] || die "size $SIZE not found (doctl compute size list)"
PRICE=$(printf '%s' "$SIZE_LINE" | awk '{print $5}')
printf '   %s MB RAM, %s vCPU, %s GB disk: USD %s per month (excl. taxes, bandwidth overage)\n' \
    "$(printf '%s' "$SIZE_LINE" | awk '{print $2}')" "$(printf '%s' "$SIZE_LINE" | awk '{print $3}')" "$(printf '%s' "$SIZE_LINE" | awk '{print $4}')" "$PRICE"
REGION_LINE=$(doctl compute region list --format Slug,Available,Sizes --no-header | awk -v r="$REGION" '$1 == r') || true
[[ -n $REGION_LINE ]] || die "region $REGION not found (doctl compute region list)"
[[ $(printf '%s' "$REGION_LINE" | awk '{print $2}') == true ]] || die "region $REGION is not available for new droplets"
if printf '%s' "$REGION_LINE" | grep -q '<nil>'; then
    printf '   (doctl did not list the sizes offered in %s; the create call will refuse an unavailable size)\n' "$REGION"
else
    printf '%s' "$REGION_LINE" | grep -qw "$SIZE" || die "size $SIZE is not offered in region $REGION"
    printf '   offered in %s: yes\n' "$REGION"
fi

printf '== existing droplets named %s\n' "$NAME"
if doctl compute droplet list --format Name --no-header | grep -qx "$NAME"; then
    doctl compute droplet list --format ID,Name,PublicIPv4,Region,Status,Created --no-header | awk -v n="$NAME" '$2 == n' | sed 's/^/   /'
    die "a droplet named $NAME already exists; refusing to create another (destroying is the owner's decision, not this script's)"
fi
printf '   none\n'

CMD=(doctl compute droplet create "$NAME"
     --region "$REGION" --size "$SIZE" --image "$IMAGE"
     --ssh-keys "$SSH_KEY" --user-data-file "$USER_DATA"
     --tag-names "$TAGS" --enable-monitoring --wait
     --format "ID,Name,PublicIPv4,Region,Status" --no-header)

printf '\n== command\n   %s\n' "${CMD[*]}"
if (( ! APPLY )); then
    printf '\nDRY RUN: nothing created. Re-run with --apply to create the droplet (USD %s/month until destroyed).\n' "$PRICE"
    exit 0
fi

printf '\n== creating %s (USD %s/month until the owner destroys it)\n' "$NAME" "$PRICE"
OUT=$("${CMD[@]}")
printf '   %s\n' "$OUT"
ID=$(printf '%s' "$OUT" | awk '{print $1}')
IP=$(printf '%s' "$OUT" | awk '{print $3}')
# The IPv4 can lag the create call by a few seconds; read it from the JSON networks list.
for _try in 1 2 3 4 5 6; do
    [[ -n $IP && $IP != "<nil>" && $IP =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] && break
    IP=$(doctl compute droplet get "$ID" --output json | python3 -c 'import json,sys; d=json.load(sys.stdin)[0]; print(next((n["ip_address"] for n in d["networks"]["v4"] if n["type"]=="public"), ""))')
    [[ -n $IP ]] || sleep 5
done

cat <<EOF

Droplet $NAME created: id $ID, public IPv4 $IP ($REGION, $SIZE, $IMAGE, tags $TAGS).
cloud-init runs for a few minutes (Docker, nginx, certbot, ufw, swap). Check with:
   ssh root@$IP cloud-init status --wait

DNS record to create in Cloudflare (deploy/production/cloudflare.md):
   Type A   Name events   Content $IP   Proxy: DNS only for the first certificate, then Proxied

Next: deploy/production/package-release.sh, upload release.tar, run bootstrap.sh (deploy/production/README.md).
EOF

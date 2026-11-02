#!/usr/bin/env bash
# Checks a live server from outside it: names, certificates, and which ports are open and closed.
#   deploy/live/verify-public.sh live.example.com rtc.example.com turn.example.com
# Run this from somewhere other than the server. It needs curl, openssl and nc. It can't prove UDP
# or TURN really work: for that, make a real call, and run live/tests/turn/check.cjs.
set -uo pipefail
if [ $# -ne 3 ]; then echo "Usage: $0 <live host> <media host> <TURN host>" >&2; exit 2; fi
live=$1 rtc=$2 turn=$3
bad=0
ok() { echo "ok    $1"; }
no() { echo "FAIL  $1"; bad=1; }
note() { echo "note  $1"; }
for host in "$live" "$rtc" "$turn"; do
  end=$(echo | timeout 10 openssl s_client -connect "$host:443" -servername "$host" -verify_hostname "$host" -verify_return_error 2>/dev/null | openssl x509 -noout -enddate 2>/dev/null)
  if [ -n "$end" ]; then ok "$host has a valid certificate ($end)"; else no "$host has no valid certificate on 443. If a port was closed at the start: docker compose restart traefik"; fi
done
# A connect timeout for nc. macOS's nc ignores -w while connecting, so a firewalled port waits about
# 75 seconds there; -G is its connect timeout. Other nc builds don't have -G.
probe() { if [ "$(uname)" = Darwin ]; then nc -z -G "$3" -w "$3" "$1" "$2"; else nc -z -w "$3" "$1" "$2"; fi; }
curl -fsS --max-time 10 "https://$live/healthz" >/dev/null && ok "the service answers" || no "the service doesn't answer on https://$live/healthz"
[ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "https://$live/captions/in/x")" = 403 ] && ok "the captions way in is closed from outside" || no "https://$live/captions/in/ should be refused from outside"
curl -s -o /dev/null --max-time 10 "https://$rtc/" && ok "media signalling answers" || no "media signalling doesn't answer on https://$rtc/"
probe "$rtc" 7881 5 && ok "7881/tcp is open (media over TCP)" || no "7881/tcp is closed: people on UDP-blocking networks fall back to TURN"
# Nothing else should answer from outside.
for port in 5349 6379 7880 8000 8080 8085 8090 5000 9090; do
  if probe "$live" "$port" 3 2>/dev/null; then no "$port/tcp answers from outside. Close it in your firewall"; else ok "$port/tcp is closed"; fi
done
note "UDP can't be checked from here: a closed port and an open one look the same. Open 3478 and 30000-40000 and 50000-60000 in your firewall, then make a call from a phone on mobile data."
echo
[ "$bad" = 0 ] && echo "All good so far. Now try a real call, and live/tests/turn/check.cjs for networks that only allow port 443." || echo "Something needs fixing."
exit "$bad"

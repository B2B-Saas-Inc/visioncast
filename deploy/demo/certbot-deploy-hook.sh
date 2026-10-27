#!/bin/sh
# certbot deploy hook: reload nginx after a successful renewal.
# Installed to /etc/letsencrypt/renewal-hooks/deploy/visioncast-nginx-reload (root, 0755).
set -eu
nginx -t -q && systemctl reload nginx

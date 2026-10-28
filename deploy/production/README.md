# Production deployment: events.growthcast.app

Full install of the webinar platform (web + worker + PostgreSQL under Docker Compose, nginx +
Let's Encrypt on the host, encrypted backups, retention cron) on one DigitalOcean droplet
(Ubuntu 24.04, `s-1vcpu-2gb`, USD 12/month) behind Cloudflare (proxied DNS, Access in front of
the studio and admin). Everything in this directory was rehearsed locally only (see
[docs/validation.md](../../docs/validation.md), entry "Production deployment tooling"); **no
droplet, DNS record or Cloudflare setting has been created by it**. The first real run is the
owner's.

| File | Runs where | Purpose |
|---|---|---|
| `provision.sh` | workstation | `doctl` droplet creation; **dry run by default**, `--apply` creates |
| `cloud-init.yaml` | droplet first boot | `deploy` user, unattended-upgrades, ufw 22/80/443, fail2ban, 2 GB swap, Docker + Compose, nginx, certbot, age |
| `package-release.sh` | workstation | `git archive` of an allowlist from a tag → `dist/visioncast-<version>.tar` + SHA-256 |
| `update.sh` / `host-update.sh` | workstation / droplet | upload a release, run `deploy/upgrade.sh` (backup → migrate → restart → `sync_roles` → health), switch `current` |
| `bootstrap.sh` | droplet (root) | the install itself; `--dry-run` prints every step and every rendered file with secrets redacted |
| `render_env.py` | droplet | renders `.env` from `.env.example` with generated secrets; never prints values |
| `nginx-site.conf.tmpl`, `nginx-site-tls.conf.tmpl` | droplet | edge config derived from `docker/nginx.conf.example` (all its zones and locations) plus Cloudflare real-IP and the origin gate |
| `cloudflare-ips.sh` (+ `.fallback`) | droplet | Cloudflare address ranges → nginx snippets (bootstrap + weekly cron) |
| `cron.tmpl`, `backup-nightly.sh`, `logrotate.conf`, `certbot-deploy-hook.sh` | droplet | schedule and housekeeping |
| `verify.sh` | workstation | PASS/FAIL table against the public domain (TLS, HSTS, redirect, version, OpenAPI, Access, MCP 401, 429 burst, headers) |
| `cloudflare.md` | owner | DNS, Full (strict), Always Use HTTPS, Access, WAF rate limits, cache bypass, Origin CA option |

## What the owner needs before starting

1. **DigitalOcean**: `doctl` authenticated (`doctl auth init`), SSH key id `60221334` present in
   the account (it is), the private key for it on the workstation.
2. **Cloudflare**: admin access to the `growthcast.app` zone and to Zero Trust (Access, free up
   to 50 users); optionally an API token for the `curl` calls in `cloudflare.md`.
3. **age recipient** for backup encryption: `age-keygen -o ~/visioncast-backups.key` on the
   workstation; the public key (`age1…`, printed as "Public key:") goes to the droplet, the
   identity file stays **off** the droplet (password manager + offline copy). Without it, the
   nightly dumps cannot be decrypted; without the dumps *and* the master key, nothing can be
   restored.
4. **SMTP relay** credentials (Postmark, SES SMTP, Resend SMTP, Google Workspace relay, …) for
   waiting-room verification codes and teammate invitations. The password is placed on the
   droplet in `/root/visioncast-secrets/smtp-password` (one line, mode 600) before bootstrap, never
   on a command line. Only `A-Z a-z 0-9 _ . / : , { } = @ + % ! * ~ ^ -` are accepted (shell/compose
   safe); pick a relay that issues such a credential.
5. **Image source**, one of:
   * build on the droplet from the uploaded source (default; ~5 minutes on 1 vCPU, hash-locked
     wheels only, no compiler), or
   * pull `ghcr.io/b2b-saas-inc/visioncast:<tag>` (private package): a GitHub token with
     `read:packages` placed in `/root/visioncast-secrets/ghcr-token` (mode 600) and
     `--image ghcr.io/b2b-saas-inc/visioncast:<tag>`. The script logs in with the token from the
     file, pulls, and logs out again.
6. **Stripe** (only if paid tickets are used): test-mode secret key and webhook signing secret,
   configured later in the studio (`docs/tickets.md`); `api.stripe.com` goes into
   `WEBHOOK_HOST_ALLOWLIST`.
7. Decisions: the Let's Encrypt account e-mail (`ACME_EMAIL`), the administrator username/e-mail,
   `WEBHOOK_HOST_ALLOWLIST` (every receiver and vendor host, `docs/release-checklist.md` D7),
   retention (`--purge-days`, default 30, must match the privacy notice).

A release **tag that contains `deploy/production/`**. `v0.3.0-pre` predates this directory and
cannot be packaged; tag the merged tree (for example `v0.3.0`) first, or package a branch ref
with `package-release.sh --ref <branch>` for a rehearsal.

## Order of operations (first install)

```sh
# 0. From the repository root on the workstation.
deploy/production/provision.sh                       # dry run: prints the doctl command and USD 12.00/month
deploy/production/provision.sh --apply               # creates visioncast-events in nyc3; prints the IP
ssh root@<IP> cloud-init status --wait               # "done" after a few minutes (Docker, nginx, certbot, ufw, swap)

# 1. Cloudflare DNS: A events -> <IP>, proxy OFF for now (cloudflare.md section 2, variant A).
dig +short events.growthcast.app                     # must print <IP> before the next step

# 2. Secrets the operator places on the droplet (values never typed on a command line):
ssh root@<IP> 'umask 077; cat > /root/visioncast-secrets/smtp-password'   < ~/path/to/relay-password.txt
ssh root@<IP> 'umask 077; cat > /root/visioncast-secrets/ghcr-token'      < ~/path/to/ghcr-token.txt   # only for --image ghcr.io/...

# 3. Package and upload the release.
deploy/production/package-release.sh v0.3.0          # dist/visioncast-0.3.0.tar + .sha256
deploy/production/update.sh --host <IP> --tarball dist/visioncast-0.3.0.tar --upload-only

# 4. Bootstrap (dry run first, then apply). Parameters are environment variables or flags;
#    they are saved on the droplet in /srv/visioncast/bootstrap.conf for later runs.
ssh root@<IP> 'DOMAIN=events.growthcast.app ACME_EMAIL=ops@growthcast.app \
  AGE_RECIPIENT=age1... SMTP_HOST=smtp.example.com SMTP_USERNAME=... \
  WEBHOOK_HOST_ALLOWLIST=api.stripe.com,hooks.zapier.com bash -s -- --dry-run' \
  < deploy/production/bootstrap.sh
ssh root@<IP> 'DOMAIN=events.growthcast.app ACME_EMAIL=ops@growthcast.app \
  AGE_RECIPIENT=age1... SMTP_HOST=smtp.example.com SMTP_USERNAME=... \
  WEBHOOK_HOST_ALLOWLIST=api.stripe.com,hooks.zapier.com bash -s' \
  < deploy/production/bootstrap.sh
#    (add --image ghcr.io/b2b-saas-inc/visioncast:0.3.0 to pull instead of building; add --agent-key 1
#    for a config_read key for agents; --web-concurrency, --purge-days, --backup-keep-days as needed)

# 5. Cloudflare: proxy the record, Full (strict), Always Use HTTPS, Access, WAF rules, cache rules
#    (cloudflare.md sections 1-5).

# 6. Verify from the workstation.
deploy/production/verify.sh --domain events.growthcast.app --expect-version 0.3.0 --expect-access
deploy/production/verify.sh --domain events.growthcast.app --origin <IP> --no-burst   # origin gate: /admin/ 404 direct
```

`bootstrap.sh` steps, in order (`--steps` selects a subset; every step is idempotent):
`preflight` (root, Ubuntu 24.04, tools, parameters) → `dirs` → `release` (verify SHA-256, unpack
`/srv/visioncast/release.tar` into `/srv/visioncast/app/<version>`, `current` symlink) → `env`
(render `.env` mode 600 with generated `DJANGO_SECRET_KEY`, `POSTGRES_PASSWORD`,
`INTEGRATION_MASTER_KEY`; `DJANGO_TRUST_X_FORWARDED_PROTO=1`,
`RATE_LIMIT_TRUST_X_FORWARDED_FOR=1`, `RATE_LIMIT_TRUSTED_PROXY_COUNT=1`,
`CSRF_TRUSTED_ORIGINS=https://<domain>`, `CAPABILITY_COOKIE_SECURE=1`,
`DJANGO_HSTS_SECONDS=31536000`, `WEBINAR_IMAGE` pinned, `WEBINAR_PULL_POLICY=never`; copy of
the master key to `/root/visioncast-secrets/integration-master-key`) → `image` (build or pull)
→ `compose` (`migrate`, `manage check`, `sync_roles`, `bootstrap_admin` → `admin-password`,
`create_api_key --name growthcast-site --scope proxy` → `proxy-api-key`, optional `agent` key,
`up -d`, wait healthy, loopback `/healthz`) → `cloudflare` (address ranges → nginx snippets) →
`nginx` (port 80: ACME + redirect) → `certbot` (webroot HTTP-01) → `nginx` HTTPS server →
`cron` (`/etc/cron.d/visioncast`, logrotate) → `status` (`/srv/visioncast/STATUS.md`) → `verify`
(loopback checks). If certbot fails (DNS not ready, proxied with Always Use HTTPS on), nginx
keeps serving port 80, the rest completes, and `bootstrap.sh --steps certbot,nginx,status,verify`
finishes the job later.

Secret files on the droplet (`/root/visioncast-secrets/`, root 0700, each 0600):
`integration-master-key` (**copy into the owner's password manager now; delete the file
afterwards if you prefer, `.env` keeps the working copy**), `admin-password`, `proxy-api-key`,
`agent-api-key` (optional), and the operator-provided `smtp-password` / `ghcr-token`. Nothing
prints them; read them with `ssh root@<IP> cat /root/visioncast-secrets/<name>` into a password
manager or pipe them straight into the consumer (`… | vercel env add VISIONCAST_PROXY_KEY production`).

## After bootstrap: studio steps

1. Open `https://events.growthcast.app/studio/` — Cloudflare Access asks for the owner's e-mail
   (one-time PIN or Google), then the studio login: user `admin` (or `--admin-username`),
   password from `/root/visioncast-secrets/admin-password`.
2. **Change the password** immediately (studio profile / `manage changepassword`), then
   delete `admin-password` on the droplet.
3. Invite marketers from the studio (teammate invitations use the SMTP relay); give them a role
   group, never superuser. Add their e-mails to the Access policy.
4. The site proxy key (`growthcast-site`, scope `proxy`) is already in `proxy-api-key`; put it in
   the marketing site's server-side secret store (`PROXY_API_KEY` of the first-party proxy,
   `docs/browser-integration.md`), never in GTM or page source. If it was not created
   (`--skip-admin` runs, lost file): studio → API keys → create, scope `proxy`, or
   `cd /srv/visioncast/app/current && docker compose run --rm -T web manage create_api_key --name growthcast-site-2 --scope proxy`
   and revoke the old one.
5. `PUBLIC_EVENT_URL_TEMPLATE` is already `https://growthcast.app/events/{slug}` (calendar links
   point at the marketing site). Change it with
   `bootstrap.sh --public-event-url-template … --steps env,compose` (containers are recreated by
   `docker compose up -d` only when `.env` changed; run `docker compose up -d web worker` to be sure).
6. Configure destinations (webhooks, native integrations) in the studio. Every destination host
   must be in `WEBHOOK_HOST_ALLOWLIST` (`bootstrap.sh --webhook-host-allowlist a,b,c --steps env,compose`).
7. Stripe (if used): account and prices in the studio, webhook endpoint
   `https://events.growthcast.app/api/v1/payments/stripe/webhook` with the five events listed in
   `docs/tickets.md` (checklist D8).
8. Launch acceptance: `docs/implementation-guide.md` §11.1; record results in `docs/validation.md`.

## Day-2 operations

| Task | Command (workstation unless noted) |
|---|---|
| Update to a new release | `deploy/production/package-release.sh v0.3.1 && deploy/production/update.sh --host <IP> --tarball dist/visioncast-0.3.1.tar [--image ghcr.io/b2b-saas-inc/visioncast:0.3.1]` (backup → migrate → restart → `sync_roles` → health → `manage check` → `current` switch → nginx/cron/STATUS refresh); `--dry-run` first |
| Verify after anything | `deploy/production/verify.sh --domain events.growthcast.app --expect-access --expect-version <v>` |
| Logs (droplet) | `cd /srv/visioncast/app/current && docker compose logs --since 1h web worker`; nginx `/var/log/nginx/visioncast.access.log` (no query strings, cookies or user agents); cron `/var/log/visioncast/*.log` |
| Backups | nightly 02:15 UTC → `/srv/visioncast/backups/webinars-<ts>.dump.age`, 14 kept. **Copy them off-host** (e.g. `rsync -a root@<IP>:/srv/visioncast/backups/ ~/visioncast-backups/`); nothing here uploads anywhere. Manual: `cd /srv/visioncast/app/current && deploy/backup.sh --encrypt-to age1… /srv/visioncast/backups` |
| Backup verification (restore drill) | on the droplet: `age -d -i <identity> -o /root/restore.dump …dump.age` (identity copied over for the drill only, then shredded), then `cd /srv/visioncast/app/current && COMPOSE_PROJECT_NAME=wprod-drill deploy/restore.sh --no-start /root/restore.dump` (separate project, own volume), inspect with `COMPOSE_PROJECT_NAME=wprod-drill docker compose run --rm -T web manage showmigrations`, finish with `COMPOSE_PROJECT_NAME=wprod-drill docker compose down -v` and `shred -u /root/restore.dump`. Record the date in `docs/validation.md` (checklist D9). |
| Retention | daily 03:15 UTC `purge_personal_data --older-than-days 30` (`--purge-days`); erasure requests: `docker compose run --rm -T web manage purge_personal_data --person <uuid>` |
| Configuration check | weekly Monday 04:15 UTC `manage check` + `docker compose ps` into `/var/log/visioncast/check.log`; also run by every update |
| Cloudflare ranges | weekly Monday 04:45 UTC `cloudflare-ips.sh --reload`; on demand after a Cloudflare announcement |
| Certificate | certbot's packaged timer renews; `/etc/letsencrypt/renewal-hooks/deploy/visioncast-nginx-reload` reloads nginx; expiry is in `STATUS.md` and `verify.sh` |
| Change a parameter | `ssh root@<IP> bash -s -- --<flag> value --steps env,compose,status < deploy/production/bootstrap.sh` (saved in `bootstrap.conf`; secrets untouched) |
| Rotate the master key | `docs/operations.md` §2 (edit `.env` by hand for `INTEGRATION_MASTER_KEY_PREVIOUS`; `render_env.py update` keeps it) |
| OS updates | unattended-upgrades installs security updates and reboots at 04:30 UTC when required; Docker restarts the containers (`restart: unless-stopped`) |
| Host status | `cat /srv/visioncast/STATUS.md` (versions, dates, schedule; no secrets) |

## Rollback

Every release lives in its own directory with its own `.env`; `current` is a symlink.

* **No schema change in the new release** (the usual case; release notes must say):
  ```sh
  ln -sfn /srv/visioncast/app/<previous> /srv/visioncast/app/current
  cd /srv/visioncast/app/current && docker compose up -d web worker      # previous .env pins the previous image
  bash deploy/production/bootstrap.sh --steps cloudflare,nginx,cron,status,verify
  ```
* **Schema changed**: reverse the migration with the new image first, or restore the
  pre-upgrade dump that `update.sh` took (`/srv/visioncast/backups/`, `deploy/restore.sh --force`)
  with the previous image — `docs/operations.md` §5. Registrations taken since the dump are lost
  in the second case.
* **Certificate or nginx problem**: `nginx -t`; the previous rendered files are not kept, re-run
  `bootstrap.sh --steps cloudflare,nginx,certbot,status`.
* **Whole host**: a fresh droplet + `bootstrap.sh` + `deploy/restore.sh` with the latest
  `.dump.age` (needs the age identity and the master key from the password manager). Rehearse
  this once before relying on it.

## Security posture

Provided by this tooling:

* TLS 1.2/1.3 at nginx (Let's Encrypt, auto-renew), HTTP → HTTPS at nginx and at Cloudflare,
  HSTS one year from Django; `X-Forwarded-Proto` overwritten by nginx, so
  `DJANGO_TRUST_X_FORWARDED_PROTO=1` is safe.
* Visitor address: `CF-Connecting-IP` trusted only from Cloudflare's published ranges (refreshed
  weekly), written as the single `X-Forwarded-For` hop → `RATE_LIMIT_TRUSTED_PROXY_COUNT=1`.
* Rate limits at nginx on every path the checklist names (registration, reset, profile,
  my-registration, calendar, `/w/`, `/mcp`, `/studio/login/`, `/admin/login/`, Stripe webhook,
  `/api/v1/`), 20 connections per client, 64 KB bodies (3 MB on studio/admin for covers), plus
  Cloudflare WAF rules as the outer layer.
* `/admin/` and `/studio/` answer 404 unless the request came through Cloudflare (origin gate),
  where Access enforces identity + MFA. Unknown `Host` on port 80 is closed (444); unknown SNI on
  443 is rejected.
* Containers: read-only root filesystem, `cap_drop ALL`, `no-new-privileges`, uid 10001, web
  published on `127.0.0.1:8000` only, database on an internal network with no published port.
* Host: ufw (22/80/443), fail2ban on sshd, password SSH disabled, unattended security upgrades,
  Docker log rotation, `.env` and secret files root-only (0600), release trees root-owned and
  read-only to others.
* Secrets generated on the host, never printed; API key and admin password captured straight into
  0600 files; GHCR token only read from a file and logged out after the pull.
* Backups age-encrypted nightly, rotated after 14 days; master key copy kept apart from the dumps.

Still the operator's job (nothing here does it):

* **Copy backups and the master key off-host**, keep the age identity off-host, and run a restore
  drill (D9).
* **Cloudflare**: everything in `cloudflare.md` (Access policy membership, WAF rules, Full strict).
  Until Access is enabled the studio is protected by its password only, and `verify.sh` says so.
* **Secrets lifecycle**: change the bootstrapped admin password, rotate API keys and the SMTP
  credential on staff changes, keep `ghcr-token` short-lived or delete it after the pull.
* **`WEBHOOK_HOST_ALLOWLIST`** discipline: only receivers you control or contract with.
* **Monitoring/alerting**: DigitalOcean's monitoring agent is enabled, but no alert policy,
  uptime check (`https://events.growthcast.app/healthz`) or log shipping is configured.
* **Capacity**: 1 vCPU / 2 GB with `WEB_CONCURRENCY=2` is sized for a marketing site's
  registration traffic, not for a launch spike; resize the droplet (`doctl compute droplet-action resize`)
  and raise `--web-concurrency` when needed. Building the image on the droplet takes a few
  minutes of CPU; pulling from GHCR avoids it.
* **DigitalOcean account**: the droplet costs USD 12/month until destroyed; destroying it deletes
  the database. No DO backups add-on is enabled (the platform's own dumps replace it); enable it
  as well if a second, independent copy is wanted.
* **Everything in `docs/release-checklist.md` section D** that is not listed as "provided" above
  (D4 edge mode, D7 vendor hosts, D8 Stripe events, D11 marketers to `/studio/`), and the
  launch acceptance run.


> **Note (2026-10-28):** when the script is fed over `bash -s`, stdin *is* the script. Every Compose invocation now redirects stdin from `/dev/null` so no step can swallow the rest of the script; after the `release` step you can also run it from the installed tree: `bash /srv/visioncast/app/current/deploy/production/bootstrap.sh --steps …`.

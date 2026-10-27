# VisionCast public demo: native droplet deployment

A Django + PostgreSQL backend for the Vercel demo site (`https://visioncast-ten.vercel.app`).
It runs natively on Ubuntu 24.04 with systemd on the dedicated droplet that the owner approved:

| | |
|---|---|
| Droplet | `visioncast-demo`, ID `612305850`, IPv4 `64.225.16.137` (basic, $6/month, no paid extras) |
| Backend URL | `https://64-225-16-137.sslip.io` (free sslip.io name; Let's Encrypt certificate) |
| Source | already uploaded by the operator to `/srv/visioncast/app` |

> **Billing:** the droplet costs money from the moment it exists until the owner destroys it.
> If a deploy run fails, or you roll back, or you power the droplet off, it **still bills**.
> Nothing in this directory destroys it. Destroying is a separate, irreversible owner decision.

None of this has been executed yet. The author could not run it (no shell or network access), so
the first dry run on the droplet is also the first test.

## What gets deployed

* **Packages** (apt): `postgresql`, `python3-venv`, `nginx`, `certbot`. No other services.
* **Swap**: a 2 GB `/swapfile`, created only if the host has no active swap.
* **User** `visioncast`: a system account with no login shell, no home directory and no sudo.
  It owns only `/srv/visioncast/static`. The code and the venv are owned by root and the service can only read them.
* **PostgreSQL**: role and database `visioncast`, which is not a superuser. Django connects over the local socket
  `/var/run/postgresql` with **peer** authentication, so no database password is used.
  `POSTGRES_PASSWORD` is still generated because settings require it, but nothing reads it.
* **Env file** `/etc/visioncast/visioncast.env` (root, 0600). `write_env.py` creates it once with
  fresh random `DJANGO_SECRET_KEY`, `POSTGRES_PASSWORD` and `INTEGRATION_MASTER_KEY`. It is never
  overwritten, and later runs check it by key name only. `EMAIL_DELIVERY=disabled`. Note that the
  setting is `EMAIL_DELIVERY` (see `config/settings.py`); `EMAIL_DELIVERY_MODE` does not exist.
  `WEBHOOK_HOST_ALLOWLIST` is empty.
* **venv** `/srv/visioncast/venv`, installed from `requirements.txt` with `--require-hashes`
  and wheels only. It uses `uv` by default, or `python3-venv` + pip with `--installer pip`.
* **systemd**:
  * `visioncast-web.service` runs gunicorn with **1 worker** (2 threads) on **127.0.0.1:8000**.
    It is sandboxed (`ProtectSystem=strict`, no capabilities, `IPAddressDeny=any` except
    localhost, so it cannot open outbound connections) and capped at `MemoryMax=512M`. The only writable path is
    its tmpfs `RuntimeDirectory`.
  * `visioncast-migrate.service` and `visioncast-collectstatic.service` are one-shots that
    `deploy.sh` starts explicitly. Only collectstatic may write, and only to `/srv/visioncast/static`.
  * **There is no worker unit.** Nothing is delivered and no email is sent. Outbox and reminder
    rows created by registrations stay pending.
* **nginx**:
  * The public HTTPS server is an allowlist: `/api/v1/*` (except `/api/v1/manage*`),
    `/static/`, `/media/covers/` and `/healthz`. Every other path returns 404, including `/studio/`,
    `/admin/`, `/mcp`, `/api/v1/manage/*`, `/w/`, `/developers/` and `/webinar-api/*`.
  * Per-IP rate limits, a 64 KB body limit, header and body timeouts, and 20 connections per IP.
  * Port 80 only serves ACME challenges and redirects to HTTPS. Unknown hosts are dropped (444) and
    unknown SNI is refused.
  * The owner listener is `127.0.0.1:8081`. It exposes the whole app, including studio and admin,
    and can only be reached through an SSH tunnel.
* **TLS**: `certbot certonly --webroot` with no account email, wrapped in `timeout 300`.
  Renewal uses certbot's packaged timer, and a deploy hook reloads nginx.
* **Bootstrap**: `bootstrap_demo.py` uses the models API directly, with the same calls as
  `bootstrap_admin` and `create_api_key`. It creates the Administrator `demo-owner` and an
  API key with only the `proxy` scope. Each secret is written straight into a new root-only file.
  The bootstrap refuses to write to a terminal or a pipe, so the values are never displayed.
  * `/root/visioncast-secrets/admin-password`
  * `/root/visioncast-secrets/proxy-api-key`
* **Seed**: `seed_public_demo --dry-run --allow-registration`, then the same command without
  `--dry-run`. It creates 6 fictional events, with registration open on the 4 upcoming ones. It
  creates no destinations, and the bootstrap preflight refuses to continue if any destination exists.

## Files

| File | Installed to |
|---|---|
| `deploy.sh` | run from the transferred bundle (not installed) |
| `write_env.py` | run from the bundle with the system `python3 -I` |
| `visioncast-manage` | `/usr/local/sbin/visioncast-manage` (root 0700) |
| `bootstrap_demo.py`, `gunicorn.conf.py` | `/srv/visioncast/deploy/` |
| `systemd/*.service` | `/etc/systemd/system/` |
| `nginx/visioncast.conf`, `nginx/visioncast-tls.conf` | `/etc/nginx/conf.d/` |
| `nginx/visioncast-proxy.conf` | `/etc/nginx/snippets/` |
| `certbot-deploy-hook.sh` | `/etc/letsencrypt/renewal-hooks/deploy/visioncast-nginx-reload` |

## 1. Transfer (from the workstation, repository root)

The source must already be at `/srv/visioncast/app`. It must include the **untracked**
`webinars/management/commands/seed_public_demo.py`, which a `git archive` of `main` would miss.
`deploy.sh` stops if the file is missing.

```sh
tar -C deploy/demo -cf - . \
  | ssh root@64.225.16.137 'umask 077 && mkdir -p /root/visioncast-deploy && tar --no-same-owner -C /root/visioncast-deploy -xf -'
```

**uv (default installer).** Ubuntu 24.04 has no apt package for uv, and `deploy.sh` never downloads it.
Either upload a pinned, checksum-verified binary to `/usr/local/bin/uv`, or pass `--installer pip`.
Example, after choosing a release `<VER>` on the workstation:

```sh
curl -fLO https://github.com/astral-sh/uv/releases/download/<VER>/uv-x86_64-unknown-linux-gnu.tar.gz
curl -fLO https://github.com/astral-sh/uv/releases/download/<VER>/uv-x86_64-unknown-linux-gnu.tar.gz.sha256
sha256sum -c uv-x86_64-unknown-linux-gnu.tar.gz.sha256
tar -xzf uv-x86_64-unknown-linux-gnu.tar.gz
scp uv-x86_64-unknown-linux-gnu/uv root@64.225.16.137:/usr/local/bin/uv
ssh root@64.225.16.137 'chown root:root /usr/local/bin/uv && chmod 0755 /usr/local/bin/uv && /usr/local/bin/uv --version'
```

The operator should confirm the asset names on the release page before running this.

## 2. Start

```sh
ssh root@64.225.16.137
bash /root/visioncast-deploy/deploy.sh                       # dry run: identity check + report, no changes
bash /root/visioncast-deploy/deploy.sh --apply               # deploy (uv at /usr/local/bin/uv)
# or: bash /root/visioncast-deploy/deploy.sh --apply --installer pip
```

Before any other step, the script reads `http://169.254.169.254/metadata/v1/{id,hostname,interfaces/public/0/ipv4/address}`
and `hostname`. If they are not exactly `612305850`, `visioncast-demo` and `64.225.16.137`, it stops.
The dry run makes no changes. It only reads metadata, the package state, the PostgreSQL catalogue and the
env file key names. If the host is already deployed, it also runs `check --deploy`,
`migrate --plan --check` and `seed_public_demo --dry-run`, which rolls back its transaction.

## 3. Verify

`deploy.sh --apply` finishes with these on-host checks:

* `/healthz` and `/api/v1/events` return 200.
* The blocked paths return 404.
* HTTP redirects to HTTPS (301).
* The owner tunnel's studio login returns 200.
* gunicorn listens only on 127.0.0.1.
* No worker unit exists.

From the workstation:

```sh
H=https://64-225-16-137.sslip.io
curl -sS -o /dev/null -w '%{http_code}\n' $H/healthz                         # 200
curl -sS "$H/api/v1/events?when=upcoming" | head -c 300; echo                 # JSON, "Demo: ..." titles
for p in /studio/ /admin/ /mcp /api/v1/manage/events /; do
  printf '%s ' $p; curl -sS -o /dev/null -w '%{http_code}\n' $H$p; done       # all 404
```

**Vercel.** Set `VISIONCAST_API_BASE=https://64-225-16-137.sslip.io` and
`VISIONCAST_PROXY_KEY`. The key is the content of `/root/visioncast-secrets/proxy-api-key`.
Pipe it without displaying it, for example
`ssh root@64.225.16.137 'cat /root/visioncast-secrets/proxy-api-key' | vercel env add VISIONCAST_PROXY_KEY production`
(the Vercel CLI reads the value from stdin). Then redeploy the site. This publishes the key
into Vercel's secret store, which is intended.

**Owner access (studio/admin)**: use Chrome or Firefox. Safari may refuse the `Secure` cookies on
`http://localhost`. The local port must be **8081** because it is the CSRF-trusted origin.

```sh
ssh -N -L 8081:127.0.0.1:8081 root@64.225.16.137
# browse http://localhost:8081/studio/  user: demo-owner
# password: /root/visioncast-secrets/admin-password (copy it into a password manager,
# change it after the first login, then delete the file on the droplet)
```

**Logs**: `journalctl -u visioncast-web -n 100`, `/var/log/nginx/visioncast*.log` (path
without query string). The deploy never prints env values. Don't run `env`, `printenv` or
`diffsettings` through `visioncast-manage`.

## 4. Rollback: stop services, keep all data

```sh
systemctl disable --now visioncast-web.service     # backend down
systemctl disable --now nginx.service              # nothing listens on 80/443
# optional: systemctl stop postgresql.service
```

The following are all left in place:

* the database (`/var/lib/postgresql`)
* `/etc/visioncast/visioncast.env`
* `/root/visioncast-secrets/`
* `/srv/visioncast/` (code, venv, static)
* the certificate in `/etc/letsencrypt`
* the swap file

To bring the backend back, run `deploy.sh --apply` again. It is idempotent: it keeps the existing
secrets, admin, key and seed, and re-runs the seed only to converge.
To cut the Vercel site off without stopping anything, revoke the `visioncast-vercel` key in
`/admin/` (through the tunnel).

**A rolled-back, failed or powered-off droplet keeps billing at $6/month.** Only the owner
destroying it in the DigitalOcean control panel stops the charge, and that also deletes all data.

## Failure behaviour

* Every step stops at the first error. The error names the step and the host is left as it was
  at that point (running, reachable over SSH, billable).
* If the identity check fails, nothing has been touched.
* **certbot fails** (for example sslip.io DNS, the Let's Encrypt rate limit for the shared `sslip.io`
  domain, or port 80 blocked): nginx keeps the port-80 configuration only, with no HTTPS. The web
  service is already running on loopback. Re-run later.
* **Changed `requirements.txt`**: the script refuses to modify an existing venv. Stop
  `visioncast-web`, move `/srv/visioncast/venv` aside and re-run.
* **Admin or key already exists but the secret file is missing**: the bootstrap refuses. Revoke
  or replace them through the tunnel.

## Known limits

* Registrations are real personal data in this database. There are no backups,
  and the retention purge does not run automatically:
  `visioncast-manage purge_personal_data --older-than-days N --dry-run` previews it.
* Rate limits are per source IP. Vercel functions share egress IPs, so heavy demo traffic can
  return 429. Tune the limits in `nginx/visioncast.conf`.
* HSTS is set to 1 day (`DJANGO_HSTS_SECONDS=86400`) because the sslip.io name is tied to an IP
  that may be reassigned later.

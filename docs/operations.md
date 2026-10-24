# Operations guide

All commands assume the repository root, Docker Compose v2 and a filled-in `.env`.
`docker compose run --rm web manage <command>` runs `python manage.py <command>` in a
throwaway container from the same image (ports are not published by `run`).

> None of the procedures below have been rehearsed against a running deployment at
> the time of writing. Rehearse backup/restore and rollback on a staging copy before
> relying on them.

## 1. First run

```sh
cp .env.example .env && chmod 600 .env
# Generate secrets (paste into .env):
python3 -c "import secrets; print(secrets.token_urlsafe(50))"                               # DJANGO_SECRET_KEY
python3 -c "import secrets; print(secrets.token_urlsafe(32))"                               # POSTGRES_PASSWORD
python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"    # INTEGRATION_MASTER_KEY
# Set DJANGO_ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS, PUBLIC_BASE_URL, WEBINAR_ALLOWED_ORIGINS,
# THANK_YOU_REDIRECT_HOSTS, WEBHOOK_HOST_ALLOWLIST for your domains.

docker compose build
docker compose run --rm migrate          # creates schema and the Administrator/Editor/Analyst groups
docker compose run --rm web manage bootstrap_admin --username admin --email admin@example.com
docker compose run --rm web manage create_api_key --name "marketing-site proxy" --scope proxy
docker compose up -d
docker compose ps                        # web and worker should become "healthy"
```

* `bootstrap_admin` creates the first Administrator. If you used
  `BOOTSTRAP_ADMIN_PASSWORD` for a non-interactive run, remove it from `.env`
  immediately afterwards and change the password at first login.
* The raw API key is printed **once**. Put it in the proxy's secret store
  (`PROXY_API_KEY`, see `docs/browser-integration.md`); never in GTM or page source.
* Put a TLS-terminating reverse proxy in front of `127.0.0.1:8000`, forward
  `Host`, overwrite `X-Forwarded-Proto`, and only then set
  `DJANGO_TRUST_X_FORWARDED_PROTO=1`. Add rate limits there: registration, profile,
  reset and `/admin/` login have none in the application (the waiting room, `/mcp`,
  studio login and invitation acceptance have only coarse per-process limits,
  `webinars/ratelimit.py`).
* Outgoing email (waiting-room verification, team invitations) is off unless
  `EMAIL_DELIVERY=smtp` and the `SMTP_*` variables are set — see `docs/email.md`.
  These are not in `.env.example`. Marketers use `/studio/`; `/admin/` is the
  fallback Django admin.
* End-to-end rollout (site, forms, proxy, webhooks, launch checks):
  `docs/implementation-guide.md`.
* Run `docker compose run --rm web manage sync_roles` after upgrades that add
  models/permissions, to keep the role groups converged.

## 2. Key and secret management

| Secret | Generation | Rotation |
|---|---|---|
| `DJANGO_SECRET_KEY` | `secrets.token_urlsafe(50)` | Replace and restart; invalidates admin sessions and signed tokens. Browser capabilities are stored as hashes and are not affected. |
| `POSTGRES_PASSWORD` | `secrets.token_urlsafe(32)` | `ALTER ROLE … PASSWORD …` inside `db`, update `.env`, restart web/worker. Note: the postgres image only reads it on first initialisation of the volume. |
| `INTEGRATION_MASTER_KEY` | 32 random bytes, url-safe base64 (Fernet) | See below. |
| API keys | `create_api_key` | Create a new key, deploy it to the client, then revoke the old one in admin (sets `revoked_at`). |
| Destination signing secret | `set_destination_secret --destination <id> --generate` | Generates and stores a new encrypted secret; coordinate with the receiver (which may accept both during the switch). |

### Fernet master key rotation

`INTEGRATION_MASTER_KEY` encrypts; decryption tries it and then every key in the
comma-separated `INTEGRATION_MASTER_KEY_PREVIOUS`.

1. Generate a new key.
2. In `.env`: `INTEGRATION_MASTER_KEY=<new>`, `INTEGRATION_MASTER_KEY_PREVIOUS=<old>`.
3. `docker compose up -d web worker` (recreates containers with the new env).
4. Re-encrypt every stored secret under the new key:

   ```sh
   docker compose run --rm web manage shell -c "
   from webinars.models import Destination
   from webinars.crypto import rotate_secret
   for d in Destination.objects.all():
       fields = []
       for f in ('signing_secret_encrypted', 'credentials_encrypted'):
           if getattr(d, f):
               setattr(d, f, rotate_secret(getattr(d, f))); fields.append(f)
       if fields: d.save(update_fields=fields)
   print('ok')"
   ```

5. Verify deliveries succeed, then remove the old key from
   `INTEGRATION_MASTER_KEY_PREVIOUS` and recreate the containers again.
6. Backups taken before step 4 still need the **old** key to decrypt destination
   secrets; keep the old key in your secret store until those backups expire.

Never store the master key in the same place as database backups.

## 3. Backup

The database is the only stateful component (plus `.env`/secrets).

```sh
ts=$(date -u +%Y%m%dT%H%M%SZ)
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --no-owner' \
  > "webinars-$ts.dump"
# Encrypt before it leaves the host, e.g. with age:
age -r <recipient-public-key> -o "webinars-$ts.dump.age" "webinars-$ts.dump" && shred -u "webinars-$ts.dump"
```

* Copy the encrypted file off-host; restrict access; set an expiry consistent with
  your retention policy (backups contain PII that `purge_personal_data` cannot reach).
* Test restores regularly (section 4) and record the result.
* Back up `.env` separately via your secret manager, not alongside the dump.

## 4. Restore

Restore into a **fresh, empty** database, then apply migrations of the image you are
running.

```sh
docker compose stop web worker
age -d -i <identity-file> -o restore.dump webinars-<ts>.dump.age
docker compose exec -T db sh -c 'dropdb -U "$POSTGRES_USER" --if-exists "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'
docker compose exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --exit-on-error' < restore.dump
shred -u restore.dump
docker compose run --rm migrate     # brings an older dump up to the current schema
docker compose up -d web worker
```

* The `.env` used must contain the `INTEGRATION_MASTER_KEY` (or keep it in
  `INTEGRATION_MASTER_KEY_PREVIOUS`) that was current when the dump was taken.
* After restoring an old backup, re-run `purge_personal_data` for any erasure
  requests processed after the backup date (keep a list of erased person IDs outside
  the database for this purpose).
* Outbox rows restored as `pending`/`in_flight` will be (re)delivered; receivers
  dedupe on `Webinar-Delivery-Id`.

## 5. Upgrade and rollback

Tag every image you deploy so you can go back:

```sh
docker tag webinar-platform:local webinar-platform:$(date -u +%Y%m%d)-<git-sha>
```

Upgrade:

1. Take a backup (section 3). **Always back up before `migrate`.**
2. `docker compose build`
3. `docker compose run --rm migrate`
4. `docker compose up -d web worker`, then `docker compose run --rm web manage sync_roles`.
5. Check `docker compose ps` health and the admin outbox for failures.

Rollback:

* **No schema change in the release**: retag the previous image as
  `webinar-platform:local` and `docker compose up -d web worker`.
* **Schema changed**: Django migrations can be reversed with
  `docker compose run --rm web manage migrate webinars <previous_migration>` **using the
  new image** (the old image does not contain the newer migration files). Reversal is
  only safe when every migration in between is reversible and does not drop data; data
  migrations and column drops may be irreversible. When in doubt, restore the pre-upgrade
  backup (section 4) with the old image, accepting loss of registrations taken since.
* Run the worker and web from the same image version at all times.

## 6. Worker operations

* `worker` runs `manage.py run_worker`: claims outbox and reminder rows with
  `SELECT … FOR UPDATE SKIP LOCKED` and a lease (`OUTBOX_LEASE_SECONDS`, default 120).
* **Scaling**: multiple workers are safe — `docker compose up -d --scale worker=3`.
  Each claimed row is leased to one worker; a crashed worker's lease expires and the
  row is retried. Delivery is at-least-once; receivers must dedupe by delivery ID.
* **Health**: the worker touches `WORKER_HEARTBEAT_FILE` (default
  `/tmp/worker-heartbeat`, on the tmpfs) every loop; `docker/worker_healthcheck.py`
  marks the container unhealthy if it is older than 60 s
  (`WORKER_HEARTBEAT_MAX_AGE` overrides).
* **Retries**: exponential backoff with jitter capped at 1 h, up to the destination's
  `max_attempts` (default 8), then status `dead`.
* **Dead letters**: in admin → Outbox messages, filter `status = dead`, inspect
  `last_status_code` / `last_error` (no payload values are logged), fix the cause
  (receiver down, allowlist, secret mismatch), then use the **requeue** action.
  Messages for erased people have their payload purged and should be cancelled, not
  requeued.
* **Reminders**: late reminders beyond `REMINDER_LATE_GRACE_MINUTES` are marked
  `skipped_late` (not sent) — expected after worker downtime.

## 7. Retention and erasure

```sh
# Preview what would be removed:
docker compose run --rm web manage purge_personal_data --older-than-days 365 --dry-run
# Scheduled retention (host cron, e.g. daily at 03:15 UTC):
15 3 * * * cd /srv/webinar-platform && docker compose run --rm -T web manage purge_personal_data --older-than-days 365 >> /var/log/webinar-purge.log 2>&1
# Individual erasure request:
docker compose run --rm web manage purge_personal_data --person <person-uuid>
```

The command clears person PII, submitted profiles, consent evidence, attribution
touches, organisation IDs, capabilities and queued outbox payloads. It cannot recall
data already delivered to webhook receivers or stored in backups — handle those via
the receivers' own deletion processes and backup expiry. Choose `--older-than-days`
to match your published privacy notice.

## 8. Healthchecks

| Service | Check |
|---|---|
| `db` | `pg_isready` |
| `web` | `docker/web_healthcheck.py`: `GET http://127.0.0.1:8000/healthz` with `Host` = first `DJANGO_ALLOWED_HOSTS` entry; 200 required, redirects not followed |
| `worker` | `docker/worker_healthcheck.py`: heartbeat file age < 60 s |
| `migrate` | one-shot; must exit 0 before web/worker start |

External monitoring should probe `https://<host>/healthz` through the reverse proxy.

## 9. Logging policy

* Logs go to stdout/stderr; collect them with your container log driver and set a
  retention period.
* gunicorn access logs record time, method, **path without query string**, protocol,
  status, size and duration — no client IP, referrer, user agent, cookies or headers.
  Your reverse proxy may log more; configure it equally conservatively.
* Application logs and the `AuditLog` table contain IDs and action names only — never
  email, phone, names, tokens, API keys, secrets or webhook payloads. Report any log
  line containing PII as a bug.
* `DJANGO_DEBUG` must be `0` in production (debug pages can expose settings and
  request data).

## 10. Hardening checklist

- [ ] `.env` is `chmod 600`, not in VCS, secrets generated freshly per environment
- [ ] Images and base images pinned by digest; requirements hash-locked
- [ ] TLS reverse proxy with rate limiting in front of web; admin restricted (VPN/SSO/MFA)
- [ ] `WEBHOOK_HOST_ALLOWLIST` contains only receivers you control or contract with
- [ ] Backups encrypted, off-host, restore tested, master key stored separately
- [ ] Retention cron installed and monitored

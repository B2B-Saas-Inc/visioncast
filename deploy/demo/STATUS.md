# Live demo — 2026-10-24T21:58:20Z (backend updated 2026-10-27T19:02Z)

**2026-10-27T19:02Z backend update (owner-approved):** source from tag `v0.2.0-pre` (`1462cb7`) installed with `update-test-backend.sh --apply` (database and previous source kept in `/srv/visioncast/backups/20261027T190217Z`); migration `0011_attendance_import` applied, roles synced, static collected, `check` clean, service healthy. Public `/healthz` reports `{"status": "ok", "version": "0.2.0-pre"}`, OpenAPI serves 61 paths (5 attendance-import routes), 4 upcoming demo events unchanged; admin, studio, MCP and management stay 404 publicly. Still no worker, email, destinations or outbound access, so provider pulls and native deliveries cannot run on this host. The Vercel frontend was **not** redeployed: its catalogue still shows Salesforce, Pipedrive, ActiveCampaign and Mailchimp as roadmap until `site/` is deployed again.

- Public site: https://visioncast-ten.vercel.app
- Immutable deployment: https://visioncast-oxs8a0qen-b2b-saas.vercel.app
- Requested `visioncast.vercel.app` was unavailable (Vercel alias already in use); never link to that unrelated deployment.
- Backend: https://64-225-16-137.sslip.io/healthz
- DigitalOcean droplet `visioncast-demo`, ID `612305850`, NYC3, Ubuntu 24.04, 1 vCPU / 1 GB / 25 GB, $6/month excluding taxes and overages. Persistent, no automatic expiry. No paid add-ons.
- Native PostgreSQL 16, one Gunicorn worker/two threads, nginx, Let's Encrypt renewal; 2 GB swap. Application runs unprivileged with outbound IP access blocked. No delivery worker, destinations or outbound email. Admin, studio and management endpoints are not publicly reachable.
- Six fictional enriched events, with real demo registration storage. Use fictional test details only; no actual webinars/recordings or email confirmations.

## Owner access

```sh
ssh -i ~/.ssh/clark_digitalocean -N -L 8081:127.0.0.1:8081 root@64.225.16.137
```

Open http://localhost:8081/studio/ in Chrome/Firefox. User: `demo-owner`. Password is only in `/root/visioncast-secrets/admin-password` on the droplet; retrieve privately in your terminal, never paste into chat. Proxy key is in `/root/visioncast-secrets/proxy-api-key` and Vercel's production-only encrypted environment. Never print the environment file.

## Evidence

- Backend: 860 PostgreSQL tests passed, Ruff, Django checks and migration drift clean. One pre-existing Django transitional-setting warning.
- Frontend: 33 tests, TypeScript and production build pass; npm audit zero known vulnerabilities. Next 15.5.28, React 19.1.10, Node24; patched transitive PostCSS/sharp overrides are pinned in package.json and package-lock.json.
- Actual deployed Chromium flow passes: landing, real API catalogue, registration, ICS, recognition after reload, mobile overflow, recording disclosure and no JS errors. Evidence: `artifacts/live-browser.log`, `live-home.png`, `live-rsvp.png`, `live-events-mobile.png`.
- Real registration test records use synthetic `@example.invalid` addresses and remain in the demo database.
- HTTPS health/catalogue return200; public admin/studio/MCP/management return404. Secrets not included in artifacts or source.
- Claude CLI workers verified `claude-opus-5-5`, no permission denials.

## Operations and limitations

This is a public pre-release demonstration, not a production certification. Real vendor/SMTP integration and public waiting-room verification are intentionally disabled. No HA or paid backups. Owner UI tunnel browser flow has not been tested end-to-end. Existing core regression suite is separate from this deployment smoke test.

Latest demo files are local/uncommitted; Vercel was deployed with CLI, not Git auto-deploy. Preserve the workspace before changing deployments. Stop `visioncast-web` to stop application traffic while preserving data; reverting Vercel uses the prior immutable deployment. Stopping services does not stop droplet billing. Do not destroy the database/droplet without owner approval.

On macOS source transfers, use `COPYFILE_DISABLE=1 tar --no-xattrs` to avoid AppleDouble `._*` files. First transfer accidentally included them; they were moved intact to `/srv/visioncast/macos-metadata`, not executed. nginx's base configuration is installed as `00-visioncast.conf` so log-format declarations precede TLS configuration.

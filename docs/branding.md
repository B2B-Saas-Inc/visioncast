# Run it as your own

Everything here runs on your servers, under your name. This guide takes a fresh server to your own studio and live rooms.

## One server, one command

You need a server of its own with Docker and Compose, a public IP, and ports 80 and 443 free. Traefik takes both, so this isn't the same server as the nginx install in [deploy/production](../deploy/production/README.md): pick one or the other. Any provider will do, if it gives you [a real server with a public address](../live/README.md#choosing-a-provider). Plan on 16 vCPUs and 32 GB to record, broadcast and caption at once; [live/README.md](../live/README.md#capacity) has sizes by attendance, and how to [run a small server between events](../live/README.md#between-sessions).

Do these two things first, because certificates are asked for as soon as it starts:

1. Point four names at it, DNS only. Cloudflare's proxy can't carry the media ports or TURN.
2. Open the firewall: 80/tcp, 443/tcp, 7881/tcp, 3478/udp, 30000-40000/udp and 50000-60000/udp. Nothing else should be open.

```bash
git clone https://github.com/B2B-Saas-Inc/visioncast.git && cd visioncast
deploy/quickstart/install.sh \
  --studio events.example.com --live live.example.com --rtc rtc.example.com --turn turn.example.com \
  --email you@example.com --name "Acme Events" --accent "#0b5a8a" --logo /path/to/logo.png
```

It makes new secrets and keeps them in `.env` files, never on the screen. It builds and starts the studio and the live server, makes your first accounts, connects the two, and sets your brand on both, the first time. Certificates are issued automatically. Run it again any time to bring things up to date: existing `.env` files are kept and your brand isn't touched, and if you pass a different hostname it stops and says so. `--no-captions` skips Whisper. Optional parts (RTMP input and translated captions) are switched on with `COMPOSE_PROFILES` in `deploy/live/.env`.

Then:

1. Sign in to the studio as `owner`, with the password in `deploy/quickstart/.first-admin`, and to `https://live.example.com/admin` with `LIVE_ADMIN_EMAIL` and `LIVE_ADMIN_PASSWORD` from `deploy/live/.env`. Change both passwords.
2. Set up email. Until you do, nothing is sent: see `EMAIL_DELIVERY` in [operations.md](operations.md).
3. For recording, fill in `RECORDING_S3_*` in `deploy/live/.env`, then run the installer again.
4. Put more in front of the sign-in pages. The installer limits how fast anyone can guess a password, but neither page has two-factor sign-in. Put a VPN or an SSO proxy in front of `/studio/` and `/admin` if you can.
5. Set up backups, below.
6. From somewhere else, run `deploy/live/verify-public.sh live.example.com rtc.example.com turn.example.com`.

To try it on your own machine, add `--local`. There are no real certificates, so browsers warn, and the studio can't reach the live server over HTTPS, so a session's room can't be created from it. It's for seeing the pages.

The server has to be able to reach its own public address, because the studio talks to the live server by name.

## Your brand

| Where | What it sets | Who |
|---|---|---|
| Studio, Workspace, **Brand** | Name, colour and logo on the studio, developer pages and waiting room. Saving passes the colour and logo on to every connected live server | Administrators |
| `/admin`, **Brand**, on the live server | Logo, colour, stage background, light or dark attendee page, font, your own font file | People with an account there |
| The room's **Look** tab | One session's own colour, background and font, plus frames, overlays and graphics | Hosts and producers |
| `BRAND_NAME`, `BRAND_ACCENT` in `.env` | The studio's name and colour until someone saves the Brand page | Whoever installs it |

Your public website is your own: it uses the API and doesn't carry any VisionCast name unless you add one ([integration.md](integration.md)).

**Rules.** The colour has to be dark enough for white text on buttons, or it's refused. Logos are PNGs of at most 2 MB. The studio decodes them and rebuilds them from their pixels. The live server checks their first bytes and serves them as images, under a strict policy. Fonts are checked by their first bytes. SVG isn't accepted anywhere, because it can run script.

**What still says VisionCast.** With no name set, the studio does. So do code, documentation, image names and the `/developers` address. Nothing an attendee sees says it once you've set your logo: until then, the room's header and browser tab show the default mark.

## Backups and upgrades

Keep these somewhere else, and the two `.env` files apart from the rest, because they hold the keys:

| What | Where | How |
|---|---|---|
| Studio database | `pgdata` volume | `deploy/backup.sh`, see [operations.md](operations.md) |
| Live server: accounts, crew, chat, polls, stream keys, uploads | `live-data` volume | Stop it, or copy the SQLite file with `sqlite3 .backup`, then copy the volume |
| Secrets | `.env`, `deploy/live/.env`, `deploy/quickstart/.first-admin` | Copy them once, and again whenever they change |
| Recordings | Your bucket | Your bucket's own versioning or replication |

To restore a live server, bring back `live-data` and `deploy/live/.env` together: `LIVE_TICKET_KEY` signs every link and seals stream keys, so one without the other doesn't work.

To upgrade: take a backup, `git pull`, then run the installer again, which applies the studio's migrations.

Changing a secret: `LIVE_SERVICE_SECRET` needs setting in `deploy/live/.env` and again on the hosting account in the studio. `LIVE_TICKET_KEY` invalidates every link and stream key, so change it between sessions. `LIVEKIT_API_SECRET` only needs a restart.

## Images

A tagged release publishes `ghcr.io/<owner>/<repo>`, and `-live`, `-captions` and `-slides` next to it. Without one, everything builds from this repository, which is the default. A private package needs `docker login ghcr.io` with a token that has `read:packages` first. To use a release, pass `--version 1.2.3`, and `--image-owner you` for your own fork's images.

## Your first event

1. In the studio, create an event and a session.
2. On the session's Hosting page, choose the Live account, then preview and confirm **Create**.
3. Under Crew, add each host, producer and guest, and send each their own link.
4. Turn on the waiting room and share `/w/<code>/`. It gives each registrant their personal link.
5. On the day, meet in the green room, put speakers on stage, and press **Go live**.

The [implementation guide](implementation-guide.md#92-your-own-live-video-livekit-add-on-supported-preview) has the rest.

# Your own video: LiveKit and visioncast-live

An optional add-on. VisionCast doesn't need it: Zoom, Wistia and the other hosting providers
keep working as they are. Use it when you want live sessions and recordings on servers you
control, with no per-attendee or per-seat bill.

| Piece | What it does | Language |
|---|---|---|
| [LiveKit server](https://github.com/livekit/livekit) | Carries the audio and video | Go, unmodified upstream image |
| [LiveKit Egress](https://github.com/livekit/egress) | Records the room to an MP4 in your storage | Go, unmodified upstream image |
| `visioncast-live` (this folder) | Rooms, personal links, the room page, `/admin`, attendance from webhooks, retention | Rust |
| `visioncast-captions` (`captions/`) | Whisper, in its own container, for live captions | Rust and whisper.cpp |
| `visioncast-slides` (`slides/`) | Turns PDF and PowerPoint into page images, with no network at all | Python, LibreOffice, Poppler |
| [LiveKit Ingress](https://github.com/livekit/ingress) | Takes RTMP from an encoder. Optional | Go, unmodified upstream image |
| [LibreTranslate](https://github.com/LibreTranslate/LibreTranslate) | Translates captions, on your server. Optional | Python, unmodified upstream image |
| VisionCast `livekit` provider | Creates the room from the studio and hands out links at the waiting room | Python |

## Addresses

Pick three hostnames, like `live.example.com` for the service, `rtc.example.com` for media and `turn.example.com` for people on restricted networks.

| Address | What's there |
|---|---|
| `/{slug}` | The session, for attendees |
| `/{slug}/lobby` | Attendees before it goes live: a countdown, chat, Q&A and polls |
| `/{slug}/green-room` | Hosts, producers and guests |
| `/login` | Sign in to the settings, as yourself. There's no sign-up: an owner adds people |
| `/admin` | Sessions, crew, brand, stream keys, recordings and captions, people, activity |
| `/join/{signed link}` | What VisionCast hands out. It sends you to the right address and is gone from the address bar straight away |

The slug comes from the event in VisionCast. An event with several sessions adds the date and time to tell them apart. An address can't be used twice, or be one of the server's own, like `admin`.

The address alone gets nobody in. A visitor without a link sees the session's title and a way to the waiting room. The signed link travels in the URL fragment, which browsers don't send to servers or in referrers, and that browser tab remembers it, so a reload works. Attendees still come through VisionCast's waiting room, at `/w/{code}/` on your VisionCast host, then land on `/{slug}/lobby`.

## The studio

Everyone uses the same page. What they can do is decided by the service and enforced by LiveKit, not by the page.

| | Host | Producer | Guest | Attendee |
|---|---|---|---|---|
| Goes on stage | Yes | **Never** | When brought on | If invited and they accept |
| Heard by the audience | When on stage | **Never** | When on stage | When on stage |
| Green room | Yes | Yes | Yes | Only after accepting an invitation |
| Go live, end the session | Yes | Yes, unless a host turns it off | No | No |
| Move people on and off stage | Yes | Yes | No | No |
| Layouts, frames, overlays, video, calls to action | Yes | Yes | No | No |
| Screen share | Yes | Yes, as "Presentation" | When on stage | When on stage |
| Turn slides | Yes | Yes | When on stage | No |
| Breakout rooms | Open, close, visit | Open, close, visit | Visit | Their own, or choose |
| Polls | Run and vote | Run and vote | Vote | Vote |
| Chat | Moderate | Moderate | Post | Post |
| Crew-only chat | Yes | Yes | Yes | **Never sees it**, unless invited on stage |
| Q&A | Moderate, answer | Moderate, answer | Answer | Ask, upvote |
| Files | Upload, share | Upload, share | Download shared | Download shared |
| Recording | Yes | Yes | No | Sees that it's on |

**How the audience is kept out.** There are two LiveKit rooms. The stage (the session's ID) is what the audience watches and the recorder records. The green room (`<id>--bs`) is for the crew, and only the crew ever gets a credential for it. So:

* An attendee gets no stage credential at all until someone goes live. Before that they see a countdown, and can chat and ask questions.
* An attendee's credential can't publish, so LiveKit refuses their camera and microphone whatever their browser does.
* A producer's stage credential can't publish either, and they're hidden from the participant list. Their screen goes through a separate connection that can only publish a screen. Their voice exists only in the green room.
* Putting someone on stage is a server action that opens publishing for them. Taking them off closes it, and LiveKit removes their tracks.
* Chat, questions and polls go through this service, not LiveKit's data channel, so they can be moderated, are there for people who join late, and can't be spoofed.

**Taking access away.** A LiveKit credential lasts 15 minutes, and LiveKit keeps a live connection's fresh. Removing someone or replacing their link closes their connection and their event stream at once. If they try to come back with a credential they kept, the service sees them arrive and puts them out, and anyone publishing on the stage who isn't on it has publishing closed. A replaced link's old credential can still be used to watch for up to 15 minutes, because it's the same person's identity. The Share credential can publish any kind of track its browser labels as a screen, so a host or producer who deliberately modifies their browser could send their voice that way. They run the show, so that's a matter of trust.

**On stage, you hear the stage and the producers.** That's how a producer talks to a speaker on air. The audience can't hear it, except through a speaker's own microphone if they're not on headphones.

**One picture.** The audience, the crew and the recorder all draw the stage with the same code (`assets/stage.js`), so the recording has the layout, frame and overlays the audience saw. The recorder opens `/program/<ticket>`, which sees only what the audience sees.

**Privacy.** VisionCast sends the service the session title, its times and a registration ID. Names and email addresses are not sent. A person's display name is whatever they type. Chat, questions, votes and files are kept with the room and removed after the retention period.

## Crew and people

**Everyone has their own link.** A host, producer or guest is one person with one link, made under Crew on the session's page at `/admin` or its Hosting page in the studio. Their name and title come ready. Replacing someone's link makes the old one stop working and puts them out of the room. Removing them does the same. Opening your own link somewhere else closes the first window. A host who opens the room from the studio gets a link that's theirs alone.

**Everyone has their own account.** `/login` takes an email and a password. The first owner comes from `LIVE_ADMIN_EMAIL` and `LIVE_ADMIN_PASSWORD`, once. Owners add people under People, who set their own password from a link that works once, within 3 days. People lists everyone in all five roles: owners and admins, who sign in, and hosts, producers and guests with their sessions, who don't. Adding a host, producer or guest there asks which session, and gives you their link. A VisionCast Administrator pressing Open live settings is signed in as themselves, with an account made for them the first time. Owners can reset passwords and remove people; there's always at least one owner. Changing a password signs you out everywhere else. Activity lists who did what. Locked out: `docker compose exec live visioncast-live reset-admin you@example.com` prints a link.

## In the room

* **Slides.** Upload a PDF or PowerPoint under Look. It becomes page images, shown as a window on the stage, so they're in recordings and broadcasts. Attendees only ever get the page on stage. Speaker notes show only to the crew. Animations and transitions aren't kept.
* **Devices.** Change camera, microphone or speaker mid-session. If one is unplugged, the room moves to what's left and says so.
* **Backgrounds.** Blur or an image, drawn in the speaker's own browser. Nothing is sent to a CDN. Browsers that can't do it say so and show the camera as it is.
* **Layouts.** Name and save a layout, and bring it back with one press.
* **Banner.** An image attendees see while they wait.
* **Chat.** Attendees react to messages, one reaction each. A host or producer can put a message on the picture as a lower third.
* **Private questions.** Turn on Keep questions private and each question is seen only by the crew and whoever asked, answer included. A host can show one to everyone, or make one private, at any time.
* **Polls.** A poll can pop up over the stage, open by itself some minutes after going live, and close itself some seconds after opening.
* **Survey.** Set one in Polls. Attendees see it when the session ends, and answer once.
* **Breakouts and the networking hub.** Each is a separate LiveKit room. People choose a room, or the audience is split evenly, or tables stay open as a hub, even after the session ends, until the room's window closes four hours after its scheduled end. Rooms can have a limit. Assignments aren't sent to anyone but the person. Nothing in them is recorded, and the crew can visit any of them. Someone in a breakout can't be put on stage until they come back.
* **Simulive.** Schedule a video under Look, for a time still to come. The session goes live by itself at that time, plays the video for everyone, and can end itself when it's over. Chat, questions and polls work as usual. Attendees aren't told it's a recording. This keeps working across a restart of the service.
* **Encoders.** With `LIVE_INGRESS_URL` set, create an input under Broadcast and put its server and key in OBS, vMix or a hardware encoder. It's on stage for exactly as long as its stream runs, so start it when you want it seen. RTMP only, not RTMPS, so its stream key travels unencrypted. Inputs are deleted when the session ends.
* **Each speaker's own copy.** Turn this on in Settings and, while recording, each person on stage also records their own camera and microphone in their browser, uploaded as it goes. The files are listed with the recordings, and stay on this server: they don't go to the bucket.

**What people did goes back to VisionCast.** From about 10 minutes after a session's scheduled end, VisionCast fetches each attendee's chat, questions, poll votes and survey answers every hour, for a day after it ends, stores them on their registration and sends `engagement.recorded` for whatever changed. An anonymous question isn't tied to anyone, even by the crew, so it isn't included. Crew aren't included. Each person's lists are cut to the first 20 entries of each kind.

## Live captions

Each speaker's microphone is transcribed by Whisper while the session is live, on your own server. No audio leaves it.

* **Viewers choose.** Everyone gets a Captions button, off to start with, and their choice is remembered in that browser. A host or producer can turn captions off for the session in Settings, or put them on the picture so they're in recordings and broadcasts.
* **Per speaker, for certain.** The recorder sends each microphone separately, so who said what is known, not guessed. Captions name the speaker.
* **Saved with the recording.** When a recording finishes, these go in the bucket next to its MP4, timed from the recording's own start:

  | File | What's in it |
  |---|---|
  | `{recording}.srt` | Everyone, with each line naming its speaker |
  | `{recording}.txt` | A transcript, grouped by speaker, with timestamps |
  | `{recording}.{speaker}.srt` | One speaker |
  | `{recording}.{speaker}.txt` | One speaker |

  They're listed in `/admin`, the room's Files tab and the VisionCast studio. Hosts and producers can also download the session so far, timed from going live, with or without a recording.
* **What's never transcribed:** the green room, rehearsal before going live, screen-share sound, and video played on stage. An attendee invited on stage is told their words may be transcribed before they accept.
* **Speed.** Words appear about 2 to 3 seconds after a pause: 0.6 seconds to notice it, about 2 seconds for Whisper on 2 CPU threads. Whisper takes about the same time whatever the length, up to its 30-second window. Two transcriptions run at once by default. If it falls behind, it skips a sentence rather than holding up the room, and the crew's log says so.
* **Other languages.** The default model, `ggml-base.en`, is English only. For others, download `ggml-base.bin` to `deploy/live/models`, and set `WHISPER_MODEL=ggml-base.bin` and `WHISPER_LANGUAGE=auto` in `.env`. A larger model is more accurate and slower.

A person's name and what they said are personal data. Captions are kept with the room and removed after `LIVE_RETENTION_DAYS`; the files in your bucket are yours to manage.

## On-screen graphics

In the room's Look tab, hosts and producers can put GIFs, PNGs, WebP images and muted MP4 or WebM videos anywhere on the picture, with live words on top. That's how you make an animated chyron or ticker: a looping band, with text you can change during the session.

* Place one with a preset (lower third, ticker band, corner, side panel, full stage), or drag and resize it with Move and resize.
* Words can stay put or scroll across.
* Use a WebM with transparency for shapes that aren't rectangles. Chrome and Firefox show transparent WebM; Safari shows it with a solid background, and hasn't been tested. For the same look everywhere, use a GIF.
* Graphics are in recordings and broadcasts, like everything on the picture. Up to 12 per session.

## Brand

Set at `/admin`: logo, brand colour, stage background, light or dark attendee page, and font. Saving updates open rooms straight away. Everything on the picture uses it, including overlays and polls, so recordings and broadcasts carry it too.

A host or producer can override a single session from the room's Look tab, where frames, backgrounds and full-stage images also live. The built-in fonts are ones already on people's devices, so nothing loads from elsewhere. You can also upload your own font at `/admin`, which is served from this server.

The studio's own Brand page ([docs/branding.md](../docs/branding.md)) sets the name, colour and logo across the studio, the waiting room and every connected live server.

## Broadcasting to other platforms

Sends the stage to YouTube, Meta, X and LinkedIn over RTMP or RTMPS: the same picture as the audience's, with frames and layout.

* **Where it's set.** At `/admin`, on the session's page. Keys you reuse can be saved under Saved stream keys. LinkedIn creates a new server and key per scheduled stream, available shortly before the start, so it's per session only.
* **When it runs.** Turned-on platforms start when someone goes live, in one encode. From the room's Broadcast tab, hosts and producers see each platform's status and can start or stop it. Guests and attendees see none of this.
* **When one fails.** The others and your audience carry on. The crew sees which one failed.
* **Going live on the platform.** Sending the stream usually isn't enough. Meta, X and LinkedIn each need you to press Go live on their side, and YouTube does unless auto-start is on. Do that once the stream shows there.
* **Stream keys.** Kept only on this server, encrypted, and never sent to any browser or written to a log.
* **Addresses.** Only `rtmp://` and `rtmps://`, and never a private address: the recorder runs inside your server's network. `rtmps://` certificates are checked.

Each recording and each broadcast is its own recorder process, and Egress reserves 4 CPUs' worth of capacity for each by default. To record and broadcast at once, plan on at least 16 vCPUs: see [Capacity](#capacity). Adding more platforms to a broadcast doesn't add an encode, but does add upload bandwidth. Captions take a little of Egress's capacity per speaker, and about 2 CPU threads per transcription running at once.

## Capacity

These are estimates for planning, not measurements on a server. They scale LiveKit's published benchmark (1 publisher to 3,000 viewers at 720p used 92% of 16 vCPUs at 531 MB/s, so about 3.5 vCPUs per Gbps). The table assumes every viewer takes a 1080p picture at 3 Mbps and 128 kbps audio, and that you record, broadcast and caption at once. Run `live/tests/load/run.sh` on your own server before relying on it.

| Attendees | Outbound | Transfer per hour | Server |
|---|---|---|---|
| 100 | 0.3 Gbps | 140 GB | 16 vCPUs, 32 GB |
| 500 | 1.6 Gbps | 700 GB | 32 vCPUs, 64 GB |
| 1,000 | 3.1 Gbps | 1.4 TB | 32 vCPUs, 64 GB |
| 2,000 | 6.3 Gbps | 2.8 TB | 48 vCPUs, 96 GB |
| Over 2,500 | Too close to a 10 Gbps limit | | Send the overflow to YouTube or a CDN through the broadcast tab |

* **Those are the defaults.** Speakers send 1080p, in layers of 3 Mbps, 1.7 Mbps and 450 kbps, with 128 kbps stereo audio, so plan on about 5.3 Mbps of upload each. A viewer's picture follows the size of their window, so most take less than the table assumes. Breakout rooms stay at 720p, because everyone in them publishes. Stereo only means something with a stereo microphone, since browsers' echo cancellation usually makes speech mono.
* **A room has to fit on one LiveKit server.** Adding servers gives you more rooms, not a bigger room. The deploy files put everything on one server, so moving Egress or Whisper to another means changing them.
* **Choose dedicated CPUs and enough network.** Many plans are capped: at 2 Gbps, for example, you'd reach about 600 viewers at 1080p. `deploy/live/size-check.sh` compares your server with a session's needs.
* **Each recording and broadcast reserves 4 CPUs,** and captions use up to 4 by default. The test stack lowers the reservation to 1, so the default hasn't been run under load.
* **Budget 2 GB per hour for the main recording.** It reaches the bucket when it stops, so a crash during a long session can lose it.
* **Speakers' own recordings go to the server's disk as they're made,** at 10 Mbps, which is 4.5 GB per speaker-hour. A file holds 8 GB, so a new one starts about every 1 hour 45 minutes, and a room holds 100 GB, about 22 speaker-hours. With this on, a speaker needs about 15 Mbps of steady upload, and encoding twice is hard on older laptops. If their connection can't keep up, their own copy loses a part, and the main recording isn't affected.
* **Size is checked for you, up to a point.** The room warns below 8 CPUs. It doesn't know how many people are coming, so run `size-check.sh` for that.
* **Set the session's end time honestly.** Nobody new can enter more than 4 hours after the scheduled end, and an empty room closes after 30 minutes.
* **The longest automated session so far is a few minutes.** Rehearse the length you plan to run.

### Between sessions

The live server doesn't need its full size all the time. There are three ways to run it. **Unless you have a reason not to, resize it.**

| | Resize up for events | Destroy and recreate | Always large |
|---|---|---|---|
| Between sessions | A small server stays on | Nothing runs, a snapshot is kept | The large server stays on |
| `/admin`, crew links, engagement, erasure, retention | Always work | Only while it's up | Always work |
| Where data lives | One disk, no copies | A snapshot you have to look after | One disk, no copies |
| To get ready | A few minutes of downtime | Create, assign the address, check | Nothing |
| Biggest risk | Forgetting to size up, or no large plan being free | A forgotten engagement window, or a stale snapshot | Cost |

**Resizing:**

1. Choose the small size for its disk, because most providers won't shrink a disk. Keep the disk when you size up, or you can't come back down. Docker images, the model and the system take about 12 GB. On top of that, leave room for your biggest event: run `deploy/live/size-check.sh`.
2. Use a static or reserved address if your provider doesn't keep the address through a resize: see the table below. Names have to keep pointing at the server.
3. Make sure Docker starts at boot. On most Linux servers: `systemctl enable docker`. Services are set to come back by themselves.
4. A day or so before the session, size up, in case the large plan isn't available in your region. Then, on the server, run `deploy/live/size-check.sh <attendees> [hours] [speakers keeping their own recording]`. From another machine, run `deploy/live/verify-public.sh`.
5. Afterwards, check that recordings reached the bucket, and size down. Engagement, `/admin` and speakers' own files keep working at the small size.

The stack is light when idle: about 250 MB of memory for the live services, and almost no CPU. A small server holds crew setup, uploads and a rehearsal, but it can't record. A recorder reserves 4 CPUs, and with fewer it refuses. So below 8 CPUs, hosts and producers see a warning in the room (`LIVE_MIN_CPUS` changes that number, and 0 turns it off). If they try to record anyway, they're told that nothing is being recorded, after about 20 seconds.

If VisionCast is on the same server, from the one-step installer, registration is down during each resize.

**What each provider allows,** from their documentation in November 2026. Check yours, and try a real resize before relying on it:

| Provider | Size down later | Address | Notes |
|---|---|---|---|
| DigitalOcean | Yes, if you keep the disk | Not stated, so use a reserved IP | About a minute of downtime per GB used, usually less. Basic can go to CPU-Optimized. Only Premium CPUs go past 2 Gbps |
| Hetzner | Yes, if you keep the disk | Not stated, so use a primary IP you keep | Shared to dedicated is allowed, on the same architecture |
| Akamai (Linode) | Yes, after shrinking the disks yourself | Stays the same | Shared to dedicated is allowed |
| AWS | Yes, between compatible types | Changes unless you use an Elastic IP | Must be stopped first. Same architecture. Outbound data is about nine times DigitalOcean's overage rate |
| Google Cloud | Yes, within a series | May change, so use a static IP | Must be stopped first. Moving to a third-generation series or later means a new server |
| Vultr | **No** | Not stated | Downgrades are unsupported, so use one of the other two ways |

**Destroying and recreating,** where resizing doesn't suit. Many providers, DigitalOcean among them, keep charging for a stopped server, so destroy it unless yours says otherwise:

1. Keep it on for a day after the session, because VisionCast collects engagement from about 10 minutes after the scheduled end, then hourly for a day. You can pull it again later from the session page, but survey answers given while it's off are lost.
2. Check recordings reached the bucket, and download speakers' own files or keep them in the snapshot.
3. Stop it, take a snapshot, check the snapshot, destroy the server. A snapshot holds stream keys and personal data, so treat it as a backup.
4. Next time, create a server from the snapshot, assign the same address, and run the checks above.

While it's off: registration keeps working, because links are asked for again at the waiting room's door, but you can't change rooms, crew, settings or brand. Retention purges don't run. An erasure can't reach it, so `purge_personal_data` reports `live_servers_not_reached`: turn the server on and run it again. This doesn't work with the one-step installer, because VisionCast has to stay on.

### Choosing a provider

Nothing here depends on one provider. You need:

* **A real virtual or dedicated server with a public address,** where you can run Docker with host networking and open thousands of UDP ports. Platforms that only run containers behind their own proxy, like Vercel, Railway, Render or Heroku, can't carry the media. Neither can container-based VPS plans that don't allow `SYS_ADMIN`, which the recorder needs.
* **A one-to-one public address.** Servers on AWS and Google Cloud see only a private address, and `use_external_ip`, on by default, finds the public one. Set `LIVEKIT_NODE_IP` if it can't. This hasn't been tried on a real AWS or Google Cloud server.
* **Enough outbound network for the table above.** A plan's advertised speed and what you pay per GB vary a lot, and this is often the real limit and the real cost.
* **amd64 or arm64.** A resize can't change architecture.
* **Your firewall rules in both places,** where the provider has a firewall as well as the server's own.
* **Any S3-compatible bucket:** `RECORDING_S3_ENDPOINT` for the address, and `RECORDING_S3_FORCE_PATH_STYLE=1` if it needs that. Only a strict local S3-compatible server has been tested so far.
* **Certificates and names from anywhere,** as long as the names are DNS only.

## Try it locally

```bash
live/preview/run.sh    # real LiveKit, recorder and storage, and one link per person
live/preview/stop.sh
```

Use headphones if you open several roles on one machine, or you'll hear feedback. It also starts the captions container, `/admin`, the VisionCast studio and a test RTMP server standing in for the platforms: use server `rtmp://rtmp:1935/live` with any key, and watch what arrived at `http://127.0.0.1:18888/live/<key>/`. Your real stream keys work too. `links.txt` in that folder has the passwords. Sample files to upload are in `live/preview/.preview/samples`.

## Install

For the studio and live rooms together on one server, with your brand, use the one-step installer: [deploy/quickstart/install.sh](../deploy/quickstart/install.sh), and see [docs/branding.md](../docs/branding.md).

For live rooms alone, next to a VisionCast you already run: you need a server of its own, with a public IP and Docker. Plan on at least 16 vCPUs and 32 GB to record, broadcast and caption at once, or 8 vCPUs and 16 GB for one of recording or broadcasting with a small audience. [Capacity](#capacity) has sizes by attendance. Each runs a headless Chrome, and Egress reserves 4 CPUs' worth of capacity for it by default. Bandwidth is the real cost of a large session.

```bash
git clone https://github.com/B2B-Saas-Inc/visioncast.git && cd visioncast
deploy/live/setup.sh live.example.com rtc.example.com turn.example.com you@example.com
```

The script writes new secrets to `deploy/live/.env`, without printing them, downloads Whisper's model (about 150 MB, checked against its published hash), and lists the remaining steps:

1. Point all three names at the server, DNS only. Cloudflare's proxy can't carry the media ports or TURN.
2. Open 80/tcp, 443/tcp, 7881/tcp, 3478/udp, 30000-40000/udp and 50000-60000/udp. Add 1935/tcp if you use RTMP input.
3. For recording, fill in the `RECORDING_S3_*` lines in `.env`. Without a bucket, rooms work and recording stays off.
4. Run `docker compose up -d --build` in `deploy/live`.
5. On your VisionCast server, add the service host to `WEBHOOK_HOST_ALLOWLIST` and restart.
6. In the studio, connect a LiveKit account with the service host and `LIVE_SERVICE_SECRET`.
7. Sign in at `/admin` with `LIVE_ADMIN_EMAIL` and `LIVE_ADMIN_PASSWORD` from `.env`, change that password, and add everyone else under People.
8. From somewhere else, run `deploy/live/verify-public.sh live.example.com rtc.example.com turn.example.com`.

**Networks that only allow port 443.** Traefik takes all of port 443 and sends each hostname where it belongs. `turn.example.com` goes to LiveKit's TURN server with the TLS taken off, so media can travel as ordinary-looking HTTPS traffic. To check it for real, run `live/tests/turn/check.cjs` against your server: it forces two browsers to use nothing else. It leaves a room called "Connection test from turn/check.cjs" in `/admin`, which ends 10 minutes later and goes with the normal retention.

**Optional parts.** Set `COMPOSE_PROFILES=encoder,translate` in `.env`, with the matching lines the script left commented out. Translation needs about 1 GB of disk and memory for each language.

## Updating and backups

**Updating.** On the server, from the checkout: `deploy/live/update.sh v0.4.6-pre` (any release tag). It refuses to run over local code changes, takes a backup first when backups are set up, checks out the tag, rebuilds the images, restarts what changed and waits for `/healthz`. `.env`, the data volumes and the captions model are kept. It ends by printing the command that goes back to the previous tag. Rooms in progress drop for the restart, so update between sessions.

**Backups.** `deploy/live/backup.sh` makes one encrypted file of the database (rooms, crew, accounts, settings, engagement), uploads (slides, graphics, the brand) and speakers' local recordings, using a consistent copy of the database while the service runs. Recordings already sent to a bucket aren't included. Set it up once, as root:

```bash
apt-get install -y age sqlite3
echo 'LIVE_BACKUP_AGE_RECIPIENT=age1...' >> deploy/live/.env   # a public key; keep the private key off this server
echo '30 2 * * * root /opt/visioncast/deploy/live/backup.sh >> /var/log/visioncast-live-backup.log 2>&1' > /etc/cron.d/visioncast-live
```

Files go to `/var/backups/visioncast-live` (`LIVE_BACKUP_DIR`) and are kept 14 days (`LIVE_BACKUP_KEEP_DAYS`). They're on the same server, so copy them somewhere else, or use your provider's server backups as well. To restore, stop the stack, then `age -d -i <identity> live-<time>.tar.age | tar -x -C <the live-data volume>`, and start it again.

## Service API

Everything under `/v1` needs `Authorization: Bearer <LIVE_SERVICE_SECRET>`. All of it is safe to repeat.

| Request | Answer |
|---|---|
| `PUT /v1/rooms/{room}` `{title, startsAt, endsAt, autoRecord, slug, entryUrl}` | `{id, slug}` |
| `GET /v1/rooms/{room}` | The room |
| `POST /v1/rooms/{room}/tickets` `{identity}` | `{id, joinUrl}`, the same link every time |
| `PUT /v1/rooms/{room}/destinations` `{destinations: [{platform, server, key, armed, clearKey}]}` | Saves those platforms. An empty key keeps the one there. The answer never has a key |
| `GET /v1/rooms/{room}/destinations` | `{destinations: [{platform, label, server, armed, status, error, key}]}`, where `key` is `own`, `saved` or `none` |
| `GET`, `PUT /v1/saved-destinations` | Servers and keys kept for every session. Not LinkedIn |
| `GET /v1/brand` | The brand |
| `POST /v1/admin-link` `{email, name, room}` | `{url}`: signs that person in to `/admin`, once, within a minute. Makes their account the first time |
| `GET`, `POST /v1/rooms/{room}/crew` `{role, name, title, email, key}` | The crew, with their links. Repeating a `key` returns the same person |
| `DELETE /v1/rooms/{room}/crew/{id}`, or `POST .../remove`, and `POST .../new-link` | Removes someone, or replaces their link. Either way the old link stops working and they're put out of the room |
| `POST /v1/rooms/{room}/people/{identity}/forget` | Erases what's held on one attendee: chat, questions, votes, survey, captions, attendance |
| `GET`, `PATCH /v1/rooms/{room}/settings` `{settings, theme, survey}` | Room settings, this session's look and its survey |
| `GET /v1/rooms/{room}/engagement?after={identity}` | `{phase, people, next}`, 200 people a page: what each attendee said and voted |
| `PUT /v1/brand` `{accent, stage, mode, font}` | The brand for every room |
| `PUT /v1/brand/logo`, `POST /v1/brand/logo/remove` | A PNG of at most 2 MB |
| `PUT /v1/brand/font`, `POST /v1/brand/font/remove` | A WOFF2, WOFF, TTF or OTF of at most 8 MB. Theme `font: custom` uses it |
| `GET /v1/rooms/{room}/attendance?after={identity}` | `{rows, complete, next}`, 200 rows a page. `complete` is false while anyone is still in the room |
| `GET /v1/rooms/{room}/recordings` | `{recordings}`, the latest 50, each with its `captions` files |

`/admin/api` has the same settings routes, plus `GET /rooms`, `GET /rooms/{room}`, `/me`, `/me/password`, `/activity`, and for owners `/accounts`, `DELETE /accounts/{id}` and `POST /accounts/{id}/reset`, behind the `/login` session.

Public routes: `/{slug}`, `/{slug}/lobby`, `/{slug}/green-room`, `/{slug}/public`, `/login`, `/logout`, `/admin`,
`/login/set/{token}`, `/login/link/{token}`, `/join/{ticket}` and its `/info`, `/enter`, `/events`, `/act`, `/share-token`, `/files`, `/local` and `/captions` actions,
`/program/{ticket}` for the recorder, `/brand/logo`, `/brand/font`, `/assets/*`, `POST /webhooks/livekit` (signature checked by
LiveKit's own receiver) and `/healthz`. `/captions/in/{ticket}` takes audio from the recorder, and the Traefik config
closes it from outside.

## Settings

Set by `deploy/live/compose.yaml` and `.env`.

| Variable | Meaning |
|---|---|
| `LIVE_HOST`, `RTC_HOST`, `TURN_HOST`, `STUDIO_HOST` | The names Traefik answers to and gets certificates for. `setup.sh` writes the first three, and `install.sh` the fourth. Required by `deploy/live/compose.yaml` |
| `LIVE_BIND` | Where the service listens. Default `0.0.0.0:8080`; the compose file sets `127.0.0.1:8080` |
| `LIVE_PUBLIC_URL` | Public address of this service |
| `LIVEKIT_URL`, `LIVEKIT_PUBLIC_URL` | LiveKit as this service reaches it, and as browsers do |
| `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | LiveKit credentials. The secret needs at least 32 characters |
| `LIVE_SERVICE_SECRET` | What VisionCast authenticates with. At least 32 characters |
| `LIVE_TICKET_KEY` | Signs personal links. Changing it invalidates every link. At least 32 characters |
| `LIVE_INTERNAL_URL` | Where the recorder's browser reaches this service. Defaults to the public address |
| `LIVE_ADMIN_EMAIL`, `LIVE_ADMIN_PASSWORD` | The first owner, made once if there are no accounts yet. The password needs at least 12 characters. If it's shorter, or the email has no `@`, nobody is made and the log says so: fix it and restart, or use `reset-admin`. Changing it later does nothing: use `/admin` |
| `LIVE_SLIDES_DIR` | The folder shared with the slides converter. Without it, decks aren't offered |
| `LIVE_INGRESS_URL` | The address encoders send RTMP to, like `rtmp://live.example.com/x`. Without it, RTMP input isn't offered |
| `LIVE_TRANSLATE_URL`, `LIVE_TRANSLATE_LANGUAGES` | A LibreTranslate server, and the languages to offer, like `es,fr` |
| `COMPOSE_PROFILES` | `encoder` and `translate` start those optional containers |
| `LIVE_MIN_CPUS` | Below this many CPUs, hosts and producers see a warning in the room. Default 8. 0 turns it off |
| `LIVE_TRUST_FORWARDED_FOR` | `1` when Traefik is in front, so password guessing is limited per address |
| `IMAGE_OWNER`, `LIVE_VERSION` | Whose published images to use, and which release. Otherwise everything builds from this checkout |
| `TLS_RESOLVER`, `LIVEKIT_USE_EXTERNAL_IP`, `LIVEKIT_NODE_IP` | For trying it on your own machine. Leave them alone on a server |
| `LIVE_CAPTIONS_URL` | Where the captions container is. Without it, there are no captions |
| `CAPTIONS_BIND` | Where the captions container listens. Default `127.0.0.1:8090` |
| `SLIDES_SPOOL` | For the slides container: the same folder as `LIVE_SLIDES_DIR` |
| `RECORDING_S3_INTERNAL_ENDPOINT` | Optional. How this service reaches the bucket, when that differs from `RECORDING_S3_ENDPOINT` |
| `CAPTIONS_THREADS`, `CAPTIONS_CONCURRENCY` | For the captions container: CPU threads per transcription, and how many at once. Default 2 and 2 |
| `WHISPER_MODEL`, `WHISPER_LANGUAGE` | The model file in `deploy/live/models`, and its language. Default `ggml-base.en.bin` and `en` |
| `LIVE_RTMP_ALLOW_PRIVATE` | `1` lets broadcasts go to private addresses. Local testing only |
| `LIVE_DB_PATH` | SQLite file. Default `/data/live.sqlite3` |
| `LIVE_UPLOAD_DIR`, `LIVE_MAX_UPLOAD_MB` | Where uploaded files go, and the size limit. Default `/data/uploads` and 500 MB |
| `LIVE_RETENTION_DAYS` | Days attendance and room records are kept after a session. Default 90. Recordings in your bucket aren't deleted |
| `RECORDING_S3_BUCKET`, `_REGION`, `_ENDPOINT`, `_ACCESS_KEY`, `_SECRET_KEY`, `_FORCE_PATH_STYLE` | Where recordings go. With a bucket set, the service won't start without both keys |
| `RECORDING_PUBLIC_BASE_URL` | Optional. Where the bucket can be read, so the studio can show a link |

## Tests

```bash
cd live && cargo test                     # API, webhooks, tickets and storage, against a fake LiveKit
live/tests/e2e/run.sh                     # every role, then every feature, on a real stack, in real browsers
E2E_SUITE=browsers live/tests/e2e/run.sh  # Firefox, Safari's engine and phone screens
E2E_SUITE=attack LIVE_RTMP_ALLOW_PRIVATE=0 live/tests/e2e/run.sh  # attacks on the running stack, as a hostile user
live/tests/e2e/run-visioncast.sh          # the same stack through VisionCast's real waiting room and import
live/tests/load/run.sh                    # how much one machine takes
python3 live/tests/captions/accuracy.py   # word error rate on recordings of real voices
node live/tests/turn/check.cjs            # media forced through TURN over TLS, against a real server
```

The browser runs need Docker, Node, ffprobe, Chromium and `playwright-core`
(`PLAYWRIGHT_MODULE`, `PLAYWRIGHT_CHROMIUM_EXECUTABLE`). Results are in [docs/validation.md](../docs/validation.md).

## Not proven yet

See [docs/remaining-gaps.md](../docs/remaining-gaps.md#your-own-live-server-livekit-add-on) for what needs your accounts and a public server, and [docs/validation.md](../docs/validation.md) for what's been measured.

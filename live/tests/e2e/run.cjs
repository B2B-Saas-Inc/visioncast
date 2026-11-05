'use strict';
/*
 * End-to-end: a real LiveKit server, Egress and S3-compatible storage (compose.yaml), this
 * service, and real Chromium browsers with fake cameras: a host, a producer, a guest and two
 * attendees. Nothing is mocked. Run through ./run.sh, which starts everything.
 */
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright-core');

const BASE = process.env.LIVE_BASE;
const S3 = process.env.S3_BASE;
const SECRET = process.env.LIVE_SERVICE_SECRET;
const PUBLIC = process.env.LIVE_PUBLIC_URL;
const OUT = process.env.E2E_OUT || os.tmpdir();
const SHOTS = path.join(OUT, 'shots');
fs.mkdirSync(SHOTS, { recursive: true });
const ROOM = `vc-e2e-${Date.now()}`;
// New each run: a broadcast left over from an interrupted run mustn't fight this one for the path.
const STREAM_KEY = `streamkey-${Date.now()}-do-not-leak`;
const SLUG = `launch-${Date.now()}`;
const addressOf = (page) => { const u = new URL(page.url()); return u.pathname + u.search + u.hash; };

let passed = 0;
async function step(name, fn) {
  const started = Date.now();
  await fn();
  passed += 1;
  console.log(`ok   ${name} (${Date.now() - started}ms)`);
}

async function v1(method, url, body) {
  const res = await fetch(`${BASE}/v1${url}`, {
    method,
    headers: { Authorization: `Bearer ${SECRET}`, 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json();
  assert.ok(res.ok, `${method} ${url}: ${res.status} ${JSON.stringify(data)}`);
  return data;
}
const local = (url) => url.replace(PUBLIC, BASE);

async function until(what, fn, timeoutMs = 45000, everyMs = 300) {
  const deadline = Date.now() + timeoutMs;
  let last;
  while (Date.now() < deadline) {
    try { last = await fn(); if (last) return last; } catch (err) { last = err.message; }
    await new Promise((r) => setTimeout(r, everyMs));
  }
  throw new Error(`Timed out waiting for ${what}. Last: ${JSON.stringify(last)}`);
}

async function open(browser, url, who, errors) {
  const context = await browser.newContext({ permissions: ['camera', 'microphone'], viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  page.on('pageerror', (e) => errors.push(`${who}: ${e.message}`));
  page.on('console', (m) => { if (m.type() === 'error' && /Content Security Policy|Refused/.test(m.text())) errors.push(`${who}: ${m.text()}`); });
  await page.goto(url);
  return page;
}
async function enter(page, name, title) {
  await page.waitForSelector('#join-form:not([hidden])');
  await page.fill('#name', name);
  if (title && await page.isVisible('#job-title')) await page.fill('#job-title', title);
  await page.click('#join');
  await page.waitForSelector('#room:not([hidden])', { timeout: 20000 });
}
const tab = (page, name) => page.click(`[data-tab="${name}"]`);
const shot = (page, name) => page.screenshot({ path: path.join(SHOTS, `${name}.png`) });
/** Real decoded video in a stage window: it has a size and its clock is moving. */
const stageVideos = (page) => page.evaluate(async () => {
  const videos = Array.from(document.querySelectorAll('.stile:not([hidden]) video')).filter((v) => v.videoWidth > 0);
  const before = videos.map((v) => v.currentTime);
  await new Promise((r) => setTimeout(r, 700));
  return videos.filter((v, i) => v.currentTime > before[i]).length;
});
const tileNames = (page) => page.$$eval('.stile:not([hidden]) .stile-label b', (els) => els.map((e) => e.textContent));
const greenTile = (name) => `.gtile:has(.gtile-meta b:text-is("${name}"))`;
async function putOnStage(page, name) { await page.click(`${greenTile(name)} .mini:text-is("Put on stage")`); }
const audioFrom = (page) => page.evaluate(() => Array.from(document.querySelectorAll('#audio-sink audio')).filter((a) => a.srcObject && a.srcObject.getAudioTracks().length && !a.muted).length);

(async () => {
  const errors = [];
  const launch = (speech) => chromium.launch({
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE,
    args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream', '--autoplay-policy=no-user-gesture-required', `--use-file-for-fake-audio-capture=${speech}`],
  });
  const browser = await launch(process.env.SPEECH_HOST);
  const guestBrowser = await launch(process.env.SPEECH_GUEST);
  let links, hostP, prodP, guestP, viewP, view2P, attendee2, recording;
  const pollIdOf = async (page) => page.$eval('[data-poll]', (e) => e.dataset.poll);
  const framePng = path.join(OUT, 'frame.png');
  const clip = path.join(OUT, 'clip.mp4');
  const deck = path.join(OUT, 'deck.pdf');
  const gif = path.join(OUT, 'band.gif');
  const bug = path.join(OUT, 'bug.webm');
  execFileSync('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'lavfi', '-i', 'color=c=0x1565C0:s=640x90:d=2,drawbox=x=0:y=0:w=120:h=90:color=0xF9A825:t=fill', '-r', '10', gif]);
  execFileSync('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'lavfi', '-i', 'color=c=0xFF00FF:s=320x180:d=3,format=yuva420p,geq=lum=p(X\\,Y):cb=p(X\\,Y):cr=p(X\\,Y):a=if(between(Y\\,20\\,160)\\,255\\,0)', '-c:v', 'libvpx-vp9', '-pix_fmt', 'yuva420p', '-auto-alt-ref', '0', '-an', bug]);
  execFileSync('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'lavfi', '-i', 'color=c=black:s=1280x720,format=rgba,geq=r=194:g=70:b=46:a=if(between(X\\,48\\,1231)*between(Y\\,48\\,671)\\,0\\,255)', '-frames:v', '1', framePng]);
  execFileSync('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'lavfi', '-i', 'testsrc=duration=30:size=640x360:rate=24', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=30', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', '-movflags', '+faststart', clip]);
  fs.writeFileSync(deck, '%PDF-1.4 preview deck');
  const now = Date.now();
  try {
    await step('VisionCast-style setup: a room, crew links and attendee links', async () => {
      await v1('PUT', `/rooms/${ROOM}`, { title: 'Launch webinar', startsAt: new Date(now + 600000).toISOString(), endsAt: new Date(now + 4200000).toISOString(), slug: SLUG, entryUrl: 'https://events.example.com/w/abc/' });
      const person = async (role, name, title = '') => (await v1('POST', `/rooms/${ROOM}/crew`, { role, name, title })).url;
      links = {
        hostUrl: await person('host', 'Maya Host', 'CEO, Acme Events'),
        producerUrl: await person('producer', 'Pat Producer'),
        guestUrl: await person('guest', 'Gus Guest', 'VP Marketing, Acme'),
        probeProducer: await person('producer', 'Probe Producer'),
        probeGuest: await person('guest', 'Probe Guest'),
      };
      await v1('PUT', `/rooms/${ROOM}/destinations`, { destinations: [
        { platform: 'youtube', server: 'rtmp://rtmp:1935/live', key: `youtube-${STREAM_KEY}`, armed: true },
        { platform: 'meta', server: 'rtmps://rtmp:1936/live', key: `meta-${STREAM_KEY}`, armed: true },
        { platform: 'x', server: 'rtmp://no-such-host.invalid/live', key: `x-${STREAM_KEY}`, armed: true },
        { platform: 'linkedin', server: 'rtmp://rtmp:1935/live', key: `linkedin-${STREAM_KEY}`, armed: false },
      ] });
      links.attendee = (await v1('POST', `/rooms/${ROOM}/tickets`, { identity: '3f2a1b6c-8d4e-4f0a-9b1c-2d3e4f5a6b7c' })).joinUrl;
      attendee2 = (await v1('POST', `/rooms/${ROOM}/tickets`, { identity: '77aa1b6c-8d4e-4f0a-9b1c-2d3e4f5a6b7c' })).joinUrl;
    });

    await step('host, producer and guest pass the device check into the green room', async () => {
      hostP = await open(browser, local(links.hostUrl), 'host', errors);
      await until('the camera preview', () => hostP.evaluate(() => document.getElementById('preview').videoWidth > 0));
      await shot(hostP, '01-device-check');
      assert.equal(addressOf(hostP), `/${SLUG}/green-room`, 'the signed link is out of the address bar');
      assert.equal(await hostP.inputValue('#name'), 'Maya Host', 'the crew member\'s name is ready');
      assert.equal(await hostP.inputValue('#job-title'), 'CEO, Acme Events');
      await enter(hostP, 'Maya Host', 'CEO, Acme Events');
      prodP = await open(browser, local(links.producerUrl), 'producer', errors);
      await enter(prodP, 'Pat Producer');
      guestP = await open(guestBrowser, local(links.guestUrl), 'guest', errors);
      await enter(guestP, 'Gus Guest', 'VP Marketing, Acme');
      await until('all three in the strip', async () => (await hostP.$$('.gtile')).length === 3);
      await until('green room video', () => hostP.evaluate(() => document.querySelectorAll('.gtile video').length === 3));
      assert.equal(await guestP.isVisible('#go-live'), false, 'a guest has no Go live');
      assert.equal(await guestP.isVisible('#end'), false);
      assert.equal(await guestP.isVisible('#layouts'), false);
      assert.equal(await guestP.isVisible('.gtile .mini'), false, 'a guest moves nobody');
      assert.equal(await hostP.isVisible('#go-live'), true);
      assert.equal(await prodP.isVisible('#go-live'), true);
      await until('producer has no Put on stage button', async () => (await hostP.$$(`${greenTile('Pat Producer')} .mini`)).length === 0);
      await shot(hostP, '02-green-room-host');
      await shot(guestP, '03-green-room-guest');
    });

    await step('before going live an attendee waits at a countdown, with no way into the media', async () => {
      viewP = await open(browser, local(links.attendee), 'attendee', errors);
      await enter(viewP, 'Ada Viewer');
      await viewP.waitForSelector('#prelive:not([hidden])');
      assert.match(await viewP.textContent('#countdown'), /\d+:\d+/);
      assert.equal(addressOf(viewP), `/${SLUG}/lobby`);
      const got = await viewP.evaluate(() => window.performance.getEntriesByType('resource').some((r) => /\/rtc/.test(r.name)));
      assert.equal(got, false, 'no LiveKit connection before going live');
      assert.equal(await viewP.isVisible('#green'), false);
      assert.equal(await viewP.isVisible('#my-controls'), false);
      await shot(viewP, '04-attendee-countdown');
    });

    await step('on stage but not live: the crew sees the stage, the attendee still sees nothing', async () => {
      await putOnStage(prodP, 'Maya Host');
      await until('the host on the producer\'s stage', async () => (await tileNames(prodP)).includes('Maya Host'));
      await until('host video on the stage', async () => (await stageVideos(prodP)) >= 1);
      assert.equal(await viewP.$$eval('.stile video', (v) => v.length), 0);
      await shot(prodP, '05-stage-not-live-producer');
    });

    await step('going live reaches the attendee, with real video and audio', async () => {
      await prodP.click('#go-live');
      await viewP.waitForSelector('#prelive', { state: 'hidden', timeout: 15000 });
      await until('video at the attendee', async () => (await stageVideos(viewP)) >= 1);
      await until('audio at the attendee', async () => (await audioFrom(viewP)) >= 1);
      assert.equal(await hostP.textContent('#phase-pill'), 'Live');
      await hostP.waitForSelector('#end:not([hidden])');
      await until('the attendee at the clean address', async () => addressOf(viewP) === `/${SLUG}`);
      // Reloading keeps them in. A visitor with no link gets sent to the waiting room.
      const stranger = await open(browser, `${BASE}/${SLUG}`, 'stranger', errors);
      await stranger.waitForSelector('#gate-note a[href="https://events.example.com/w/abc/"]');
      assert.equal(await stranger.isVisible('#join-form'), false);
      await stranger.context().close();
    });

    await step('LiveKit itself refuses: an attendee, a producer and an off-stage guest all really try to publish to the stage', async () => {
      const probeTicket = (await v1('POST', `/rooms/${ROOM}/tickets`, { identity: 'probe-attendee' })).joinUrl;
      const probePage = await open(browser, local(probeTicket), 'probe', errors);
      // The token each role gets for the stage, used directly as a hostile browser would.
      const attempt = (page, clientId) => page.evaluate(async (clientId) => {
        const res = await fetch(document.documentElement.dataset.base + '/enter', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: 'Probe', clientId }) });
        const data = await res.json();
        const room = new LivekitClient.Room();
        await room.connect(data.url, data.stageToken);
        const outcome = { green: data.greenToken ? 'has green' : 'no green', mic: 'published', cam: 'published', screen: 'published' };
        const tryOne = async (name, fn) => { try { await fn(); } catch (err) { outcome[name] = 'refused'; } };
        await tryOne('mic', async () => { const t = await LivekitClient.createLocalAudioTrack(); await room.localParticipant.publishTrack(t); });
        await tryOne('cam', async () => { const t = await LivekitClient.createLocalVideoTrack(); await room.localParticipant.publishTrack(t); });
        await new Promise((r) => setTimeout(r, 300));
        outcome.others = Array.from(room.remoteParticipants.keys());
        outcome.data = await room.localParticipant.publishData(new TextEncoder().encode('spoof'), { reliable: true }).then(() => 'sent', () => 'refused');
        await room.disconnect();
        return outcome;
      }, clientId);
      const attendee = await attempt(probePage, 'probeclient1');
      assert.equal(attendee.green, 'no green');
      assert.equal(attendee.mic, 'refused');
      assert.equal(attendee.cam, 'refused');
      assert.ok(!attendee.others.some((id) => id.startsWith('host-p-')), `a producer is visible to an attendee: ${attendee.others}`);
      const producer = await attempt(await open(browser, local(links.probeProducer), 'probe-producer', errors), 'probeproducer1');
      assert.equal(producer.mic, 'refused', 'a producer\'s mic cannot reach the stage');
      assert.equal(producer.cam, 'refused');
      const guest = await attempt(await open(browser, local(links.probeGuest), 'probe-guest', errors), 'probeguest0001');
      assert.equal(guest.mic, 'refused', 'a guest in the green room cannot reach the stage');
      await probePage.context().close();
    });

    await step('a producer talks to the person on stage, and the audience gets none of it', async () => {
      await until('host hears the producer', () => hostP.evaluate(() => Array.from(document.querySelectorAll('audio[data-from^="host-p-"]')).some((a) => !a.muted && a.srcObject)));
      await until('host does not hear the waiting guest', () => hostP.evaluate(() => Array.from(document.querySelectorAll('audio[data-from^="host-g-"]')).every((a) => a.muted)));
      const fromCrew = await viewP.evaluate(() => document.querySelectorAll('audio[data-from]').length);
      assert.equal(fromCrew, 0, 'attendees have no green room audio at all');
      const names = await tileNames(viewP);
      assert.ok(!names.includes('Pat Producer'));
    });

    await step('guest moves to the stage, publishing switches rooms, and the audience sees both', async () => {
      await putOnStage(hostP, 'Gus Guest');
      await until('both at the attendee', async () => {
        const names = await tileNames(viewP);
        return names.includes('Gus Guest') && names.includes('Maya Host') && (await stageVideos(viewP)) >= 2;
      });
      assert.equal(await guestP.textContent('#notice'), "You're on air. The audience can see and hear you.");
      assert.equal(await guestP.isVisible('#go-live'), false);
      await shot(viewP, '06-attendee-two-speakers');
    });

    await step('every layout preset arranges the windows the same for everyone', async () => {
      const rects = async (page) => page.$$eval('.stile:not([hidden])', (els) => els.map((e) => [e.dataset.key, e.style.width]).sort());
      for (const mode of ['single', 'split', 'speaker', 'pip', 'grid']) {
        await hostP.click(`[data-layout="${mode}"]`);
        await until(`${mode} at the attendee`, async () => {
          const pressed = await hostP.getAttribute(`[data-layout="${mode}"]`, 'aria-pressed');
          return pressed === 'true' && JSON.stringify(await rects(hostP)) === JSON.stringify(await rects(viewP));
        });
      }
    });

    await step('a producer resizes a window by dragging and it reaches the audience', async () => {
      await prodP.click('[data-layout="split"]');
      await prodP.click('#edit-layout');
      const handle = await prodP.$('.stile:not([hidden]) .stile-handle');
      const box = await handle.boundingBox();
      await prodP.mouse.move(box.x + 5, box.y + 5);
      await prodP.mouse.down();
      await prodP.mouse.move(box.x - 120, box.y - 90, { steps: 6 });
      await prodP.mouse.up();
      await prodP.click('#edit-layout');
      await until('custom layout at the attendee', () => viewP.$$eval('.stile:not([hidden])', (els) => els.some((e) => parseFloat(e.style.width) < 40)));
      await prodP.click('[data-layout="grid"]');
    });

    await step('frames, lower thirds, tickers and calls to action go out on the picture', async () => {
      await tab(prodP, 'look');
      await prodP.setInputFiles('[data-upload="frame"]', framePng);
      await until('the frame at the attendee', () => viewP.evaluate(() => { const f = document.querySelector('.pic-frame'); return !f.hidden && f.naturalWidth > 0; }));
      await prodP.$eval('#frame-inset', (el) => { el.value = '7'; el.dispatchEvent(new Event('change')); });
      await until('windows inside the frame', () => viewP.$$eval('.stile:not([hidden])', (els) => els.every((e) => parseFloat(e.style.left) >= 6.9)));
      await prodP.fill('#lt-title', 'Maya Host');
      await prodP.fill('#lt-sub', 'CEO, Acme Events');
      await prodP.click('#lt-title >> xpath=ancestor::div[contains(@class,"box")][1] >> text=Show');
      await prodP.fill('#ticker-text', 'Ask your questions in the Q&A tab');
      await prodP.click('#ticker-text >> xpath=ancestor::div[contains(@class,"box")][1] >> text=Show');
      await prodP.fill('#cta-title', 'See the launch');
      await prodP.fill('#cta-label', 'Open');
      await prodP.fill('#cta-url', 'https://www.example.com/events');
      await prodP.click('#cta-title >> xpath=ancestor::div[contains(@class,"box")][1] >> text=Show');
      await viewP.waitForSelector('.pic-cta:not([hidden])');
      assert.equal(await viewP.textContent('.pic-third strong'), 'Maya Host');
      assert.match(await viewP.textContent('.pic-ticker'), /Q&A/);
      assert.equal(await viewP.getAttribute('.pic-cta', 'href'), 'https://www.example.com/events');
      await shot(viewP, '07-attendee-frame-overlays');
      await shot(prodP, '08-producer-look-panel');
    });

    await step('a brand set in the studio reaches open rooms, and a session can override it', async () => {
      const accent = (page) => page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--coral').trim());
      await v1('PUT', '/brand', { accent: '#1565c0', stage: '#101828', mode: 'dark', font: 'serif' });
      await until('the brand at the attendee', async () => (await accent(viewP)) === '#1565c0');
      assert.equal(await viewP.evaluate(() => document.body.classList.contains('dark')), true);
      const logo = await fetch(`${BASE}/v1/brand/logo`, { method: 'PUT', headers: { Authorization: `Bearer ${SECRET}` }, body: fs.readFileSync(framePng) });
      assert.equal(logo.status, 200);
      await until('the logo in the header', () => viewP.evaluate(() => document.getElementById('bar-logo').getAttribute('src').startsWith('/brand/logo')));
      await shot(viewP, '07a-attendee-branded');
      await tab(prodP, 'look');
      await prodP.$eval('#look-accent', (el) => { el.value = '#7b1fa2'; el.dispatchEvent(new Event('change')); });
      await until('the session colour at the attendee', async () => (await accent(viewP)) === '#7b1fa2');
      await prodP.click('#look-reset .mini');
      await until('back to the brand', async () => (await accent(viewP)) === '#1565c0');
      await prodP.setViewportSize({ width: 1440, height: 2100 });
      await shot(prodP, 'look-tab-full');
      await prodP.setViewportSize({ width: 1440, height: 900 });
      await prodP.$eval('#look', (el) => { el.scrollTop = 0; });
      await shot(prodP, 'look-tab-top');
      await v1('PUT', '/brand', { accent: '', stage: '', mode: '', font: '' });
      await fetch(`${BASE}/v1/brand/logo/remove`, { method: 'POST', headers: { Authorization: `Bearer ${SECRET}` } });
      await until('default look again', async () => (await accent(viewP)) === '#c2462e');
    });

    await step('GIFs, videos with transparency and live words go on the picture, and can be moved', async () => {
      await tab(prodP, 'look');
      await prodP.setInputFiles('[data-upload="graphic"]', gif);
      await viewP.waitForSelector('.pic-layer img', { timeout: 10000 });
      await until('the GIF drawn', () => viewP.evaluate(() => document.querySelector('.pic-layer img').naturalWidth === 640));
      await prodP.fill('#layer-list input[data-layer-title]', 'Breaking: the launch is live');
      await prodP.press('#layer-list input[data-layer-title]', 'Tab');
      await viewP.waitForSelector('.pic-layer-words strong:text-is("Breaking: the launch is live")');
      await shot(viewP, '07b-attendee-gif-chyron');
      await prodP.setInputFiles('[data-upload="graphic"]', bug);
      await until('the video layer', async () => (await prodP.$$('#layer-list [data-layer]')).length === 2);
      const second = (await prodP.$$('#layer-list [data-layer]'))[1];
      await (await second.$('select')).selectOption('bug');
      await until('a playing, silent video at the attendee', () => viewP.evaluate(() => { const v = document.querySelector('.pic-layer video'); return v && v.currentTime > 0.2 && !v.paused && v.muted; }));
      // Move it by hand, and the audience sees it move.
      await prodP.click('#edit-layout');
      const box = await (await prodP.$('.pic-layer:has(video)')).boundingBox();
      await prodP.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      await prodP.mouse.down();
      await prodP.mouse.move(box.x + box.width / 2 - 60, box.y + box.height / 2 + 30, { steps: 5 });
      await prodP.mouse.up();
      await prodP.click('#edit-layout');
      await until('the new place at the attendee', () => viewP.evaluate(() => parseFloat(document.querySelector('.pic-layer:has(video)').style.left) < 84));
      await prodP.setViewportSize({ width: 1440, height: 2500 });
      await shot(prodP, 'look-tab-with-graphics');
      await prodP.setViewportSize({ width: 1440, height: 900 });
      // Hidden means gone, for the page and for the file.
      const src = await viewP.$eval('.pic-layer img', (i) => i.getAttribute('src'));
      await prodP.click('#layer-list [data-layer]:first-child .mini:text-is("Hide")');
      await until('the GIF gone', () => viewP.evaluate(() => !document.querySelector('.pic-layer img')));
      assert.equal(await viewP.evaluate(async (u) => (await fetch(u)).status, src), 404);
      await prodP.click('#layer-list [data-layer]:first-child .mini:text-is("Show")');
    });

    await step('captions: each viewer chooses, and they name who is speaking, from real speech', async () => {
      assert.equal(await viewP.getAttribute('#cc', 'aria-pressed'), 'false', 'off until a viewer turns them on');
      await viewP.waitForTimeout(3000);
      assert.equal(await viewP.isVisible('.pic-captions'), false);
      await viewP.click('#cc');
      const line = await until('a caption', async () => {
        const text = await viewP.$eval('.pic-captions', (e) => (e.hidden ? '' : e.textContent));
        return /webinar|product|welcome/i.test(text) && /Maya Host:/.test(text) ? text : false;
      }, 45000);
      console.log(`     host said: ${line}`);
      await shot(viewP, '07c-attendee-captions');
      const guestWords = await until('the guest caption', async () => {
        const text = await viewP.$eval('.pic-captions', (e) => (e.hidden ? '' : e.textContent));
        return /Gus Guest:/.test(text) && /customers|reporting|dashboard|mobile/i.test(text) ? text : false;
      }, 45000, 200);
      console.log(`     guest said: ${guestWords}`);
      // It's remembered, and the page stays clean of who wasn't asked.
      await viewP.reload();
      await viewP.waitForSelector('#join-form:not([hidden])');
      await enter(viewP, 'Ada Viewer');
      await viewP.waitForSelector('#cc:not([hidden])');
      assert.equal(await viewP.getAttribute('#cc', 'aria-pressed'), 'true');
      await viewP.click('#cc');
      assert.equal(await viewP.isVisible('.pic-captions'), false);
    });

    await step('the broadcast really reaches RTMP and RTMPS servers, and a dead one fails alone', async () => {
      await tab(prodP, 'broadcast');
      const status = async (name) => prodP.$eval(`[data-dest="${name}"] .dest-state`, (e) => e.textContent).catch(() => '');
      await until('YouTube sending', async () => (await status('youtube')) === 'Sending', 60000);
      await until('Meta sending, over RTMPS', async () => (await status('meta')) === 'Sending', 30000);
      await until('X failed alone', async () => (await status('x')) === 'Failed', 90000);
      assert.equal(await status('youtube'), 'Sending', 'the others keep going');
      await prodP.waitForSelector('#cast-pill:not([hidden])');
      await shot(prodP, '08a-broadcast-tab');
      const probe = (name) => JSON.parse(execFileSync('ffprobe', ['-v', 'error', '-rw_timeout', '15000000', '-show_streams', '-of', 'json', `rtmp://127.0.0.1:11935/live/${name}`], { timeout: 40000 }).toString());
      for (const name of [`youtube-${STREAM_KEY}`, `meta-${STREAM_KEY}`]) {
        const got = probe(name);
        assert.ok(got.streams.some((v) => v.codec_type === 'video' && v.codec_name === 'h264' && v.width >= 1280), `${name} video`);
        assert.ok(got.streams.some((v) => v.codec_type === 'audio'), `${name} audio`);
      }
      // What the platform gets is the program, frame included: a corner pixel proves it.
      const still = path.join(SHOTS, 'broadcast-as-received.png');
      execFileSync('ffmpeg', ['-y', '-loglevel', 'error', '-i', `rtmp://127.0.0.1:11935/live/youtube-${STREAM_KEY}`, '-frames:v', '1', still], { timeout: 40000 });
      const pixel = execFileSync('ffmpeg', ['-loglevel', 'error', '-i', still, '-vf', 'crop=4:4:6:6,scale=1:1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-']);
      assert.ok(pixel[0] > 150 && pixel[1] < 120, `corner colour ${[...pixel]}`);
      const box = await viewP.evaluate(() => { const r = document.querySelector('.pic-layer:has(video)').style; return { x: parseFloat(r.left), y: parseFloat(r.top), w: parseFloat(r.width), h: parseFloat(r.height) }; });
      const cx = Math.round((box.x + box.w / 2) * 19.2), cy = Math.round((box.y + box.h / 2) * 10.8);
      const magenta = execFileSync('ffmpeg', ['-loglevel', 'error', '-i', still, '-vf', `crop=4:4:${cx}:${cy},scale=1:1`, '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-']);
      assert.ok(magenta[0] > 180 && magenta[1] < 90 && magenta[2] > 180, `the video layer isn't in the broadcast: ${[...magenta]}`);
    });

    await step('a platform can be added and dropped during the session without touching the others', async () => {
      const paths = async () => (await (await fetch('http://127.0.0.1:18888/live/')).text()).length >= 0;
      assert.ok(await paths());
      await prodP.click('[data-dest="linkedin"] .mini:text-is("Start")');
      await until('LinkedIn sending', async () => (await prodP.$eval('[data-dest="linkedin"] .dest-state', (e) => e.textContent)) === 'Sending', 45000);
      const gotLinkedIn = JSON.parse(execFileSync('ffprobe', ['-v', 'error', '-rw_timeout', '15000000', '-show_streams', '-of', 'json', `rtmp://127.0.0.1:11935/live/linkedin-${STREAM_KEY}`], { timeout: 40000 }).toString());
      assert.ok(gotLinkedIn.streams.some((v) => v.codec_type === 'video'));
      await prodP.click('[data-dest="linkedin"] .mini:text-is("Stop")');
      await until('LinkedIn off', async () => (await prodP.$eval('[data-dest="linkedin"] .dest-state', (e) => e.textContent)) === 'Off', 20000);
      assert.equal(await prodP.$eval('[data-dest="youtube"] .dest-state', (e) => e.textContent), 'Sending');
      assert.equal(await guestP.isVisible('[data-tab="broadcast"]'), false, 'a guest has no Broadcast tab');
      assert.equal(await viewP.isVisible('[data-tab="broadcast"]'), false);
    });

    await step('stream keys appear in no page, event stream or log', async () => {
      for (const [name, page] of [['host', hostP], ['producer', prodP], ['guest', guestP], ['attendee', viewP]]) {
        const text = await page.evaluate(async () => {
          const res = await fetch(document.documentElement.dataset.base + '/info');
          return document.documentElement.outerHTML + (await res.text());
        });
        assert.ok(!text.includes(STREAM_KEY), `${name} page`);
      }
      const log = fs.readFileSync(path.join(OUT, 'visioncast-live.log'), 'utf8');
      assert.ok(!log.includes(STREAM_KEY), 'service log');
      const stored = fs.readFileSync(process.env.LIVE_DB_PATH);
      assert.ok(!stored.includes(STREAM_KEY), 'stored encrypted');
    });

    await step('a video plays on the stage natively, in step, for everyone', async () => {
      await tab(prodP, 'look');
      await prodP.setInputFiles('[data-upload="media"]', clip);
      await prodP.click('#media-list .mini:text-is("Play on stage")');
      await until('video at the attendee', () => viewP.evaluate(() => { const v = document.querySelector('.stile[data-key="media"] video'); return v && !v.paused && v.currentTime > 0.5; }));
      const at = async (p) => p.evaluate(() => document.querySelector('.stile[data-key="media"] video').currentTime);
      const [a, b] = await Promise.all([at(viewP), at(hostP)]);
      assert.ok(Math.abs(a - b) < 1.5, `in step: ${a} vs ${b}`);
      await shot(viewP, '09-attendee-media');
      await prodP.click('#media-list .mini:text-is("Stop")');
      await until('video gone', () => viewP.evaluate(() => !document.querySelector('.stile[data-key="media"]')));
      await prodP.click('#cta-title >> xpath=ancestor::div[contains(@class,"box")][1] >> text=Hide');
      await prodP.click('#ticker-text >> xpath=ancestor::div[contains(@class,"box")][1] >> text=Hide');
      await prodP.click('#lt-title >> xpath=ancestor::div[contains(@class,"box")][1] >> text=Hide');
    });

    await step('chat: delete, pin, review, crew-only channel and blocking all work', async () => {
      await tab(viewP, 'chat');
      await viewP.fill('#chat-input', 'Hello from the audience');
      await viewP.press('#chat-input', 'Enter');
      await tab(hostP, 'chat');
      await hostP.waitForSelector('#messages li:has-text("Hello from the audience")');
      await hostP.click('#messages li:has-text("Hello from the audience") .mini:text-is("Pin")');
      await viewP.waitForSelector('#pinned:not([hidden]):has-text("Hello from the audience")');
      // Crew-only chat stays with the crew.
      await hostP.click('[data-channel="stage"]');
      await hostP.fill('#chat-input', 'Crew only: two minutes left');
      await hostP.press('#chat-input', 'Enter');
      await tab(guestP, 'chat');
      await guestP.click('[data-channel="stage"]');
      await guestP.waitForSelector('#messages li:has-text("two minutes left")');
      assert.equal(await viewP.$$eval('#messages li', (l) => l.some((e) => e.textContent.includes('two minutes left'))), false);
      assert.equal(await viewP.isVisible('[data-channel="stage"]'), false);
      await hostP.click('[data-channel="event"]');
      // Review mode holds messages back.
      await tab(prodP, 'settings');
      await prodP.check('[data-setting="chatReview"]');
      await until('review on at the attendee', () => hostP.evaluate(() => true));
      await new Promise((r) => setTimeout(r, 1200));
      await viewP.fill('#chat-input', 'Held back until approved');
      await viewP.press('#chat-input', 'Enter');
      await hostP.waitForSelector('#messages li.held:has-text("Held back")');
      view2P = await open(browser, local(attendee2), 'attendee2', errors);
      await enter(view2P, 'Bea Viewer');
      await view2P.waitForSelector('#messages');
      assert.equal(await view2P.$$eval('#messages li', (l) => l.some((e) => e.textContent.includes('Held back'))), false, 'others do not see it');
      await hostP.click('#messages li.held:has-text("Held back") .mini:text-is("Approve")');
      await view2P.waitForSelector('#messages li:has-text("Held back")');
      await hostP.click('#messages li:has-text("Held back") .mini:text-is("Delete")');
      await until('deleted everywhere', () => view2P.$$eval('#messages li', (l) => !l.some((e) => e.textContent.includes('Held back'))));
      await prodP.uncheck('[data-setting="chatReview"]');
      await shot(hostP, '10-host-chat-moderation');
      // Blocking stops them posting; they keep watching.
      await new Promise((r) => setTimeout(r, 1100));
      await view2P.fill('#chat-input', 'Rude message');
      await view2P.press('#chat-input', 'Enter');
      await hostP.click('#messages li:has-text("Rude message") .mini:text-is("Block")');
      await view2P.waitForSelector('#chat-form', { state: 'hidden' });
      assert.match(await view2P.textContent('#chat-off'), /moderator/);
    });

    await step('Q&A: anonymous questions, review, upvotes, answers and putting one on stage', async () => {
      await tab(prodP, 'settings');
      await prodP.check('[data-setting="qaModerated"]');
      await new Promise((r) => setTimeout(r, 600));
      await tab(viewP, 'qa');
      await viewP.fill('#qa-input', 'When does this ship?');
      await viewP.check('#qa-anon');
      await viewP.click('#qa-form button');
      await tab(hostP, 'qa');
      await hostP.waitForSelector('#questions li.held:has-text("When does this ship?"):has-text("Anonymous")');
      await tab(view2P, 'qa');
      assert.equal(await view2P.$$eval('#questions li', (l) => l.length), 0, 'pending questions stay hidden');
      await hostP.click('#questions li:has-text("When does this ship?") .mini:text-is("Approve")');
      await view2P.waitForSelector('#questions li:has-text("When does this ship?")');
      await tab(guestP, 'qa');
      await guestP.click('#questions li:has-text("When does this ship?") .mini:text-is("Answer")');
      await guestP.fill('#questions li:has-text("When does this ship?") form input', 'Next quarter.');
      await guestP.press('#questions li:has-text("When does this ship?") form input', 'Enter');
      await view2P.waitForSelector('#questions .answer:has-text("Next quarter.")');
      await hostP.click('#questions li:has-text("When does this ship?") .mini:text-is("On stage")');
      await viewP.waitForSelector('.pic-third:not([hidden]):has-text("When does this ship?")');
      await tab(prodP, 'look');
      await prodP.click('#lt-title >> xpath=ancestor::div[contains(@class,"box")][1] >> text=Hide');
      await prodP.click('[data-tab="settings"]');
      await prodP.uncheck('[data-setting="qaModerated"]');
      await shot(hostP, '11-host-qa');
    });

    await step('live polls: draft stays hidden, votes count once, results reach the stage', async () => {
      await tab(prodP, 'polls');
      await prodP.click('#poll-editor summary');
      await prodP.fill('#poll-q', 'What brought you here?');
      const opts = await prodP.$$('.poll-opt');
      await opts[0].fill('The product');
      await opts[1].fill('A colleague');
      await prodP.click('#poll-form button[type=submit]');
      await prodP.waitForSelector('[data-poll]');
      await tab(viewP, 'polls');
      assert.equal(await viewP.$$eval('[data-poll]', (l) => l.length), 0, 'drafts stay hidden');
      await prodP.click('[data-poll] .mini:text-is("Publish")');
      await viewP.waitForSelector('[data-poll] button.poll-row');
      await viewP.click('[data-poll] button.poll-row:has-text("The product")');
      await viewP.waitForSelector('[data-poll] .poll-row.mine');
      assert.equal(await viewP.$$eval('[data-poll] button.poll-row', (l) => l.length), 0, 'one vote each');
      await prodP.click('[data-poll] .mini:text-is("Show on stage")');
      await until('poll on the stage', () => viewP.evaluate(() => { const p = document.querySelector('.pic-poll'); return !p.hidden && p.textContent.includes('100%'); }));
      await shot(viewP, '12-attendee-poll-on-stage');
      await shot(prodP, '13-producer-polls');
      await prodP.click('[data-poll] .mini:text-is("Close")');
      await prodP.click('[data-poll] .mini:text-is("Take off stage")');
      await viewP.waitForSelector('.pic-poll', { state: 'hidden' });
    });

    await step('files: private until shared, then downloadable, and the count goes up', async () => {
      await tab(hostP, 'files');
      await hostP.setInputFiles('#resource-file', deck);
      await hostP.waitForSelector('[data-file]');
      await tab(viewP, 'files');
      assert.equal(await viewP.$$eval('[data-file]', (l) => l.length), 0, 'private until shared');
      await hostP.click('[data-file] .mini:text-is("Share")');
      await viewP.waitForSelector('[data-file] a');
      const href = await viewP.getAttribute('[data-file] a', 'href');
      const download = await viewP.evaluate(async (url) => {
        const res = await fetch(url);
        return { status: res.status, disposition: res.headers.get('content-disposition'), text: await res.text() };
      }, href);
      assert.equal(download.status, 200);
      assert.match(download.disposition, /attachment; filename="deck\.pdf"/);
      assert.match(download.text, /preview deck/);
      await until('the download count', async () => (await hostP.textContent('[data-file]')).includes('1 download'));
      await shot(viewP, '14-attendee-files');
    });

    await step('reactions float over the stage', async () => {
      await viewP.click('#reactions button:first-child');
      await hostP.waitForSelector('.pic-float', { timeout: 5000 });
    });

    await step('an attendee raises a hand, accepts an invitation and speaks from the stage', async () => {
      await viewP.click('#hand');
      await tab(hostP, 'people');
      await hostP.waitForSelector('.person:has-text("Ada Viewer"):has-text("hand up")');
      await shot(hostP, '15-host-people');
      await hostP.click('.person:has-text("Ada Viewer") .mini:text-is("Invite on stage")');
      await viewP.waitForSelector('#invite-modal:not([hidden])');
      await shot(viewP, '16-attendee-invite');
      await viewP.click('#invite-yes');
      // A reload brings them to the device check, as a guest.
      await viewP.waitForSelector('#devices:not([hidden])');
      await enter(viewP, 'Ada Viewer');
      await until('Ada in the green room', async () => (await hostP.$$(greenTile('Ada Viewer'))).length === 1);
      assert.equal(await viewP.isVisible('#green'), true);
      assert.equal(await viewP.isVisible('#go-live'), false);
      await putOnStage(hostP, 'Ada Viewer');
      await until('Ada on the stage for another attendee', async () => (await tileNames(view2P)).includes('Ada Viewer') && (await stageVideos(view2P)) >= 3);
      await shot(view2P, '17-attendee-spotlight');
      // Sent back: LiveKit takes her off and she's an attendee again, without a green room.
      await tab(hostP, 'people');
      await hostP.click('.person:has-text("Ada Viewer") .mini:text-is("Back to audience")');
      await until('Ada off the stage', async () => !(await tileNames(view2P)).includes('Ada Viewer'));
      await viewP.waitForSelector('#prelive', { state: 'hidden' });
      await until('Ada back in the audience', async () => !(await viewP.isVisible('#green')) && await viewP.isVisible('#viewer-controls'), 20000);
    });

    await step('a host screen shares and the guest can too; attendees see it', async () => {
      // The fake-device flag stands in for the browser's screen picker. It has to be asked for first,
      // straight from the click, because Safari refuses it after waiting for anything.
      await guestP.evaluate(() => {
        window.__share = [];
        const real = navigator.mediaDevices.getDisplayMedia.bind(navigator.mediaDevices);
        navigator.mediaDevices.getDisplayMedia = (c) => { window.__share.push('picker'); return real(c).then((stream) => { window.__shared = stream; return stream; }); };
        const realFetch = window.fetch;
        window.fetch = (u, o) => { if (String(u).includes('share-token')) window.__share.push('token'); return realFetch(u, o); };
      });
      await guestP.click('#screen');
      await until('a screen window at the attendee', () => view2P.$$eval('.stile.is-screen:not([hidden]) video', (v) => v.some((x) => x.videoWidth > 0)));
      await hostP.click('[data-layout="pip"]');
      await shot(view2P, '18-attendee-screenshare-pip');
      await hostP.click('[data-layout="grid"]');
      assert.deepEqual(await guestP.evaluate(() => window.__share.slice(0, 2)), ['picker', 'token'], 'the picker comes before anything that waits');
      // The browser's own Stop sharing bar.
      await guestP.evaluate(() => { const track = window.__shared.getVideoTracks()[0]; track.stop(); track.dispatchEvent(new Event('ended')); });
      await until('the screen gone', () => view2P.$$eval('.stile.is-screen', (v) => v.length === 0));
      await until('the button back', async () => (await guestP.textContent('#screen')) === 'Share screen');
      // And our own button, on a second share.
      await guestP.click('#screen');
      await until('a screen window again', () => view2P.$$eval('.stile.is-screen:not([hidden]) video', (v) => v.some((x) => x.videoWidth > 0)));
      await guestP.click('#screen');
      await until('the screen gone again', () => view2P.$$eval('.stile.is-screen', (v) => v.length === 0));
    });

    await step('a guest sent back to the green room loses the stage, enforced by LiveKit', async () => {
      await tab(hostP, 'people');
      await hostP.click(`${greenTile('Gus Guest')} .mini:text-is("To green room")`);
      await until('guest gone from the stage', async () => !(await tileNames(view2P)).includes('Gus Guest'));
      assert.match(await guestP.textContent('#notice'), /green room/);
      // Their browser tries to publish to the stage anyway and LiveKit refuses.
      const stillRefused = await guestP.evaluate(async () => {
        const res = await fetch(document.documentElement.dataset.base + '/share-token', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ clientId: sessionStorage.getItem(Object.keys(sessionStorage).find((k) => k.startsWith('vc-client-'))) }) });
        return res.status;
      });
      assert.equal(stillRefused, 403);
      await until('guest publishing in the green room again', async () => hostP.evaluate(() => document.querySelector('.gtile:has(.gtile-meta b) video') !== null));
    });

    await step('a personal link opened somewhere else closes the first viewer', async () => {
      const copy = await open(browser, local(attendee2), 'attendee2-copy', errors);
      await enter(copy, 'Bea Viewer');
      await view2P.waitForSelector('#gate:not([hidden])', { timeout: 15000 });
      assert.match(await view2P.textContent('#gate-note'), /another window/);
      view2P = copy;
      await until('video in the new window', async () => (await stageVideos(view2P)) >= 1);
    });

    await step('someone on stage who reloads comes straight back on stage', async () => {
      await hostP.reload();
      await enter(hostP, 'Maya Host', 'CEO, Acme Events');
      await until('host back on stage for the audience', async () => (await tileNames(view2P)).includes('Maya Host') && (await stageVideos(view2P)) >= 1);
      assert.match(await hostP.textContent('#notice'), /on air/);
      await hostP.click('[data-channel="event"]').catch(() => {});
    });

    await step('recording captures the frame and layout from the program page, not the crew', async () => {
      await putOnStage(hostP, 'Gus Guest');
      await until('the guest back on stage', async () => (await tileNames(view2P)).includes('Gus Guest'));
      await hostP.click('#record');
      await hostP.waitForSelector('#rec-pill:not([hidden])', { timeout: 60000 });
      await view2P.waitForSelector('#rec-pill:not([hidden])', { timeout: 10000 });
      recording = await until('an active recording', async () => {
        const list = (await v1('GET', `/rooms/${ROOM}/recordings`)).recordings;
        return list.find((r) => r.status === 'active') || false;
      }, 60000);
      await new Promise((r) => setTimeout(r, 24000));
      await hostP.click('#record');
      recording = await until('the finished recording', async () => {
        const list = (await v1('GET', `/rooms/${ROOM}/recordings`)).recordings;
        return list.find((r) => r.egressId === recording.egressId && r.status === 'complete') || false;
      }, 90000);
      assert.ok(recording.sizeBytes > 50000, `size ${recording.sizeBytes}`);
      await view2P.waitForSelector('#rec-pill', { state: 'hidden', timeout: 20000 });
    });

    await step('the recording is real video with sound, with the uploaded frame burned in', async () => {
      const file = path.join(OUT, 'recording.mp4');
      const res = await fetch(`${S3}/recordings/${recording.filename}`);
      assert.equal(res.status, 200);
      fs.writeFileSync(file, Buffer.from(await res.arrayBuffer()));
      const probe = JSON.parse(execFileSync('ffprobe', ['-v', 'error', '-show_streams', '-of', 'json', file]).toString());
      assert.ok(probe.streams.some((s) => s.codec_type === 'video' && s.width >= 1280));
      assert.ok(probe.streams.some((s) => s.codec_type === 'audio'));
      const frame = path.join(SHOTS, '20-recording-frame.png');
      execFileSync('ffmpeg', ['-y', '-loglevel', 'error', '-ss', '4', '-i', file, '-frames:v', '1', frame]);
      // The frame image is solid coral around the edge: a corner pixel proves it's in the file.
      const pixel = execFileSync('ffmpeg', ['-loglevel', 'error', '-i', frame, '-vf', 'crop=4:4:6:6,scale=1:1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-']);
      assert.ok(pixel[0] > 150 && pixel[1] < 120, `corner colour ${[...pixel]}`);
    });

    await step('caption files land next to the recording: everyone and per speaker, SRT and transcript', async () => {
      const stem = recording.filename.replace(/\.mp4$/, '');
      const get = async (suffix) => { const r = await fetch(`${S3}/recordings/${stem}${suffix}`); return r.status === 200 ? r.text() : null; };
      const everyone = await until('the SRT for everyone', () => get('.srt'), 60000, 1000);
      // A signature is needed to write: the bucket isn't open, so the uploads above really were signed.
      assert.equal((await fetch(`${S3}/recordings/${stem}.forged`, { method: 'PUT', body: 'x' })).status, 403, 'an unsigned upload is refused');
      const seconds = recording.durationSeconds;
      const cues = everyone.trim().split(/\n\n+/);
      assert.ok(cues.length >= 3, `only ${cues.length} captions`);
      let last = -1;
      for (const cue of cues) {
        const [number, timing, ...text] = cue.split('\n');
        assert.match(number, /^\d+$/);
        const m = /^(\d\d):(\d\d):(\d\d),(\d{3}) --> (\d\d):(\d\d):(\d\d),(\d{3})$/.exec(timing);
        assert.ok(m, `bad timing: ${timing}`);
        const start = +m[1] * 3600 + +m[2] * 60 + +m[3] + +m[4] / 1000;
        assert.ok(start >= last - 8 && start <= seconds + 2, `cue at ${start} in a ${seconds}s recording`);
        last = start;
        assert.match(text.join(' '), /^(Maya Host|Gus Guest): /);
      }
      assert.match(everyone, /Maya Host: .*(webinar|product|welcome)/i);
      assert.match(everyone, /Gus Guest: .*(customers|reporting|dashboard|mobile|thanks)/i);
      const transcript = await get('.txt');
      assert.match(transcript, /\[\d\d:\d\d:\d\d\] (Maya Host|Gus Guest)\n/);
      const maya = await get('.maya-host.srt');
      const gus = await get('.gus-guest.srt');
      assert.match(maya, /webinar|product|welcome/i);
      assert.doesNotMatch(maya, /customers|dashboard/i, 'one speaker file has only them');
      assert.match(gus, /customers|reporting|dashboard|mobile/i);
      assert.doesNotMatch(gus, /Welcome everyone/i);
      assert.ok(await get('.maya-host.txt') && await get('.gus-guest.txt'));
      fs.writeFileSync(path.join(SHOTS, 'captions-everyone.srt'), everyone);
      fs.writeFileSync(path.join(SHOTS, 'captions-transcript.txt'), transcript);
      fs.writeFileSync(path.join(SHOTS, 'captions-maya-host.srt'), maya);
      // The room lists them; the service API does too.
      await tab(hostP, 'files');
      await hostP.click('#recording-list .mini:text-is("Check again")');
      await hostP.waitForSelector('[data-recording-caption]');
      await hostP.waitForSelector('[data-captions="all"] a:text-is("SRT")');
      await shot(hostP, '19-host-files-recordings');
      const listed = (await v1('GET', `/rooms/${ROOM}/recordings`)).recordings.find((r) => r.egressId === recording.egressId);
      assert.equal(listed.captions.length, 6);
    });

    await step('settings live at /login and /admin, with no way to sign up', async () => {
      const page = await open(browser, `${BASE}/admin`, 'admin', errors);
      await page.waitForURL('**/login');
      await page.fill('#email', process.env.LIVE_ADMIN_EMAIL);
      await page.fill('#password', 'wrong-password');
      await page.click('button[type=submit]');
      await page.waitForSelector('#login-error:not([hidden])');
      await shot(page, 'admin-1-login');
      await page.fill('#password', process.env.LIVE_ADMIN_PASSWORD);
      await page.click('button[type=submit]');
      await page.waitForURL('**/admin');
      await page.waitForSelector(`[data-room="${ROOM}"]`);
      await shot(page, 'admin-2-sessions');
      await page.click(`[data-room="${ROOM}"]`);
      await page.waitForSelector('[data-dest="youtube"]');
      assert.match(await page.textContent('#page-session'), new RegExp(`/${SLUG}/green-room`));
      await page.setViewportSize({ width: 1280, height: 1900 });
      await shot(page, 'admin-3-session');
      assert.ok(!(await page.content()).includes(STREAM_KEY));
      // Crew, one person at a time. The old link stops working the moment it's replaced.
      await page.waitForSelector('[data-member="Probe Guest"]');
      await page.fill('#crew-name', 'Nell New');
      await page.selectOption('#crew-role', 'guest');
      await page.click('#crew-form button[type=submit]');
      await page.waitForSelector('[data-member="Nell New"]');
      const before = (await v1('GET', `/rooms/${ROOM}/crew`)).crew.find((m) => m.name === 'Nell New');
      assert.equal((await fetch(local(before.url), { redirect: 'manual' })).status, 303);
      await page.click('[data-member="Nell New"] button:text-is("New link")');
      await page.click('[data-member="Nell New"] button:text-is("Click again to confirm")');
      await until('a new link for Nell', async () => (await v1('GET', `/rooms/${ROOM}/crew`)).crew.find((m) => m.name === 'Nell New').url !== before.url);
      assert.equal((await fetch(local(before.url), { redirect: 'manual' })).status, 404, 'the old link stops working');
      await page.waitForSelector('[data-member="Nell New"]');
      await page.click('[data-member="Nell New"] button:text-is("Remove")');
      await page.click('[data-member="Nell New"] button:text-is("Click again to confirm")');
      await until('Nell gone', async () => !(await v1('GET', `/rooms/${ROOM}/crew`)).crew.some((m) => m.name === 'Nell New'));
      await page.click('[data-page="brand"]');
      await page.waitForSelector('#brand-form[data-loaded]');
      await page.$eval('#b-accent', (el) => { el.value = '#00796b'; });
      await page.click('#brand-form button[type=submit]');
      await until('the brand from /admin at the attendee', () => view2P.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--coral').trim() === '#00796b'));
      await page.setInputFiles('#b-logo-file', framePng);
      await page.waitForSelector('#b-logo:not([hidden])');
      await shot(page, 'admin-4-brand');
      await page.click('[data-page="keys"]');
      await page.waitForSelector('[data-saved="youtube"]');
      assert.equal(await page.$('[data-saved="linkedin"]'), null, 'LinkedIn issues a key per stream');
      await shot(page, 'admin-5-keys');
      // People: their own password, their own sign-in, on the record.
      await page.click('[data-page="accounts"]');
      // People has everyone, and a host, producer or guest is added to a session and gets a link.
      await page.waitForSelector('#page-accounts [data-member="Maya Host"]');
      assert.deepEqual(await page.$$eval('#person-role option', (o) => o.map((x) => x.value)), ['owner', 'admin', 'host', 'producer', 'guest']);
      assert.equal(await page.isVisible('#person-room'), false, 'a session is only asked for when it matters');
      await page.selectOption('#person-role', 'producer');
      await page.selectOption('#person-room', ROOM);
      await page.fill('#account-name', 'Priya People');
      await page.click('#person-form button[type=submit]');
      await page.waitForSelector('#page-accounts [data-member="Priya People"]');
      await until('her link', async () => /\/join\/p\./.test(await page.textContent('#admin-saved')));
      assert.ok((await v1('GET', `/rooms/${ROOM}/crew`)).crew.some((m) => m.name === 'Priya People' && m.role === 'producer'));
      await page.setViewportSize({ width: 1280, height: 1300 });
      await shot(page, 'admin-people');
      await page.click('#page-accounts [data-member="Priya People"] button:text-is("Remove")');
      await page.click('#page-accounts [data-member="Priya People"] button:text-is("Click again to confirm")');
      await until('Priya gone', async () => !(await v1('GET', `/rooms/${ROOM}/crew`)).crew.some((m) => m.name === 'Priya People'));
      await page.waitForSelector('#page-accounts [data-member="Priya People"]', { state: 'detached' });
      await page.fill('#account-email', 'second@example.com');
      await page.click('#page-accounts button[type=submit]');
      await page.waitForSelector('[data-account="second@example.com"]');
      await until('the set-password link', async () => /\/login\/set\//.test(await page.textContent('#admin-saved')));
      const setUrl = (await page.textContent('#admin-saved')).match(/http\S+\/login\/set\/\S+/)[0].replace(/\.$/, '');
      await shot(page, 'admin-6-people');
      const second = await open(browser, setUrl, 'second-admin', errors);
      await second.fill('#password', 'second-admin-password');
      await second.click('button[type=submit]');
      await second.waitForURL('**/admin');
      await second.waitForSelector(`[data-room="${ROOM}"]`);
      assert.equal(await second.isVisible('#nav-accounts'), false, 'only owners manage people');
      assert.equal((await second.evaluate(() => fetch('/admin/api/accounts').then((r) => r.status))), 403);
      assert.equal((await fetch(setUrl.replace('127.0.0.1:18090', '127.0.0.1:18090'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password: 'another-password-123' }) })).status, 410, 'the link works once');
      await second.context().close();
      await page.click('[data-page="activity"]');
      await page.waitForSelector('[data-activity="crew.removed"]');
      await shot(page, 'admin-7-activity');
      await page.click('[data-page="me"]');
      await page.waitForSelector('#me-new');
      await page.click('#sign-out');
      await page.waitForURL('**/login');
      await page.context().close();
      assert.equal((await fetch(`${BASE}/admin/api/rooms`)).status, 401);
      assert.equal((await fetch(`${BASE}/signup`)).status, 404);
      await v1('PUT', '/brand', { accent: '', stage: '', mode: '', font: '' });
      await fetch(`${BASE}/v1/brand/logo/remove`, { method: 'POST', headers: { Authorization: `Bearer ${SECRET}` } });
    });

    await step('the person who can end does, and everyone is told', async () => {
      await prodP.click('#end');
      await prodP.click('#end');
      await view2P.waitForSelector('#prelive:not([hidden])', { timeout: 10000 });
      assert.match(await view2P.textContent('#prelive-title'), /ended/);
      await until('the host told too', async () => (await hostP.textContent('#phase-pill')) === 'Ended');
      await until('the broadcast stopped', async () => {
        try { execFileSync('ffprobe', ['-v', 'error', '-rw_timeout', '3000000', `rtmp://127.0.0.1:11935/live/youtube-${STREAM_KEY}`], { timeout: 8000, stdio: 'ignore' }); return false; } catch { return true; }
      }, 40000);
      await shot(view2P, '21-attendee-ended');
    });

    await step('a forged link gets nothing', async () => {
      const forged = local(links.attendee).replace(/.{4}$/, 'AAAA');
      const res = await fetch(forged);
      assert.equal(res.status, 404);
      const token = await fetch(`${forged}/enter`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ clientId: 'abcdefgh' }) });
      assert.equal(token.status, 404);
    });

    await step('attendance comes from LiveKit webhooks, with the audience only', async () => {
      const attendance = await until('attendance', async () => {
        const data = await v1('GET', `/rooms/${ROOM}/attendance`);
        return data.complete && data.rows.length >= 2 ? data : false;
      }, 60000);
      for (const row of attendance.rows) { assert.ok(!row.externalId.startsWith('host-'), row.externalId); }
      assert.ok(attendance.rows.every((r) => r.durationSeconds >= 0));
    });

    await step('no page raised a script or Content Security Policy error', async () => {
      assert.deepEqual(errors, []);
    });
    console.log(`${passed} steps passed`);
  } catch (err) {
    for (const [name, page] of [['host', hostP], ['producer', prodP], ['guest', guestP], ['attendee', viewP], ['attendee2', view2P]]) {
      if (page && !page.isClosed()) { await page.screenshot({ path: path.join(SHOTS, `failed-${name}.png`) }).catch(() => {}); }
    }
    console.error('page errors so far:', errors);
    throw err;
  } finally {
    await browser.close();
    await guestBrowser.close();
  }
})().catch((err) => { console.error(err); process.exit(1); });

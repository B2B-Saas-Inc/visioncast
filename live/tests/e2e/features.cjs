'use strict';
/*
 * The rest of the studio against the real stack in compose.yaml: reactions, private questions,
 * scheduled polls, surveys, saved layouts, slides, backgrounds, device changes, breakouts, RTMP
 * input, translated captions, each speaker's own recording, fonts, and simulive. Run through run.sh.
 */
const assert = require('node:assert/strict');
const { execFileSync, spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright-core');

const BASE = process.env.LIVE_BASE;
const SECRET = process.env.LIVE_SERVICE_SECRET;
const PUBLIC = process.env.LIVE_PUBLIC_URL;
const OUT = process.env.E2E_OUT || os.tmpdir();
const SHOTS = path.join(OUT, 'shots');
fs.mkdirSync(SHOTS, { recursive: true });
const STAMP = Date.now();
const ROOM = `vc-features-${STAMP}`;
const SIM = `vc-simulive-${STAMP}`;
const HUB = `vc-hub-${STAMP}`;

// A two-page PDF, written out by hand so the test needs no PDF library.
function twoPagePdf() {
  const page = (text) => `BT /F1 48 Tf 100 400 Td (${text}) Tj ET`;
  const streams = [page('First PDF page'), page('Second PDF page')];
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 960 540] /Contents 6 0 R /Resources << /Font << /F1 5 0 R >> >> >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 960 540] /Contents 7 0 R /Resources << /Font << /F1 5 0 R >> >> >>',
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    ...streams.map((body) => `<< /Length ${body.length} >>\nstream\n${body}\nendstream`),
  ];
  let out = '%PDF-1.4\n';
  const offsets = objects.map((body, index) => { const at = out.length; out += `${index + 1} 0 obj\n${body}\nendobj\n`; return at; });
  const xref = out.length;
  out += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n${offsets.map((at) => `${String(at).padStart(10, '0')} 00000 n \n`).join('')}`;
  return `${out}trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
}

let passed = 0;
async function step(name, fn) {
  const started = Date.now();
  await fn();
  passed += 1;
  console.log(`ok   ${name} (${Date.now() - started}ms)`);
}
async function v1(method, url, body) {
  const res = await fetch(`${BASE}/v1${url}`, { method, headers: { Authorization: `Bearer ${SECRET}`, 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
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
  const context = await browser.newContext({ permissions: ['camera', 'microphone'], viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  // Keeps each page's connections, so a step can read what's really being sent.
  await page.addInitScript(() => {
    const Real = window.RTCPeerConnection;
    window.__connections = [];
    window.RTCPeerConnection = function (...args) { const pc = new Real(...args); window.__connections.push(pc); return pc; };
    window.RTCPeerConnection.prototype = Real.prototype;
  });
  page.on('pageerror', (e) => errors.push(`${who}: ${e.message}`));
  page.on('console', (m) => { if (m.type() === 'error' && /Content Security Policy|Refused/.test(m.text())) errors.push(`${who}: ${m.text()}`); });
  await page.goto(url);
  return page;
}
async function enter(page, name) {
  await page.waitForSelector('#join-form:not([hidden])');
  await page.fill('#name', name);
  await page.click('#join');
  await page.waitForSelector('#room:not([hidden])', { timeout: 20000 });
}
const tab = (page, name) => page.click(`[data-tab="${name}"]`);
const shot = (page, name) => page.screenshot({ path: path.join(SHOTS, `features-${name}.png`) });
const stageVideos = (page) => page.evaluate(async () => {
  const videos = Array.from(document.querySelectorAll('.stile:not([hidden]) video')).filter((v) => v.videoWidth > 0);
  const before = videos.map((v) => v.currentTime);
  await new Promise((r) => setTimeout(r, 700));
  return videos.filter((v, i) => v.currentTime > before[i]).length;
});
const ID_A = '11aa1b6c-8d4e-4f0a-9b1c-2d3e4f5a6b7c';
const ID_B = '22bb1b6c-8d4e-4f0a-9b1c-2d3e4f5a6b7c';

(async () => {
  const errors = [];
  const browser = await chromium.launch({
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE,
    args: ['--use-fake-device-for-media-stream=device-count=2', '--use-fake-ui-for-media-stream', '--autoplay-policy=no-user-gesture-required', `--use-file-for-fake-audio-capture=${process.env.SPEECH_HOST}`],
  });
  const now = Date.now();
  const clip = path.join(OUT, 'sim.mp4');
  const banner = path.join(OUT, 'banner.png');
  const deck = path.join(OUT, 'deck.pptx');
  execFileSync('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'lavfi', '-i', 'testsrc=duration=8:size=640x360:rate=24', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=8', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', '-movflags', '+faststart', clip]);
  execFileSync('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'lavfi', '-i', 'color=c=0x1565C0:s=800x300', '-frames:v', '1', banner]);
  execFileSync(process.env.DECK_PYTHON || 'python3', [path.join(__dirname, 'make_deck.py'), deck]);
  let host, a, b, crew;
  try {
    await step('a room, a host, and two attendees in the lobby', async () => {
      await v1('PUT', `/rooms/${ROOM}`, { title: 'Features', startsAt: new Date(now + 300000).toISOString(), endsAt: new Date(now + 3600000).toISOString(), slug: `features-${STAMP}` });
      crew = await v1('POST', `/rooms/${ROOM}/crew`, { role: 'host', name: 'Maya Host' });
      host = await open(browser, local(crew.url), 'host', errors);
      await enter(host, 'Maya Host');
      a = await open(browser, local((await v1('POST', `/rooms/${ROOM}/tickets`, { identity: ID_A })).joinUrl), 'a', errors);
      await enter(a, 'Ada');
      b = await open(browser, local((await v1('POST', `/rooms/${ROOM}/tickets`, { identity: ID_B })).joinUrl), 'b', errors);
      await enter(b, 'Bo');
    });

    await step('a banner shows attendees while they wait, and goes when the session is live', async () => {
      await tab(host, 'look');
      await host.setInputFiles('[data-upload="banner"]', banner);
      await until('the banner at the attendee', () => a.evaluate(() => { const i = document.getElementById('prelive-banner'); return !i.hidden && i.naturalWidth === 800; }));
      await shot(a, 'banner');
    });

    await step('layouts are saved by name and come back', async () => {
      await host.click('[data-layout="speaker"]');
      await host.fill('#layout-name', 'Interview');
      await host.click('#saved-layouts .mini:text-is("Save layout")');
      await host.waitForSelector('[data-saved-layout="Interview"]');
      await host.click('[data-layout="grid"]');
      await until('grid', () => host.evaluate(() => document.querySelector('[data-layout="grid"]').getAttribute('aria-pressed') === 'true'));
      await host.click('[data-saved-layout="Interview"]');
      await until('speaker back', () => host.evaluate(() => document.querySelector('[data-layout="speaker"]').getAttribute('aria-pressed') === 'true'));
    });

    await step('a person on stage, live, with sound and video reaching attendees', async () => {
      await host.click('.gtile .mini:text-is("Put on stage")');
      await until('host on stage', async () => (await stageVideos(host)) > 0);
      await host.click('#go-live');
      await until('video at attendee', async () => (await stageVideos(a)) > 0);
      assert.equal(await a.isVisible('#prelive-banner'), false);
    });

    await step('a speaker sends 1080p in three layers, with 128 kbps stereo audio', async () => {
      const sent = await until('a picture and sound going out', () => host.evaluate(() => {
        const live = window.__connections.flatMap((pc) => pc.getSenders()).filter((s) => s.track && s.track.readyState === 'live');
        const video = live.find((s) => s.track.kind === 'video');
        const audio = live.find((s) => s.track.kind === 'audio');
        if (!video || !audio) return null;
        return {
          width: video.track.getSettings().width,
          height: video.track.getSettings().height,
          layers: video.getParameters().encodings.map((e) => e.maxBitrate).sort((x, y) => x - y),
          audio: audio.getParameters().encodings[0].maxBitrate,
          stereo: window.__connections.some((pc) => /stereo=1/.test(pc.localDescription ? pc.localDescription.sdp : '')),
        };
      }));
      assert.deepEqual([sent.width, sent.height], [1920, 1080], JSON.stringify(sent));
      assert.deepEqual(sent.layers, [450000, 1700000, 3000000], JSON.stringify(sent));
      assert.equal(sent.audio, 128000, JSON.stringify(sent));
      assert.ok(sent.stereo, 'the audio is offered in stereo');
      console.log(`     sending: ${JSON.stringify(sent)}`);
    });

    await step('a device changes mid-session without leaving the stage', async () => {
      await host.click('#devices-btn');
      await until('two cameras listed', () => host.evaluate(() => document.getElementById('live-cam').options.length >= 2));
      const second = await host.$eval('#live-cam', (s) => s.options[1].value);
      await host.selectOption('#live-cam', second);
      await until('video still arriving', async () => (await stageVideos(a)) > 0);
      assert.equal(await host.isVisible('#room-error'), false);
    });

    await step('a blurred background is drawn with files from this server, and goes out', async () => {
      const external = [];
      host.on('request', (r) => { if (!r.url().startsWith(BASE) && /^https?:/.test(r.url()) && !r.url().includes('172.30.77.10') && !r.url().includes('127.0.0.1:17880')) external.push(r.url()); });
      const wasm = host.waitForResponse((r) => r.url().endsWith('/assets/vbg/vision_wasm_internal.wasm') && r.status() === 200, { timeout: 30000 });
      await host.selectOption('#vbg', 'blur');
      await wasm;
      await until('the processor running', () => host.evaluate(() => document.documentElement.dataset.background === 'blur'), 30000);
      await until('video still arriving', async () => (await stageVideos(a)) > 0);
      assert.deepEqual(external, [], 'nothing from a CDN');
      await shot(host, 'background');
      await host.selectOption('#vbg', '');
      await until('the processor gone', () => host.evaluate(() => document.documentElement.dataset.background === ''));
    });

    await step('attendees react to a chat message; a producer can put it on the picture', async () => {
      await a.fill('#chat-input', 'Great to be here');
      await a.click('#chat-form button[type=submit]');
      await b.waitForSelector('#messages li');
      const li = b.locator('#messages li').first();
      await li.locator('[data-react-open]').click();
      await li.locator('[data-react="🎉"]').click();
      await until('the count at the other attendee', () => a.evaluate(() => (document.querySelector('[data-react="🎉"]') || {}).textContent === '🎉 1'));
      await b.locator('#messages li').first().locator('[data-react="🎉"]').click();
      await until('taken back', () => a.evaluate(() => !document.querySelector('[data-react="🎉"]')));
      await tab(host, 'chat');
      await host.locator('#messages li .mini:text-is("On stage")').first().click();
      await until('on the picture', () => a.evaluate(() => { const t = document.querySelector('.pic-third'); return t && !t.hidden && t.textContent.includes('Great to be here'); }));
    });

    await step('captions sit above a lower third, and a viewer can read them in Spanish', async () => {
      await until('captions lifted', () => a.evaluate(() => document.querySelector('.pic-captions').style.bottom === '24%'));
      await host.click('[data-tab="look"]');
      await a.click('#cc');
      await a.waitForSelector('#cc-language:not([hidden])');
      await a.selectOption('#cc-language', 'es');
      await until('a Spanish caption', () => a.evaluate(() => { const c = document.querySelector('.pic-captions p'); return c && /\b(el|la|los|las|de|nuevo|producto|bienvenidos|todos|hoy|cómo)\b/i.test(c.textContent) && !/\b(the|everyone|today)\b/i.test(c.textContent); }), 90000, 200);
      await shot(a, 'spanish-captions');
      await a.selectOption('#cc-language', '');
      await a.click('#cc');
    });

    await step('private questions stay between the asker and the crew', async () => {
      await tab(host, 'settings');
      await host.check('[data-setting="qaPrivate"]');
      await until('setting on', () => a.evaluate(() => true));
      await new Promise((r) => setTimeout(r, 500));
      await tab(a, 'qa'); await tab(b, 'qa');
      await a.fill('#qa-input', 'An anonymous worry');
      await a.check('#qa-anon');
      await a.click('#qa-form button[type=submit]');
      await a.waitForSelector('#questions li:has-text("An anonymous worry")');
      await a.uncheck('#qa-anon');
      await new Promise((r) => setTimeout(r, 5200));
      await a.fill('#qa-input', 'A private matter');
      await a.click('#qa-form button[type=submit]');
      await a.waitForSelector('#questions li:has-text("A private matter")');
      await tab(host, 'qa');
      await host.waitForSelector('#questions li:has-text("A private matter") .badge:text-is("private")');
      await new Promise((r) => setTimeout(r, 1500));
      assert.equal(await b.locator('#questions li:has-text("A private matter")').count(), 0, 'another attendee does not see it');
      const res = await b.evaluate(async () => { const id = 'nope'; return (await fetch(document.documentElement.dataset.base + '/act', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: { type: 'qa_vote', id } }) })).status; });
      assert.equal(res, 404);
      await host.locator('#questions li:has-text("A private matter") .mini:text-is("Show to everyone")').click();
      await b.waitForSelector('#questions li:has-text("A private matter")');
      await host.locator('#questions li:has-text("A private matter") .mini:text-is("Make private")').click();
      await until('private again', async () => (await b.locator('#questions li:has-text("A private matter")').count()) === 0);
      await tab(host, 'settings');
      await host.uncheck('[data-setting="qaPrivate"]');
    });

    await step('a scheduled poll opens by itself, pops up, takes votes and closes by itself', async () => {
      await tab(host, 'polls');
      await host.click('#poll-editor summary');
      await host.fill('#poll-q', 'Which one?');
      const options = await host.$$('.poll-opt');
      await options[0].fill('This');
      await options[1].fill('That');
      await host.fill('#poll-open-after', '0');
      await host.fill('#poll-close-after', '10');
      await host.click('#poll-form button[type=submit]');
      await a.waitForSelector('#poll-pop:not([hidden]) .poll-row', { timeout: 15000 });
      await shot(a, 'poll-popup');
      await a.click('#poll-pop .poll-row:has-text("That")');
      await until('popup gone after voting', () => a.evaluate(() => document.getElementById('poll-pop').hidden));
      await b.waitForSelector('#poll-pop:not([hidden])');
      await b.click('#poll-pop .pop-close');
      await until('popup dismissed', () => b.evaluate(() => document.getElementById('poll-pop').hidden));
      await host.waitForSelector('#poll-list .box:has-text("Closed")', { timeout: 20000 });
    });

    await step('a PowerPoint becomes slides: notes for the crew only, and nobody reads ahead', async () => {
      await tab(host, 'look');
      await host.setInputFiles('[data-upload="deck"]', deck);
      await host.waitForSelector('[data-deck="deck.pptx"][data-status="ready"]', { timeout: 180000 });
      await host.click('[data-deck="deck.pptx"] .mini:text-is("Show on stage")');
      await until('page 1 at the attendee', () => a.evaluate(() => { const i = document.querySelector('.stile-slide'); return i && i.naturalWidth === 1920 && i.src.endsWith('/pages/1'); }));
      await host.waitForSelector('#slide-notes:has-text("Remember to welcome everyone")');
      assert.equal(await a.isVisible('#slide-notes'), false);
      assert.equal(await a.isVisible('#slide-controls'), false);
      const peek = await a.evaluate(async () => { const src = document.querySelector('.stile-slide').src; return [(await fetch(src)).status, (await fetch(src.replace('/pages/1', '/pages/2'))).status]; });
      assert.deepEqual(peek, [200, 404], 'an attendee gets only the page on stage');
      await host.click('#slide-next');
      await until('page 2 at the attendee', () => a.evaluate(() => document.querySelector('.stile-slide').src.endsWith('/pages/2')));
      await host.waitForSelector('#slide-notes:has-text("Mention the launch date")');
      await shot(a, 'slides');
      await host.click('[data-deck="deck.pptx"] .mini:text-is("Take off stage")');
      await until('slides gone', () => a.evaluate(() => !document.querySelector('.stile-slide')));
    });

    await step('a PDF becomes slides, page by page', async () => {
      const pdf = path.join(OUT, 'talk.pdf');
      fs.writeFileSync(pdf, twoPagePdf());
      await tab(host, 'look');
      await host.setInputFiles('[data-upload="deck"]', pdf);
      await host.waitForSelector('[data-deck="talk.pdf"][data-status="ready"]', { timeout: 120000 });
      await host.click('[data-deck="talk.pdf"] .mini:text-is("Show on stage")');
      await until('page 1 at the attendee', () => a.evaluate(() => { const i = document.querySelector('.stile-slide'); return i && i.naturalWidth === 1920 && i.src.endsWith('/pages/1'); }));
      await host.click('#slide-next');
      await until('page 2 at the attendee', () => a.evaluate(() => document.querySelector('.stile-slide').src.endsWith('/pages/2')));
      await host.click('[data-deck="talk.pdf"] .mini:text-is("Take off stage")');
      await until('slides gone', () => a.evaluate(() => !document.querySelector('.stile-slide')));
    });

    await step('breakouts: each attendee can only get into their own room, sees and hears who is in it, and comes back', async () => {
      await tab(host, 'rooms');
      await host.selectOption('#br-kind', 'assigned');
      await host.fill('#br-count', '2');
      await host.click('#br-form button[type=submit]');
      await a.waitForSelector('[data-tab="rooms"]');
      await tab(a, 'rooms');
      await tab(b, 'rooms');
      // Two rooms, two attendees: each sees only their own, and the server refuses the other.
      await a.waitForSelector('#rooms .file .mini:text-is("Join")');
      await b.waitForSelector('#rooms .file .mini:text-is("Join")');
      assert.equal(await a.locator('#rooms .file').count(), 1);
      assert.equal(await b.locator('#rooms .file').count(), 1);
      const aRoom = await a.getAttribute('#rooms .file', 'data-breakout-room');
      const other = aRoom === 'Room 1' ? '2' : '1';
      const refused = await a.evaluate(async (id) => (await fetch(document.documentElement.dataset.base + '/act', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: { type: 'breakout_join', id } }) })).status, other);
      assert.equal(refused, 403, 'an attendee is refused a room they were not assigned');
      // The host goes into the attendee's room: the crew can visit any of them.
      await a.click('#rooms .file .mini:text-is("Join")');
      await a.waitForSelector('#breakout:not([hidden])');
      assert.equal(await a.isVisible('#stage'), false);
      await a.click('#br-cam');
      await a.click('#br-mic');
      await host.click('.gtile .mini:text-is("To green room")');
      await until('host off stage', () => host.evaluate(() => !document.querySelector('.gtile.on-stage')));
      await tab(host, 'rooms');
      await host.click(`[data-breakout-room="${aRoom}"] .mini:text-is("Join")`);
      await host.waitForSelector('#breakout:not([hidden])');
      await until('two people in the room', async () => (await host.locator('.br-tile').count()) === 2);
      await until('video from the other person', () => host.evaluate(() => Array.from(document.querySelectorAll('.br-tile video')).some((v) => v.videoWidth > 0)));
      await until('sound from the other person', () => host.evaluate(() => Array.from(document.querySelectorAll('audio[data-breakout]')).some((el) => el.srcObject && el.srcObject.getAudioTracks().length && !el.muted)));
      await until('the stage is muted while inside', () => host.evaluate(() => Array.from(document.getElementById('audio-sink').children).every((el) => el.muted)));
      await shot(host, 'breakout');
      await until('host sees two inside', () => host.evaluate((name) => /2 people/.test(document.querySelector(`[data-breakout-room="${name}"]`).textContent), aRoom));
      await host.click('#br-leave');
      await host.waitForSelector('#breakout', { state: 'hidden' });
      await host.click('#rooms .mini:text-is("Close all rooms")');
      await host.click('#rooms .mini:text-is("Click again to confirm")');
      await a.waitForSelector('#breakout', { state: 'hidden', timeout: 15000 });
      await host.click('.gtile .mini:text-is("Put on stage")');
      await until('back on the stage', async () => (await stageVideos(a)) > 0);
    });

    await step('a closed list of assignments stays private: attendees never get each other\'s identities', async () => {
      const state = await a.evaluate(() => new Promise((resolve) => {
        const source = new EventSource(document.documentElement.dataset.base + '/events');
        source.addEventListener('snapshot', (e) => { source.close(); resolve(e.data); });
      }));
      assert.ok(!state.includes(ID_B), 'one attendee sees another\'s identity');
    });

    await step('an encoder pushing RTMP goes on stage while its stream runs', async () => {
      await tab(host, 'broadcast');
      await host.fill('#ingress-name', 'Main camera');
      await host.click('#ingress-box .mini:text-is("Create an input")');
      await host.waitForSelector('[data-ingress="Main camera"]');
      const shown = await host.textContent('[data-ingress="Main camera"]');
      const key = /Stream key: (\S+?)(Hide|$)/.exec(shown)[1];
      const url = /rtmp:\/\/\S+?\/x/.exec(shown)[0];
      const ffmpeg = spawn('ffmpeg', ['-re', '-loglevel', 'error', '-f', 'lavfi', '-i', 'testsrc=size=640x360:rate=24', '-f', 'lavfi', '-i', 'sine=frequency=220', '-c:v', 'libx264', '-preset', 'veryfast', '-tune', 'zerolatency', '-g', '48', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-f', 'flv', `${url}/${key}`], { stdio: 'ignore' });
      try {
        await until('the encoder on stage at the attendee', async () => {
          const names = await a.$$eval('.stile:not([hidden]) .stile-label b', (els) => els.map((e) => e.textContent));
          return names.includes('Main camera') && (await stageVideos(a)) >= 2;
        }, 60000);
        await shot(a, 'encoder');
      } finally { ffmpeg.kill('SIGTERM'); }
      await until('off stage when the stream stops', async () => !(await a.$$eval('.stile:not([hidden]) .stile-label b', (els) => els.map((e) => e.textContent))).includes('Main camera'), 60000);
      await host.click('[data-ingress="Main camera"] .mini:text-is("Delete")');
      await host.click('[data-ingress="Main camera"] .mini:text-is("Click again to confirm")');
      await host.waitForSelector('[data-ingress="Main camera"]', { state: 'detached' });
    });

    await step('each speaker records their own copy, in full quality, next to the main recording', async () => {
      await tab(host, 'settings');
      await host.check('[data-setting="localRecordings"]');
      await new Promise((r) => setTimeout(r, 500));
      await host.click('#record');
      await a.waitForSelector('#rec-pill:not([hidden])', { timeout: 45000 });
      await new Promise((r) => setTimeout(r, 24000));
      await host.click('#record');
      await a.waitForSelector('#rec-pill', { state: 'hidden', timeout: 45000 });
      await tab(host, 'files');
      await host.click('#recording-list .mini:text-is("Check again")');
      const link = await host.waitForSelector('[data-local-recording="Maya Host"] a', { timeout: 30000 });
      const href = await link.getAttribute('href');
      const file = path.join(OUT, 'local.webm');
      await until('the file finished', async () => {
        await host.click('#recording-list .mini:text-is("Check again")');
        const text = await host.textContent('[data-local-recording="Maya Host"]');
        return !text.includes('still uploading');
      }, 30000);
      const bytes = await host.evaluate(async (u) => Array.from(new Uint8Array(await (await fetch(u)).arrayBuffer())), href);
      fs.writeFileSync(file, Buffer.from(bytes));
      const probe = JSON.parse(execFileSync('ffprobe', ['-v', 'error', '-show_streams', '-of', 'json', file]));
      const kinds = probe.streams.map((s) => s.codec_type).sort();
      assert.deepEqual(kinds, ['audio', 'video']);
      assert.equal(probe.streams.find((s) => s.codec_type === 'video').width, 1920, 'a speaker keeps a full 1080p copy');
      const refused = await a.evaluate(async (u) => (await fetch(u.replace(/^\/join\/[^/]+/, document.documentElement.dataset.base))).status, href);
      assert.equal(refused, 403, 'an attendee cannot download it');
    });

    await step('captions line up with the sound in the recording, with one speaker to measure', async () => {
      const S3 = process.env.S3_BASE;
      const recording = await until('the recording finished', async () => (await v1('GET', `/rooms/${ROOM}/recordings`)).recordings.find((r) => r.status === 'complete'), 60000, 1000);
      const stem = recording.filename.replace(/\.mp4$/, '');
      const srt = await until('the SRT', async () => { const r = await fetch(`${S3}/recordings/${stem}.srt`); return r.status === 200 ? r.text() : null; }, 60000, 1000);
      const mp4 = path.join(OUT, 'sync.mp4');
      fs.writeFileSync(mp4, Buffer.from(await (await fetch(`${S3}/recordings/${recording.filename}`)).arrayBuffer()));
      const silence = execFileSync('sh', ['-c', `ffmpeg -nostats -i "${mp4}" -af silencedetect=noise=-40dB:d=0.5 -f null - 2>&1 || true`]).toString();
      const starts = [...silence.matchAll(/silence_end: ([\d.]+)/g)].map((m) => Number(m[1]));
      // A recording can begin mid-sentence: with no silence before it, that speech starts at zero.
      const firstQuiet = /silence_start: ([\d.]+)/.exec(silence);
      if (firstQuiet && (!starts.length || Number(firstQuiet[1]) < starts[0])) starts.unshift(0);
      const cues = srt.trim().split(/\n\n+/).map((cue) => { const m = /(\d\d):(\d\d):(\d\d),(\d{3}) -->/.exec(cue); return +m[1] * 3600 + +m[2] * 60 + +m[3] + +m[4] / 1000; });
      assert.ok(starts.length >= 2 && cues.length >= 2, `not enough to compare: ${starts.length} speech starts, ${cues.length} cues`);
      const offsets = cues.map((at) => Math.min(...starts.map((s) => Math.abs(s - at))));
      const worst = Math.max(...offsets);
      console.log(`     captions vs sound: worst ${worst.toFixed(2)}s, average ${(offsets.reduce((x, y) => x + y, 0) / offsets.length).toFixed(2)}s, ${cues.length} cues, ${starts.length} speech starts`);
      assert.ok(worst < 2, `a caption is ${worst}s from the speech it belongs to: ${offsets}`);
    });

    await step('a survey goes up when the session ends, and its answers come back', async () => {
      await tab(host, 'polls');
      await host.waitForSelector('#survey-editor:not([hidden])');
      await host.fill('#survey-title', 'How was it?');
      await host.fill('.sq-text', 'Rate the session');
      await host.click('#survey-editor .mini:text-is("Add a question")');
      const texts = await host.$$('.sq-text');
      await texts[1].fill('Anything else?');
      const kinds = await host.$$('.sq-kind');
      await kinds[1].selectOption('text');
      await host.click('#survey-editor .mini:text-is("Save survey")');
      await host.waitForSelector('#survey-editor .mini:text-is("Remove the survey")');
      assert.equal(await a.isVisible('#survey-box'), false, 'not until the end');
      await host.click('#end'); await host.click('#end');
      await a.waitForSelector('#survey-form');
      await shot(a, 'survey');
      await a.check('#survey-form input[value="5"]');
      await a.fill('#survey-form textarea', 'Loved it');
      await a.click('#survey-form button[type=submit]');
      await a.waitForSelector('#survey-box:has-text("Thanks")');
      await host.click('#survey-editor .mini:text-is("See answers")');
      await host.waitForSelector('[data-survey-count="1"]');
      assert.match(await host.textContent('#survey-editor'), /Average 5\.0/);
    });

    await step('what each attendee did comes out for the CRM, and anonymous stays anonymous', async () => {
      const data = await v1('GET', `/rooms/${ROOM}/engagement`);
      const row = data.people[ID_A];
      assert.equal(row.chatMessages, 1);
      assert.equal(row.chat[0].text, 'Great to be here');
      assert.deepEqual(row.questions.map((q) => q.text), ['A private matter']);
      assert.ok(!JSON.stringify(data).includes('An anonymous worry'), 'an anonymous question is not tied to anyone');
      assert.deepEqual(row.polls, [{ question: 'Which one?', answer: 'That' }]);
      assert.equal(row.survey['Rate the session'], '5');
      assert.equal(row.survey['Anything else?'], 'Loved it');
      assert.ok(!Object.keys(data.people).some((id) => id.startsWith('host-')), 'the crew are not attendees');
    });

    await step('settings can be read and changed from outside the room', async () => {
      const before = await v1('GET', `/rooms/${ROOM}/settings`);
      assert.equal(before.settings.localRecordings, true);
      const after = await v1('PATCH', `/rooms/${ROOM}/settings`, { settings: { ...before.settings, chatEnabled: false }, theme: { accent: '#112233', stage: '', mode: '', font: '' } });
      assert.equal(after.settings.chatEnabled, false);
      assert.equal(after.theme.accent, '#112233');
      await until('at the attendee', () => a.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--coral').trim() === '#112233'));
    });

    await step('an uploaded font is used by the room', async () => {
      const font = ['/System/Library/Fonts/Supplemental/Arial.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'].find((f) => fs.existsSync(f));
      assert.ok(font, 'no font file on this machine to test with');
      const up = await fetch(`${BASE}/v1/brand/font`, { method: 'PUT', headers: { Authorization: `Bearer ${SECRET}` }, body: fs.readFileSync(font) });
      assert.equal(up.status, 200);
      const refused = await fetch(`${BASE}/v1/brand/font`, { method: 'PUT', headers: { Authorization: `Bearer ${SECRET}` }, body: '<svg onload=alert(1)>' });
      assert.equal(refused.status, 415);
      await v1('PATCH', `/rooms/${ROOM}/settings`, { theme: { accent: '', stage: '', mode: '', font: 'custom' } });
      await until('the font loaded', () => a.evaluate(() => Array.from(document.fonts).some((f) => f.family.replace(/"/g, '') === 'VC Brand' && f.status === 'loaded')));
      await until('the font in use', () => a.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--font').includes('VC Brand')));
      await fetch(`${BASE}/v1/brand/font/remove`, { method: 'POST', headers: { Authorization: `Bearer ${SECRET}` } });
    });

    await step('a networking hub: people choose a table, and the tables stay open after the session ends', async () => {
      await v1('PUT', `/rooms/${HUB}`, { title: 'Hub', startsAt: new Date(now + 300000).toISOString(), endsAt: new Date(now + 3600000).toISOString(), slug: `hub-${STAMP}` });
      const h = await open(browser, local((await v1('POST', `/rooms/${HUB}/crew`, { role: 'host', name: 'Maya' })).url), 'hub-host', errors);
      await enter(h, 'Maya');
      const v = await open(browser, local((await v1('POST', `/rooms/${HUB}/tickets`, { identity: ID_A })).joinUrl), 'hub-viewer', errors);
      await enter(v, 'Ada');
      await until('the host in the green room', () => h.$('.gtile .mini'));
      await h.click('.gtile .mini:text-is("Put on stage")');
      await h.click('#go-live');
      await until('live', () => v.evaluate(() => document.getElementById('phase-pill').textContent === 'Live'));
      await tab(h, 'rooms');
      await h.selectOption('#br-kind', 'hub');
      await h.fill('#br-count', '2');
      await h.click('#br-form button[type=submit]');
      await v.waitForSelector('[data-tab="rooms"]');
      await tab(v, 'rooms');
      await v.waitForSelector('#rooms .file[data-breakout-room="Table 2"] .mini:text-is("Join")');
      assert.equal(await v.locator('#rooms .file').count(), 2, 'anyone can see every table');
      await h.click('#end'); await h.click('#end');
      await until('the session over', () => v.evaluate(() => document.getElementById('phase-pill').textContent !== 'Live'));
      await tab(v, 'rooms');
      await v.click('#rooms .file[data-breakout-room="Table 1"] .mini:text-is("Join")');
      await v.waitForSelector('#breakout:not([hidden])');
      await shot(v, 'hub-after-the-session');
      await tab(h, 'rooms');
      await h.click('#rooms .mini:text-is("Close all rooms")');
      await h.click('#rooms .mini:text-is("Click again to confirm")');
      await v.waitForSelector('#breakout', { state: 'hidden', timeout: 15000 });
      await h.context().close();
      await v.context().close();
    });

    await step('simulive: a recorded video goes live by itself, plays for everyone, and ends itself', async () => {
      await v1('PUT', `/rooms/${SIM}`, { title: 'Simulive', startsAt: new Date(now + 300000).toISOString(), endsAt: new Date(now + 3600000).toISOString(), slug: `sim-${STAMP}` });
      const simHost = await v1('POST', `/rooms/${SIM}/crew`, { role: 'host', name: 'Maya' });
      const h = await open(browser, local(simHost.url), 'sim-host', errors);
      await enter(h, 'Maya');
      const v = await open(browser, local((await v1('POST', `/rooms/${SIM}/tickets`, { identity: ID_A })).joinUrl), 'sim-viewer', errors);
      await enter(v, 'Ada');
      await tab(h, 'look');
      await h.setInputFiles('[data-upload="media"]', clip);
      await until('the video listed', () => h.evaluate(() => document.getElementById('sim-file').options.length === 1));
      const when = new Date(Date.now() + 4000 + 60000 - (Date.now() % 60000));
      await h.fill('#sim-at', new Date(Date.now() - 3600000 - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16));
      await h.click('#sim-box .mini:text-is("Schedule it")');
      await h.waitForSelector('#room-error:has-text("already passed")');
      const local16 = new Date(when.getTime() - when.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
      await h.fill('#sim-at', local16);
      await h.click('#sim-box .mini:text-is("Schedule it")');
      await h.waitForSelector('[data-sim="waiting"]');
      assert.equal(await v.evaluate(() => document.getElementById('phase-pill').textContent === 'Live'), false);
      await until('live by itself', () => v.evaluate(() => document.getElementById('phase-pill').textContent === 'Live'), 75000);
      await until('the video playing at the attendee', async () => (await stageVideos(v)) > 0, 20000);
      await shot(v, 'simulive');
      await until('over by itself', () => v.evaluate(() => /ended/.test(document.getElementById('prelive-title').textContent)), 30000);
      await h.context().close(); await v.context().close();
    });

    assert.deepEqual(errors, []);
    console.log(`\n${passed} steps passed`);
  } finally {
    await browser.close();
  }
})().catch((err) => { console.error(err); process.exit(1); });

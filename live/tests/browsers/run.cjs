'use strict';
/*
 * Other browsers and phones against the real stack: attendees on Firefox, Safari's engine and phone
 * screens, and a host on Firefox. Run through tests/e2e/run.sh with E2E_SUITE=browsers. Playwright's
 * WebKit is close to Safari but isn't Safari, and a phone screen in a desktop browser isn't a phone.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const pw = require(process.env.PLAYWRIGHT_MODULE || 'playwright-core');

const BASE = process.env.LIVE_BASE;
const SECRET = process.env.LIVE_SERVICE_SECRET;
const PUBLIC = process.env.LIVE_PUBLIC_URL;
const OUT = process.env.E2E_OUT || os.tmpdir();
const SHOTS = path.join(OUT, 'shots');
fs.mkdirSync(SHOTS, { recursive: true });
const ROOM = `vc-browsers-${Date.now()}`;
const v1 = async (method, url, body) => {
  const res = await fetch(`${BASE}/v1${url}`, { method, headers: { Authorization: `Bearer ${SECRET}`, 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
  const data = await res.json();
  assert.ok(res.ok, `${method} ${url}: ${res.status}`);
  return data;
};
const local = (url) => url.replace(PUBLIC, BASE);
async function until(what, fn, timeoutMs = 40000) {
  const deadline = Date.now() + timeoutMs;
  let last;
  while (Date.now() < deadline) {
    try { last = await fn(); if (last) return last; } catch (err) { last = err.message; }
    await new Promise((r) => setTimeout(r, 300));
  }
  throw new Error(`Timed out waiting for ${what}. Last: ${JSON.stringify(last)}`);
}
const playing = (page) => page.evaluate(async () => {
  const videos = Array.from(document.querySelectorAll('.stile:not([hidden]) video')).filter((v) => v.videoWidth > 0);
  if (!videos.length) return false;
  const t = videos[0].currentTime;
  await new Promise((r) => setTimeout(r, 900));
  return videos[0].currentTime > t;
});
// What a viewer actually has, so a failure says whether the track or the picture went missing.
const mediaState = (page) => page.evaluate(() => ({
  tiles: document.querySelectorAll('.stile:not([hidden])').length,
  videos: Array.from(document.querySelectorAll('.stile video')).map((v) => ({ width: v.videoWidth, ready: v.readyState, paused: v.paused, time: v.currentTime, streams: v.srcObject ? v.srcObject.getTracks().map((t) => `${t.kind}:${t.readyState}:${t.muted}`) : [] })),
  connections: window.__connections || [],
}));
// Added before the page's scripts, so every connection the page makes is on record.
const watchConnections = () => {
  window.__connections = [];
  const Real = window.RTCPeerConnection;
  window.RTCPeerConnection = function (...args) {
    const pc = new Real(...args);
    const entry = { ice: 'new', candidates: [], remote: [], direction: [] };
    window.__connections.push(entry);
    pc.addEventListener('iceconnectionstatechange', () => { entry.ice = pc.iceConnectionState; });
    pc.addEventListener('icecandidate', (e) => { if (e.candidate) entry.candidates.push(e.candidate.candidate.split(' ').slice(4, 8).join(' ')); });
    const add = pc.addIceCandidate.bind(pc);
    pc.addIceCandidate = (c, ...rest) => { if (c && c.candidate) entry.remote.push(c.candidate.split(' ').slice(4, 8).join(' ')); return add(c, ...rest); };
    pc.addEventListener('track', (e) => entry.direction.push(`track:${e.track.kind}`));
    return pc;
  };
  window.RTCPeerConnection.prototype = Real.prototype;
};

const ALL_TARGETS = [
  { name: 'chromium-desktop', engine: 'chromium', options: { viewport: { width: 1280, height: 800 } } },
  { name: 'firefox-desktop', engine: 'firefox', options: { viewport: { width: 1280, height: 800 } } },
  { name: 'webkit-desktop', engine: 'webkit', options: { viewport: { width: 1280, height: 800 } } },
  { name: 'iphone-13-webkit', engine: 'webkit', options: { ...pw.devices['iPhone 13'] } },
  { name: 'ipad-mini-webkit', engine: 'webkit', options: { ...pw.devices['iPad Mini'] } },
  { name: 'pixel-7-chromium', engine: 'chromium', options: { ...pw.devices['Pixel 7'] } },
];

const only = (process.env.BROWSER_TARGETS || '').split(',').filter(Boolean);
const TARGETS = only.length ? only.map((name) => ALL_TARGETS.find((t) => t.name === name)) : ALL_TARGETS;

// The test server's media ports are only on Docker's bridge. Without camera permission Firefox uses just its
// main network interface, so it couldn't reach them. A public server doesn't have this problem.
async function launch(engine) {
  if (engine === 'firefox') return pw.firefox.launch({ firefoxUserPrefs: { 'media.navigator.streams.fake': true, 'media.navigator.permission.disabled': true, 'media.autoplay.default': 0, 'media.autoplay.blocking_policy': 0, 'media.peerconnection.ice.default_address_only': false, 'media.peerconnection.ice.obfuscate_host_addresses': false } });
  if (engine === 'webkit') return pw.webkit.launch();
  return pw.chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE, args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream', '--autoplay-policy=no-user-gesture-required', ...(engine === 'chromium-speech' ? [] : [])] });
}

(async () => {
  const now = Date.now();
  await v1('PUT', `/rooms/${ROOM}`, { title: 'Browsers', startsAt: new Date(now + 300000).toISOString(), endsAt: new Date(now + 3600000).toISOString() });
  const hostBrowser = await launch('chromium');
  const hostContext = await hostBrowser.newContext({ permissions: ['camera', 'microphone'] });
  const host = await hostContext.newPage();
  await host.goto(local((await v1('POST', `/rooms/${ROOM}/crew`, { role: 'host', name: 'Maya Host' })).url));
  await host.waitForSelector('#join-form:not([hidden])');
  await host.click('#join');
  await host.waitForSelector('#room:not([hidden])');
  await until('the host in the green room', () => host.$('.gtile .mini'));
  await host.click('.gtile .mini:text-is("Put on stage")');
  await host.click('#go-live');
  const started = Date.now();
  await until('the host seeing their own camera', () => host.evaluate(() => Array.from(document.querySelectorAll('.stile video')).some((v) => v.videoWidth > 0)), 60000);
  console.log(`host on stage with a picture after ${Date.now() - started}ms`);
  const results = {};
  let id = 100;
  for (const target of TARGETS) {
    const row = { video: false, chat: false, poll: false, overflow: null, errors: [] };
    results[target.name] = row;
    let browser;
    try {
      browser = await launch(target.engine);
      const context = await browser.newContext(target.options);
      const page = await context.newPage();
      page.on('pageerror', (e) => row.errors.push(e.message.slice(0, 200)));
      page.on('console', (m) => { if (process.env.BROWSER_CONSOLE && m.type() !== 'debug') console.log(`  ${target.name} ${m.type()}: ${m.text().slice(0, 240)}`); });
      await page.addInitScript(watchConnections);
      const identity = `${String(id += 1).padStart(8, '0')}-8d4e-4f0a-9b1c-2d3e4f5a6b7c`;
      await page.goto(local((await v1('POST', `/rooms/${ROOM}/tickets`, { identity })).joinUrl));
      await page.waitForSelector('#join-form:not([hidden])', { timeout: 20000 });
      await page.fill('#name', target.name);
      await page.click('#join');
      await page.waitForSelector('#room:not([hidden])', { timeout: 20000 });
      await until('video', () => playing(page), 30000).then(() => { row.video = true; }).catch(async (e) => { row.videoError = e.message.slice(0, 160); row.media = await mediaState(page); });
      if (await page.isVisible('#enable-sound')) { row.soundButton = true; await page.click('#enable-sound'); }
      await page.fill('#chat-input', `hello from ${target.name}`);
      await page.click('#chat-form button[type=submit]');
      await until('chat at the host', () => host.evaluate((text) => document.getElementById('messages').textContent.includes(text), `hello from ${target.name}`)).then(() => { row.chat = true; });
      const poll = await host.evaluate(async (q) => {
        const act = (action) => fetch(document.documentElement.dataset.base + '/act', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action }) }).then((r) => r.json());
        const saved = await act({ type: 'poll_save', id: null, question: q, options: ['Yes', 'No'], correct: null, showResults: 'live', showCounts: false, popup: true });
        await act({ type: 'poll_status', id: saved.id, status: 'open' });
        return saved.id;
      }, `Works on ${target.name}?`);
      await page.waitForSelector('#poll-pop:not([hidden]) .poll-row', { timeout: 10000 });
      await page.click('#poll-pop .poll-row:text-is("Yes")');
      await until('the vote counted', () => page.evaluate(() => document.getElementById('poll-pop').hidden)).then(() => { row.poll = true; });
      await host.evaluate((pollId) => fetch(document.documentElement.dataset.base + '/act', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: { type: 'poll_status', id: pollId, status: 'closed' } }) }), poll);
      row.overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      await page.screenshot({ path: path.join(SHOTS, `browser-${target.name}.png`) });
      await context.close();
    } catch (err) {
      row.failed = err.message.slice(0, 240);
    } finally {
      if (browser) await browser.close();
    }
    console.log(`${target.name}: ${JSON.stringify(row)}`);
  }
  // Screen sharing on Safari's engine: the picker has to open from the click itself.
  const shareRow = { asked: false, errors: [] };
  results['webkit-share'] = shareRow;
  let webkit;
  try {
    webkit = await launch('webkit');
    const page = await (await webkit.newContext({ viewport: { width: 1280, height: 800 } })).newPage();
    page.on('pageerror', (e) => shareRow.errors.push(e.message.slice(0, 200)));
    await page.addInitScript(() => {
      window.__share = [];
      const real = navigator.mediaDevices.getDisplayMedia.bind(navigator.mediaDevices);
      navigator.mediaDevices.getDisplayMedia = (c) => { window.__share.push('picker'); return real(c).catch((e) => { window.__share.push(`refused: ${e.message}`); throw e; }); };
      const realFetch = window.fetch;
      window.fetch = (u, o) => { if (String(u).includes('share-token')) window.__share.push('token'); return realFetch(u, o); };
    });
    await page.goto(local((await v1('POST', `/rooms/${ROOM}/crew`, { role: 'producer', name: 'Safari Producer' })).url));
    await page.waitForSelector('#join-form:not([hidden])');
    await page.uncheck('#use-cam');
    await page.uncheck('#use-mic');
    await page.click('#join');
    await page.waitForSelector('#room:not([hidden])');
    await until('the room ready', () => page.isVisible('#screen'));
    await page.click('#screen');
    await until('the picker asked for', () => page.evaluate(() => window.__share.length > 0));
    await new Promise((r) => setTimeout(r, 1500));
    shareRow.steps = await page.evaluate(() => window.__share);
    assert.equal(shareRow.steps[0], 'picker', 'before anything else');
    assert.ok(!shareRow.steps.some((step) => /user gesture/.test(step)), shareRow.steps.join(', '));
    shareRow.asked = true;
    shareRow.message = await page.textContent('#room-error');
  } catch (err) {
    shareRow.failed = err.message.slice(0, 240);
  } finally {
    if (webkit) await webkit.close();
  }
  console.log(`webkit-share: ${JSON.stringify(shareRow)}`);
  // A host on Firefox: publishing, not just watching.
  const row = { published: false, errors: [] };
  results['firefox-host'] = row;
  let firefox;
  try {
    firefox = await launch('firefox');
    const browser = firefox;
    const page = await (await browser.newContext({ viewport: { width: 1280, height: 800 } })).newPage();
    page.on('pageerror', (e) => row.errors.push(e.message.slice(0, 200)));
    await page.goto(local((await v1('POST', `/rooms/${ROOM}/crew`, { role: 'guest', name: 'Firefox Guest' })).url));
    await page.waitForSelector('#join-form:not([hidden])');
    await page.click('#join');
    await page.waitForSelector('#room:not([hidden])');
    await until('the guest in the green room', () => host.$('.gtile:has-text("Firefox Guest") .mini:text-is("Put on stage")'));
    await host.click('.gtile:has-text("Firefox Guest") .mini:text-is("Put on stage")');
    await until('Firefox video at the host', () => host.evaluate(() => Array.from(document.querySelectorAll('.stile video')).filter((v) => v.videoWidth > 0).length >= 2), 30000);
    row.published = true;
  } catch (err) {
    row.failed = err.message.slice(0, 240);
    row.host = await mediaState(host).catch(() => null);
  } finally {
    if (firefox) await firefox.close();
  }
  console.log(`firefox-host: ${JSON.stringify(row)}`);
  fs.writeFileSync(path.join(OUT, 'browsers.json'), JSON.stringify(results, null, 2));
  await hostBrowser.close();
  const bad = Object.entries(results).filter(([, r]) => r.failed || r.video === false || r.chat === false || r.poll === false || r.published === false || r.asked === false || (r.overflow && r.overflow > 2) || (r.errors && r.errors.length));
  console.log(bad.length ? `${bad.length} of ${Object.keys(results).length} had a problem` : `all ${Object.keys(results).length} worked`);
  process.exit(bad.length ? 1 : 0);
})().catch((err) => { console.error(err); process.exit(1); });

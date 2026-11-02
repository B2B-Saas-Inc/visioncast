'use strict';
/*
 * The browser half of tests/test_hosting_livekit_stack.py: a host and an attendee, on the
 * links VisionCast handed out, in real Chromium with fake cameras.
 */
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright-core');

async function until(what, fn, timeoutMs = 45000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try { if (await fn()) return; } catch { /* not yet */ }
    await new Promise((r) => setTimeout(r, 300));
  }
  throw new Error(`Timed out waiting for ${what}`);
}
async function open(browser, url, name, errors) {
  const context = await browser.newContext({ permissions: ['camera', 'microphone'] });
  const page = await context.newPage();
  page.on('pageerror', (e) => errors.push(`${name}: ${e.message}`));
  await page.goto(url);
  await page.waitForSelector('#join-form:not([hidden])');
  await page.fill('#name', name);
  await page.click('#join');
  await page.waitForSelector('#room:not([hidden])', { timeout: 20000 });
  return page;
}
const playing = (page) => page.evaluate(async () => {
  const v = document.querySelector('.stile video');
  if (!v || !v.videoWidth) return false;
  const t = v.currentTime;
  await new Promise((r) => setTimeout(r, 700));
  return v.currentTime > t;
});

(async () => {
  const errors = [];
  const browser = await chromium.launch({
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE,
    args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream', '--autoplay-policy=no-user-gesture-required',
      `--use-file-for-fake-audio-capture=${process.env.SPEECH_WAV}`],
  });
  try {
    const host = await open(browser, process.env.HOST_URL, 'Maya Host', errors);
    const viewer = await open(browser, process.env.JOIN_URL, 'Ada Viewer', errors);
    await host.click('.gtile .mini:text-is("Put on stage")');
    await until('the host on stage', () => playing(host));
    await host.click('#go-live');
    await until('video at the attendee', () => playing(viewer));
    console.log('ok   attendee on their VisionCast link sees the host once live');
    await until('the studio brand at the attendee', () => viewer.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--coral').trim() === '#1565c0'));
    console.log('ok   the brand set in the studio is on the attendee page');
    await until('the broadcast at the RTMP server', () => {
      try { return JSON.parse(execFileSync('ffprobe', ['-v', 'error', '-rw_timeout', '5000000', '-show_streams', '-of', 'json', 'rtmp://127.0.0.1:11935/live/from-the-studio'], { timeout: 12000 }).toString()).streams.length >= 2; } catch { return false; }
    }, 60000);
    console.log('ok   the destination set in the studio received the broadcast');
    await host.click('#record');
    await viewer.waitForSelector('#rec-pill:not([hidden])', { timeout: 45000 });
    console.log('ok   recording started from the room');
    await new Promise((r) => setTimeout(r, 16000));
    await host.click('#record');
    await viewer.close();
    console.log('ok   recording stopped and attendee left');
    await new Promise((r) => setTimeout(r, 2000));
    await host.close();
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
})().catch((err) => { console.error(err); process.exit(1); });

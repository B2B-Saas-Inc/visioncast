'use strict';
/*
 * End-to-end browser check of the marketer studio against a real backend.
 *
 * Run:
 *   STUDIO_BASE_URL=http://localhost:8000 STUDIO_USERNAME=admin STUDIO_PASSWORD=... \
 *     node tests/browser-studio.cjs
 *
 * The user must be a superuser or Administrator. The script creates a draft event
 * from the "feature-release" starter, edits it, adds a session, saves a form version
 * in the builder, publishes, opens the preview, enables the waiting room and walks the
 * attendee email step in a fresh browser context. Exits non-zero on the first failure.
 * Selectors prefer accessible names (labels, roles) so they also check accessibility.
 */
const assert = require('node:assert/strict');
const { chromium } = require('playwright');

const BASE = (process.env.STUDIO_BASE_URL || '').replace(/\/+$/, '');
const USERNAME = process.env.STUDIO_USERNAME || '';
const PASSWORD = process.env.STUDIO_PASSWORD || '';
const HEADLESS = process.env.HEADED ? false : true;
const STAMP = Date.now().toString(36);
const TITLE = `Release walkthrough ${STAMP}`;
const CUSTOM_KEY = `team${STAMP}`.replace(/[^a-zA-Z0-9_]/g, '').slice(0, 30);
const FORM_VERSION = `e2e-${STAMP}`;

if (!BASE || !USERNAME || !PASSWORD) {
  console.error('Set STUDIO_BASE_URL, STUDIO_USERNAME and STUDIO_PASSWORD.');
  process.exit(2);
}

const results = [];

async function step(name, fn) {
  const started = Date.now();
  try {
    await fn();
    results.push({ name, ok: true });
    console.log(`ok   ${name} (${Date.now() - started}ms)`);
  } catch (err) {
    results.push({ name, ok: false });
    console.error(`FAIL ${name}\n     ${err && err.stack ? err.stack : err}`);
    throw err;
  }
}

function url(path) {
  return BASE + path;
}

/** Click the first visible element among candidate locators. */
async function clickFirst(page, locators, what) {
  for (const loc of locators) {
    const first = loc.first();
    if (await first.count() && await first.isVisible().catch(() => false)) {
      await first.click();
      return;
    }
  }
  throw new Error(`Could not find control: ${what}`);
}

async function fillFirst(page, locators, value, what) {
  for (const loc of locators) {
    const first = loc.first();
    if (await first.count() && await first.isVisible().catch(() => false)) {
      await first.fill(value);
      return;
    }
  }
  throw new Error(`Could not find field: ${what}`);
}

async function expectNoServerError(page) {
  const text = await page.locator('body').innerText();
  assert.ok(!/Server Error \(500\)|Traceback/i.test(text), 'page shows a server error');
}

async function expectNoErrorSummary(page, what) {
  const summary = page.locator('.error-summary');
  if (await summary.count() && await summary.first().isVisible()) {
    throw new Error(`${what}: form errors shown: ${(await summary.first().innerText()).slice(0, 400)}`);
  }
}

async function main() {
  const launch = { headless: HEADLESS };
  if (process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE) launch.executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE;
  const browser = await chromium.launch(launch);
  const staff = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await staff.newPage();
  page.setDefaultTimeout(15000);
  const pageErrors = [];
  page.on('pageerror', (e) => pageErrors.push(String(e)));

  let editorUrl = null;
  let sessionUrl = null;
  let roomUrl = null;

  try {
    await step('login', async () => {
      await page.goto(url('/studio/login/'));
      await page.getByLabel(/username/i).fill(USERNAME);
      await page.getByLabel(/password/i).fill(PASSWORD);
      await clickFirst(page, [page.getByRole('button', { name: /sign in|log in/i })], 'sign in button');
      await page.waitForURL((u) => !u.pathname.startsWith('/studio/login'));
      await expectNoServerError(page);
      await page.screenshot({ path: 'artifacts/studio-dashboard.png', fullPage: true });
    });

    await step('Tracking & GTM screen', async () => {
      await page.getByRole('link', { name: 'Tracking & GTM', exact: true }).click();
      await page.waitForURL('**/studio/tracking/');
      await expectNoServerError(page);
      const code = await page.locator('pre').allTextContents();
      if (!code.some(text => text.includes('webinarFormGtmInstall'))) throw new Error('Missing GTM install snippet');
      if (code.some(text => text.includes('wbk_'))) throw new Error('API key in snippet');
      await page.screenshot({ path: 'artifacts/studio-tracking-gtm.png', fullPage: true });
    });

    await step('create event from feature-release starter', async () => {
      await page.goto(url('/studio/events/new/'));
      await expectNoServerError(page);
      // Starter chooser: a link/button for the feature-release starter (?starter=feature-release).
      const starter = page.locator('a[href*="starter=feature-release"], button[value="feature-release"], input[value="feature-release"]');
      if (await starter.count()) {
        await starter.first().click();
      } else {
        await page.goto(url('/studio/events/new/?starter=feature-release'));
      }
      await page.waitForLoadState('domcontentloaded');
      const titleField = page.getByLabel(/public title|title/i).first();
      assert.ok((await titleField.inputValue()).trim().length > 0, 'starter prefills the title');
      await clickFirst(page, [
        page.getByRole('button', { name: /create (draft|event)|save draft|create/i }),
      ], 'create draft button');
      await page.waitForURL(u => /\/studio\/events\/[0-9a-f-]{36}\/$/.test(u.pathname));
      await expectNoErrorSummary(page, 'create event');
      editorUrl = new URL(page.url()).origin + new URL(page.url()).pathname;
    });

    await step('edit title', async () => {
      await page.goto(editorUrl);
      await page.getByLabel(/public title/i).first().fill(TITLE);
      await clickFirst(page, [page.getByRole('button', { name: /^save( changes)?$/i }), page.getByRole('button', { name: /save/i })], 'save');
      await page.waitForLoadState('domcontentloaded');
      await expectNoServerError(page);
      await expectNoErrorSummary(page, 'save event');
      assert.equal(await page.getByLabel(/public title/i).first().inputValue(), TITLE);
      await page.screenshot({ path: 'artifacts/studio-editor.png', fullPage: true });
    });

    await step('upload cover and configure CTA', async () => {
      await page.goto(editorUrl);
      const cover = await page.locator('h1').first().screenshot();
      await page.locator('[name="hero_image_url"]').fill('');
      await page.getByLabel('Upload a cover image').setInputFiles({ name: 'cover.png', mimeType: 'image/png', buffer: cover });
      await page.getByLabel(/^Button text\s*\(optional\)$/).fill('Reserve my spot');
      await page.getByLabel('Button text for returning visitors').fill('Confirm my spot');
      await page.getByRole('button', { name: 'Save changes', exact: true }).click();
      await expectNoErrorSummary(page, 'cover and CTA');
      await page.goto(editorUrl + 'preview');
      assert.equal(await page.getByRole('button', { name: 'Reserve my spot', exact: true }).count(), 1);
      const coverImage = page.locator('img[src*="/media/covers/"]');
      assert.equal(await coverImage.count(), 1);
      assert.ok(await coverImage.evaluate(img => img.complete && img.naturalWidth > 0));
    });

    await step('add session', async () => {
      await page.goto(editorUrl);
      const d = new Date(Date.now() + 7 * 24 * 3600 * 1000);
      const pad = (n) => String(n).padStart(2, '0');
      const local = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T15:00`;
      const startField = page.locator('input[name="local_start"]').first();
      if (!(await startField.isVisible().catch(() => false))) {
        await clickFirst(page, [page.getByRole('button', { name: /add (a )?session/i }), page.getByRole('link', { name: /add (a )?session/i })], 'add session');
      }
      await page.locator('input[name="local_start"]').first().fill(local);
      const tz = page.locator('select[name="timezone"], input[name="timezone"]').first();
      if (await tz.count()) {
        const tag = await tz.evaluate((el) => el.tagName);
        if (tag === 'SELECT') await tz.selectOption('Europe/London').catch(() => {});
        else await tz.fill('Europe/London');
      }
      const duration = page.locator('input[name="duration_minutes"]').first();
      if (await duration.count()) await duration.fill('45');
      const join = page.locator('input[name="join_url"]').first();
      if (await join.count()) await join.fill('https://meet.example.com/e2e-' + STAMP);
      const form = page.locator('form[action*="/sessions/new"]').first();
      await form.locator('button[type="submit"], button:not([type])').first().click();
      await page.waitForLoadState('domcontentloaded');
      await expectNoServerError(page);
      await expectNoErrorSummary(page, 'add session');
      await page.goto(editorUrl);
      const sessionLink = page.locator('a[href*="/studio/sessions/"]').first();
      assert.ok(await sessionLink.count(), 'session appears on the event page');
      sessionUrl = new URL(await sessionLink.getAttribute('href'), BASE).toString();
    });

    await step('embed snippet and served SDK', async () => {
      await page.goto(editorUrl);
      await page.getByRole('link', { name: 'Embed on your site', exact: true }).first().click();
      const snippet = await page.locator('#embed-snippet').innerText();
      assert.ok(snippet.includes('Reserve my spot') && snippet.includes('Confirm my spot'));
      const src = snippet.match(/<script src="([^"]+)"/)[1];
      const response = await page.request.get(new URL(src, BASE).toString());
      assert.equal(response.status(), 200);
      assert.ok((await response.text()).includes('WebinarForm'));
      await page.goto(editorUrl);
      await page.getByRole('link', { name: 'Waiting room & admission', exact: true }).first().click();
      assert.equal(await page.getByRole('heading', { name: 'Waiting room & admission', exact: true }).count(), 1);
    });

    await step('form builder: add custom field, reorder, save', async () => {
      await page.goto(url('/studio/forms/new/'));
      await expectNoServerError(page);
      const version = page.getByLabel(/version/i).first();
      if (await version.count()) await version.fill(FORM_VERSION);
      await clickFirst(page, [page.getByRole('button', { name: /add (a )?(custom )?(question|field)/i })], 'add custom field');
      const rows = page.locator('[data-rows] > [data-row]');
      const lastRow = rows.last();
      await lastRow.locator('select[data-row-key]').selectOption({ label: 'Custom field…' });
      await lastRow.locator('input[name$="-custom_key"]').fill(CUSTOM_KEY);
      await lastRow.locator('input[name$="-label"]').first().fill('Which team are you on?');
      const before = await rows.count();
      await clickFirst(page, [lastRow.getByRole('button', { name: /move field \d+ up/i })], 'move up');
      assert.equal(await rows.count(), before, 'reordering keeps every field');
      await clickFirst(page, [page.getByRole('button', { name: /save( form| version)?|create form/i })], 'save form');
      await page.waitForLoadState('domcontentloaded');
      await expectNoServerError(page);
      await expectNoErrorSummary(page, 'save form');
      await page.reload();
      assert.ok((await page.locator('input[name$="-label"]').evaluateAll(els => els.map(el => el.value))).includes('Which team are you on?'), 'custom field label persisted');
    });

    await step('publish', async () => {
      await page.goto(editorUrl);
      const publishForm = page.locator('form[action*="/publish"]').first();
      const visibility = publishForm.locator('[name="visibility"]');
      if (await visibility.count()) {
        const tag = await visibility.first().evaluate((el) => el.tagName + ':' + (el.type || ''));
        if (tag.startsWith('SELECT')) await visibility.first().selectOption('public');
        else await publishForm.locator('[name="visibility"][value="public"]').check().catch(() => {});
      }
      await clickFirst(page, [publishForm.getByRole('button', { name: /publish/i }), publishForm.locator('button')], 'publish');
      await page.waitForLoadState('domcontentloaded');
      await expectNoServerError(page);
      await expectNoErrorSummary(page, 'publish');
      const text = await page.locator('body').innerText();
      assert.ok(/public|published|live/i.test(text), 'event reports a public state');
    });

    await step('open preview', async () => {
      const previewUrl = editorUrl + 'preview';
      const resp = await page.goto(previewUrl);
      assert.equal(resp.status(), 200);
      await expectNoServerError(page);
      assert.ok((await page.locator('body').innerText()).includes(TITLE), 'preview shows the edited title');
    });

    await step('enable waiting room on session', async () => {
      await page.goto(sessionUrl);
      const wrForm = page.locator('form[action*="/waiting-room"]').first();
      const toggle = wrForm.locator('input[name="enabled"]').first();
      if (await toggle.count() && (await toggle.getAttribute('type')) === 'checkbox') {
        if (!(await toggle.isChecked())) await toggle.check({ force: true });
      }
      await clickFirst(page, [wrForm.getByRole('button', { name: /save|turn on|enable/i }), wrForm.locator('button')], 'enable waiting room');
      await page.waitForLoadState('domcontentloaded');
      await expectNoServerError(page);
      const href = (await page.locator('#room-url').innerText()).trim();
      assert.ok(new URL(href, BASE).pathname.startsWith('/w/'), 'room URL shown');
      roomUrl = new URL(new URL(href, BASE).pathname, BASE).toString();
    });

    await step('attendee: email form, generic code step', async () => {
      const attendee = await browser.newContext({ viewport: { width: 375, height: 800 } });
      try {
        const room = await attendee.newPage();
        const resp = await room.goto(roomUrl);
        assert.equal(resp.status(), 200);
        const email = room.getByLabel(/email/i).first();
        assert.ok(await email.isVisible(), 'unknown browser sees the email form');
        await room.screenshot({ path: 'artifacts/waiting-room-mobile.png', fullPage: true });
        assert.ok(await room.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'no mobile horizontal overflow');
        const html = await room.content();
        assert.ok(!html.includes('meet.example.com/e2e-'), 'join link never rendered');
        await email.fill(`e2e+${STAMP}@example.com`);
        await clickFirst(room, [room.getByRole('button', { name: /continue|send|next|code/i }), room.locator('form button')], 'send code');
        await room.waitForURL(/step=code/);
        assert.ok(await room.getByLabel(/code/i).first().isVisible(), 'code step shown');
        const text = await room.locator('body').innerText();
        assert.ok(/check your email|we sent|code/i.test(text), 'generic check-your-email copy');
        await page.goto(url('/studio/mailbox/'));
        await page.getByRole('row').filter({ hasText: `e2e+${STAMP}@example.com` }).getByRole('link').click();
        const message = await page.locator('pre').innerText();
        const code = message.match(/\b\d{6}\b/);
        assert.ok(code, 'preview mailbox contains verification code');
        await room.getByLabel(/code/i).fill(code[0]);
        await room.getByRole('button', { name: 'Continue', exact: true }).click();
        await room.getByLabel(/first name/i).fill('Morgan');
        await room.getByLabel(/last name/i).fill('Example');
        await room.getByRole('button', { name: /save my seat|confirm my seat/i }).click();
        await room.getByRole('heading', { name: "You're on the list" }).waitFor();
        await room.reload();
        await room.getByRole('heading', { name: "You're on the list" }).waitFor();
        assert.ok(!await room.locator('input[name="email"]').count(), 'recognized browser skips email');
        await room.screenshot({ path: 'artifacts/waiting-room-countdown.png', fullPage: true });
        let admittedLocation = '';
        await room.route('**/enter', async route => {
          const response = await route.fetch({ maxRedirects: 0 });
          assert.equal(response.status(), 303);
          admittedLocation = response.headers().location;
          await route.fulfill({ status: 200, contentType: 'text/html', body: '<h1>Test viewing room</h1>' });
        });
        await page.goto(sessionUrl);
        await page.getByRole('button', { name: /open now/i }).click();
        await room.getByRole('heading', { name: 'Test viewing room' }).waitFor({ timeout: 20000 });
        assert.equal(admittedLocation, 'https://meet.example.com/e2e-' + STAMP);
        await page.goto(sessionUrl);
        const checked = await page.request.get(sessionUrl + 'live.json');
        assert.equal((await checked.json()).checkedIn, 1, 'admission records one check-in');
      } finally {
        await attendee.close();
      }
    });

    await step('admission: Open now', async () => {
      await page.goto(sessionUrl);
      const admission = page.locator('form[action*="/admission"]');
      await clickFirst(page, [
        admission.getByRole('button', { name: /open now/i }),
        admission.locator('button[value="open"]'),
      ], 'Open now');
      await page.waitForLoadState('domcontentloaded');
      await expectNoServerError(page);
      const live = await page.request.get(sessionUrl.replace(/\/$/, '') + '/live.json');
      assert.equal(live.status(), 200);
      const data = await live.json();
      assert.equal(data.admissionMode, 'open');
      assert.ok(!JSON.stringify(data).includes('meet.example.com'), 'live.json has no join link');
    });

    assert.deepEqual(pageErrors, [], 'no uncaught page errors');
  } finally {
    await browser.close();
  }
}

main()
  .then(() => {
    console.log(`\n${results.filter((r) => r.ok).length}/${results.length} steps passed`);
    process.exit(0);
  })
  .catch(() => {
    console.error(`\n${results.filter((r) => r.ok).length}/${results.length} steps passed before failure`);
    process.exit(1);
  });

'use strict';
/*
 * Real Chromium check of the Riverside registrant controls against the preview backend.
 *
 * Run ONLY from tests/run-studio-browser.sh inside the disposable managed container,
 * after tests/riverside_browser_seed.py wrote the synthetic fixture:
 *   STUDIO_BASE_URL=... STUDIO_USERNAME=... STUDIO_PASSWORD=... RIVERSIDE_FIXTURE=/tmp/.../fixture.json \
 *     node tests/browser-riverside.cjs
 *
 * No request may leave the preview origin: every other host is aborted at the browser
 * context before any network I/O, and Riverside navigations are recorded, not followed.
 * Secrets (password, capabilities, personal link) are never logged or screenshotted.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { chromium } = require('playwright');

const BASE = (process.env.STUDIO_BASE_URL || '').replace(/\/+$/, '');
const USERNAME = process.env.STUDIO_USERNAME || '';
const PASSWORD = process.env.STUDIO_PASSWORD || '';
const FIXTURE_PATH = process.env.RIVERSIDE_FIXTURE || '';
const HEADLESS = process.env.HEADED ? false : true;
const TIMEOUT = 15000;

if (!BASE || !USERNAME || !PASSWORD || !FIXTURE_PATH.startsWith('/tmp/')) {
  console.error('Set STUDIO_BASE_URL, STUDIO_USERNAME, STUDIO_PASSWORD and RIVERSIDE_FIXTURE (/tmp/...).');
  process.exit(2);
}

const F = JSON.parse(fs.readFileSync(FIXTURE_PATH, 'utf8'));
const ORIGIN = new URL(BASE).origin;
const SECRETS = [PASSWORD, F.editorPassword, F.stalledCapability, F.missingCapability, F.personalLink,
  new URL(F.personalLink).searchParams.get('t')];
const results = [];

/** Never echo a fixture secret, even inside an assertion message or stack. */
function redact(text) {
  let out = String(text);
  for (const s of SECRETS) if (s) out = out.split(s).join('[redacted]');
  return out;
}

async function step(name, fn) {
  const started = Date.now();
  try {
    await fn();
    results.push({ name, ok: true });
    console.log(`ok   ${name} (${Date.now() - started}ms)`);
  } catch (err) {
    results.push({ name, ok: false });
    console.error(`FAIL ${name}\n     ${redact(err && err.stack ? err.stack : err)}`);
    throw err;
  }
}

function url(path) {
  return BASE + path;
}

function assertNoSecrets(text, what) {
  for (const s of SECRETS) {
    if (s && text.includes(s)) throw new Error(`${what} contains a fixture secret`);
  }
  if (/riverside\.fm\/studio\/browser-e2e|SYNTH-/.test(text)) throw new Error(`${what} contains the personal link`);
}

/**
 * Abort every request that is not for the preview origin before it touches the network,
 * recording provider targets so tests can assert where the browser was sent.
 */
async function guardContext(context) {
  const provider = [];
  const foreign = [];
  await context.route(() => true, async (route) => {
    const target = route.request().url();
    if (target.startsWith(ORIGIN + '/') || target === ORIGIN) {
      // Browser routing does not re-intercept HTTP redirect hops. Stop the backend's
      // admission response before Chromium can follow it outside the preview.
      if (new URL(target).pathname.endsWith('/enter') && route.request().method() === 'POST') {
        const response = await route.fetch({ maxRedirects: 0 });
        const location = response.headers().location;
        if (location && new URL(location, target).origin !== ORIGIN) {
          const next = new URL(location, target).href;
          if (/^https:\/\/([a-z0-9-]+\.)*riverside\.(fm|com)\//i.test(next)) provider.push(next);
          else foreign.push(next);
          assert.equal(response.status(), 303, 'admission uses a 303 redirect');
          return route.abort('blockedbyclient');
        }
        return route.fulfill({ response });
      }
      return route.continue();
    }
    if (/^https:\/\/([a-z0-9-]+\.)*riverside\.(fm|com)\//i.test(target)) provider.push(target);
    else foreign.push(target);
    return route.abort('blockedbyclient');
  });
  context.on('request', (req) => {
    const target = req.url();
    if (/^https?:\/\/([a-z0-9-]+\.)*riverside\.(fm|com)\//i.test(target) && !provider.includes(target)) provider.push(target);
  });
  return { provider, foreign };
}

async function expectNoServerError(page) {
  const text = await page.locator('body').innerText();
  assert.ok(!/Server Error \(500\)|Traceback/i.test(text), 'page shows a server error');
}

async function login(page, username, password) {
  await page.goto(url('/studio/login/'));
  await page.getByLabel(/username/i).fill(username);
  await page.getByLabel(/password/i).fill(password);
  await page.getByRole('button', { name: /sign in|log in/i }).first().click();
  await page.waitForURL((u) => !u.pathname.startsWith('/studio/login'), { timeout: TIMEOUT });
}

async function attendeeContext(browser, capability, options = {}) {
  const context = await browser.newContext({ viewport: { width: 375, height: 800 }, ...options });
  const guard = await guardContext(context);
  const host = new URL(BASE).hostname;
  await context.addCookies([{ name: F.cookieName, value: capability, domain: host, path: '/', httpOnly: true, sameSite: 'Lax' }]);
  const page = await context.newPage();
  page.setDefaultTimeout(TIMEOUT);
  return { context, page, guard };
}

/** Submit the room's enter form (visible Join button, or the hidden form if the room keeps it hidden). */
async function submitEnter(page) {
  const join = page.getByRole('button', { name: 'Join the session now', exact: true });
  if (await join.count() && await join.first().isVisible().catch(() => false)) {
    await join.first().click();
  } else {
    const form = page.locator('form[data-enter-form]');
    assert.ok(await form.count(), 'room renders an enter form for a registered viewer');
    await form.first().evaluate((f) => f.submit());
  }
}

async function main() {
  const browser = await chromium.launch({ headless: HEADLESS });
  const pageErrors = [];
  try {
    const staff = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const staffGuard = await guardContext(staff);
    const page = await staff.newPage();
    page.setDefaultTimeout(TIMEOUT);
    page.on('pageerror', (e) => pageErrors.push(String(e)));
    page.on('dialog', (d) => d.accept());  // data-confirm prompts
    const registrantsUrl = url(`/studio/sessions/${F.occurrenceId}/hosting/registrants`);
    const roomPath = `/w/${F.roomCode}/`;

    await step('riverside: admin login', async () => {
      await login(page, USERNAME, PASSWORD);
      await expectNoServerError(page);
    });

    await step('riverside: admin sees stalled registrant, masked, no link', async () => {
      const resp = await page.goto(registrantsUrl);
      assert.equal(resp.status(), 200);
      await page.getByRole('heading', { name: 'Riverside registrants to check', exact: true }).waitFor();
      const rows = page.getByRole('row').filter({ hasText: F.stalledRegistrationId });
      assert.equal(await rows.count(), 1, 'stalled registrant listed once');
      assert.equal(await page.getByRole('row').filter({ hasText: F.missingRegistrationId }).count(), 0,
        'registrant without a provider record is not listed');
      const rowText = await rows.first().innerText();
      assert.ok(rowText.includes(F.maskedEmail) && rowText.includes('needs_reconciliation'), 'masked email and state shown');
      const html = await page.content();
      assert.ok(!html.includes(F.stalledEmail) && !html.includes(F.missingEmail), 'full emails never rendered');
      assertNoSecrets(html, 'registrants page');
      const linkField = rows.first().getByLabel("This registrant's own Riverside link");
      assert.equal(await linkField.getAttribute('type'), 'password', 'link field is masked');
    });

    await step('riverside: forged state token refused', async () => {
      await page.goto(registrantsUrl);
      const form = page.getByRole('row').filter({ hasText: F.stalledRegistrationId })
        .locator('form').filter({ has: page.getByRole('button', { name: 'It was created', exact: true }) });
      await form.locator('input[name="state_token"]').evaluate((el) => { el.value = 'forged'; });
      await form.getByLabel("This registrant's own Riverside link").fill(F.personalLink);
      await form.getByRole('button', { name: 'It was created', exact: true }).click();
      await page.getByRole('alert').filter({ hasText: /out of date/i }).waitFor();
      assertNoSecrets(await page.content(), 'refusal page');
      assert.equal(await page.getByRole('row').filter({ hasText: F.stalledRegistrationId }).count(), 1, 'still stalled');
    });

    await step('riverside: admin confirms personal link through the GUI', async () => {
      await page.goto(registrantsUrl);
      const row = page.getByRole('row').filter({ hasText: F.stalledRegistrationId });
      const form = row.locator('form').filter({ has: page.getByRole('button', { name: 'It was created', exact: true }) });
      assert.ok((await form.locator('input[name="state_token"]').inputValue()).length > 20, 'signed state token present');
      await form.getByLabel("This registrant's own Riverside link").fill(F.personalLink);
      await Promise.all([
        page.waitForURL((u) => u.pathname === new URL(registrantsUrl).pathname, { timeout: TIMEOUT }),
        form.getByRole('button', { name: 'It was created', exact: true }).click(),
      ]);
      await page.getByText('No Riverside registrants need checking for this session.').waitFor();
      assertNoSecrets(await page.content(), 'registrants page after confirm');
      await page.screenshot({ path: 'artifacts/riverside-registrants.png', fullPage: true });
    });

    await step('riverside: editor is refused the registrants page', async () => {
      const editor = await browser.newContext();
      try {
        await guardContext(editor);
        const ep = await editor.newPage();
        ep.setDefaultTimeout(TIMEOUT);
        await login(ep, F.editorUsername, F.editorPassword);
        const resp = await ep.goto(registrantsUrl);
        assert.equal(resp.status(), 403, 'Editor gets 403');
        assertNoSecrets(await ep.content(), 'editor refusal page');
        assert.ok(!(await ep.content()).includes(F.stalledRegistrationId), 'refusal shows no registrant');
      } finally {
        await editor.close();
      }
    });

    await step('riverside: linked attendee enters via personal link, no shared link', async () => {
      const { context, page: room, guard } = await attendeeContext(browser, F.stalledCapability);
      try {
        room.on('pageerror', (e) => pageErrors.push(String(e)));
        // Fetch markup before navigation: the live page auto-submits immediately.
        const resp = await context.request.get(url(roomPath));
        assert.equal(resp.status(), 200);
        const html = await resp.text();
        assert.ok(html.includes("You're on the list"), 'recognised attendee markup');
        assertNoSecrets(html, 'room page');
        const status = await room.request.get(url(roomPath + 'status'));
        assert.equal(status.status(), 200);
        const statusText = await status.text();
        assertNoSecrets(statusText, 'room status');
        assert.equal(JSON.parse(statusText).admit, true, 'status admits the linked registrant');
        // Personal link is the only destination: the 303 target is aborted at the context route.
        await room.goto(url(roomPath), { waitUntil: 'commit' }).catch((err) => {
          if (!guard.provider.length) throw err;
        });
        const deadline = Date.now() + TIMEOUT;
        while (!guard.provider.length && Date.now() < deadline) await room.waitForTimeout(100);
        assert.ok(guard.provider.length >= 1, 'browser was sent to a Riverside link');
        assert.ok(guard.provider.every((u) => u === F.personalLink), 'redirect target mismatch: ' + JSON.stringify(guard.provider.map((u) => { const p = new URL(u); return { origin: p.origin, path: p.pathname, keys: [...p.searchParams.keys()] }; })));
        assert.deepEqual(guard.foreign, [], 'no other off-origin requests');
      } finally {
        await context.close();
      }
    });

    await step('riverside: registrant without a personal link is denied', async () => {
      const { context, page: room, guard } = await attendeeContext(browser, F.missingCapability, { javaScriptEnabled: false });
      try {
        const resp = await room.goto(url(roomPath));
        assert.equal(resp.status(), 200);
        await room.getByRole('heading', { name: "You're on the list" }).waitFor();
        await Promise.all([
          room.waitForResponse((r) => r.url().endsWith(roomPath + 'enter') && r.request().method() === 'POST'),
          submitEnter(room),
        ]);
        await room.waitForLoadState('domcontentloaded');
        assert.equal(new URL(room.url()).pathname, roomPath, 'denied enter returns to the room');
        await room.waitForTimeout(1000);  // let any auto-enter script run once more
        assert.deepEqual(guard.provider, [], 'no provider navigation for a registrant without a link');
        assert.deepEqual(guard.foreign, [], 'no off-origin requests');
        assertNoSecrets(await room.content(), 'denied room page');
        const statusText = await (await room.request.get(url(roomPath + 'status'))).text();
        assertNoSecrets(statusText, 'denied room status');
      } finally {
        await context.close();
      }
    });

    await step('riverside: unrecognised browser is not admitted', async () => {
      const context = await browser.newContext();
      try {
        const guard = await guardContext(context);
        const room = await context.newPage();
        room.setDefaultTimeout(TIMEOUT);
        await room.goto(url(roomPath));
        assert.ok(await room.getByLabel(/email/i).first().isVisible(), 'unknown browser sees the email form');
        assert.equal(await room.locator('form[data-enter-form]').count(), 0, 'no enter form for unknown browser');
        const resp = await room.request.post(url(roomPath + 'enter'), { maxRedirects: 0, failOnStatusCode: false });
        assert.ok([303, 403].includes(resp.status()), `enter without capability refused (${resp.status()})`);
        assert.ok(!(resp.headers().location || '').includes('riverside'), 'no provider redirect');
        assert.deepEqual(guard.provider, []);
      } finally {
        await context.close();
      }
    });

    assert.deepEqual(staffGuard.foreign, [], 'studio made no off-origin requests');
    assert.deepEqual(staffGuard.provider, [], 'studio never navigated to Riverside');
    assert.deepEqual(pageErrors.map(redact), [], 'no uncaught page errors');
  } finally {
    await browser.close();
  }
}

main()
  .then(() => {
    console.log(`\n${results.filter((r) => r.ok).length}/${results.length} riverside steps passed`);
    process.exit(0);
  })
  .catch((err) => {
    if (!results.some((r) => !r.ok)) console.error(redact(err && err.stack ? err.stack : err));
    console.error(`\n${results.filter((r) => r.ok).length}/${results.length} riverside steps passed before failure`);
    process.exit(1);
  });

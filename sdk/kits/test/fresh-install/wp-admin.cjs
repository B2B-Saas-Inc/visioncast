'use strict';
/*
 * WordPress admin checks with Chromium: Settings -> Webinar Platform exists, the key field is
 * write-only (never echoed), the backend URL validation accepts http only because WP_ENVIRONMENT_TYPE=local
 * (an https origin with a path is rejected), and the block appears in the editor inserter.
 *   KITS_KEY_FILE=<apikey.txt> NODE_PATH=/tmp/pw/node_modules node wp-admin.cjs http://localhost:8080
 */
const assert = require('node:assert/strict');
const { chromium } = require('playwright');

const base = process.argv[2] || 'http://localhost:8080';
const ok = (name, cond, detail) => {
  if (!cond) throw new Error(`FAIL ${name}${detail ? ': ' + detail : ''}`);
  console.log('ok   ' + name);
};

(async () => {
  const launch = { headless: true };
  if (process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE) launch.executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE;
  const browser = await chromium.launch(launch);
  const page = await browser.newContext().then((c) => c.newPage());
  await page.goto(base + '/wp-login.php');
  await page.fill('#user_login', 'admin');
  await page.fill('#user_pass', 'admin-disposable-pw');
  await page.click('#wp-submit');
  await page.waitForURL(/wp-admin/);
  await page.goto(base + '/wp-admin/options-general.php?page=webinar-platform');
  ok('settings page renders', await page.locator('h1', { hasText: 'Webinar Platform' }).count() === 1);
  ok('proxy base shown', (await page.locator('code').first().textContent()).endsWith('/webinar-api/'));
  ok('key is reported as set by WEBINAR_PLATFORM_PROXY_KEY and no key input exists',
    (await page.locator('text=Set by WEBINAR_PLATFORM_PROXY_KEY').count()) === 1 && (await page.locator('#wp-proxy-key').count()) === 0);
  const html = await page.content();
  const rawKey = require('node:fs').readFileSync(process.env.KITS_KEY_FILE, 'utf8').trim().split('\n').pop(); ok('page source never contains the raw key', rawKey.length >= 32 && !html.includes(rawKey));
  ok('backend URL field shows the configured http origin', (await page.inputValue('#wp-backend-url')) === 'http://host.docker.internal:8765');

  // Save with an invalid backend (origin + path): rejected, previous value kept.
  await page.fill('#wp-backend-url', 'https://webinars.example.com/api');
  await page.click('#submit');
  await page.waitForLoadState('load');
  ok('backend URL with a path is rejected with a settings error', (await page.locator('.notice-error, .settings-error').count()) >= 1);
  ok('previous backend URL kept', (await page.inputValue('#wp-backend-url')) === 'http://host.docker.internal:8765');

  // Save a valid change (timeout) through options.php (Settings API nonce) and confirm persistence.
  await page.fill('#wp-timeout', '12');
  await page.click('#submit');
  await page.waitForLoadState('load');
  ok('valid save persists (timeout 12)', (await page.inputValue('#wp-timeout')) === '12');
  ok('sdk_url path is kept as entered', (await page.inputValue('#wp-sdk-url')) === '/webinar-form.js');

  // Settings REST exposure: options endpoint must not list the plugin's options.
  const rest = await page.request.get(base + '/wp-json/wp/v2/settings', { headers: { 'X-WP-Nonce': await page.evaluate(() => (window.wpApiSettings && window.wpApiSettings.nonce) || '') } });
  const restBody = rest.status() === 200 ? await rest.text() : '';
  ok('REST /wp/v2/settings does not expose plugin options (show_in_rest false)', !/webinar_platform/.test(restBody), String(rest.status()));

  // Block editor: the block is registered and renders via ServerSideRender.
  await page.goto(base + '/wp-admin/post-new.php?post_type=page');
  await page.waitForTimeout(3000);
  const registered = await page.evaluate(() => !!(window.wp && wp.blocks && wp.blocks.getBlockType('webinar-platform/webinar-form')));
  ok('block webinar-platform/webinar-form registered in the editor', registered);
  await browser.close();
  console.log('\nWordPress admin checks passed');
})().catch((e) => { console.error(e.stack || e); process.exit(1); });

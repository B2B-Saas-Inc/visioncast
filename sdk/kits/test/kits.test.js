'use strict';
/*
 * Invariants for sdk/kits: manifest integrity, route parity with sdk/proxy/server.js,
 * no secrets in browser-facing files, WordPress settings/proxy guards, the Node mount,
 * and the headless seam. node:test only; no network (fetch is injected).
 *
 *   node --test sdk/kits/test/kits.test.js
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..', '..');
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), 'utf8');
const manifest = JSON.parse(read('sdk/kits/manifest.json'));
const { ROUTES } = require(path.join(ROOT, 'sdk/proxy/server.js'));

const WP = 'sdk/kits/wordpress/webinar-platform/';
const PHP_FILES = [
  'webinar-platform.php',
  'uninstall.php',
  'includes/class-settings.php',
  'includes/class-proxy.php',
  'includes/class-form.php',
  'blocks/webinar-form/index.asset.php',
].map((f) => WP + f);

test('manifest: every kit has an explicit status, prerequisites and existing files', () => {
  const statuses = Object.keys(manifest.statuses);
  assert.ok(manifest.kits.length >= 10);
  const ids = new Set();
  for (const kit of manifest.kits) {
    assert.ok(!ids.has(kit.id), `duplicate kit ${kit.id}`);
    ids.add(kit.id);
    assert.ok(statuses.includes(kit.status), `${kit.id}: unknown status ${kit.status}`);
    assert.equal(kit.productionTested, false, `${kit.id}: nothing here is production-tested`);
    assert.ok(Array.isArray(kit.prerequisites) && kit.prerequisites.length, `${kit.id}: prerequisites`);
    assert.ok(kit.files.length, `${kit.id}: files`);
    for (const f of [...kit.files, ...kit.browserFiles]) {
      assert.ok(fs.existsSync(path.join(ROOT, f)), `${kit.id}: missing ${f}`);
      assert.ok(fs.statSync(path.join(ROOT, f)).size > 200, `${kit.id}: ${f} looks like an empty scaffold`);
    }
    for (const f of kit.browserFiles) assert.ok(kit.files.includes(f), `${kit.id}: browser file not listed in files`);
  }
  for (const id of ['wordpress', 'nextjs', 'react', 'astro', 'nuxt-vue', 'sveltekit', 'webflow', 'framer', 'sanity', 'payload', 'prismic', 'contentful']) {
    assert.ok(ids.has(id), `kit ${id} missing`);
  }
  assert.equal(manifest.kits.find((k) => k.id === 'wordpress').status, 'installable-plugin');
});

test('manifest contract and docs list exactly the Node proxy routes', () => {
  assert.deepEqual([...manifest.contract.proxyRoutes].sort(), Object.keys(ROUTES).sort());
  // GitHub-style heading anchors: lower case, punctuation dropped, spaces to hyphens.
  const anchors = new Set(
    read('docs/kits.md')
      .split('\n')
      .filter((l) => /^#{2,4} /.test(l))
      .map((l) => l.replace(/^#+ /, '').trim().toLowerCase().replace(/[^\w\- ]/g, '').replace(/ /g, '-'))
  );
  for (const kit of manifest.kits) {
    const [file, anchor] = kit.docs.split('#');
    assert.equal(file, 'docs/kits.md', `${kit.id}: docs path`);
    assert.ok(anchors.has(anchor), `${kit.id}: docs/kits.md has no heading for #${anchor}`);
  }
});

test('WordPress proxy route table matches sdk/proxy/server.js (paths, methods, upstreams)', () => {
  const php = read(WP + 'includes/class-proxy.php');
  const re = /'([a-z.-]+)'\s*=>\s*array\(\s*'method'\s*=>\s*'(GET|POST)',\s*'upstream'\s*=>\s*'([^']+)'\s*\)/g;
  const routes = {};
  let m;
  while ((m = re.exec(php))) routes[`/webinar-api/${m[1]}`] = { method: m[2], upstream: m[3] };
  assert.deepEqual(Object.keys(routes).sort(), Object.keys(ROUTES).sort());
  for (const [p, spec] of Object.entries(ROUTES)) {
    assert.equal(routes[p].method, spec.method, p);
    assert.equal(routes[p].upstream, spec.upstream, p);
  }
  assert.ok(php.includes("const CSRF_COOKIE = '__Host-webinar_csrf'"), 'CSRF cookie name must match the SDK');
  assert.ok(php.includes('hash_equals('), 'constant-time CSRF compare');
  assert.ok(/'redirection'\s*=>\s*0,/.test(php), 'no upstream redirects');
  assert.ok(php.includes('HttpOnly') && php.includes("SameSite=Lax"), 'session cookie rewrite');
  const code = php.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');
  assert.ok(!/\$_(GET|POST|REQUEST|COOKIE)\b/.test(code), 'proxy parses raw input, never $_GET/$_POST/$_COOKIE');
});

test('WordPress PHP: guards, nonce via Settings API, no REST exposure, anchored regexes', () => {
  for (const f of PHP_FILES) {
    const src = read(f);
    assert.ok(src.startsWith('<?php'), `${f}: <?php`);
    if (!f.endsWith('index.asset.php')) {
      assert.ok(/defined\( '(ABSPATH|WP_UNINSTALL_PLUGIN)' \) \|\| exit;/.test(src), `${f}: direct-access guard`);
    }
    assert.ok(!src.includes('register_rest_route'), `${f}: no REST routes`);
    // A PHP "$" anchor also matches before a trailing newline; every anchored pattern needs /D.
    for (const [, flags] of src.matchAll(/\$[\/#]([a-zA-Z]*)'/g)) assert.ok(flags.includes('D'), `${f}: regex without D flag`);
  }
  const settings = read(WP + 'includes/class-settings.php');
  assert.equal((settings.match(/'show_in_rest'\s*=>\s*false/g) || []).length, 2, 'both options hidden from REST');
  assert.ok(settings.includes("'manage_options'") && settings.includes("current_user_can( 'manage_options' )"));
  assert.ok(settings.includes('settings_fields( self::GROUP )'), 'options.php nonce');
  assert.ok(/name="<\?php echo esc_attr\( self::KEY_OPTION \); \?>" value=""/.test(settings), 'key field is write-only');
  const main = read(WP + 'webinar-platform.php');
  assert.ok(main.includes("add_option( Webinar_Platform_Settings::KEY_OPTION, '', '', false )"), 'key not autoloaded');
  const form = read(WP + 'includes/class-form.php');
  assert.ok(form.includes('esc_attr( $occurrence_id )') && form.includes('Webinar_Platform_Proxy::UUID_RE'));
  assert.ok(!form.includes('proxy_key'), 'form renderer never touches the key');
});

test('browser-facing kit files contain no secrets, backend calls or public env keys', () => {
  const files = new Set(manifest.kits.flatMap((k) => k.browserFiles));
  for (const f of files) {
    const src = read(f);
    assert.ok(!/PROXY_API_KEY|WEBINAR_PLATFORM_PROXY_KEY|Authorization|Bearer\s/i.test(src), `${f}: secret reference`);
    assert.ok(!/(NEXT_PUBLIC|PUBLIC|VITE|NUXT_PUBLIC)_\w*(KEY|SECRET|TOKEN)/.test(src), `${f}: public env secret`);
    assert.ok(!/\/api\/v1\//.test(src), `${f}: browser must use /webinar-api, not the backend API`);
  }
  for (const f of ['sdk/kits/frameworks/react/WebinarForm.jsx', 'sdk/kits/frameworks/vue/WebinarForm.vue', 'sdk/kits/frameworks/svelte/WebinarForm.svelte', 'sdk/kits/frameworks/astro/WebinarForm.astro', 'sdk/kits/nocode/embed-snippet.html']) {
    const src = read(f);
    assert.ok(src.includes('data-webinar-form'), `${f}: SDK form marker`);
    assert.ok(src.includes("'/webinar-api'"), `${f}: first-party proxy base`);
    assert.ok(!/innerHTML|v-html|\{@html|dangerouslySetInnerHTML|set:html/.test(src), `${f}: no raw HTML sinks`);
  }
  assert.ok(read('sdk/kits/nocode/embed-snippet.html').includes('/static/webinar-sdk/webinar-form.js'));
});

test('mount.cjs routes /webinar-api/* to the unchanged proxy and everything else to the app', async () => {
  const { withWebinarProxy, isProxyPath } = require('../node/mount.cjs');
  assert.equal(isProxyPath('/webinar-api/profile?x=1'), true);
  assert.equal(isProxyPath('/webinar-api'), true);
  assert.equal(isProxyPath('/webinar-apix'), false);
  assert.throws(() => withWebinarProxy(() => {}, { env: {} }), /Invalid proxy configuration/);

  const env = {
    PROXY_BACKEND_URL: 'https://backend.invalid',
    PROXY_API_KEY: 'k'.repeat(40),
    PROXY_ALLOWED_ORIGINS: 'https://www.example.com',
  };
  let appCalls = 0;
  const handler = withWebinarProxy(() => { appCalls += 1; }, { env });
  const fake = (url, method) => {
    const res = {
      statusCode: 200, headers: {}, body: '', headersSent: false,
      setHeader(k, v) { this.headers[k.toLowerCase()] = v; },
      getHeader(k) { return this.headers[k.toLowerCase()]; },
      end(b) { this.body = b ? String(b) : ''; },
      destroy() {},
    };
    return [{ url, method, headers: {}, socket: {} }, res];
  };
  let [req, res] = fake('/webinar-api/anything', 'GET');
  await handler(req, res);
  assert.equal(res.statusCode, 404);
  assert.deepEqual(JSON.parse(res.body), { error: 'not_found' });
  assert.equal(res.headers['cache-control'], 'no-store');
  [req, res] = fake('/webinar-api/registrations', 'GET');
  await handler(req, res);
  assert.equal(res.statusCode, 405);
  [req, res] = fake('/about', 'GET');
  await handler(req, res);
  assert.equal(appCalls, 1);
});

test('headless seam: config validation, normalisation and occurrence choice (fake fetch)', async () => {
  const h = await import(path.join(ROOT, 'sdk/kits/headless/webinar-events.mjs'));
  assert.throws(() => h.backendOrigin({ WEBINAR_API_BASE: 'http://api.example.com' }), /https/);
  assert.throws(() => h.backendOrigin({ WEBINAR_API_BASE: 'https://api.example.com/x' }), /origin only/);
  assert.equal(h.backendOrigin({ WEBINAR_API_BASE: 'http://localhost:8000', WEBINAR_ALLOW_INSECURE_BACKEND: '1' }), 'http://localhost:8000');
  assert.equal(h.sdkScriptUrl({ WEBINAR_API_BASE: 'https://api.example.com' }), 'https://api.example.com/static/webinar-sdk/webinar-form.js');
  for (const bad of ['javascript:alert(1)', '//evil.example/x.js', 'http://cdn.example/x.js', 'https://u:p@cdn.example/x.js', '/a b.js']) {
    assert.equal(h.safeScriptSrc(bad), null, bad);
  }
  const occ = (id, open) => ({ id, startsAt: '2026-11-01T10:00:00Z', timezone: 'UTC', status: 'scheduled', registrationOpen: open });
  const A = '11111111-1111-4111-8111-111111111111';
  const B = '22222222-2222-4222-8222-222222222222';
  const raw = { slug: 'launch', title: 'Launch', formVersion: 'v1', secret: 'x', occurrences: [occ(A, false), occ(B, true), { id: 'nope' }] };
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url: String(url), init });
    return { status: 200, ok: true, text: async () => JSON.stringify(raw) };
  };
  const env = { WEBINAR_API_BASE: 'https://api.example.com' };
  assert.equal(await h.getWebinarEvent('../admin', { env, fetchImpl }), null);
  assert.equal(calls.length, 0, 'invalid slug never reaches the network');
  const event = await h.getWebinarEvent('launch', { env, fetchImpl });
  assert.equal(calls[0].url, 'https://api.example.com/api/v1/events/launch');
  assert.equal(calls[0].init.redirect, 'error');
  assert.ok(!('authorization' in calls[0].init.headers), 'public catalogue: no key sent');
  assert.equal(event.secret, undefined);
  assert.equal(event.occurrences.length, 2);
  assert.equal(h.pickOccurrence(event, A).id, B, 'closed pinned session falls back to next open');
  assert.deepEqual(h.formProps(event, h.pickOccurrence(event)), { occurrenceId: B, formVersion: 'v1' });

  const cms = await import(path.join(ROOT, 'sdk/kits/cms/resolve-reference.mjs'));
  assert.deepEqual(cms.toReference({ primary: { event_slug: 'launch', occurrence_id: B.toUpperCase() } }), { eventSlug: 'launch', occurrenceId: B });
  assert.deepEqual(cms.toReference({ fields: { eventSlug: 'launch' } }), { eventSlug: 'launch', occurrenceId: null });
  assert.equal(cms.toReference({ eventSlug: 'bad slug' }), null);
  const resolved = await cms.resolveWebinarReference({ eventSlug: 'launch', occurrenceId: B }, { env, fetchImpl });
  assert.deepEqual(resolved.form, { occurrenceId: B, formVersion: 'v1' });
});

test('fresh-install findings stay fixed (nginx precedence, runes, Next 16 segment config, quoted cookies, WP sdk path)', () => {
  // nginx: "location ^~ /webinar-api/" would shadow the regex route block, so every route 404s (nginx 1.27).
  const nginx = read('sdk/kits/node/nginx-webinar-api.conf').replace(/#.*$/gm, '');
  assert.match(nginx, /location ~ \^\/webinar-api\/\(profile\|registrations\|reset\|my-registration\|calendar\\\.ics\)\$/);
  assert.ok(!/location\s+\^~\s+\/webinar-api\//.test(nginx), 'catch-all must not use ^~');
  assert.match(nginx, /location \/webinar-api\/ \{\s*return 404;/);
  // SvelteKit 3 scaffolds compile with runes: true, where `export let` and `$:` are compile errors.
  const svelte = read('sdk/kits/frameworks/svelte/WebinarForm.svelte');
  assert.ok(svelte.includes('$props()') && svelte.includes('$effect('), 'runes syntax');
  assert.ok(!/^\s*export let /m.test(svelte) && !/^\s*\$:\s/m.test(svelte), 'no legacy reactive syntax');
  // Next 16 (cacheComponents) rejects route segment config; the page must use Suspense instead.
  const page = read('sdk/kits/frameworks/nextjs/app/webinars/[slug]/page.jsx');
  assert.ok(!/export const (revalidate|dynamic|fetchCache|runtime)\b/.test(page), 'no route segment config');
  assert.ok(page.includes('<Suspense') && page.includes('await params'), 'Suspense boundary + awaited params');
  // Django's delete_cookie emits webinar_session=""; both proxies must keep the expiry (SDK "Not you?" reset).
  const php = read(WP + 'includes/class-proxy.php');
  assert.equal(
    (() => { const { rewriteSetCookie } = require(path.join(ROOT, 'sdk/proxy/server.js'));
      return rewriteSetCookie('webinar_session=""; expires=Thu, 01 Jan 1970 00:00:00 GMT; HttpOnly; Max-Age=0; Path=/; SameSite=Lax', { sessionCookieName: 'webinar_session' }); })(),
    'webinar_session=; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Max-Age=0; HttpOnly; Secure; SameSite=Lax'
  );
  assert.ok(/'"' === \$value\[0\] && '"' === substr\( \$value, -1 \)/.test(php), 'PHP port unquotes DQUOTE-wrapped cookie values');
  // A self-hosted SDK path must resolve against home_url(), or a /blog/ install emits /blog/blog/....
  const settings = read(WP + 'includes/class-settings.php');
  assert.ok(settings.includes("'/' === $override[0] ? home_url( $override ) : $override"), 'sdk_url path resolves via home_url');
  // The repeatable fresh-install run exists, is excluded from node --test, and covers every kit stage.
  const fresh = read('sdk/kits/test/fresh-install.sh');
  for (const stage of ['wordpress()', 'next()', 'sveltekit()', 'approach_b()', 'react()', 'cms()', 'down()']) assert.ok(fresh.includes(stage), stage);
  for (const helper of ['flow.cjs', 'wp-admin.cjs', 'cms-validate.mjs', 'payload-check.ts', 'tsconfig.json', 'nginx-test-front.conf', 'wp-enable-cache.php', 'kit-form.py']) {
    assert.ok(fs.existsSync(path.join(ROOT, 'sdk/kits/test/fresh-install', helper)), helper);
  }
  for (const kit of manifest.kits) {
    assert.ok(kit.verified && typeof kit.verified.date === 'string' && typeof kit.verified.summary === 'string', `${kit.id}: verified entry`);
  }
});

test('CMS schema files store references only (slug + optional occurrence UUID)', () => {
  const contentful = JSON.parse(read('sdk/kits/cms/contentful/webinar-reference.import.json'));
  assert.deepEqual(contentful.contentTypes[0].fields.map((f) => f.id), ['eventSlug', 'occurrenceId']);
  const prismic = JSON.parse(read('sdk/kits/cms/prismic/webinar_reference/model.json'));
  assert.deepEqual(Object.keys(prismic.variations[0].primary), ['event_slug', 'occurrence_id']);
  for (const f of ['sdk/kits/cms/sanity/webinarReference.js', 'sdk/kits/cms/payload/webinarReference.ts']) {
    const src = read(f);
    assert.ok(src.includes("'eventSlug'") && src.includes("'occurrenceId'"), f);
    assert.ok(!/startsAt|title:\s*\{|price/i.test(src.replace(/title: '[^']*'/g, '')), `${f}: no duplicated event fields`);
  }
});

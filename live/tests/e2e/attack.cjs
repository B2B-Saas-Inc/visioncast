'use strict';
/*
 * Attacks on the running stack, as a hostile user would make them: forged links, tokens kept after
 * removal, traversal, stored scripts, login floods and forged webhooks. Unlike the other suites it
 * doesn't stop at the first failure, so one run reports everything. Run through ./run.sh with
 * E2E_SUITE=attack and LIVE_RTMP_ALLOW_PRIVATE=0.
 */
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright-core');

const BASE = process.env.LIVE_BASE;
const SECRET = process.env.LIVE_SERVICE_SECRET;
const PUBLIC = process.env.LIVE_PUBLIC_URL;
const WORK = path.dirname(process.env.LIVE_DB_PATH);
const ROOM = `vc-attack-${Date.now()}`;
const SLUG = `attack-${Date.now()}`;
const OTHER = `${ROOM}-other`;
const SCRIPT = '<img src=x onerror="window.__pwned=1"><script>window.__pwned=1</script>';

const failures = [];
let passed = 0;
async function check(name, fn) {
  try {
    await fn();
    passed += 1;
    console.log(`held    ${name}`);
  } catch (err) {
    failures.push(name);
    console.log(`BROKEN  ${name}\n        ${String(err.message).split('\n').slice(0, 3).join(' ').slice(0, 400)}`);
  }
}
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
async function until(what, fn, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  let last;
  while (Date.now() < deadline) {
    try { last = await fn(); if (last) return last; } catch (err) { last = err.message; }
    await wait(300);
  }
  throw new Error(`Timed out waiting for ${what}. Last: ${JSON.stringify(last)}`);
}
async function v1(method, url, body) {
  const res = await fetch(`${BASE}/v1${url}`, { method, headers: { Authorization: `Bearer ${SECRET}`, 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
  const data = await res.json().catch(() => ({}));
  assert.ok(res.ok, `${method} ${url}: ${res.status} ${JSON.stringify(data)}`);
  return data;
}
const ticketOf = (url) => /\/join\/([^/?#]+)/.exec(url)[1];
const person = async (room, identity) => ticketOf((await v1('POST', `/rooms/${room}/tickets`, { identity })).joinUrl);
const crew = async (room, role, name) => { const m = await v1('POST', `/rooms/${room}/crew`, { role, name }); return { ...m, ticket: ticketOf(m.url) }; };
const uuid = (n) => `${String(n).padStart(8, '0')}-8d4e-4f0a-9b1c-2d3e4f5a6b7c`;
const join = (ticket, rest = '', options = {}) => fetch(`${BASE}/join/${ticket}${rest}`, options);
const post = (ticket, rest, body) => join(ticket, rest, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
const act = (ticket, action) => post(ticket, '/act', { action });
const enter = async (ticket, name = 'Probe') => (await post(ticket, '/enter', { name })).json();
const claims = (token) => JSON.parse(Buffer.from(token.split('.')[1], 'base64url').toString());
const refused = (status) => status >= 400 && status < 500;

// Joins LiveKit straight from a page with a token kept from earlier, as someone holding it could.
const connect = (page, url, token) => page.evaluate(async ([u, t]) => {
  const room = new LivekitClient.Room();
  window.__rooms = window.__rooms || [];
  window.__rooms.push(room);
  try { await room.connect(u, t); return window.__rooms.length - 1; } catch (err) { return `refused: ${err.message}`; }
}, [url, token]);
const stateOf = (page, index) => page.evaluate((i) => window.__rooms[i].state, index);

(async () => {
  const now = Date.now();
  const times = { startsAt: new Date(now + 600000).toISOString(), endsAt: new Date(now + 3600000).toISOString() };
  await v1('PUT', `/rooms/${ROOM}`, { title: 'Attack', slug: SLUG, ...times });
  await v1('PUT', `/rooms/${OTHER}`, { title: 'Other', ...times });
  const host = await crew(ROOM, 'host', 'Hal Host');
  const producer = await crew(ROOM, 'producer', 'Pat Producer');
  const guest = await crew(ROOM, 'guest', 'Gil Guest');
  const attendee = await person(ROOM, uuid(1));
  const elsewhere = await person(OTHER, uuid(2));
  await enter(host.ticket, 'Hal Host');
  await enter(guest.ticket, 'Gil Guest');
  await enter(attendee, 'Ann Attendee');

  await check('altered and invented links get nothing', async () => {
    const parts = attendee.split('.');
    const tag = parts[parts.length - 1];
    const flipped = (tag[0] === 'A' ? 'B' : 'A') + tag.slice(1);
    const bad = [
      [...parts.slice(0, -1), flipped].join('.'),
      [...parts.slice(0, -1), tag.slice(0, -2)].join('.'),
      [...parts.slice(0, -1), ''].join('.'),
      `a.${ROOM}.${uuid(9)}.${tag}`,
      `a.${OTHER}.${uuid(1)}.${tag}`,
      'nonsense',
      '..%2F..%2Fetc',
    ];
    for (const ticket of bad) {
      for (const [method, rest] of [['GET', '/info'], ['POST', '/enter'], ['POST', '/act'], ['GET', '/events'], ['GET', '/files/x']]) {
        const res = await join(ticket, rest, { method, headers: { 'Content-Type': 'application/json' }, body: method === 'POST' ? '{"name":"x","action":{"type":"go_live"}}' : undefined });
        assert.ok(refused(res.status), `${ticket} ${rest}: ${res.status}`);
      }
    }
  });

  await check("an attendee's link can't be rewritten into a host's by changing its role letter", async () => {
    const [, room, identity, tag] = attendee.split('.');
    const hostParts = host.ticket.split('.');
    for (const code of ['h', 'p', 'g', 'c', 'r', 'host', hostParts[0]]) {
      const res = await act(`${code}.${room}.${identity}.${tag}`, { type: 'go_live' });
      assert.ok(refused(res.status), `${code}: ${res.status}`);
    }
    const stolen = await act(`${hostParts[0]}.${room}.${hostParts[2]}.${tag}`, { type: 'go_live' });
    assert.ok(refused(stolen.status), `${stolen.status}`);
    assert.equal((await v1('GET', `/rooms/${ROOM}`)).phase ?? 'scheduled', (await v1('GET', `/rooms/${ROOM}`)).phase ?? 'scheduled');
  });

  const crewOnly = [
    { type: 'go_live' }, { type: 'end_session' }, { type: 'record', on: true },
    { type: 'stage_add', identity: uuid(1) }, { type: 'stage_remove', identity: uuid(1) },
    { type: 'set_lower_third', title: 'x', subtitle: null }, { type: 'set_ticker', text: 'x' },
    { type: 'chat_delete', id: 'x' }, { type: 'chat_approve', id: 'x' }, { type: 'chat_pin', id: 'x' },
    { type: 'block', identity: host.id, on: true }, { type: 'remove', identity: uuid(1) },
    { type: 'poll_save', id: null, question: 'q', options: ['a', 'b'], correct: null, showResults: 'live', showCounts: true },
    { type: 'poll_delete', id: 'x' }, { type: 'qa_moderate', id: 'x', action: 'approve' }, { type: 'file_delete', id: 'x' },
    { type: 'file_public', id: 'x', public: true }, { type: 'invite', identity: uuid(1), on: true },
    { type: 'simulive_clear' }, { type: 'slides_stop' }, { type: 'breakouts_close' }, { type: 'survey_results' },
    { type: 'ingress_create', name: 'x' }, { type: 'ingress_list' }, { type: 'layout_save', name: 'x' }, { type: 'list_recordings' },
  ];
  await check('an attendee is refused every action that runs the show', async () => {
    for (const action of crewOnly) {
      const res = await act(attendee, action);
      assert.equal(res.status, 403, `${action.type}: ${res.status} ${await res.text()}`);
      await wait(80); // Under the limit on actions, which has its own check below.
    }
  });

  await check('a guest is refused going live, ending, removing people and polls', async () => {
    for (const action of crewOnly.filter((a) => ['go_live', 'end_session', 'record', 'block', 'remove', 'poll_save', 'chat_delete', 'ingress_create', 'list_recordings'].includes(a.type))) {
      const res = await act(guest.ticket, action);
      assert.equal(res.status, 403, `${action.type}: ${res.status}`);
      await wait(80);
    }
  });

  await check("someone can't remove the host or a crew member from outside their room", async () => {
    const { identity } = await enter(host.ticket, 'Hal Host');
    for (const [who, ticket] of [['a producer', producer.ticket], ['the host themselves', host.ticket]]) {
      const res = await act(ticket, { type: 'remove', identity });
      assert.ok(refused(res.status), `${who} removing the host: ${res.status}`);
    }
    assert.ok(refused((await act(elsewhere, { type: 'remove', identity })).status), 'an attendee of another room');
    assert.equal((await join(host.ticket, '/info')).status, 200);
  });

  await check('attendee links are refused by the routes meant for others', async () => {
    assert.ok(refused((await post(attendee, '/share-token', {})).status));
    assert.ok(refused((await join(attendee, '/files?kind=resource&name=a.pdf', { method: 'PUT', body: 'x' })).status));
    assert.ok(refused((await join(attendee, '/local/abc/0', { method: 'PUT', body: 'x' })).status));
    assert.ok(refused((await join(attendee, '/captions/all.srt')).status));
    for (const url of [`${BASE}/program/${attendee}`, `${BASE}/captions/in/${attendee}`, `${BASE}/program/nonsense`, `${BASE}/captions/in/nonsense`]) {
      assert.ok(refused((await fetch(url)).status), url);
    }
  });

  await check('the tokens each role gets carry no more than that role should have', async () => {
    const hosted = await enter(host.ticket, 'Hal Host');
    const producing = await enter(producer.ticket, 'Pat Producer');
    const guesting = await enter(guest.ticket, 'Gil Guest');
    for (const [who, token] of [['host', hosted.stageToken], ['host green', hosted.greenToken], ['producer', producing.stageToken], ['guest', guesting.stageToken]]) {
      const c = claims(token);
      assert.ok(c.exp - c.nbf <= 900 + 5, `${who} lasts ${c.exp - c.nbf}s`);
      for (const power of ['roomAdmin', 'roomCreate', 'roomRecord', 'roomList', 'ingressAdmin']) assert.ok(!c.video[power], `${who} has ${power}`);
    }
    assert.equal(claims(producing.stageToken).video.canPublish, false);
    assert.equal(claims(producing.stageToken).video.hidden, true);
    assert.equal(claims(guesting.stageToken).video.canPublish, false, "a guest off stage can't publish there");
    assert.ok(claims(guesting.greenToken).video.room.endsWith('--bs'));
    assert.equal((await enter(attendee)).stageToken, undefined, 'no token before going live');
  });

  await check('files outside the upload folder stay out of reach', async () => {
    const sentinel = path.join(WORK, 'sentinel.txt');
    fs.writeFileSync(sentinel, 'SENTINEL');
    const sneaky = ['../sentinel.txt', '../../sentinel.txt', '..%2Fsentinel.txt', '..%2F..%2Fsentinel.txt', '%2e%2e%2fsentinel.txt', '..%5Csentinel.txt', '%2e%2e/%2e%2e/sentinel.txt', '....//sentinel.txt'];
    for (const id of sneaky) {
      assert.ok(refused((await act(host.ticket, { type: 'file_delete', id: decodeURIComponent(id) })).status), `delete ${id}`);
      for (const rest of [`/files/${id}`, `/files/${id}/pages/1`, `/local/${id}`, `/captions/${id}`]) {
        const res = await join(host.ticket, rest);
        assert.ok(!(await res.text()).includes('SENTINEL'), `${rest} returned the file`);
      }
      await join(host.ticket, `/local/${id}/0`, { method: 'PUT', body: 'OVERWRITTEN' });
    }
    assert.equal(fs.readFileSync(sentinel, 'utf8'), 'SENTINEL');
    assert.ok(!fs.existsSync(path.join(WORK, 'sentinel.txt.webm')));
    assert.ok(refused((await fetch(`${BASE}/v1/rooms/..%2F..%2Fetc`, { method: 'PUT', headers: { Authorization: `Bearer ${SECRET}`, 'Content-Type': 'application/json' }, body: JSON.stringify(times) })).status));
  });

  await check("uploads can't run as pages on the service's address", async () => {
    const svg = '<svg xmlns="http://www.w3.org/2000/svg"><script>window.__pwned=1</script></svg>';
    for (const [kind, name, body] of [['resource', 'evil.html', '<script>1</script>'], ['resource', 'evil.pdf', '<script>1</script>'], ['graphic', 'evil.html', '<script>1</script>'], ['graphic', 'evil.svg', svg], ['graphic', 'evil.png', svg], ['banner', 'evil.svg', svg], ['frame', 'evil.html', '<script>1</script>']]) {
      const put = await join(host.ticket, `/files?kind=${kind}&name=${encodeURIComponent(name)}`, { method: 'PUT', body });
      if (!put.ok) continue;
      const { id } = await put.json();
      await act(host.ticket, { type: 'file_public', id, public: true });
      const got = await join(host.ticket, `/files/${id}`);
      const type = got.headers.get('content-type') || '';
      assert.ok(!/html/i.test(type), `${kind} ${name} came back as ${type}`);
      if (/svg/i.test(type)) {
        assert.ok(!/unsafe-inline/.test(got.headers.get('content-security-policy') || ''), 'an SVG with scripts allowed');
        assert.ok(got.headers.get('content-security-policy'), 'an SVG with no CSP');
      }
      assert.equal(got.headers.get('x-content-type-options'), 'nosniff');
    }
  });

  await check("a file name can't add a response header or escape the quotes", async () => {
    const name = 'a"; filename="evil.exe\r\nX-Evil: 1.pdf';
    const put = await join(host.ticket, `/files?kind=resource&name=${encodeURIComponent(name)}`, { method: 'PUT', body: 'x' });
    if (!put.ok) return;
    const got = await join(host.ticket, `/files/${(await put.json()).id}`);
    assert.equal(got.headers.get('x-evil'), null);
    assert.ok(!/filename="[^"]*"[^"]*filename=/.test(got.headers.get('content-disposition') || ''), got.headers.get('content-disposition'));
  });

  await check('a link for one room reads nothing from another', async () => {
    const put = await join(host.ticket, '/files?kind=resource&name=a.pdf', { method: 'PUT', body: 'private' });
    const { id } = await put.json();
    await act(host.ticket, { type: 'file_public', id, public: true });
    assert.equal((await join(attendee, `/files/${id}`)).status, 200, 'shared in its own room');
    assert.ok(refused((await join(elsewhere, `/files/${id}`)).status));
  });

  await check("an attendee's event stream carries no crew chat, drafts or people", async () => {
    const marker = `MARKER${Date.now()}`;
    const abort = new AbortController();
    const res = await join(attendee, '/events', { signal: abort.signal });
    let seen = '';
    const reading = (async () => { for await (const chunk of res.body) seen += Buffer.from(chunk).toString(); })().catch(() => {});
    await wait(800);
    const crewLine = await act(host.ticket, { type: 'chat_send', text: `${marker}-crew`, channel: 'stage' });
    assert.equal(crewLine.status, 200, 'the crew message was sent');
    const draft = await act(host.ticket, { type: 'poll_save', id: null, question: `${marker}-draft`, options: ['a', 'b'], correct: null, showResults: 'live', showCounts: true });
    assert.equal(draft.status, 200, 'the draft was saved');
    const publicLine = await act(host.ticket, { type: 'chat_send', text: `${marker.toLowerCase()}-public`, channel: 'event' });
    assert.equal(publicLine.status, 200, 'the public message was sent');
    await until('the public message to arrive', async () => seen.includes(`${marker.toLowerCase()}-public`), 5000);
    abort.abort();
    await reading;
    assert.ok(seen.length > 0, 'the stream sent nothing');
    assert.ok(!seen.includes(marker), 'leaked to an attendee');
    for (const word of ['stream_key', 'streamKey', 'sealed', 'simulive', 'savedLayouts']) assert.ok(!seen.includes(`"${word}"`), `${word} in the stream`);
  });

  await check('oversized bodies and floods are turned away', async () => {
    assert.equal((await act(attendee, { type: 'chat_send', text: 'x'.repeat(300 * 1024), channel: 'event' })).status, 413);
    // A long message is cut short, not refused: what reaches other people is never over the limit.
    const abortLong = new AbortController();
    let delivered = '';
    const reader = (async () => { for await (const c of (await join(host.ticket, '/events', { signal: abortLong.signal })).body) delivered += Buffer.from(c).toString(); })().catch(() => {});
    await wait(500);
    assert.equal((await act(attendee, { type: 'chat_send', text: 'z'.repeat(5000), channel: 'event' })).status, 200);
    await until('the long message', async () => delivered.includes('zzzzzzzz'), 5000);
    abortLong.abort();
    await reader;
    const longest = Math.max(...(delivered.match(/z+/g) || ['']).map((run) => run.length));
    assert.ok(longest <= 500, `${longest} characters went out`);
    const flood = await Promise.all(Array.from({ length: 100 }, () => act(attendee, { type: 'hand', up: false })));
    assert.ok(flood.some((r) => r.status === 429), 'no 429 from 100 actions at once');
    await wait(1200);
    const abort = new AbortController();
    const streams = [];
    for (let i = 0; i < 7; i += 1) streams.push(await join(attendee, '/events', { signal: abort.signal }));
    assert.ok(streams.some((r) => r.status === 429), `streams: ${streams.map((r) => r.status)}`);
    abort.abort();
    await wait(300);
  });

  await check('nothing reflects a script from the address', async () => {
    const payload = encodeURIComponent('<script>alert(1)</script>');
    for (const url of [`/${payload}`, `/${payload}/lobby`, `/${payload}/green-room`, `/login/link/${payload}`, `/login/set/${payload}`, `/join/${payload}`, `/assets/${payload}`]) {
      const res = await fetch(`${BASE}${url}`, { redirect: 'manual' });
      assert.ok(!(await res.text()).includes('<script>alert'), url);
    }
  });

  await check('private routes are refused without the secret or a session', async () => {
    const privateRoutes = [['GET', `/v1/rooms/${ROOM}`], ['PUT', `/v1/rooms/${ROOM}`], ['POST', `/v1/rooms/${ROOM}/tickets`], ['GET', `/v1/rooms/${ROOM}/attendance`], ['GET', `/v1/rooms/${ROOM}/recordings`], ['POST', '/v1/admin-link'], ['GET', `/v1/rooms/${ROOM}/crew`], ['POST', `/v1/rooms/${ROOM}/crew`], ['GET', `/v1/rooms/${ROOM}/engagement`], ['GET', `/v1/rooms/${ROOM}/settings`], ['GET', '/v1/brand'], ['PUT', '/v1/brand'], ['GET', '/v1/saved-destinations'], ['GET', `/v1/rooms/${ROOM}/destinations`], ['POST', `/v1/rooms/${ROOM}/people/${uuid(1)}/forget`]];
    for (const [method, url] of privateRoutes) {
      for (const authorization of [undefined, 'Bearer wrong', `Bearer ${SECRET}x`, `Basic ${SECRET}`, `Bearer ${process.env.LIVE_TICKET_KEY}`]) {
        const res = await fetch(`${BASE}${url}`, { method, headers: { 'Content-Type': 'application/json', ...(authorization ? { Authorization: authorization } : {}) }, body: method === 'GET' ? undefined : '{}' });
        assert.equal(res.status, 401, `${method} ${url} with ${authorization}: ${res.status}`);
      }
    }
    for (const url of ['/rooms', '/accounts', '/activity', '/me', `/rooms/${ROOM}`, '/brand']) {
      for (const cookie of [undefined, 'vc_admin=forged', `vc_session=${SECRET}`]) {
        const res = await fetch(`${BASE}/admin/api${url}`, { headers: cookie ? { Cookie: cookie } : {} });
        assert.equal(res.status, 401, `${url} with ${cookie}: ${res.status}`);
      }
    }
  });

  await check('forged LiveKit webhooks are ignored', async () => {
    const body = JSON.stringify({ event: 'participant_left', room: { name: ROOM }, participant: { identity: uuid(1) } });
    const b64 = (v) => Buffer.from(JSON.stringify(v)).toString('base64url');
    const sign = (claimsIn, key) => { const head = b64({ alg: 'HS256', typ: 'JWT' }); const data = b64(claimsIn); return `${head}.${data}.${crypto.createHmac('sha256', key).update(`${head}.${data}`).digest('base64url')}`; };
    const sum = crypto.createHash('sha256').update(body).digest('base64');
    const nowSeconds = Math.floor(Date.now() / 1000);
    const later = nowSeconds + 600;
    // Complete claims, so each forgery is refused for what's wrong with it and not for its shape.
    const fields = { sub: 'x', nbf: nowSeconds - 600, video: {} };
    const attempts = [
      undefined,
      'nonsense',
      sign({ ...fields, iss: process.env.LIVEKIT_API_KEY, exp: later, sha256: sum }, 'wrong-secret-wrong-secret-wrong-secret'),
      sign({ ...fields, iss: process.env.LIVEKIT_API_KEY, exp: later, sha256: crypto.createHash('sha256').update('something else').digest('base64') }, process.env.LIVEKIT_API_SECRET),
      sign({ ...fields, iss: process.env.LIVEKIT_API_KEY, exp: nowSeconds - 300, sha256: sum }, process.env.LIVEKIT_API_SECRET),
      `${b64({ alg: 'none' })}.${b64({ ...fields, iss: process.env.LIVEKIT_API_KEY, exp: later, sha256: sum })}.`,
    ];
    for (const authorization of attempts) {
      const res = await fetch(`${BASE}/webhooks/livekit`, { method: 'POST', headers: { 'Content-Type': 'application/webhook+json', ...(authorization ? { Authorization: authorization } : {}) }, body });
      assert.ok(refused(res.status), `attempt ${attempts.indexOf(authorization)}: ${res.status}`);
    }
    // The same token, properly signed, is what LiveKit sends, so the refusals above mean something.
    const genuine = await fetch(`${BASE}/webhooks/livekit`, { method: 'POST', headers: { 'Content-Type': 'application/webhook+json', Authorization: sign({ ...fields, iss: process.env.LIVEKIT_API_KEY, exp: later, sha256: sum }, process.env.LIVEKIT_API_SECRET) }, body });
    assert.equal(genuine.status, 200, 'a genuine webhook');
  });

  await check('a broadcast address can only be a public one', async () => {
    for (const server of ['rtmp://127.0.0.1/x', 'rtmp://localhost/x', 'rtmp://169.254.169.254/x', 'rtmp://[::ffff:127.0.0.1]/x', 'rtmp://[::1]/x', 'rtmp://10.0.0.1/x', 'rtmp://192.168.1.1/x', 'rtmp://0.0.0.0/x', 'rtmp://2130706433/x', 'rtmp://0x7f.0.0.1/x', 'http://example.com/x', 'file:///etc/passwd', 'rtmp://user:pass@127.0.0.1/x']) {
      const res = await fetch(`${BASE}/v1/rooms/${ROOM}/destinations`, { method: 'PUT', headers: { Authorization: `Bearer ${SECRET}`, 'Content-Type': 'application/json' }, body: JSON.stringify({ destinations: [{ platform: 'custom', server, key: 'k', armed: true }] }) });
      assert.ok(refused(res.status), `${server}: ${res.status}`);
    }
  });

  await check('brand settings refuse anything that could break out of a colour or font', async () => {
    for (const body of [{ accent: 'red;}*{display:none}' }, { accent: `url(${BASE})` }, { accent: '</style><script>1</script>' }, { stage: 'expression(1)' }, { mode: '</style>' }, { font: '"};*{display:none}' }]) {
      const res = await fetch(`${BASE}/v1/brand`, { method: 'PUT', headers: { Authorization: `Bearer ${SECRET}`, 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      assert.ok(refused(res.status), `${JSON.stringify(body)}: ${res.status}`);
    }
  });

  await check('an owner signs in; the session cookie is locked down, and floods stop the right password too', async () => {
    const email = process.env.LIVE_ADMIN_EMAIL;
    const login = (password, forwarded) => fetch(`${BASE}/login`, { method: 'POST', redirect: 'manual', headers: { 'Content-Type': 'application/json', ...(forwarded ? { 'X-Forwarded-For': forwarded } : {}) }, body: JSON.stringify({ email, password }) });
    const good = await login(process.env.LIVE_ADMIN_PASSWORD);
    assert.ok(good.status < 400, `the real password: ${good.status}`);
    const cookie = good.headers.get('set-cookie');
    assert.match(cookie, /HttpOnly/);
    assert.match(cookie, /SameSite=Strict/);
    const session = cookie.split(';')[0];
    assert.equal((await fetch(`${BASE}/admin/api/me`, { headers: { Cookie: session } })).status, 200);
    const statuses = [];
    for (let i = 0; i < 9; i += 1) statuses.push((await login(`wrong-${i}`, `203.0.113.${i}`)).status);
    assert.ok(statuses.slice(0, 8).every((s) => s === 401), `first tries: ${statuses}`);
    assert.equal(statuses[8], 429, `the ninth try, with a spoofed address each time: ${statuses}`);
    assert.equal((await login(process.env.LIVE_ADMIN_PASSWORD)).status, 429, 'the right password during the lockout');
    assert.equal((await fetch(`${BASE}/admin/api/me`, { headers: { Cookie: session } })).status, 200, 'an existing session keeps working');
    const unknown = await fetch(`${BASE}/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email: 'nobody@example.com', password: 'x'.repeat(2000) }) });
    assert.ok(refused(unknown.status), `a very long password: ${unknown.status}`);
  });

  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE, args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream', '--autoplay-policy=no-user-gesture-required'] });
  const openRoom = async (ticket, name) => {
    const context = await browser.newContext({ permissions: ['camera', 'microphone'], viewport: { width: 1280, height: 800 } });
    const page = await context.newPage();
    await page.goto(`${BASE}/join/${ticket}`);
    await page.waitForSelector('#join-form:not([hidden])');
    if (await page.isVisible('#name')) await page.fill('#name', name);
    await page.click('#join');
    await page.waitForSelector('#room:not([hidden])');
    return page;
  };
  const hostPage = await openRoom(host.ticket, 'Hal Host');
  await until('the host in the green room', () => hostPage.$('.gtile .mini'));
  await hostPage.click('.gtile .mini:text-is("Put on stage")');
  await hostPage.click('#go-live');
  await until('the session live', async () => (await enter(attendee)).stageToken);

  await check('words from people stay words in every browser that shows them', async () => {
    const viewer = await person(ROOM, uuid(3));
    await enter(viewer, SCRIPT);
    assert.equal((await act(viewer, { type: 'chat_send', text: SCRIPT, channel: 'event' })).status, 200);
    assert.equal((await act(viewer, { type: 'qa_ask', text: SCRIPT, anonymous: false })).status, 200);
    await act(viewer, { type: 'hand', up: true });
    assert.equal((await act(host.ticket, { type: 'set_lower_third', title: SCRIPT, subtitle: SCRIPT })).status, 200);
    assert.equal((await act(host.ticket, { type: 'set_ticker', text: SCRIPT })).status, 200);
    const saved = await (await act(host.ticket, { type: 'poll_save', id: null, question: SCRIPT, options: [SCRIPT, 'b'], correct: null, showResults: 'live', showCounts: true, popup: true })).json();
    assert.ok(saved.id, JSON.stringify(saved));
    await act(host.ticket, { type: 'poll_status', id: saved.id, status: 'open' });
    const viewerPage = await openRoom(await person(ROOM, uuid(4)), 'Second Viewer');
    await wait(2500);
    for (const [who, page] of [['host', hostPage], ['viewer', viewerPage]]) {
      assert.equal(await page.evaluate(() => window.__pwned), undefined, `a script ran on the ${who}'s page`);
      assert.equal(await page.evaluate(() => document.querySelectorAll('img[src="x"], img[src$="/x"]').length), 0, `an image tag was built on the ${who}'s page`);
    }
    assert.ok((await hostPage.textContent('#messages')).includes('onerror'), 'the words are shown as words');
    await viewerPage.context().close();
  });

  await check("a call-to-action link can't be a script", async () => {
    for (const url of ['javascript:window.__pwned=1', 'JaVaScRiPt:window.__pwned=1', 'data:text/html,<script>1</script>', ' javascript:1', 'vbscript:1']) {
      const res = await act(host.ticket, { type: 'set_cta', title: 't', subtitle: null, label: 'go', url });
      if (res.ok) {
        const href = await until('the link on the page', () => hostPage.evaluate(() => { const a = document.querySelector('.pic-cta'); return a ? a.getAttribute('href') || 'none' : null; }), 3000).catch(() => 'none');
        assert.ok(!/^\s*(javascript|data|vbscript):/i.test(href), `${url} became ${href}`);
      }
    }
    await act(host.ticket, { type: 'set_cta', title: null, subtitle: null, label: null, url: null });
  });

  const attackerPage = await (await browser.newContext()).newPage();
  await attackerPage.goto(`${BASE}/join/${attendee}`);
  await attackerPage.waitForFunction(() => typeof LivekitClient !== 'undefined');

  await check("an attendee's own token really can't publish or send data", async () => {
    const kept = await enter(attendee);
    const listener = await enter(await person(ROOM, uuid(7)), 'Listener');
    const outcome = await attackerPage.evaluate(async ([u, t, l]) => {
      const room = new LivekitClient.Room();
      await room.connect(u, t);
      const ear = new LivekitClient.Room();
      let heard = 0;
      ear.on(LivekitClient.RoomEvent.DataReceived, () => { heard += 1; });
      await ear.connect(u, l);
      const tries = {};
      try { await room.localParticipant.setMicrophoneEnabled(true); tries.mic = 'published'; } catch (e) { tries.mic = 'refused'; }
      try { await room.localParticipant.publishData(new TextEncoder().encode('x'), { reliable: true }); tries.data = 'sent'; } catch (e) { tries.data = 'refused'; }
      try { await room.localParticipant.setMetadata('admin'); tries.meta = 'set'; } catch (e) { tries.meta = 'refused'; }
      await new Promise((r) => setTimeout(r, 1500));
      tries.published = Array.from(room.localParticipant.trackPublications.values()).filter((p) => p.track && p.track.sid).length;
      tries.canPublish = room.localParticipant.permissions ? room.localParticipant.permissions.canPublish : null;
      tries.canPublishData = room.localParticipant.permissions ? room.localParticipant.permissions.canPublishData : null;
      tries.heard = heard;
      await room.disconnect();
      await ear.disconnect();
      return tries;
    }, [kept.url, kept.stageToken, listener.stageToken]);
    assert.equal(outcome.canPublish, false, JSON.stringify(outcome));
    assert.equal(outcome.canPublishData, false, JSON.stringify(outcome));
    assert.equal(outcome.published, 0, JSON.stringify(outcome));
    assert.equal(outcome.heard, 0, `another viewer received it: ${JSON.stringify(outcome)}`);
  });

  await check('a removed attendee is put out of LiveKit, and a kept token gets thrown out again', async () => {
    const victim = uuid(5);
    const ticket = await person(ROOM, victim);
    const kept = await enter(ticket, 'Removed Viewer');
    const index = await connect(attackerPage, kept.url, kept.stageToken);
    assert.equal(typeof index, 'number', String(index));
    assert.equal((await act(host.ticket, { type: 'remove', identity: victim })).status, 200);
    await until('the first connection to be dropped', async () => (await stateOf(attackerPage, index)) === 'disconnected');
    assert.ok(refused((await post(ticket, '/enter', { name: 'x' })).status), 'entering again');
    assert.ok(refused((await act(ticket, { type: 'chat_send', text: 'still here', channel: 'all' })).status), 'chatting');
    assert.ok(refused((await join(ticket, '/events')).status), 'listening');
    const again = await connect(attackerPage, kept.url, kept.stageToken);
    if (typeof again === 'number') await until('the kept token to be thrown out', async () => (await stateOf(attackerPage, again)) === 'disconnected', 40000);
  });

  await check('removed crew lose their link, and their kept tokens get thrown out', async () => {
    const member = await crew(ROOM, 'guest', 'Fired Guest');
    const kept = await enter(member.ticket, 'Fired Guest');
    const abort = new AbortController();
    const stream = await join(member.ticket, '/events', { signal: abort.signal });
    const reading = (async () => { let seen = ''; for await (const c of stream.body) seen += Buffer.from(c).toString(); return seen; })().catch(() => 'cut');
    const green = await connect(attackerPage, kept.url, kept.greenToken);
    assert.equal(typeof green, 'number', String(green));
    await v1('POST', `/rooms/${ROOM}/crew/${member.id}/remove`);
    const ended = await Promise.race([reading, wait(10000).then(() => 'still open')]);
    assert.notEqual(ended, 'still open', 'the event stream stayed open');
    abort.abort();
    assert.ok(refused((await join(member.ticket, '/info')).status), 'the link still answers');
    assert.ok(refused((await post(member.ticket, '/enter', { name: 'x' })).status), 'entering again');
    await until('the green room connection to be dropped', async () => (await stateOf(attackerPage, green)) === 'disconnected');
    const again = await connect(attackerPage, kept.url, kept.greenToken);
    if (typeof again === 'number') await until('the kept green room token to be thrown out', async () => (await stateOf(attackerPage, again)) === 'disconnected', 40000);
    const stage = await connect(attackerPage, kept.url, kept.stageToken);
    if (typeof stage === 'number') await until('the kept stage token to be thrown out', async () => (await stateOf(attackerPage, stage)) === 'disconnected', 40000);
  });

  await check('a new link voids the old one', async () => {
    const member = await crew(ROOM, 'producer', 'Rotated Producer');
    const fresh = await v1('POST', `/rooms/${ROOM}/crew/${member.id}/new-link`);
    assert.ok(refused((await join(member.ticket, '/info')).status), 'the old link still answers');
    assert.ok(refused((await act(member.ticket, { type: 'go_live' })).status), 'the old link can act');
    assert.equal((await join(ticketOf(fresh.url), '/info')).status, 200);
  });

  await check("a viewer's token kept after the session ends gets nothing", async () => {
    const kept = await enter(await person(ROOM, uuid(6)), 'Lingering Viewer');
    const before = await connect(attackerPage, kept.url, kept.stageToken);
    assert.equal(typeof before, 'number', String(before));
    assert.equal((await act(host.ticket, { type: 'end_session' })).status, 200);
    await until('the live connection to end', async () => (await stateOf(attackerPage, before)) === 'disconnected');
    await wait(1000);
    const after = await connect(attackerPage, kept.url, kept.stageToken);
    if (typeof after === 'number') await until('the kept token to be thrown out', async () => (await stateOf(attackerPage, after)) === 'disconnected', 40000);
  });

  await browser.close();
  console.log(`\n${passed} held, ${failures.length} broken`);
  process.exit(failures.length ? 1 : 0);
})().catch((err) => { console.error(err); process.exit(1); });

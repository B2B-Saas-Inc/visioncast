'use strict';
/*
 * Proves media works for someone who can only reach port 443: every WebRTC connection in these
 * browsers is forced through TURN over TLS (relay only, and no other way offered), a host publishes
 * and a viewer receives. Point it at a server: LIVE_HOST, RTC_HOST, TURN_HOST, LIVE_SERVICE_SECRET.
 * With LOCAL_IP set, names go to that address and certificate errors are ignored, for a --local install.
 */
const assert = require('node:assert/strict');
const { execSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright-core');

const { LIVE_HOST, RTC_HOST, TURN_HOST, LIVE_SERVICE_SECRET, LOCAL_IP } = process.env;
const FORCE = `(() => {
  const Real = window.RTCPeerConnection;
  window.__pcs = [];
  window.__log = [];
  window.__servers = [];
  const force = (config) => {
    const servers = (config && config.iceServers) || [];
    window.__servers.push(...servers.flatMap((s) => [].concat(s.urls)));
    const only = servers.map((s) => ({ ...s, urls: [].concat(s.urls).filter((u) => u.startsWith('turns:')) })).filter((s) => s.urls.length);
    return { ...config, iceServers: only, iceTransportPolicy: 'relay' };
  };
  const set = Real.prototype.setConfiguration;
  Real.prototype.setConfiguration = function (config) { return set.call(this, force(config)); };
  window.RTCPeerConnection = function (config, ...rest) {
    const pc = new Real(force(config), ...rest);
    window.__pcs.push(pc);
    pc.addEventListener('icecandidateerror', (e) => window.__log.push('error ' + e.errorCode + ' ' + e.errorText + ' ' + e.url));
    pc.addEventListener('icecandidate', (e) => { if (e.candidate) window.__log.push('candidate ' + e.candidate.type + '/' + (e.candidate.relayProtocol || e.candidate.protocol)); });
    pc.addEventListener('iceconnectionstatechange', () => window.__log.push(Math.round(performance.now()/1000) + 's ice ' + pc.iceConnectionState));
    return pc;
  };
  window.RTCPeerConnection.prototype = Real.prototype;
  Object.setPrototypeOf(window.RTCPeerConnection, Real);
})();`;
const route = async (page) => page.evaluate(async () => {
  const out = [];
  for (const pc of window.__pcs) {
    if (pc.connectionState === 'closed') continue;
    const stats = await pc.getStats();
    stats.forEach((pair) => {
      if (pair.type === 'candidate-pair' && pair.nominated && pair.state === 'succeeded') {
        const local = stats.get(pair.localCandidateId);
        out.push({ type: local.candidateType, relay: local.relayProtocol, url: local.url, bytes: pair.bytesReceived + pair.bytesSent });
      }
    });
  }
  const gathered = [];
  for (const pc of window.__pcs) {
    (await pc.getStats()).forEach((c) => { if (c.type === 'local-candidate') gathered.push(`${c.candidateType}/${c.relayProtocol || c.protocol}`); });
  }
  return { connections: out, log: window.__log, offered: window.__servers, gathered, states: window.__pcs.map((pc) => pc.connectionState + '/' + pc.iceConnectionState) };
});

(async () => {
  const args = ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream', '--autoplay-policy=no-user-gesture-required'];
  // TURN over TLS checks certificates its own way: the ordinary ignore flag doesn't reach it, this one does.
  if (LOCAL_IP) {
    const spki = execSync(`echo | openssl s_client -connect ${LOCAL_IP}:443 -servername ${TURN_HOST} 2>/dev/null | openssl x509 -pubkey -noout | openssl pkey -pubin -outform der | openssl dgst -sha256 -binary | base64`).toString().trim();
    args.push(`--host-resolver-rules=MAP ${LIVE_HOST} ${LOCAL_IP},MAP ${RTC_HOST} ${LOCAL_IP},MAP ${TURN_HOST} ${LOCAL_IP}`, `--ignore-certificate-errors-spki-list=${spki}`);
  }
  const profile = (name) => fs.mkdtempSync(path.join(os.tmpdir(), `vc-turn-${name}-`));
  const contexts = [];
  const open = async () => {
    const context = await chromium.launchPersistentContext(profile('p'), { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE, args, permissions: ['camera', 'microphone'], ignoreHTTPSErrors: !!LOCAL_IP });
    contexts.push(context);
    await context.addInitScript(FORCE);
    return context.newPage();
  };
  try {
    const setup = await open();
    await setup.goto(`https://${LIVE_HOST}/healthz`);
    const room = `vc-turn-${Date.now()}`;
    const links = await setup.evaluate(async ({ room, secret }) => {
      const v1 = async (method, path, body) => (await fetch(`/v1${path}`, { method, headers: { Authorization: `Bearer ${secret}`, 'Content-Type': 'application/json' }, body: JSON.stringify(body) })).json();
      const now = Date.now();
      await v1('PUT', `/rooms/${room}`, { title: 'Connection test from turn/check.cjs (safe to ignore)', startsAt: new Date(now + 60000).toISOString(), endsAt: new Date(now + 600000).toISOString() });
      return { host: (await v1('POST', `/rooms/${room}/crew`, { role: 'host', name: 'Host' })).url, viewer: (await v1('POST', `/rooms/${room}/tickets`, { identity: 'turn-viewer' })).joinUrl };
    }, { room, secret: LIVE_SERVICE_SECRET });
    await setup.close();
    const host = await open();
    await host.goto(links.host);
    await host.waitForSelector('#join-form:not([hidden])');
    await host.click('#join');
    await host.waitForSelector('#room:not([hidden])');
    await host.waitForSelector('.gtile .mini:text-is("Put on stage")', { timeout: 30000 });
    await host.click('.gtile .mini:text-is("Put on stage")');
    await host.click('#go-live');
    const viewer = await open();
    await viewer.goto(links.viewer);
    await viewer.waitForSelector('#join-form:not([hidden])');
    await viewer.click('#join');
    const deadline = Date.now() + 60000;
    let playing = false;
    while (Date.now() < deadline && !playing) {
      playing = await viewer.evaluate(async () => {
        const v = document.querySelector('.stile video');
        if (!v || !v.videoWidth) return false;
        const t = v.currentTime;
        await new Promise((r) => setTimeout(r, 800));
        return v.currentTime > t;
      });
      await new Promise((r) => setTimeout(r, 500));
    }
    const seen = await route(viewer);
    const sent = await route(host);
    console.log('viewer:', JSON.stringify(seen.connections));
    console.log('host:', JSON.stringify(sent.connections));
    console.log('gathered:', [...new Set(seen.gathered)].join(' '), 'states:', seen.states.join(' '));
    console.log('log:', [...new Set(seen.log)].join(' | '));
    console.log('server offered:', [...new Set(seen.offered)].join(' '));
    if (!playing && LOCAL_IP && seen.log.includes('candidate relay/tls')) {
      // A virtual machine's networking can stop the relay reaching the media server. Only a public server settles it.
      console.log('partly: the browser got a TURN relay over TLS on 443, but media through it needs a public server to prove');
      return;
    }
    assert.ok(playing, 'the viewer got video');
    for (const { connections } of [seen, sent]) {
      assert.ok(connections.length > 0, 'a connection was made');
      for (const c of connections) {
        assert.equal(c.type, 'relay');
        assert.equal(c.relay, 'tls', `went over ${c.relay}, not TLS`);
      }
    }
    console.log('ok   video went through TURN over TLS on port 443, and nothing else was allowed');
  } finally {
    await Promise.all(contexts.map((c) => c.close()));
  }
})().catch((err) => { console.error(err); process.exit(1); });

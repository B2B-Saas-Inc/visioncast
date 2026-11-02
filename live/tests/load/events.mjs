// Opens many attendee event streams, then measures how long a change takes to reach them all, under
// chat and votes. It stands in for what browsers do; it doesn't draw anything or carry any media.
const BASE = process.env.LIVE_BASE;
const SECRET = process.env.LIVE_SERVICE_SECRET;
const CLIENTS = Number(process.env.EVENT_CLIENTS || 1000);
const SECONDS = Number(process.env.SECONDS_TO_RUN || 40);
const ROOM = `load-${Date.now()}`;
const v1 = async (method, path, body) => {
  const res = await fetch(`${BASE}/v1${path}`, { method, headers: { Authorization: `Bearer ${SECRET}`, 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
  if (!res.ok) throw new Error(`${method} ${path}: ${res.status}`);
  return res.json();
};
const pathOf = (url) => new URL(url).pathname;
const act = (ticket, action) => fetch(`${BASE}${ticket}/act`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action }) });
const percentile = (rows, p) => { const s = [...rows].sort((a, b) => a - b); return s.length ? Math.round(s[Math.min(s.length - 1, Math.floor(s.length * p))]) : null; };

const now = Date.now();
await v1('PUT', `/rooms/${ROOM}`, { title: 'Load', startsAt: new Date(now + 60000).toISOString(), endsAt: new Date(now + 3600000).toISOString() });
const host = pathOf((await v1('POST', `/rooms/${ROOM}/crew`, { role: 'host', name: 'Host' })).url);
await fetch(`${BASE}${host}/enter`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{"name":"Host"}' });
const poll = await (await act(host, { type: 'poll_save', id: null, question: 'Q', options: ['A', 'B'], correct: null, showResults: 'live', showCounts: true })).json();
await act(host, { type: 'poll_status', id: poll.id, status: 'open' });

let connected = 0, failed = 0, disconnected = 0, received = 0;
const latencies = [];
const sent = new Map();
const tickets = [];
const controller = new AbortController();
async function stream(ticket) {
  try {
    const res = await fetch(`${BASE}${ticket}/events`, { signal: controller.signal });
    if (!res.ok) { failed += 1; return; }
    connected += 1;
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    for (;;) {
      const { done, value } = await reader.read();
      if (done) { disconnected += 1; return; }
      buffer += decoder.decode(value, { stream: true });
      let at;
      while ((at = buffer.indexOf('\n\n')) >= 0) {
        const block = buffer.slice(0, at);
        buffer = buffer.slice(at + 2);
        if (!block.startsWith('event: chat')) continue;
        received += 1;
        const marker = /"text":"m(\d+)"/.exec(block);
        if (marker && sent.has(marker[1])) latencies.push(Date.now() - sent.get(marker[1]));
      }
    }
  } catch (err) { if (!controller.signal.aborted) { failed += 1; } }
}
const openStart = Date.now();
for (let i = 0; i < CLIENTS; i += 1) {
  const identity = `00000000-0000-4000-8000-${String(i).padStart(12, '0')}`;
  const ticket = pathOf((await v1('POST', `/rooms/${ROOM}/tickets`, { identity })).joinUrl);
  tickets.push(ticket);
  stream(ticket);
  if (i % 100 === 99) await new Promise((r) => setTimeout(r, 50));
}
for (let i = 0; i < 100 && connected + failed < CLIENTS; i += 1) await new Promise((r) => setTimeout(r, 200));
const openMs = Date.now() - openStart;

let chats = 0, votes = 0, errors = 0;
const stop = Date.now() + SECONDS * 1000;
let n = 0, voter = 0;
while (Date.now() < stop) {
  const id = String(n += 1);
  sent.set(id, Date.now());
  const res = await act(host, { type: 'chat_send', text: `m${id}`, channel: 'event' });
  if (res.ok) chats += 1; else errors += 1;
  await Promise.all(Array.from({ length: 20 }, () => act(tickets[(voter += 1) % tickets.length], { type: 'poll_vote', id: poll.id, option: voter % 2 }).then((r) => { if (r.ok) votes += 1; else errors += 1; })));
  await new Promise((r) => setTimeout(r, 200));
}
await new Promise((r) => setTimeout(r, 3000));
controller.abort();
console.log(JSON.stringify({ clients: CLIENTS, connected, failed, disconnected, openSeconds: Math.round(openMs / 1000), chatsSent: chats, votes, errors, deliveries: received, expectedDeliveries: chats * connected, deliveryP50ms: percentile(latencies, 0.5), deliveryP95ms: percentile(latencies, 0.95), deliveryP99ms: percentile(latencies, 0.99) }));
process.exit(0);

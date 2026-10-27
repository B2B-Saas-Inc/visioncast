import assert from 'node:assert/strict';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { generateArt } from '../lib/art.ts';

const ROOT = fileURLToPath(new URL('..', import.meta.url));

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    if (name === 'node_modules' || name === '.next' || name === 'tests') return [];
    const p = join(dir, name);
    return statSync(p).isDirectory() ? walk(p) : /\.(ts|tsx)$/.test(name) ? [p] : [];
  });
}

test('art is deterministic, varied and does not embed the name', () => {
  const a = generateArt('data-101');
  assert.equal(a, generateArt('data-101'));
  assert.notEqual(a, generateArt('another-event'));
  assert.ok(a.startsWith('<svg xmlns="http://www.w3.org/2000/svg"'));
  assert.ok(!generateArt('"><script>alert(1)</script>').includes('script'));
});

test('client components never read process.env', () => {
  for (const file of walk(ROOT)) {
    const src = readFileSync(file, 'utf8');
    if (/^\s*['"]use client['"]/.test(src)) assert.ok(!src.includes('process.env'), relative(ROOT, file));
  }
});

test('the proxy key is referenced only by server-only proxy config', () => {
  const allowed = new Set(['lib/proxy-core.ts']);
  for (const file of walk(ROOT)) {
    const rel = relative(ROOT, file);
    if (readFileSync(file, 'utf8').includes('VISIONCAST_PROXY_KEY')) assert.ok(allowed.has(rel), rel);
  }
  for (const rel of ['lib/proxy.ts', 'lib/backend.ts']) {
    assert.match(readFileSync(join(ROOT, rel), 'utf8'), /^import 'server-only';/, rel);
  }
});

test('no NEXT_PUBLIC_ variables are used', () => {
  for (const file of walk(ROOT)) assert.ok(!readFileSync(file, 'utf8').includes('NEXT_PUBLIC_'), relative(ROOT, file));
});

test('the demo recording page embeds no media and only serves seeded recording links', () => {
  const src = readFileSync(join(ROOT, 'app', 'demo-recording', '[slug]', 'page.tsx'), 'utf8');
  assert.ok(!/<(video|audio|iframe|embed|object|source)\b/i.test(src));
  assert.match(src, /recordingUrl === path/);
  assert.match(src, /notFound\(\)/);
});

test('only the five allowlisted proxy routes exist', () => {
  const dir = join(ROOT, 'app', 'webinar-api');
  assert.deepEqual(readdirSync(dir).sort(), ['[...rest]', 'calendar.ics', 'my-registration', 'profile', 'registrations', 'reset']);
});

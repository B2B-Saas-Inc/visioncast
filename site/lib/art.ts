/*
 * Abstract event art, generated locally and deterministically from a name (the slug).
 * The name only seeds a hash; it is never written into the SVG, so there is nothing
 * to escape. Palette: charcoal, ivory and lime.
 */

const PALETTES = [
  { bg: '#1c1d1f', ink: '#f6f1e7', accent: '#c6f432', muted: '#3a3b3e' },
  { bg: '#f6f1e7', ink: '#1c1d1f', accent: '#c6f432', muted: '#d9d2c3' },
  { bg: '#26282b', ink: '#c6f432', accent: '#f6f1e7', muted: '#44474b' },
  { bg: '#ebe4d4', ink: '#2b2c2f', accent: '#a9d41c', muted: '#cfc6b2' },
] as const;

/** FNV-1a 32-bit. */
export function hash(name: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < name.length; i++) {
    h ^= name.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h >>> 0;
}

/** Small deterministic PRNG (mulberry32). */
function rng(seed: number) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const W = 1600;
const H = 900;
const n = (v: number) => Math.round(v * 10) / 10;

function arcs(r: () => number, p: (typeof PALETTES)[number]): string {
  const cx = n(W * (0.55 + r() * 0.35));
  const cy = n(H * (0.3 + r() * 0.5));
  const rings: string[] = [];
  const count = 6 + Math.floor(r() * 6);
  for (let i = 0; i < count; i++) {
    const radius = n(80 + i * (60 + r() * 30));
    const stroke = i % 3 === 0 ? p.accent : p.ink;
    const width = n(i % 3 === 0 ? 10 + r() * 14 : 2 + r() * 3);
    rings.push(`<circle cx="${cx}" cy="${cy}" r="${radius}" fill="none" stroke="${stroke}" stroke-width="${width}" opacity="${n(0.35 + r() * 0.6)}"/>`);
  }
  return rings.join('');
}

function grid(r: () => number, p: (typeof PALETTES)[number]): string {
  const cols = 10 + Math.floor(r() * 6);
  const rows = Math.round(cols * (H / W));
  const step = W / cols;
  const dots: string[] = [];
  for (let y = 0; y < rows; y++) {
    for (let x = 0; x < cols; x++) {
      const v = r();
      if (v < 0.18) continue;
      const fill = v > 0.9 ? p.accent : p.ink;
      const rad = n(step * (v > 0.9 ? 0.32 : 0.06 + r() * 0.12));
      dots.push(`<circle cx="${n(step * (x + 0.5))}" cy="${n(step * (y + 0.5))}" r="${rad}" fill="${fill}" opacity="${v > 0.9 ? 1 : 0.55}"/>`);
    }
  }
  return dots.join('');
}

function bands(r: () => number, p: (typeof PALETTES)[number]): string {
  const out: string[] = [];
  const count = 5 + Math.floor(r() * 5);
  const angle = n(-35 + r() * 70);
  for (let i = 0; i < count; i++) {
    const y = n(-H * 0.4 + (i / count) * H * 1.8);
    const h = n(20 + r() * 110);
    const fill = i === Math.floor(count / 2) ? p.accent : i % 2 ? p.ink : p.muted;
    out.push(`<rect x="${-W}" y="${y}" width="${W * 3}" height="${h}" fill="${fill}" opacity="${n(0.4 + r() * 0.6)}"/>`);
  }
  return `<g transform="rotate(${angle} ${W / 2} ${H / 2})">${out.join('')}</g>`;
}

function waves(r: () => number, p: (typeof PALETTES)[number]): string {
  const out: string[] = [];
  const lines = 9 + Math.floor(r() * 8);
  const freq = 1 + r() * 2.5;
  const amp = 30 + r() * 70;
  for (let i = 0; i < lines; i++) {
    const baseY = (H / (lines + 1)) * (i + 1);
    const phase = r() * Math.PI * 2;
    let d = '';
    for (let x = 0; x <= W; x += 40) {
      const y = baseY + Math.sin((x / W) * Math.PI * 2 * freq + phase + i * 0.35) * amp;
      d += `${x === 0 ? 'M' : 'L'}${x} ${n(y)} `;
    }
    const accent = i === Math.floor(lines * 0.6);
    out.push(`<path d="${d.trim()}" fill="none" stroke="${accent ? p.accent : p.ink}" stroke-width="${accent ? 9 : 2}" opacity="${accent ? 1 : 0.5}"/>`);
  }
  return out.join('');
}

const MOTIFS = [arcs, grid, bands, waves];

export function generateArt(name: string): string {
  const seed = hash(name);
  const r = rng(seed);
  const p = PALETTES[seed % PALETTES.length];
  const motif = MOTIFS[(seed >>> 4) % MOTIFS.length];
  const blockX = n(W * (0.04 + r() * 0.1));
  const blockY = n(H * (0.62 + r() * 0.2));
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" preserveAspectRatio="xMidYMid slice" aria-hidden="true">` +
    `<rect width="${W}" height="${H}" fill="${p.bg}"/>` +
    motif(r, p) +
    `<rect x="${blockX}" y="${blockY}" width="${n(W * 0.18)}" height="14" fill="${p.accent}"/>` +
    `</svg>`
  );
}

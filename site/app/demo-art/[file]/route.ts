import { generateArt } from '@/lib/art';

// /demo-art/<name>.svg: abstract art generated locally from <name> (normally the event slug).

export async function GET(_req: Request, { params }: { params: Promise<{ file: string }> }) {
  const { file } = await params;
  const m = /^([A-Za-z0-9_-]{1,80})\.svg$/.exec(file);
  if (!m) return new Response('Not found', { status: 404, headers: { 'Content-Type': 'text/plain; charset=utf-8' } });
  return new Response(generateArt(m[1]), {
    headers: {
      'Content-Type': 'image/svg+xml; charset=utf-8',
      'Cache-Control': 'public, max-age=86400, stale-while-revalidate=604800',
      'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'",
      'X-Content-Type-Options': 'nosniff',
    },
  });
}

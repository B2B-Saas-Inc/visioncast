import { proxyHandler } from '@/lib/proxy';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export const GET = proxyHandler('calendar');
export const POST = GET;

import { proxyHandler } from '@/lib/proxy';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export const POST = proxyHandler('reset');
export const GET = POST;

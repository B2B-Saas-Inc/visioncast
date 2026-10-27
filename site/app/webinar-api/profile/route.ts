import { proxyHandler } from '@/lib/proxy';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export const GET = proxyHandler('profile');
export const POST = GET;

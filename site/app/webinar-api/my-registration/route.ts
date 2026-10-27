import { proxyHandler } from '@/lib/proxy';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export const GET = proxyHandler('my-registration');
export const POST = GET;

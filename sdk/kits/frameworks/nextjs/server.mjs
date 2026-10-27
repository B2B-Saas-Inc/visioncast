/*
 * Self-hosted Next.js (`next start` replacement) with the first-party proxy mounted
 * in-process. One origin, one port; the proxy key stays in this process's env.
 *
 *   cp sdk/kits/node/mount.cjs  <app>/webinar-kit/mount.cjs
 *   cp sdk/proxy/server.js      <app>/webinar-kit/server.cjs
 *   next build && NODE_ENV=production node server.mjs
 *
 * On Vercel or other serverless hosts there is no long-lived Node server. Use the
 * route-handler port this repository maintains in site/ instead (site/lib/proxy.ts,
 * site/app/webinar-api/*), which uses the server-only VISIONCAST_* variables.
 */
import http from 'node:http';
import { createRequire } from 'node:module';
import next from 'next';

const require = createRequire(import.meta.url);
const { withWebinarProxy } = require('./webinar-kit/mount.cjs');

const dev = process.env.NODE_ENV !== 'production';
const app = next({ dev });
await app.prepare();

const handler = withWebinarProxy(app.getRequestHandler()); // fails closed on bad PROXY_* config
const port = Number(process.env.PORT || 3000);
const host = process.env.HOST || '127.0.0.1';
http.createServer(handler).listen(port, host, () => {
  console.log(`next + webinar proxy on http://${host}:${port}`);
});

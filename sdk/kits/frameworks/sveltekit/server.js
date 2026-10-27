/*
 * SvelteKit (@sveltejs/adapter-node) custom server with the first-party proxy mounted
 * in-process. build/handler.js serves static assets and SSR; /webinar-api/* goes to
 * sdk/proxy/server.js unchanged.
 *
 *   cp sdk/kits/node/mount.cjs  <app>/webinar-kit/mount.cjs
 *   cp sdk/proxy/server.js      <app>/webinar-kit/server.cjs
 *   npm run build && node server.js
 *
 * The same pattern works for Astro (@astrojs/node mode: 'middleware') and Nuxt/Nitro
 * node-listener output; see docs/kits.md for the parts not verified here.
 */
import http from 'node:http';
import { createRequire } from 'node:module';
import { handler } from './build/handler.js';

const require = createRequire(import.meta.url);
const { withWebinarProxy } = require('./webinar-kit/mount.cjs');

const site = withWebinarProxy(handler); // fails closed on bad PROXY_* config
const port = Number(process.env.PORT || 3000);
const host = process.env.HOST || '127.0.0.1';
http.createServer(site).listen(port, host, () => {
  console.log(`sveltekit + webinar proxy on http://${host}:${port}`);
});

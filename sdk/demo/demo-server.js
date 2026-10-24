#!/usr/bin/env node
'use strict';
/*
 * Local demo: serves the demo page and SDK on http://localhost:8080 and mounts
 * the real proxy handler on /webinar-api/*, pointed at an in-memory mock
 * backend on 127.0.0.1:8090. Demo only; never expose this to a network.
 */
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { loadConfig, createHandler } = require('../proxy/server.js');
const { createMockBackend } = require('./mock-backend.js');

const PORT = 8080;
const BACKEND_PORT = 8090;
const proxyKey = crypto.randomBytes(32).toString('hex'); // ephemeral, never sent to the browser

const config = loadConfig({
  PROXY_BACKEND_URL: `http://127.0.0.1:${BACKEND_PORT}`,
  PROXY_ALLOW_INSECURE_BACKEND: '1',
  PROXY_API_KEY: proxyKey,
  PROXY_ALLOWED_ORIGINS: `http://localhost:${PORT}`,
});
const proxy = createHandler(config);

const STATIC = {
  '/': { file: path.join(__dirname, 'index.html'), type: 'text/html; charset=utf-8' },
  '/webinar-form.js': { file: path.join(__dirname, '..', 'src', 'webinar-form.js'), type: 'text/javascript; charset=utf-8' },
};

createMockBackend({ proxyKey }).listen(BACKEND_PORT, '127.0.0.1');

http.createServer((req, res) => {
  const pathname = String(req.url).split('?')[0];
  if (pathname.startsWith('/webinar-api/')) return proxy(req, res);
  const entry = req.method === 'GET' && Object.prototype.hasOwnProperty.call(STATIC, pathname) ? STATIC[pathname] : null;
  if (!entry) {
    res.writeHead(404, { 'content-type': 'text/plain' });
    return res.end('not found');
  }
  res.writeHead(200, {
    'content-type': entry.type,
    'cache-control': 'no-store',
    'content-security-policy': "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'",
  });
  fs.createReadStream(entry.file).pipe(res);
}).listen(PORT, '127.0.0.1', () => {
  console.log(`Demo on http://localhost:${PORT}/ (mock backend; demo only)`);
});

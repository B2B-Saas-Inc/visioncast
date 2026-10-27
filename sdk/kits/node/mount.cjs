'use strict';
/*
 * Mount the first-party proxy (sdk/proxy/server.js, unchanged) in front of a Node
 * framework handler, so /webinar-api/* is served on the site's own origin.
 * Licensed under the Apache License, Version 2.0.
 *
 * Configuration is the proxy's own PROXY_* environment (PROXY_BACKEND_URL,
 * PROXY_API_KEY, PROXY_ALLOWED_ORIGINS, ...); loadConfig fails closed at startup.
 * Every /webinar-api path goes to the proxy, which 404s anything off its allowlist,
 * so the app never sees those paths.
 *
 * Copying into an app: put sdk/proxy/server.js next to this file as server.cjs.
 */

function loadProxy() {
  try {
    return require('./server.cjs');
  } catch (err) {
    if (err.code !== 'MODULE_NOT_FOUND' || !String(err.message).includes('server.cjs')) throw err;
    return require('../../proxy/server.js'); // in-repo layout
  }
}

const { loadConfig, createHandler } = loadProxy();

function isProxyPath(url) {
  const path = String(url || '').split('?')[0];
  return path === '/webinar-api' || path.startsWith('/webinar-api/');
}

/**
 * Wraps appHandler(req, res, next) so /webinar-api/* is answered by the proxy.
 * options.config: a loadConfig() result; options.env: environment for loadConfig.
 */
function withWebinarProxy(appHandler, options) {
  const opts = options || {};
  const config = opts.config || loadConfig(opts.env || process.env);
  const proxy = createHandler(config);
  return function webinarSiteHandler(req, res, next) {
    if (isProxyPath(req.url)) return proxy(req, res);
    return appHandler(req, res, next);
  };
}

module.exports = { withWebinarProxy, isProxyPath, loadConfig };

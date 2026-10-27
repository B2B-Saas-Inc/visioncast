/*!
 * gtm-loader.js — site-wide Google Tag Manager install helper for webinar-form.js.
 * Licensed under the Apache License, Version 2.0.
 *
 * Studio › Tracking & GTM inlines the body between the markers below into a
 * Custom HTML tag. The tag is safe to fire many times (DOM Ready, the CMP's
 * consent-update event, an after-render event from a single-page app): it
 * loads the SDK once, applies the latest consent state and binds forms rendered
 * since the last fire. The tag contains no GTM variable references: consent is
 * read from the newest plain window.dataLayer message that carries the
 * configured key (for example {webinarConsent: {analytics: 'granted',
 * advertising: 'denied'}}), with no eval, no dynamic code and no prototype
 * lookups. Anything other than an explicit grant is denied. It never reads
 * cookies, URL parameters or form values itself; the SDK does that only with
 * consent (docs/browser-integration.md §7 and §8). Holds no secrets.
 */
// @gtm-loader-start
  function webinarFormGtmInstall(w, d, cfg) {
    var DENIED = { analytics: false, advertising: false };
    function own(o, k) { return Object.prototype.hasOwnProperty.call(o, k); }
    function plain(o) { return !!o && typeof o === 'object' && Object.prototype.toString.call(o) === '[object Object]'; }
    // Only an explicit, exact grant counts. Strings are compared, never evaluated.
    function granted(v) { return v === true || v === 'granted' || v === 'true'; }
    // Newest dataLayer message that owns the consent key decides. A malformed newest message
    // denies rather than falling back to an older grant. Messages without the key are skipped.
    function readConsent(key) {
      if (typeof key !== 'string' || !key) return DENIED;
      var dl = w.dataLayer;
      if (!Array.isArray(dl)) return DENIED;
      for (var i = dl.length - 1; i >= 0; i--) {
        var msg = dl[i];
        if (!plain(msg) || !own(msg, key)) continue;
        var c = msg[key];
        if (!plain(c)) return DENIED;
        if ((own(c, 'analytics') && typeof c.analytics !== 'string' && typeof c.analytics !== 'boolean') ||
            (own(c, 'advertising') && typeof c.advertising !== 'string' && typeof c.advertising !== 'boolean')) return DENIED;
        return {
          analytics: own(c, 'analytics') && granted(c.analytics),
          advertising: own(c, 'advertising') && granted(c.advertising)
        };
      }
      return DENIED;
    }
    var consent = readConsent(cfg.consentKey);
    var proxyBase = typeof cfg.proxyBase === 'string' ? cfg.proxyBase : '/webinar-api';
    var sdk = w.WebinarForm;
    if (sdk && typeof sdk.setConsent === 'function' && typeof sdk.init === 'function') {
      if (!w.__webinarFormGtmLoading && proxyBase !== '/webinar-api' && !w.__webinarFormGtmWarned && w.console && typeof w.console.warn === 'function') {
        // Loaded by another script element before this tag ran: its proxy path, not this tag's, is in use.
        w.__webinarFormGtmWarned = true;
        w.console.warn('webinar-form.js was already loaded by another script tag; the proxy path in the GTM tag is ignored. Remove the per-session script tag.');
      }
      // Already loaded: apply the latest consent and bind any forms added since (SPA, modals).
      sdk.setConsent(consent);
      sdk.init();
      return;
    }
    // Not loaded yet: the SDK reads this once at boot (readOptions), so keep it current.
    var existing = (w.WebinarFormConfig && typeof w.WebinarFormConfig === 'object') ? w.WebinarFormConfig : {};
    existing.proxyBase = proxyBase;
    existing.autoInit = true;
    existing.consent = consent;
    w.WebinarFormConfig = existing;
    if (w.__webinarFormGtmLoading) return; // script element already injected and still loading
    w.__webinarFormGtmLoading = true;
    var s = d.createElement('script');
    s.async = true;
    s.src = cfg.src;
    s.onerror = function () { w.__webinarFormGtmLoading = false; }; // 404 or CSP block: allow a later fire to retry
    (d.head || d.body || d.documentElement).appendChild(s);
  }
// @gtm-loader-end
if (typeof module === 'object' && module && module.exports) {
  module.exports = webinarFormGtmInstall;
}

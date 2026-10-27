'use client';
/*
 * React wrapper for the webinar browser SDK (webinar-form.js). No dependencies beyond React.
 * Licensed under the Apache License, Version 2.0.
 *
 * Renders SDK markup and lets the SDK own the form: it calls the first-party
 * /webinar-api proxy, hides fields already on file, and on 202 payment_required
 * navigates to Stripe Checkout. This component holds no secrets, and nothing here
 * talks to the backend directly.
 *
 * Props: occurrenceId (UUID) and formVersion come from the server (headless/webinar-events.mjs);
 * sdkSrc is the SDK script URL (sdkScriptUrl()), also resolved on the server.
 * Consent: set window.WebinarFormConfig.consent/consentProvider before this mounts, or call
 * window.WebinarForm.setConsent() from your CMP callback.
 */
import { useEffect, useId, useRef } from 'react';

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const FORM_VERSION_RE = /^[A-Za-z0-9._:-]{1,128}$/;

// Canonical SDK field names only (webinar-form.js CANONICAL_FIELDS).
const FIELDS = {
  email: { label: 'Email', type: 'email', autoComplete: 'email' },
  firstName: { label: 'First name', type: 'text', autoComplete: 'given-name' },
  lastName: { label: 'Last name', type: 'text', autoComplete: 'family-name' },
  name: { label: 'Full name', type: 'text', autoComplete: 'name' },
  phone: { label: 'Phone', type: 'tel', autoComplete: 'tel' },
  company: { label: 'Company', type: 'text', autoComplete: 'organization' },
  jobTitle: { label: 'Job title', type: 'text', autoComplete: 'organization-title' },
};

/** https URL without credentials, or an absolute path on this origin; else null. */
export function safeScriptSrc(src) {
  if (typeof src !== 'string' || !src || src.length > 2048 || /[\s"'<>\\]/.test(src)) return null;
  if (src[0] === '/') return src[1] === '/' ? null : src;
  try {
    const u = new URL(src);
    return u.protocol === 'https:' && !u.username && !u.password ? u.href : null;
  } catch {
    return null;
  }
}

let sdkPromise = null;

function loadSdk(src) {
  if (window.WebinarForm) return Promise.resolve(window.WebinarForm);
  if (sdkPromise) return sdkPromise;
  // autoInit off: forms are bound explicitly after React has rendered them.
  window.WebinarFormConfig = Object.assign({ proxyBase: '/webinar-api' }, window.WebinarFormConfig, { autoInit: false });
  sdkPromise = new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = src;
    s.async = true;
    s.onload = () => (window.WebinarForm ? resolve(window.WebinarForm) : reject(new Error('sdk_missing')));
    s.onerror = () => {
      sdkPromise = null;
      reject(new Error('sdk_load_failed'));
    };
    document.head.appendChild(s);
  });
  return sdkPromise;
}

export default function WebinarForm({
  occurrenceId,
  formVersion,
  sdkSrc,
  fields = ['email', 'firstName', 'lastName', 'company'],
  required = ['email'],
  submitLabel = 'Register',
  confirmLabel = '',
}) {
  const scope = useRef(null);
  const uid = useId();
  const valid = UUID_RE.test(occurrenceId || '') && FORM_VERSION_RE.test(formVersion || '');
  const src = safeScriptSrc(sdkSrc);
  const shown = ['email', ...fields.filter((f) => f !== 'email' && Object.prototype.hasOwnProperty.call(FIELDS, f))];

  useEffect(() => {
    if (!valid || !src || !scope.current) return undefined;
    let active = true;
    loadSdk(src)
      .then((api) => {
        if (active && scope.current) api.init(scope.current); // already-bound forms are skipped
      })
      .catch(() => {
        // The form stays visible; submission needs the SDK. Report via your own monitoring.
      });
    return () => {
      active = false;
    };
  }, [valid, src, occurrenceId, formVersion]);

  if (!valid || !src) return null;
  return (
    <div ref={scope}>
      {/* Keyed so a different session gets a fresh form element for the SDK to bind. */}
      <form
        key={`${occurrenceId}:${formVersion}`}
        data-webinar-form=""
        data-occurrence-id={occurrenceId.toLowerCase()}
        data-form-version={formVersion}
        {...(confirmLabel ? { 'data-confirm-label': String(confirmLabel).slice(0, 60) } : {})}
      >
        {shown.map((name) => {
          const def = FIELDS[name];
          const id = `${uid}-${name}`;
          return (
            <div data-webinar-field="" key={name}>
              <label htmlFor={id}>{def.label}</label>
              <input
                id={id}
                name={name}
                type={def.type}
                autoComplete={def.autoComplete}
                required={name === 'email' || required.includes(name)}
              />
            </div>
          );
        })}
        <button type="submit">{submitLabel}</button>
        <div data-webinar-status="" />
      </form>
    </div>
  );
}

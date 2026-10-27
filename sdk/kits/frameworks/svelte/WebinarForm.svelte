<!--
  Svelte 5 / SvelteKit component for the webinar browser SDK (runes syntax). No dependencies.
  Licensed under the Apache License, Version 2.0.

  A fresh SvelteKit project (`npx sv create`) compiles every project file in runes mode, where
  the legacy `export let` / `$:` syntax is a compile error, so this file uses $props/$derived/
  $effect. For Svelte 4, replace the $props() line with `export let` declarations, the
  $derived values with `$:` statements and the $effect with onMount.

  Load occurrenceId/formVersion/sdkSrc in +page.server.js with headless/webinar-events.mjs.
  The SDK posts to /webinar-api on this origin (sveltekit/server.js mounts the proxy).
  No secret is referenced here.
-->
<script>
  import { tick } from 'svelte';

  let {
    occurrenceId,
    formVersion,
    sdkSrc,
    fields = ['email', 'firstName', 'lastName', 'company'],
    required = ['email'],
    submitLabel = 'Register',
    confirmLabel = ''
  } = $props();

  const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const FORM_VERSION_RE = /^[A-Za-z0-9._:-]{1,128}$/;
  const FIELDS = {
    email: { label: 'Email', type: 'email', autocomplete: 'email' },
    firstName: { label: 'First name', type: 'text', autocomplete: 'given-name' },
    lastName: { label: 'Last name', type: 'text', autocomplete: 'family-name' },
    name: { label: 'Full name', type: 'text', autocomplete: 'name' },
    phone: { label: 'Phone', type: 'tel', autocomplete: 'tel' },
    company: { label: 'Company', type: 'text', autocomplete: 'organization' },
    jobTitle: { label: 'Job title', type: 'text', autocomplete: 'organization-title' }
  };

  function safeScriptSrc(src) {
    if (typeof src !== 'string' || !src || src.length > 2048 || /[\s"'<>\\]/.test(src)) return null;
    if (src[0] === '/') return src[1] === '/' ? null : src;
    try {
      const u = new URL(src);
      return u.protocol === 'https:' && !u.username && !u.password ? u.href : null;
    } catch {
      return null;
    }
  }

  function loadSdk(src) {
    if (window.WebinarForm) return Promise.resolve(window.WebinarForm);
    if (window.__webinarSdkPromise) return window.__webinarSdkPromise;
    window.WebinarFormConfig = Object.assign({ proxyBase: '/webinar-api' }, window.WebinarFormConfig, { autoInit: false });
    window.__webinarSdkPromise = new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = src;
      s.async = true;
      s.onload = () => (window.WebinarForm ? resolve(window.WebinarForm) : reject(new Error('sdk_missing')));
      s.onerror = () => {
        window.__webinarSdkPromise = null;
        reject(new Error('sdk_load_failed'));
      };
      document.head.appendChild(s);
    });
    return window.__webinarSdkPromise;
  }

  let scope = $state(null);
  const valid = $derived(UUID_RE.test(occurrenceId || '') && FORM_VERSION_RE.test(formVersion || ''));
  const src = $derived(safeScriptSrc(sdkSrc));
  const shown = $derived(['email', ...fields.filter((f) => f !== 'email' && Object.hasOwn(FIELDS, f))]);
  const formKey = $derived(`${occurrenceId}:${formVersion}`);
  const prefix = $derived(`wf-${(occurrenceId || '').slice(0, 8)}`);

  async function bind() {
    await tick();
    if (!valid || !src || !scope) return;
    try {
      const api = await loadSdk(src);
      if (scope) api.init(scope); // already-bound forms are skipped
    } catch {
      // The form stays visible; submission needs the SDK.
    }
  }

  // Runs in the browser after mount and again whenever the session changes.
  $effect(() => {
    void formKey;
    bind();
  });
</script>

{#if valid && src}
  <div bind:this={scope}>
    {#key formKey}
      <form
        data-webinar-form
        data-occurrence-id={occurrenceId.toLowerCase()}
        data-form-version={formVersion}
        data-confirm-label={confirmLabel ? confirmLabel.slice(0, 60) : undefined}
      >
        {#each shown as name (name)}
          <div data-webinar-field>
            <label for={`${prefix}-${name}`}>{FIELDS[name].label}</label>
            <input
              id={`${prefix}-${name}`}
              {name}
              type={FIELDS[name].type}
              autocomplete={FIELDS[name].autocomplete}
              required={name === 'email' || required.includes(name)}
            />
          </div>
        {/each}
        <button type="submit">{submitLabel}</button>
        <div data-webinar-status></div>
      </form>
    {/key}
  </div>
{/if}

<!--
  Svelte / SvelteKit component for the webinar browser SDK (legacy `export let` syntax,
  which Svelte 4 and 5 both accept). No dependencies.
  Licensed under the Apache License, Version 2.0.

  Load occurrenceId/formVersion/sdkSrc in +page.server.js with headless/webinar-events.mjs.
  The SDK posts to /webinar-api on this origin (sveltekit/server.js mounts the proxy).
  No secret is referenced here.
-->
<script>
  import { onMount, tick } from 'svelte';

  export let occurrenceId;
  export let formVersion;
  export let sdkSrc;
  export let fields = ['email', 'firstName', 'lastName', 'company'];
  export let required = ['email'];
  export let submitLabel = 'Register';
  export let confirmLabel = '';

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

  let scope;
  let mounted = false;
  $: valid = UUID_RE.test(occurrenceId || '') && FORM_VERSION_RE.test(formVersion || '');
  $: src = safeScriptSrc(sdkSrc);
  $: shown = ['email', ...fields.filter((f) => f !== 'email' && Object.hasOwn(FIELDS, f))];
  $: formKey = `${occurrenceId}:${formVersion}`;
  $: prefix = `wf-${(occurrenceId || '').slice(0, 8)}`;

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

  onMount(() => {
    mounted = true;
    bind();
  });
  $: if (mounted && formKey) bind();
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

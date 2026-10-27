<!--
  Vue 3 / Nuxt 3 component for the webinar browser SDK. No dependencies beyond Vue.
  Licensed under the Apache License, Version 2.0.

  The SDK owns the form: it calls the first-party /webinar-api proxy and, on 202
  payment_required, navigates to Stripe Checkout. Pass occurrenceId/formVersion/sdkSrc
  resolved on the server (headless/webinar-events.mjs in a Nuxt server route or
  useAsyncData on the server). No secret is referenced here.
  In Nuxt, wrap it in <ClientOnly> or rely on onMounted (it only runs in the browser).
-->
<script setup>
import { computed, onMounted, ref, watch, nextTick } from 'vue';

const props = defineProps({
  occurrenceId: { type: String, required: true },
  formVersion: { type: String, required: true },
  sdkSrc: { type: String, required: true },
  fields: { type: Array, default: () => ['email', 'firstName', 'lastName', 'company'] },
  required: { type: Array, default: () => ['email'] },
  submitLabel: { type: String, default: 'Register' },
  confirmLabel: { type: String, default: '' },
});

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const FORM_VERSION_RE = /^[A-Za-z0-9._:-]{1,128}$/;
const FIELDS = {
  email: { label: 'Email', type: 'email', autocomplete: 'email' },
  firstName: { label: 'First name', type: 'text', autocomplete: 'given-name' },
  lastName: { label: 'Last name', type: 'text', autocomplete: 'family-name' },
  name: { label: 'Full name', type: 'text', autocomplete: 'name' },
  phone: { label: 'Phone', type: 'tel', autocomplete: 'tel' },
  company: { label: 'Company', type: 'text', autocomplete: 'organization' },
  jobTitle: { label: 'Job title', type: 'text', autocomplete: 'organization-title' },
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

let sdkPromise = null;
function loadSdk(src) {
  if (window.WebinarForm) return Promise.resolve(window.WebinarForm);
  if (sdkPromise) return sdkPromise;
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

const scope = ref(null);
const valid = computed(() => UUID_RE.test(props.occurrenceId) && FORM_VERSION_RE.test(props.formVersion));
const src = computed(() => safeScriptSrc(props.sdkSrc));
const shown = computed(() => ['email', ...props.fields.filter((f) => f !== 'email' && Object.hasOwn(FIELDS, f))]);
const formKey = computed(() => `${props.occurrenceId}:${props.formVersion}`);
const prefix = computed(() => `wf-${props.occurrenceId.slice(0, 8)}`);

async function bind() {
  await nextTick();
  if (!valid.value || !src.value || !scope.value) return;
  try {
    const api = await loadSdk(src.value);
    if (scope.value) api.init(scope.value); // already-bound forms are skipped
  } catch {
    // The form stays visible; submission needs the SDK.
  }
}

onMounted(bind);
watch(formKey, bind);
</script>

<template>
  <div v-if="valid && src" ref="scope">
    <form
      :key="formKey"
      data-webinar-form
      :data-occurrence-id="occurrenceId.toLowerCase()"
      :data-form-version="formVersion"
      :data-confirm-label="confirmLabel ? confirmLabel.slice(0, 60) : undefined"
    >
      <div v-for="name in shown" :key="name" data-webinar-field>
        <label :for="`${prefix}-${name}`">{{ FIELDS[name].label }}</label>
        <input
          :id="`${prefix}-${name}`"
          :name="name"
          :type="FIELDS[name].type"
          :autocomplete="FIELDS[name].autocomplete"
          :required="name === 'email' || required.includes(name)"
        />
      </div>
      <button type="submit">{{ submitLabel }}</button>
      <div data-webinar-status></div>
    </form>
  </div>
</template>

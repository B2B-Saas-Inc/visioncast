'use client';

import { useCallback, useEffect, useId, useRef, useState, type FormEvent } from 'react';

import type { FormField, FormSchema } from '@/lib/event-model';

/*
 * Registration panel. Talks only to this origin's /webinar-api/* proxy:
 *   GET  /webinar-api/profile          → which fields this browser still needs (issues the CSRF cookie)
 *   GET  /webinar-api/my-registration  → already registered? (CSRF header)
 *   POST /webinar-api/registrations    → submit (CSRF header, idempotency key)
 *   POST /webinar-api/reset            → "Not you?" (revokes this browser's capability)
 * Fields come from the event's canonical form schema; none are hard-coded.
 */

export interface RsvpOccurrence {
  id: string;
  label: string;
}

interface ThankYou {
  mode: 'message' | 'redirect';
  message?: string;
  redirectUrl?: string;
}

interface Calendar {
  icsUrl?: string;
  googleUrl?: string | null;
  outlookUrl?: string | null;
  office365Url?: string | null;
}

interface Confirmation {
  thankYou: ThankYou;
  calendar: Calendar | null;
}

type Phase = 'loading' | 'form' | 'submitting' | 'done' | 'closed' | 'unavailable';

const CSRF_COOKIE = '__Host-webinar_csrf';
const IDEM_PREFIX = 'visioncast:idem:';
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
/** Mirrors webinars/normalization.py MAX_LENGTHS and CUSTOM_MAX_LENGTH. */
const MAX_LENGTHS: Record<string, number> = {
  email: 254,
  firstName: 150,
  lastName: 150,
  name: 300,
  company: 300,
  jobTitle: 300,
  companySize: 64,
};
const CUSTOM_MAX_LENGTH = 1000;
/** Raw input bound for phone; the backend normalises to E.164 (at most 20 characters). */
const PHONE_INPUT_MAX = 40;

function maxLengthFor(field: FormField): number {
  if (field.custom) return CUSTOM_MAX_LENGTH;
  if (field.type === 'tel') return PHONE_INPUT_MAX;
  return MAX_LENGTHS[field.key] ?? 300;
}

function readCookie(name: string): string {
  for (const part of document.cookie.split(';')) {
    const eq = part.indexOf('=');
    if (eq !== -1 && part.slice(0, eq).trim() === name) return part.slice(eq + 1).trim();
  }
  return '';
}

function newKey(): string {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  const b = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
}

async function fingerprint(text: string): Promise<string | null> {
  try {
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
    return Array.from(new Uint8Array(digest), (x) => x.toString(16).padStart(2, '0')).join('');
  } catch {
    return null;
  }
}

/**
 * One idempotency key per submission intent: the same body (by SHA-256, so no personal
 * data is stored) reuses its key across retries and reloads in this tab; a changed body
 * gets a new key. Cleared after a completed registration.
 */
const memoryKeys = new Map<string, { fp: string; key: string }>();

function storageGet(slot: string): { fp: string; key: string } | null {
  try {
    const raw = sessionStorage.getItem(slot);
    const v = raw ? JSON.parse(raw) : null;
    if (v && typeof v.fp === 'string' && typeof v.key === 'string') return v;
  } catch {
    /* storage unavailable: fall back to memory */
  }
  return memoryKeys.get(slot) ?? null;
}

function storageSet(slot: string, value: { fp: string; key: string } | null) {
  if (value) memoryKeys.set(slot, value);
  else memoryKeys.delete(slot);
  try {
    if (value) sessionStorage.setItem(slot, JSON.stringify(value));
    else sessionStorage.removeItem(slot);
  } catch {
    /* ignore */
  }
}

async function keyFor(occurrenceId: string, body: string): Promise<string> {
  const slot = IDEM_PREFIX + occurrenceId;
  const fp = (await fingerprint(body)) ?? `mem:${body.length}:${Math.random()}`;
  const stored = storageGet(slot);
  if (stored && stored.fp === fp) return stored.key;
  const key = newKey();
  storageSet(slot, { fp, key });
  return key;
}

function errorCode(body: unknown): string {
  if (body && typeof body === 'object') {
    const e = (body as { error?: unknown }).error;
    if (typeof e === 'string') return e;
    if (e && typeof e === 'object' && typeof (e as { code?: unknown }).code === 'string') return (e as { code: string }).code;
  }
  return '';
}

/** Backend field-error keys → form keys (profile.email → email, customFields.x → custom.x). */
function formKey(k: string): string {
  if (k.startsWith('profile.')) return k.slice('profile.'.length);
  if (k.startsWith('customFields.')) return `custom.${k.slice('customFields.'.length)}`;
  return k;
}

function genericField(key: string): FormField {
  const bare = key.startsWith('custom.') ? key.slice(7) : key;
  const label = bare.replace(/([a-z0-9])([A-Z])/g, '$1 $2').replace(/_/g, ' ');
  return {
    key,
    type: key === 'email' ? 'email' : key === 'phone' ? 'tel' : 'text',
    label: label.charAt(0).toUpperCase() + label.slice(1),
    help: null,
    required: false,
    options: null,
    autocomplete: null,
    custom: key.startsWith('custom.'),
  };
}

function safeCalendar(c: Calendar | null | undefined): Calendar | null {
  if (!c) return null;
  const https = (u: unknown) => (typeof u === 'string' && u.startsWith('https://') ? u : null);
  return {
    icsUrl: typeof c.icsUrl === 'string' && c.icsUrl.startsWith('/webinar-api/calendar.ics?') ? c.icsUrl : undefined,
    googleUrl: https(c.googleUrl),
    outlookUrl: https(c.outlookUrl),
    office365Url: https(c.office365Url),
  };
}

export function RsvpPanel(props: {
  eventTitle: string;
  formVersion: string;
  occurrences: RsvpOccurrence[];
  schema: FormSchema;
  cta: { register: string; confirm: string };
  actionCopy: string;
}) {
  const { formVersion, occurrences, schema, cta, actionCopy } = props;
  const uid = useId();
  const [occurrenceId, setOccurrenceId] = useState(occurrences[0]?.id ?? '');
  const [phase, setPhase] = useState<Phase>('loading');
  const [recognized, setRecognized] = useState(false);
  const [fields, setFields] = useState<FormField[]>([]);
  const [values, setValues] = useState<Record<string, string | boolean>>({});
  const [smsConsent, setSmsConsent] = useState(false);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const [status, setStatus] = useState('');
  const requestSeq = useRef(0);
  const alertRef = useRef<HTMLDivElement>(null);
  const doneRef = useRef<HTMLHeadingElement>(null);

  const load = useCallback(
    async (occId: string) => {
      const seq = ++requestSeq.current;
      setPhase('loading');
      setFormError(null);
      setFieldErrors({});
      setStatus('Checking registration…');
      try {
        const profileRes = await fetch(`/webinar-api/profile?${new URLSearchParams({ occurrenceId: occId, formVersion })}`, {
          headers: { accept: 'application/json' },
          cache: 'no-store',
        });
        if (seq !== requestSeq.current) return;
        if (profileRes.status === 404) {
          setPhase('closed');
          return;
        }
        if (!profileRes.ok) throw new Error('profile');
        const profile = (await profileRes.json()) as { recognized?: boolean; missingFields?: string[] };
        const isKnown = profile.recognized === true;
        const missing = Array.isArray(profile.missingFields) ? profile.missingFields : [];

        // Already registered in this browser? Non-critical: on failure, show the form.
        if (isKnown) {
          try {
            const mine = await fetch(`/webinar-api/my-registration?${new URLSearchParams({ occurrenceId: occId })}`, {
              headers: { accept: 'application/json', 'X-Webinar-CSRF': readCookie(CSRF_COOKIE) },
              cache: 'no-store',
            });
            if (seq !== requestSeq.current) return;
            if (mine.ok) {
              const body = (await mine.json()) as { registered?: boolean; thankYou?: ThankYou; calendar?: Calendar };
              if (body.registered === true) {
                setRecognized(true);
                setConfirmation({ thankYou: body.thankYou ?? { mode: 'message' }, calendar: safeCalendar(body.calendar) });
                setPhase('done');
                setStatus('You are already registered for this session.');
                return;
              }
            }
          } catch {
            /* fall through to the form */
          }
        }
        if (seq !== requestSeq.current) return;
        const byKey = new Map(schema.fields.map((f) => [f.key, f]));
        setFields(isKnown ? missing.map((k) => byKey.get(k) ?? genericField(k)) : schema.fields);
        setRecognized(isKnown);
        setPhase('form');
        setStatus('');
      } catch {
        if (seq !== requestSeq.current) return;
        setPhase('unavailable');
        setStatus('');
      }
    },
    [formVersion, schema.fields],
  );

  useEffect(() => {
    if (occurrenceId) void load(occurrenceId);
  }, [occurrenceId, load]);

  useEffect(() => {
    if (formError) alertRef.current?.focus();
  }, [formError]);

  // Move focus to the confirmation only after this user submitted, never on page load.
  const focusConfirmation = useRef(false);
  useEffect(() => {
    if (phase === 'done' && focusConfirmation.current) {
      focusConfirmation.current = false;
      doneRef.current?.focus();
    }
  }, [phase]);

  const showSms = Boolean(schema.sms && fields.some((f) => f.key === 'phone'));

  function setValue(key: string, value: string | boolean) {
    setValues((v) => ({ ...v, [key]: value }));
    if (fieldErrors[key]) setFieldErrors((e) => Object.fromEntries(Object.entries(e).filter(([k]) => k !== key)));
  }

  function validate(): Record<string, string> {
    const errors: Record<string, string> = {};
    for (const f of fields) {
      const v = values[f.key];
      const text = typeof v === 'string' ? v.trim() : '';
      if (f.required && f.type !== 'checkbox' && !text) errors[f.key] = `Enter your ${f.label.toLowerCase()}.`;
      else if (f.type === 'email' && text && !EMAIL_RE.test(text)) errors[f.key] = 'Enter an email address like name@company.com.';
      else if (f.type === 'tel' && text && !/^[+()0-9\s.-]{6,40}$/.test(text)) errors[f.key] = 'Enter a phone number, including the country code.';
    }
    return errors;
  }

  function buildBody(): Record<string, unknown> {
    const profile: Record<string, string> = {};
    const customFields: Record<string, string | boolean> = {};
    for (const f of fields) {
      const v = values[f.key];
      if (f.custom) {
        const bare = f.key.slice('custom.'.length);
        if (f.type === 'checkbox') customFields[bare] = v === true;
        else if (typeof v === 'string' && v.trim()) customFields[bare] = v.trim();
      } else if (typeof v === 'string' && v.trim()) {
        profile[f.key] = v.trim();
      }
    }
    const consents: Record<string, unknown> = {
      // This site runs no analytics or advertising tags.
      analytics: { granted: false },
      advertising: { granted: false },
    };
    if (showSms && schema.sms) {
      consents.sms = smsConsent ? { granted: true, disclosureVersion: schema.sms.disclosureVersion } : { granted: false };
    }
    return {
      schemaVersion: '1',
      occurrenceId,
      formVersion,
      profile,
      consents,
      attribution: { pageUrl: `${window.location.origin}${window.location.pathname}` },
      ...(Object.keys(customFields).length ? { customFields } : {}),
    };
  }

  async function post(path: string, body: string): Promise<Response> {
    return fetch(path, {
      method: 'POST',
      headers: { 'content-type': 'application/json', accept: 'application/json', 'X-Webinar-CSRF': readCookie(CSRF_COOKIE) },
      body,
      cache: 'no-store',
    });
  }

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (phase === 'submitting') return;
    const errors = validate();
    setFieldErrors(errors);
    if (Object.keys(errors).length) {
      setFormError('Please check the highlighted fields.');
      return;
    }
    setFormError(null);
    setPhase('submitting');
    setStatus('Submitting your registration…');
    const base = buildBody();
    const baseText = JSON.stringify(base);
    const slot = IDEM_PREFIX + occurrenceId;

    for (let attempt = 0; attempt < 2; attempt++) {
      let res: Response;
      try {
        const idempotencyKey = await keyFor(occurrenceId, baseText);
        res = await post('/webinar-api/registrations', JSON.stringify({ ...base, idempotencyKey }));
      } catch {
        // Network failure: keep the key, so pressing the button again replays rather than duplicates.
        setPhase('form');
        setStatus('');
        setFormError('We could not reach the registration service. Your details are still here; press the button again to retry. You will not be registered twice.');
        return;
      }
      let body: unknown = null;
      try {
        body = await res.json();
      } catch {
        body = null;
      }
      if (res.ok) {
        storageSet(slot, null);
        const b = (body ?? {}) as { thankYou?: ThankYou; calendar?: Calendar };
        const thankYou: ThankYou = b.thankYou ?? { mode: 'message' };
        setConfirmation({ thankYou, calendar: safeCalendar(b.calendar) });
        setRecognized(true);
        focusConfirmation.current = true;
        setPhase('done');
        setStatus('Registration confirmed.');
        if (thankYou.mode === 'redirect' && thankYou.redirectUrl?.startsWith('https://')) {
          window.setTimeout(() => window.location.assign(thankYou.redirectUrl!), 1500);
        }
        return;
      }
      const code = errorCode(body);
      if (res.status === 409 && code === 'idempotency_conflict' && attempt === 0) {
        storageSet(slot, null); // a different intent reused the key: start a fresh one
        continue;
      }
      setPhase(res.status === 410 ? 'closed' : 'form');
      setStatus('');
      if (res.status === 400 && body && typeof body === 'object' && (body as { fieldErrors?: object }).fieldErrors) {
        const keys = Object.keys((body as { fieldErrors: object }).fieldErrors).map(formKey);
        const shown = new Set(fields.map((f) => f.key));
        const next: Record<string, string> = {};
        for (const k of keys) if (shown.has(k)) next[k] = 'Please check this field.';
        setFieldErrors(next);
        setFormError(
          keys.some((k) => k.startsWith('consents.sms'))
            ? 'Please check the text-message consent.'
            : Object.keys(next).length
              ? 'Please check the highlighted fields.'
              : 'Some details could not be accepted. Please review the form and try again.',
        );
      } else if (res.status === 403) {
        setFormError('Your browser’s security check did not pass. Reload the page and try again.');
      } else if (res.status === 429) {
        setFormError('Too many attempts in a short time. Please wait a minute and try again.');
      } else if (res.status >= 500) {
        setFormError('The registration service did not confirm your registration. Press the button again to retry. You will not be registered twice.');
      } else if (res.status !== 410) {
        setFormError('Your registration could not be completed. Please try again.');
      }
      return;
    }
  }

  async function onReset() {
    setStatus('Forgetting this browser…');
    try {
      await post('/webinar-api/reset', '{}');
    } catch {
      /* the reload below shows the current state either way */
    }
    storageSet(IDEM_PREFIX + occurrenceId, null);
    setValues({});
    setSmsConsent(false);
    setConfirmation(null);
    void load(occurrenceId);
  }

  const errorEntries = Object.entries(fieldErrors);
  const fieldId = (key: string) => `${uid}-${key.replace(/[^A-Za-z0-9_-]/g, '_')}`;

  return (
    <section className="rsvp dark" aria-labelledby={`${uid}-title`}>
      <p className="eyebrow">Free webinar · RSVP</p>
      <h2 id={`${uid}-title`}>{phase === 'done' ? 'You’re registered' : cta.register}</h2>
      {phase !== 'done' && actionCopy.trim() && <p>{actionCopy.trim()}</p>}

      <p className="visually-hidden" role="status" aria-live="polite">
        {status}
      </p>

      {occurrences.length > 1 && phase !== 'done' && (
        <fieldset className="field" style={{ marginTop: '1rem' }} disabled={phase === 'submitting'}>
          <legend>Choose a session</legend>
          <div className="occurrence-options">
            {occurrences.map((o) => (
              <label className="occurrence-option" key={o.id}>
                <input
                  type="radio"
                  name={`${uid}-occurrence`}
                  value={o.id}
                  checked={occurrenceId === o.id}
                  onChange={() => setOccurrenceId(o.id)}
                />
                <span>{o.label}</span>
              </label>
            ))}
          </div>
        </fieldset>
      )}
      {occurrences.length === 1 && phase !== 'done' && <p className="fine-print">{occurrences[0].label}</p>}

      {phase === 'loading' && (
        <div aria-hidden="true">
          <div className="skeleton" />
          <div className="skeleton" style={{ width: '70%' }} />
          <div className="skeleton" style={{ width: '85%' }} />
        </div>
      )}

      {phase === 'unavailable' && (
        <div className="form-alert" role="alert">
          <p style={{ margin: 0 }}>Registration could not be loaded from the Visioncast back end.</p>
          <button type="button" className="button small secondary" style={{ marginTop: '0.8rem' }} onClick={() => void load(occurrenceId)}>
            Try again
          </button>
        </div>
      )}

      {phase === 'closed' && (
        <div className="form-alert" role="alert">
          <p style={{ margin: 0 }}>Registration for this session has closed.</p>
        </div>
      )}

      {(phase === 'form' || phase === 'submitting') && (
        <form onSubmit={onSubmit} noValidate aria-describedby={formError ? `${uid}-alert` : undefined}>
          {formError && (
            <div className="form-alert" role="alert" id={`${uid}-alert`} tabIndex={-1} ref={alertRef}>
              {formError}
              {errorEntries.length > 0 && (
                <ul>
                  {errorEntries.map(([key, message]) => {
                    const f = fields.find((x) => x.key === key);
                    return (
                      <li key={key}>
                        <a href={`#${fieldId(key)}`}>
                          {f?.label ?? key}: {message}
                        </a>
                      </li>
                    );
                  })}
                </ul>
              )}
            </div>
          )}

          {recognized && fields.length === 0 && (
            <p style={{ margin: 0 }}>Welcome back. This browser already has your details, so one click is enough.</p>
          )}
          {recognized && fields.length > 0 && (
            <p style={{ margin: 0 }}>Welcome back. Just {fields.length === 1 ? 'one more detail' : 'a few more details'} for this event.</p>
          )}

          {fields.map((f) => (
            <Field
              key={f.key}
              field={f}
              id={fieldId(f.key)}
              value={values[f.key]}
              error={fieldErrors[f.key]}
              onChange={(v) => setValue(f.key, v)}
            />
          ))}

          {showSms && schema.sms && (
            <label className="check">
              <input type="checkbox" checked={smsConsent} onChange={(e) => setSmsConsent(e.target.checked)} />
              <span>{schema.sms.disclosureText || 'Text me reminders about this event.'}</span>
            </label>
          )}

          <button className="button" type="submit" disabled={phase === 'submitting'} aria-disabled={phase === 'submitting'}>
            {phase === 'submitting' ? 'Submitting…' : recognized && fields.length === 0 ? cta.confirm : cta.register}
          </button>

          <p className="fine-print">
            Demo registration: this is a real submission to the Visioncast demo back end. We set one HttpOnly cookie so
            this browser can skip known fields next time. No analytics or advertising tags run on this site.
          </p>
          {recognized && (
            <p className="fine-print">
              Not you?{' '}
              <button type="button" className="link-button" onClick={() => void onReset()}>
                Forget this browser
              </button>
            </p>
          )}
        </form>
      )}

      {phase === 'done' && confirmation && (
        <div className="confirmed">
          <span className="tick" aria-hidden="true">
            ✓
          </span>
          <h3 ref={doneRef} tabIndex={-1} style={{ margin: 0 }}>
            {occurrences.find((o) => o.id === occurrenceId)?.label}
          </h3>
          {confirmation.thankYou.message && <p style={{ margin: 0 }}>{confirmation.thankYou.message}</p>}
          {confirmation.thankYou.mode === 'redirect' && confirmation.thankYou.redirectUrl?.startsWith('https://') && (
            <p style={{ margin: 0 }}>
              Taking you to the next step… <a href={confirmation.thankYou.redirectUrl}>Continue now</a>
            </p>
          )}
          {confirmation.calendar && (
            <div>
              <h3 style={{ fontSize: '1.05rem' }}>Add it to your calendar</h3>
              <ul className="calendar-links">
                {confirmation.calendar.icsUrl && (
                  <li>
                    <a href={confirmation.calendar.icsUrl} download>
                      Apple / other calendars <span aria-hidden="true">.ics</span>
                    </a>
                  </li>
                )}
                {confirmation.calendar.googleUrl && (
                  <li>
                    <a href={confirmation.calendar.googleUrl} target="_blank" rel="noopener noreferrer">
                      Google Calendar <span aria-hidden="true">↗</span>
                      <span className="visually-hidden"> (opens in a new tab)</span>
                    </a>
                  </li>
                )}
                {confirmation.calendar.outlookUrl && (
                  <li>
                    <a href={confirmation.calendar.outlookUrl} target="_blank" rel="noopener noreferrer">
                      Outlook.com <span aria-hidden="true">↗</span>
                      <span className="visually-hidden"> (opens in a new tab)</span>
                    </a>
                  </li>
                )}
                {confirmation.calendar.office365Url && (
                  <li>
                    <a href={confirmation.calendar.office365Url} target="_blank" rel="noopener noreferrer">
                      Microsoft 365 <span aria-hidden="true">↗</span>
                      <span className="visually-hidden"> (opens in a new tab)</span>
                    </a>
                  </li>
                )}
              </ul>
            </div>
          )}
          {occurrences.length > 1 && (
            <p className="fine-print">
              <button
                type="button"
                className="link-button"
                onClick={() => setOccurrenceId(occurrences.find((o) => o.id !== occurrenceId)?.id ?? occurrenceId)}
              >
                Register for another session
              </button>
            </p>
          )}
          <p className="fine-print">
            Not you?{' '}
            <button type="button" className="link-button" onClick={() => void onReset()}>
              Forget this browser
            </button>
          </p>
        </div>
      )}
    </section>
  );
}

function Field({
  field,
  id,
  value,
  error,
  onChange,
}: {
  field: FormField;
  id: string;
  value: string | boolean | undefined;
  error?: string;
  onChange: (v: string | boolean) => void;
}) {
  const helpId = field.help ? `${id}-help` : undefined;
  const errorId = error ? `${id}-error` : undefined;
  const describedBy = [helpId, errorId].filter(Boolean).join(' ') || undefined;
  const text = typeof value === 'string' ? value : '';
  const common = {
    id,
    name: field.key,
    'aria-invalid': error ? (true as const) : undefined,
    'aria-describedby': describedBy,
    'aria-required': field.required || undefined,
  };

  if (field.type === 'checkbox') {
    return (
      <div className="field">
        <label className="check" htmlFor={id}>
          <input {...common} type="checkbox" checked={value === true} onChange={(e) => onChange(e.target.checked)} />
          <span>
            {field.label}
            {field.help && (
              <span className="help" id={helpId}>
                {field.help}
              </span>
            )}
          </span>
        </label>
        {error && (
          <span className="error-text" id={errorId}>
            {error}
          </span>
        )}
      </div>
    );
  }

  return (
    <div className="field">
      <label htmlFor={id}>
        {field.label}
        {field.required ? (
          <span className="req" aria-hidden="true">
            *
          </span>
        ) : (
          <span className="help" style={{ display: 'inline', marginLeft: '0.4rem' }}>
            (optional)
          </span>
        )}
      </label>
      {field.help && (
        <span className="help" id={helpId}>
          {field.help}
        </span>
      )}
      {field.type === 'select' && field.options ? (
        <select {...common} value={text} onChange={(e) => onChange(e.target.value)} autoComplete={field.autocomplete ?? undefined}>
          <option value="">Choose…</option>
          {field.options.map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
        </select>
      ) : field.type === 'textarea' ? (
        <textarea {...common} value={text} maxLength={maxLengthFor(field)} onChange={(e) => onChange(e.target.value)} />
      ) : (
        <input
          {...common}
          type={field.type === 'email' ? 'email' : field.type === 'tel' ? 'tel' : 'text'}
          inputMode={field.type === 'email' ? 'email' : field.type === 'tel' ? 'tel' : undefined}
          autoComplete={field.autocomplete ?? undefined}
          maxLength={maxLengthFor(field)}
          value={text}
          onChange={(e) => onChange(e.target.value)}
          spellCheck={field.type === 'email' ? false : undefined}
        />
      )}
      {error && (
        <span className="error-text" id={errorId}>
          {error}
        </span>
      )}
    </div>
  );
}

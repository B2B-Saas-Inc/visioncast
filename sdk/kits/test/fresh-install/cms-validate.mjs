// Validates the CMS kit schema files with the vendors' own packages. Run from a directory where
// sanity, @sanity/types, payload, typescript, contentful-import and @prismicio/types-internal are
// installed (fresh-install.sh cms), with KITS_REPO pointing at the repository root.
//   node cms-validate.mjs
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const REPO = process.env.KITS_REPO || path.resolve(path.dirname(new URL(import.meta.url).pathname), '../../../..');
const results = [];
const ok = (name, cond, detail) => {
  results.push([name, !!cond]);
  console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : ': ' + detail));
  if (!cond) process.exitCode = 1;
};

// --- Prismic: Slice Machine shared-slice model codecs (@prismicio/types-internal, zod4 + io-ts) ---
{
  const model = JSON.parse(fs.readFileSync(path.join(REPO, 'sdk/kits/cms/prismic/webinar_reference/model.json'), 'utf8'));
  const zod = await import('@prismicio/types-internal/zod4');
  const z = zod.SharedSliceModelSchema.safeParse(model);
  ok('prismic: model.json parses with SharedSliceModelSchema (zod4)', z.success, z.success ? '' : JSON.stringify(z.error.issues));
  // (The io-ts variant needs the optional io-ts peer dependency; the zod4 codec is the current one.)
  ok('prismic: variation.primary has exactly event_slug + occurrence_id Text fields',
    Object.entries(model.variations[0].primary).every(([k, v]) => ['event_slug', 'occurrence_id'].includes(k) && v.type === 'Text'));
}

// --- Sanity: defineType + schema compilation (sanity createSchema) ---
{
  const mod = await import('./sanity-webinarReference.mjs'); // verbatim copy; Studio projects are ESM (type: module)
  const sanity = await import('sanity');
  const schema = sanity.createSchema({
    name: 'kit',
    types: [
      mod.webinarReference,
      sanity.defineType({ name: 'page', type: 'document', fields: [sanity.defineField({ name: 'webinar', type: 'webinarReference' })] }),
    ],
  });
  const problems = schema._validation.flatMap((v) => v.problems.filter((p) => p.severity === 'error').map((p) => `${v.path.map((x) => x.name || x.kind).join('.')}: ${p.message}`));
  ok('sanity: createSchema compiles webinarReference + a document using it without errors', problems.length === 0, problems.join(' | '));
  const type = schema.get('webinarReference');
  ok('sanity: object type with eventSlug + occurrenceId string fields', type && type.jsonType === 'object' && type.fields.map((f) => f.name).join(',') === 'eventSlug,occurrenceId');
  // Validation rules run: required + regex on the slug, regex on the UUID (Rule from @sanity/validation via sanity).
  const slugRules = type.fields[0].type.validation;
  const uuidRules = type.fields[1].type.validation;
  const flat = (rules) => rules.flatMap((r) => r._rules.map((x) => x.flag));
  ok('sanity: eventSlug rules are required + regex', JSON.stringify(flat(slugRules)) === JSON.stringify(['presence', 'regex']) || flat(slugRules).includes('presence') && flat(slugRules).includes('regex'), JSON.stringify(flat(slugRules)));
  ok('sanity: occurrenceId rule is regex only', flat(uuidRules).includes('regex') && !flat(uuidRules).includes('presence'), JSON.stringify(flat(uuidRules)));
  const run = async (rules, value) => (await Promise.all(rules.map((r) => r.validate(value, { type: type.fields[0].type })))).flat();
  ok('sanity: slug regex rejects "bad slug"', (await run(slugRules, 'bad slug')).length > 0);
  ok('sanity: slug regex accepts "q3-product-update"', (await run(slugRules, 'q3-product-update')).length === 0);
  ok('sanity: uuid regex rejects "nope" and accepts a UUID',
    (await run(uuidRules, 'nope')).length > 0 && (await run(uuidRules, '59215637-811c-4b31-9921-8d05d182a656')).length === 0);
}

// --- Contentful: the import payload Joi schema used by `contentful space import` (contentful-import assertPayload) ---
{
  const payload = JSON.parse(fs.readFileSync(path.join(REPO, 'sdk/kits/cms/contentful/webinar-reference.import.json'), 'utf8'));
  const src = fs.readFileSync(path.join(process.cwd(), 'node_modules/contentful-import/dist/index.js'), 'utf8');
  const start = src.indexOf('var entrySchema = {');
  const end = src.indexOf('var import_get_entity_name4', start);
  ok('contentful: found assertPayload + Joi schemas in contentful-import', start > 0 && end > start);
  const body = src.slice(start, end) + '\nreturn payloadSchema;';
  const payloadSchema = new Function('import_joi', body)({ default: require('joi') });
  const assertPayload = (p) => { const r = payloadSchema.validate(p, { allowUnknown: true, abortEarly: false }); if (r.error) throw r.error; };
  let err = null;
  try { assertPayload(payload); } catch (e) { err = e; }
  ok('contentful: webinar-reference.import.json passes contentful-import assertPayload (Joi)', !err, err && JSON.stringify(err.details || err.message).slice(0, 400));
  const ct = payload.contentTypes[0];
  ok('contentful: both fields are Symbol with regexp validations', ct.fields.every((f) => f.type === 'Symbol' && f.validations.some((v) => v.regexp && v.regexp.pattern)));
  for (const f of ct.fields) {
    const re = new RegExp(f.validations[0].regexp.pattern);
    ok(`contentful: ${f.id} pattern ${f.id === 'eventSlug' ? 'accepts a slug and rejects a space' : 'accepts a UUID and rejects junk'}`,
      f.id === 'eventSlug' ? re.test('q3-product-update') && !re.test('bad slug') : re.test('59215637-811c-4b31-9921-8d05d182a656') && !re.test('nope'));
  }
}

console.log(`\n${results.filter((r) => r[1]).length}/${results.length} CMS checks passed`);

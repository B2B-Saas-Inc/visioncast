// Type-checks the Payload kit field against the real `payload` package (Field type) and uses it in a
// CollectionConfig, which is how a site would consume it.
import type { CollectionConfig, Field } from 'payload';
import { webinarReference } from './webinarReference.js'; // verbatim copy of sdk/kits/cms/payload/webinarReference.ts

const field: Field = webinarReference();
const named: Field = webinarReference('promo');

export const Pages: CollectionConfig = {
  slug: 'pages',
  fields: [{ name: 'title', type: 'text', required: true }, field, named],
};

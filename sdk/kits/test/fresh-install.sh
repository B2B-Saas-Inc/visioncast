#!/bin/bash
# Fresh-install verification of the website integration kits against a real local backend.
# NOT part of `node --test`; it needs Docker, npm registry access and ~10 minutes.
#
#   bash sdk/kits/test/fresh-install.sh            # everything
#   bash sdk/kits/test/fresh-install.sh wordpress  # one stage: backend|wordpress|next|sveltekit|approach-b|react|cms|down
#
# Prerequisites (verified 2026-10-27 on macOS, Docker 29.8, Node 26.4, npm 12, Python 3.12 venv):
#   - Docker with the images below pullable (wordpress:php8.2-apache, mariadb:11, wordpress:cli-php8.2,
#     php:8.2-cli, nginx:1.27-alpine); a Postgres 16 container named webinar-pg16 on 127.0.0.1:55432
#     (user webinars / password disposable-test), or set KITS_PG_* below.
#   - The backend's Python venv (KITS_PYTHON) with the project's requirements installed.
#   - Playwright: `npm install --prefix /tmp/pw playwright@1.58.2 && (cd /tmp/pw && npx playwright install chromium)`.
#     If the install is incomplete, point PLAYWRIGHT_CHROMIUM_EXECUTABLE at any Chromium build.
#   - Network access for create-next-app / sv / create-astro / create-nuxt / create-vite and the CMS packages.
#
# Everything the script creates is prefixed wk- (containers, volumes, network) and lives under KITS_SCRATCH.
# The backend DB is created on the Postgres container as $KITS_PG_DB. `down` removes all of it.
set -euo pipefail

REPO=${KITS_REPO:-$(cd "$(dirname "$0")/../../.." && pwd)}
S=${KITS_SCRATCH:-/tmp/wk-kits}
PY=${KITS_PYTHON:-$REPO/.venv-local/bin/python}
PW=${KITS_PLAYWRIGHT:-/tmp/pw/node_modules}
PG_HOST=${KITS_PG_HOST:-127.0.0.1}; PG_PORT=${KITS_PG_PORT:-55432}; PG_DB=${KITS_PG_DB:-ws_kits}
PG_USER=${KITS_PG_USER:-webinars}; PG_PASSWORD=${KITS_PG_PASSWORD:-disposable-test}; PG_CONTAINER=${KITS_PG_CONTAINER:-webinar-pg16}
BACKEND_PORT=8765
HELPERS=$REPO/sdk/kits/test/fresh-install
STAGE=${1:-all}
mkdir -p "$S"

flow() { # <url> [flow args]
  NODE_PATH=$PW node "$HELPERS/flow.cjs" "$@"
}

# ---------------------------------------------------------------------------
backend() {
  cat > "$S/env.sh" <<EOF
export DJANGO_SECRET_KEY=local-test-secret-key-not-for-production-0000000000
export INTEGRATION_MASTER_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=
export POSTGRES_HOST=$PG_HOST POSTGRES_PORT=$PG_PORT POSTGRES_DB=$PG_DB POSTGRES_USER=$PG_USER POSTGRES_PASSWORD=$PG_PASSWORD
export DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,host.docker.internal
export DJANGO_DEBUG=1 PUBLIC_BASE_URL=http://127.0.0.1:$BACKEND_PORT DJANGO_STATIC_ROOT=$S/staticfiles
export CAPABILITY_COOKIE_SECURE=0 RATE_LIMIT_ENABLED=0
EOF
  docker exec "$PG_CONTAINER" createdb -U "$PG_USER" "$PG_DB" 2>/dev/null || true
  ( . "$S/env.sh"; cd "$REPO"
    "$PY" manage.py migrate -v 0
    "$PY" manage.py seed_public_demo --allow-registration | tail -1
    "$PY" manage.py create_api_key --name kits --scope proxy > "$S/apikey.txt"
    "$PY" manage.py shell < "$HELPERS/kit-form.py"
    nohup "$PY" manage.py runserver 0.0.0.0:$BACKEND_PORT --noreload > "$S/backend.log" 2>&1 &
    echo $! > "$S/backend.pid" )
  for i in $(seq 1 30); do curl -sf "http://127.0.0.1:$BACKEND_PORT/api/v1/events" >/dev/null && break; sleep 1; done
  # The occurrence the kits register for (demo-onboarding-office-hours now uses the kit-v1 form).
  curl -s "http://127.0.0.1:$BACKEND_PORT/api/v1/events/demo-onboarding-office-hours" \
    | node -e 'let d="";process.stdin.on("data",c=>d+=c).on("end",()=>{const e=JSON.parse(d);const o=e.occurrences.find(o=>o.registrationOpen);process.stdout.write(o.id+"\n"+e.formVersion+"\n")})' > "$S/occurrence.txt"
  echo "backend up; occurrence $(head -1 "$S/occurrence.txt") form $(tail -1 "$S/occurrence.txt")"
}

key() { tail -1 "$S/apikey.txt"; }
occ() { head -1 "$S/occurrence.txt"; }
fv() { tail -1 "$S/occurrence.txt"; }

# ---------------------------------------------------------------------------
wpcli() { # [--blog] <wp args>
  local vol=wk-wp-data db=wordpress dir=/var/www/html
  if [ "${1:-}" = "--blog" ]; then vol=wk-wp-blog; db=wordpress_blog; dir=/var/www/html/blog; shift; fi
  docker run --rm --network wk-net --user 33:33 -w "$dir" \
    -e WORDPRESS_DB_HOST=wk-mariadb -e WORDPRESS_DB_USER=wp -e WORDPRESS_DB_PASSWORD=wp-disposable -e WORDPRESS_DB_NAME=$db \
    -v "$vol:$dir" -v "$REPO/sdk/kits/wordpress/webinar-platform:$dir/wp-content/plugins/webinar-platform:ro" \
    --add-host=host.docker.internal:host-gateway wordpress:cli-php8.2 wp --path="$dir" "$@"
}

wp_site() { # <container> <volume> <dir> <port> <url> <db>
  local c=$1 vol=$2 dir=$3 port=$4 url=$5 db=$6
  docker volume create "$vol" >/dev/null
  docker run -d --name "$c" --network wk-net -p 127.0.0.1:$port:80 -w "$dir" --add-host=host.docker.internal:host-gateway \
    -e WORDPRESS_DB_HOST=wk-mariadb -e WORDPRESS_DB_USER=wp -e WORDPRESS_DB_PASSWORD=wp-disposable -e WORDPRESS_DB_NAME=$db \
    -e WORDPRESS_CONFIG_EXTRA="define('WP_ENVIRONMENT_TYPE', 'local'); define('WEBINAR_PLATFORM_PROXY_KEY', '$(key)');" \
    -v "$vol:$dir" -v "$REPO/sdk/kits/wordpress/webinar-platform:$dir/wp-content/plugins/webinar-platform:ro" \
    wordpress:php8.2-apache >/dev/null
  for i in $(seq 1 60); do [ "$(curl -s -o /dev/null -w '%{http_code}' "$url/wp-admin/install.php")" = 200 ] && break; sleep 2; done
  local blog=(); [ "$dir" != /var/www/html ] && blog=(--blog)
  for i in $(seq 1 30); do wpcli "${blog[@]}" db check >/dev/null 2>&1 && break; sleep 2; done
  wpcli "${blog[@]}" core install --url="$url" --title="Kit test" --admin_user=admin --admin_password=admin-disposable-pw --admin_email=admin@example.invalid --skip-email
  wpcli "${blog[@]}" plugin activate webinar-platform
  wpcli "${blog[@]}" rewrite structure '/%postname%/' --hard >/dev/null 2>&1 || true
  if [ "$dir" != /var/www/html ]; then
    # The image's .htaccess rewrites to /index.php; a subdirectory install needs RewriteBase + /blog/index.php.
    printf '<IfModule mod_rewrite.c>\nRewriteEngine On\nRewriteBase /blog/\nRewriteRule ^index\\.php$ - [L]\nRewriteCond %%{REQUEST_FILENAME} !-f\nRewriteCond %%{REQUEST_FILENAME} !-d\nRewriteRule . /blog/index.php [L]\n</IfModule>\n' > "$S/blog.htaccess"
    docker cp "$S/blog.htaccess" "$c:$dir/.htaccess"
  fi
  # Self-hosted SDK copy: the browser on the host cannot resolve host.docker.internal.
  docker cp "$REPO/webinars/static/webinar-sdk/webinar-form.js" "$c:$dir/webinar-form.js"
  wpcli "${blog[@]}" option update webinar_platform_settings --format=json \
    '{"backend_url":"http://host.docker.internal:'$BACKEND_PORT'","sdk_url":"/webinar-form.js","allowed_origins":[],"session_cookie":"webinar_session","timeout":10}' >/dev/null
  wpcli "${blog[@]}" post create --post_type=page --post_status=publish --post_title=Register --post_name=register --porcelain \
    --post_content="<!-- wp:webinar-platform/webinar-form {\"occurrenceId\":\"$(occ)\",\"formVersion\":\"$(fv)\",\"fields\":[\"email\",\"firstName\",\"lastName\",\"company\"],\"required\":[\"email\",\"firstName\",\"lastName\"]} /-->" >/dev/null
  wpcli "${blog[@]}" post create --post_type=page --post_status=publish --post_title=Shortcode --post_name=register-sc --porcelain \
    --post_content="[webinar_form occurrence=\"$(occ)\" form_version=\"$(fv)\" fields=\"email,firstName,lastName,company\" required=\"email,firstName,lastName\"]" >/dev/null
}

wordpress() {
  docker run --rm -v "$REPO/sdk/kits/wordpress/webinar-platform:/plugin:ro" php:8.2-cli sh -c 'for f in $(find /plugin -name "*.php" | sort); do php -l "$f"; done'
  docker network create wk-net >/dev/null 2>&1 || true
  docker run -d --name wk-mariadb --network wk-net -e MARIADB_DATABASE=wordpress -e MARIADB_USER=wp -e MARIADB_PASSWORD=wp-disposable -e MARIADB_ROOT_PASSWORD=root-disposable mariadb:11 >/dev/null
  wp_site wk-wordpress wk-wp-data /var/www/html 8080 http://localhost:8080 wordpress
  flow http://localhost:8080/register/ --confirm-only --label wp-block
  flow http://localhost:8080/register-sc/ --confirm-only --label wp-shortcode
  KITS_KEY_FILE="$S/apikey.txt" NODE_PATH=$PW node "$HELPERS/wp-admin.cjs" http://localhost:8080
  # Caching plugin: pages cached, /webinar-api/* not (DONOTCACHEPAGE + no-store).
  wpcli plugin install wp-super-cache --activate >/dev/null
  docker cp "$HELPERS/wp-enable-cache.php" wk-wordpress:/var/www/html/wk-enable-cache.php
  docker exec --user 33:33 wk-wordpress php /var/www/html/wk-enable-cache.php
  curl -s http://localhost:8080/register/ >/dev/null; sleep 1
  curl -s http://localhost:8080/register/ | grep -q "Cached page generated by WP-Super-Cache" && echo "ok   page cached by WP Super Cache"
  flow http://localhost:8080/register/ --confirm-only --label wp-cached
  [ "$(docker exec wk-wordpress sh -c 'find /var/www/html/wp-content/cache -path "*webinar-api*" | wc -l')" = 0 ] && echo "ok   no cache files for /webinar-api"
  # Subdirectory install.
  docker exec wk-mariadb mariadb -uroot -proot-disposable -e "CREATE DATABASE IF NOT EXISTS wordpress_blog; GRANT ALL ON wordpress_blog.* TO 'wp'@'%'; FLUSH PRIVILEGES;"
  wp_site wk-wordpress-blog wk-wp-blog /var/www/html/blog 8084 http://localhost:8084/blog wordpress_blog
  flow http://localhost:8084/blog/register/ --proxy-base /blog/webinar-api --confirm-only --label wp-blog
  [ "$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:8084/webinar-api/profile?occurrenceId=$(occ)&formVersion=$(fv)")" = 404 ] && echo "ok   root-level /webinar-api is not served on a /blog/ install"
}

# ---------------------------------------------------------------------------
proxy_env() {
  export PROXY_BACKEND_URL=http://127.0.0.1:$BACKEND_PORT PROXY_ALLOW_INSECURE_BACKEND=1 PROXY_API_KEY="$(key)"
  export WEBINAR_API_BASE=http://127.0.0.1:$BACKEND_PORT WEBINAR_ALLOW_INSECURE_BACKEND=1 WEBINAR_SDK_URL=/webinar-form.js
}

next() {
  cd "$S"; rm -rf next-app
  npx --yes create-next-app@latest next-app --js --app --no-tailwind --no-eslint --no-src-dir --no-turbopack --import-alias "@/*" --use-npm --disable-git --yes >/dev/null
  cd next-app
  mkdir -p components lib "app/webinars/[slug]" webinar-kit
  cp "$REPO/sdk/kits/frameworks/react/WebinarForm.jsx" components/WebinarForm.jsx
  cp "$REPO/sdk/kits/headless/webinar-events.mjs" lib/webinar-events.mjs
  cp "$REPO/sdk/kits/frameworks/nextjs/app/webinars/[slug]/page.jsx" "app/webinars/[slug]/page.jsx"
  cp "$REPO/sdk/kits/frameworks/nextjs/server.mjs" server.mjs
  cp "$REPO/sdk/kits/node/mount.cjs" webinar-kit/mount.cjs
  cp "$REPO/sdk/proxy/server.js" webinar-kit/server.cjs
  cp "$REPO/webinars/static/webinar-sdk/webinar-form.js" public/webinar-form.js
  proxy_env; export PROXY_ALLOWED_ORIGINS=http://localhost:3000
  npx next build | tail -3
  NODE_ENV=production PORT=3000 HOST=127.0.0.1 nohup node server.mjs > "$S/next.log" 2>&1 & echo $! > "$S/next.pid"
  sleep 3
  flow http://localhost:3000/webinars/demo-onboarding-office-hours --confirm-only --label next
}

sveltekit() {
  cd "$S"; rm -rf sveltekit-app
  npx --yes sv@latest create sveltekit-app --template minimal --no-types --no-add-ons --install npm >/dev/null
  cd sveltekit-app
  npm install -D @sveltejs/adapter-node >/dev/null
  mkdir -p src/lib "src/routes/webinars/[slug]" webinar-kit static
  cp "$REPO/sdk/kits/frameworks/svelte/WebinarForm.svelte" src/lib/WebinarForm.svelte
  cp "$REPO/sdk/kits/headless/webinar-events.mjs" src/lib/webinar-events.mjs
  cp "$REPO/sdk/kits/frameworks/sveltekit/server.js" server.js
  cp "$REPO/sdk/kits/node/mount.cjs" webinar-kit/mount.cjs
  cp "$REPO/sdk/proxy/server.js" webinar-kit/server.cjs
  cp "$REPO/webinars/static/webinar-sdk/webinar-form.js" static/webinar-form.js
  cat > "src/routes/webinars/[slug]/+page.server.js" <<'EOF'
import { error } from '@sveltejs/kit';
import { formProps, getWebinarEvent, pickOccurrence, sdkScriptUrl } from '../../../lib/webinar-events.mjs';
export async function load({ params }) {
  const event = await getWebinarEvent(params.slug);
  if (!event) error(404, 'Not found');
  const occurrence = pickOccurrence(event);
  return { event, occurrence, form: formProps(event, occurrence), sdkSrc: sdkScriptUrl() };
}
EOF
  cat > "src/routes/webinars/[slug]/+page.svelte" <<'EOF'
<script>
  import WebinarForm from '../../../lib/WebinarForm.svelte';
  let { data } = $props();
</script>
<h1>{data.event.title}</h1>
{#if data.form}<WebinarForm {...data.form} sdkSrc={data.sdkSrc} />{:else}<p>Registration is closed.</p>{/if}
EOF
  sed -i.bak 's|@sveltejs/adapter-auto|@sveltejs/adapter-node|' vite.config.js && rm vite.config.js.bak
  proxy_env; export PROXY_ALLOWED_ORIGINS=http://localhost:3001
  npm run build | tail -2
  PORT=3001 HOST=127.0.0.1 ORIGIN=http://localhost:3001 nohup node server.js > "$S/sveltekit.log" 2>&1 & echo $! > "$S/sveltekit.pid"
  sleep 2
  flow http://localhost:3001/webinars/demo-onboarding-office-hours --confirm-only --label sveltekit
}

approach_b() { # Astro (@astrojs/node standalone), Nuxt 4 and a static page from the nocode snippet, behind nginx + sidecar.
  cd "$S"; rm -rf astro-app nuxt-app site nginx; mkdir -p site nginx/kit
  npm create astro@latest astro-app -- --template minimal --install --no-git --yes --skip-houston >/dev/null
  ( cd astro-app
    mkdir -p src/components src/lib src/pages/webinars public
    cp "$REPO/sdk/kits/frameworks/astro/WebinarForm.astro" src/components/WebinarForm.astro
    cp "$REPO/sdk/kits/headless/webinar-events.mjs" src/lib/webinar-events.mjs
    cp "$REPO/webinars/static/webinar-sdk/webinar-form.js" public/webinar-form.js
    cat > "src/pages/webinars/[slug].astro" <<'EOF'
---
export const prerender = false;
import WebinarForm from '../../components/WebinarForm.astro';
import { formProps, getWebinarEvent, pickOccurrence, sdkScriptUrl } from '../../lib/webinar-events.mjs';
const event = await getWebinarEvent(Astro.params.slug);
if (!event) return new Response(null, { status: 404 });
const props = formProps(event, pickOccurrence(event));
---
<html lang="en"><head><meta charset="utf-8" /><title>{event.title}</title></head>
<body><h1>{event.title}</h1>{props ? <WebinarForm {...props} sdkSrc={sdkScriptUrl()} /> : <p>Registration is closed.</p>}</body></html>
EOF
    npx astro add node --yes >/dev/null
    proxy_env; npm run build | tail -1
    HOST=127.0.0.1 PORT=4321 nohup node dist/server/entry.mjs > "$S/astro.log" 2>&1 & echo $! > "$S/astro.pid" )
  CI=1 npx --yes create-nuxt@latest nuxt-app --template minimal --packageManager npm --gitInit false --no-install --force >/dev/null
  ( cd nuxt-app
    npm install >/dev/null
    mkdir -p app/components app/pages/webinars server/api/webinar server/utils public
    cp "$REPO/sdk/kits/frameworks/vue/WebinarForm.vue" app/components/WebinarForm.vue
    cp "$REPO/sdk/kits/headless/webinar-events.mjs" server/utils/webinar-events.mjs
    cp "$REPO/webinars/static/webinar-sdk/webinar-form.js" public/webinar-form.js
    cat > "server/api/webinar/[slug].get.js" <<'EOF'
import { formProps, getWebinarEvent, pickOccurrence, sdkScriptUrl } from '../../utils/webinar-events.mjs';
export default defineEventHandler(async (event) => {
  const webinar = await getWebinarEvent(getRouterParam(event, 'slug'));
  if (!webinar) throw createError({ statusCode: 404, statusMessage: 'Not found' });
  const occurrence = pickOccurrence(webinar);
  return { event: webinar, occurrence, form: formProps(webinar, occurrence), sdkSrc: sdkScriptUrl() };
});
EOF
    cat > "app/pages/webinars/[slug].vue" <<'EOF'
<script setup>
const route = useRoute();
const { data, error } = await useFetch(`/api/webinar/${route.params.slug}`);
if (error.value) throw createError({ statusCode: 404, statusMessage: 'Not found' });
</script>
<template>
  <main>
    <h1>{{ data.event.title }}</h1>
    <WebinarForm v-if="data.form" v-bind="data.form" :sdk-src="data.sdkSrc" />
    <p v-else>Registration is closed.</p>
  </main>
</template>
EOF
    printf '<template>\n  <NuxtPage />\n</template>\n' > app/app.vue
    proxy_env; npm run build | tail -1
    HOST=127.0.0.1 PORT=3002 nohup node .output/server/index.mjs > "$S/nuxt.log" 2>&1 & echo $! > "$S/nuxt.pid" )
  # Static page from the nocode snippet (Webflow/Framer markup), SDK self-hosted on this origin.
  cp "$REPO/webinars/static/webinar-sdk/webinar-form.js" site/webinar-form.js
  { printf '<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><title>Static embed</title></head><body>\n';
    sed -e "s/00000000-0000-0000-0000-000000000000/$(occ)/" -e "s/REPLACE_FORM_VERSION/$(fv)/" \
        -e 's|https://webinars.example.com/static/webinar-sdk/webinar-form.js|/webinar-form.js|' "$REPO/sdk/kits/nocode/embed-snippet.html";
    printf '</body></html>\n'; } > site/index.html
  # Sidecar proxy (bound to 0.0.0.0 only so the nginx container can reach the host; production binds 127.0.0.1).
  ( proxy_env; export PROXY_ALLOWED_ORIGINS=http://localhost:8081,http://localhost:8082,http://localhost:8083 PROXY_TRUST_FORWARDED_FOR=1 HOST=0.0.0.0 PORT=8787
    nohup node "$REPO/sdk/proxy/server.js" > "$S/sidecar.log" 2>&1 & echo $! > "$S/sidecar.pid" )
  # nginx front: the shipped location block verbatim (syntax-checked), plus a host-rewritten copy for Docker.
  printf 'server { listen 80; include /etc/nginx/kit/nginx-webinar-api.conf; }\n' > nginx/verbatim.conf
  docker run --rm -v "$S/nginx/verbatim.conf:/etc/nginx/conf.d/default.conf:ro" -v "$REPO/sdk/kits/node/nginx-webinar-api.conf:/etc/nginx/kit/nginx-webinar-api.conf:ro" nginx:1.27-alpine nginx -t
  sed 's|127.0.0.1:8787|host.docker.internal:8787|' "$REPO/sdk/kits/node/nginx-webinar-api.conf" > nginx/kit/nginx-webinar-api.conf
  cp "$HELPERS/nginx-test-front.conf" nginx/default.conf
  docker run -d --name wk-nginx --add-host=host.docker.internal:host-gateway -p 127.0.0.1:8081:80 -p 127.0.0.1:8082:81 -p 127.0.0.1:8083:82 \
    -v "$S/nginx/default.conf:/etc/nginx/conf.d/default.conf:ro" -v "$S/nginx/kit/nginx-webinar-api.conf:/etc/nginx/kit/nginx-webinar-api.conf:ro" \
    -v "$S/site:/site:ro" nginx:1.27-alpine >/dev/null
  sleep 2
  flow http://localhost:8081/webinars/demo-onboarding-office-hours --confirm-only --label astro
  flow http://localhost:8082/webinars/demo-onboarding-office-hours --confirm-only --label nuxt
  flow http://localhost:8083/ --label static   # snippet has no company field, so not confirm-only
}

react() { # Vite build + mount smoke only
  cd "$S"; rm -rf react-app
  npm create vite@latest react-app -- --template react --no-interactive >/dev/null
  cd react-app && npm install >/dev/null
  mkdir -p src/components public
  cp "$REPO/sdk/kits/frameworks/react/WebinarForm.jsx" src/components/WebinarForm.jsx
  cp "$REPO/webinars/static/webinar-sdk/webinar-form.js" public/webinar-form.js
  cat > src/App.jsx <<EOF
import WebinarForm from './components/WebinarForm.jsx';
export default function App() {
  return <WebinarForm occurrenceId="$(occ)" formVersion="$(fv)" sdkSrc="/webinar-form.js" />;
}
EOF
  npm run build | tail -2
}

cms() {
  mkdir -p "$S/cms"; cd "$S/cms"
  [ -f package.json ] || npm init -y >/dev/null
  npm install --no-audit --no-fund sanity @sanity/types payload typescript contentful-import @prismicio/types-internal >/dev/null
  cp "$REPO/sdk/kits/cms/sanity/webinarReference.js" sanity-webinarReference.mjs   # Studio projects are ESM
  cp "$REPO/sdk/kits/cms/payload/webinarReference.ts" webinarReference.ts
  cp "$HELPERS/cms-validate.mjs" "$HELPERS/payload-check.ts" "$HELPERS/tsconfig.json" .
  KITS_REPO="$REPO" node cms-validate.mjs
  npx tsc -p tsconfig.json && echo "ok   payload: webinarReference.ts type-checks against payload $(node -p "require('payload/package.json').version")"
}

down() {
  for p in backend next sveltekit astro nuxt sidecar; do [ -f "$S/$p.pid" ] && kill "$(cat "$S/$p.pid")" 2>/dev/null || true; done
  docker rm -f wk-wordpress wk-wordpress-blog wk-mariadb wk-nginx >/dev/null 2>&1 || true
  docker volume rm wk-wp-data wk-wp-blog >/dev/null 2>&1 || true
  docker network rm wk-net >/dev/null 2>&1 || true
  docker exec "$PG_CONTAINER" dropdb -U "$PG_USER" --if-exists "$PG_DB" 2>/dev/null || true
  echo "wk- resources removed; scratch left at $S (delete it yourself)"
}

case "$STAGE" in
  all) backend; wordpress; next; sveltekit; approach_b; react; cms ;;
  backend|wordpress|next|sveltekit|react|cms|down) "$STAGE" ;;
  approach-b) approach_b ;;
  *) echo "unknown stage $STAGE"; exit 2 ;;
esac

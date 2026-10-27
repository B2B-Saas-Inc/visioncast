=== Webinar Platform ===
Requires at least: 6.3
Requires PHP: 7.4
Stable tag: 0.1.0
License: Apache-2.0

Webinar RSVP forms for WordPress with a first-party proxy. The API key stays on the server.

== Description ==

* Block "Webinar registration form" and shortcode [webinar_form].
* Proxy at /webinar-api/{profile,registrations,reset,my-registration,calendar.ics}. It is a PHP port of sdk/proxy/server.js with the same checks and projections. It is not a general forwarder.
* Paid sessions: the SDK follows the backend's 202 payment_required response to Stripe Checkout. Nobody is registered until the payment webhook arrives.

Status: installable plugin, not production-tested. See docs/kits.md in the platform repository.

== Installation ==

1. Copy the webinar-platform/ folder into wp-content/plugins/ and activate it.
2. Create a proxy-scoped key on the backend: python manage.py create_api_key --name wordpress --scope proxy
3. Put the key in wp-config.php: define( 'WEBINAR_PLATFORM_PROXY_KEY', '...' ); You can also enter it once in Settings → Webinar Platform. The settings page never shows it again.
4. In Settings → Webinar Platform, set the Backend URL (https origin).
5. Pretty permalinks must be enabled, or the web server must route /webinar-api/* to index.php.
6. Exclude /webinar-api/* from any page cache or CDN cache.
7. Add the block, or [webinar_form occurrence="<uuid>" form_version="<version>"], to a page.

== Shortcode ==

[webinar_form occurrence="<uuid>" form_version="v1" fields="email,firstName,lastName,company" required="email" submit_label="Register" confirm_label="Confirm my RSVP"]

Fields are limited to: email, firstName, lastName, name, phone, company, jobTitle. For custom.*, SMS consent or select fields, use the Studio "Embed on your site" snippet in a Custom HTML block. Only users with the unfiltered_html capability can save that snippet.

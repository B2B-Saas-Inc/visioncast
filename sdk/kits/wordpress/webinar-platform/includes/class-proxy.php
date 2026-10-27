<?php
/**
 * First-party proxy: a PHP port of sdk/proxy/server.js for WordPress.
 *
 * Serves exactly the five /webinar-api/* routes the browser SDK uses, on this
 * site's origin. It applies the same checks as the Node proxy: exact Origin,
 * Sec-Fetch-Site, double-submit CSRF, JSON content type, body size, response
 * projection and session-cookie rewriting. The scoped proxy key is held
 * server-side only and is sent to the configured backend origin, never to the
 * browser. Not a general forwarder: the upstream path comes from the fixed
 * route table below, never from the request.
 *
 * @package WebinarPlatform
 */

defined( 'ABSPATH' ) || exit;

/** An HTTP error with a machine code; the code is the only body the browser sees. */
final class Webinar_Platform_Http_Error extends Exception {
	/** @var int */
	public $status;
	/** @var string */
	public $error_code;

	public function __construct( $status, $error_code ) {
		parent::__construct( $error_code );
		$this->status     = (int) $status;
		$this->error_code = (string) $error_code;
	}
}

final class Webinar_Platform_Proxy {
	// Keep in step with ROUTES in sdk/proxy/server.js (sdk/kits/test/kits.test.js checks this).
	const ROUTES = array(
		'profile'         => array( 'method' => 'GET', 'upstream' => '/api/v1/profile' ),
		'registrations'   => array( 'method' => 'POST', 'upstream' => '/api/v1/registrations' ),
		'reset'           => array( 'method' => 'POST', 'upstream' => '/api/v1/reset' ),
		'my-registration' => array( 'method' => 'GET', 'upstream' => '/api/v1/my-registration' ),
		'calendar.ics'    => array( 'method' => 'GET', 'upstream' => '/api/v1/occurrences/{id}/calendar.ics' ),
	);

	// The SDK hard-codes this cookie name (webinar-form.js CSRF_COOKIE).
	const CSRF_COOKIE = '__Host-webinar_csrf';

	const UUID_RE         = '/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/iD';
	const BACKEND_ICS_RE  = '#^/api/v1/occurrences/([0-9a-fA-F-]{36})/calendar\.ics$#D';
	const TOKEN_RE        = '/^[!#$%&\'*+.^_`|~0-9A-Za-z-]{1,128}$/D';
	const COOKIE_VALUE_RE = '/^[\x21\x23-\x2B\x2D-\x3A\x3C-\x5B\x5D-\x7E]{1,4096}$/D';
	const QUERY_VALUE_RE  = '/^[A-Za-z0-9._:-]{1,128}$/D';
	const CSRF_VALUE_RE   = '/^[A-Za-z0-9_-]{43}$/D';

	const MAX_UPSTREAM_BODY = 262144; // 256 KiB
	const MAX_ICS_BODY      = 65536;  // 64 KiB
	const MAX_WEB_LINK      = 8192;

	/** Exact https origin + path of each web calendar link (webinars/calendar_links.py). */
	const CALENDAR_WEB_LINKS = array(
		'googleUrl'    => 'https://calendar.google.com/calendar/render',
		'outlookUrl'   => 'https://outlook.live.com/calendar/0/deeplink/compose',
		'office365Url' => 'https://outlook.office.com/calendar/0/deeplink/compose',
	);

	public static function register() {
		// parse_request runs before redirect_canonical and template loading.
		add_action( 'parse_request', array( __CLASS__, 'maybe_handle' ), 0 );
	}

	/** "/webinar-api/" under the site's home path (e.g. "/blog/webinar-api/" for a subdirectory install). */
	public static function base_path() {
		$home = wp_parse_url( home_url( '/' ), PHP_URL_PATH );
		$home = is_string( $home ) ? rtrim( $home, '/' ) : '';
		return $home . '/webinar-api/';
	}

	public static function maybe_handle() {
		$uri  = self::server( 'REQUEST_URI' );
		$base = self::base_path();
		if ( null === $uri ) {
			return;
		}
		$raw_path = explode( '?', $uri, 2 )[0];
		if ( 0 !== strpos( $raw_path, $base ) && rtrim( $base, '/' ) !== $raw_path ) {
			return;
		}
		if ( ! defined( 'DONOTCACHEPAGE' ) ) {
			define( 'DONOTCACHEPAGE', true ); // honoured by common page-cache plugins
		}
		header( 'Cache-Control: no-store' );
		header( 'X-Content-Type-Options: nosniff' );
		header( 'Referrer-Policy: no-referrer' );
		header( 'Vary: Origin, Cookie' );
		// Exact match on the raw path: encoded, traversal and trailing-slash variants get 404.
		$name = (string) substr( $raw_path, strlen( $base ) );
		try {
			if ( ! isset( self::ROUTES[ $name ] ) ) {
				throw new Webinar_Platform_Http_Error( 404, 'not_found' );
			}
			if ( ! Webinar_Platform_Settings::is_configured() ) {
				throw new Webinar_Platform_Http_Error( 503, 'not_configured' );
			}
			self::route( $name, self::ROUTES[ $name ] );
		} catch ( Webinar_Platform_Http_Error $e ) {
			if ( 413 === $e->status ) {
				header( 'Connection: close' );
			}
			if ( 405 === $e->status && isset( self::ROUTES[ $name ] ) ) {
				header( 'Allow: ' . self::ROUTES[ $name ]['method'] );
			}
			self::send_json( $e->status, array( 'error' => $e->error_code ) );
		} catch ( Throwable $e ) {
			error_log( 'webinar-platform proxy: unexpected ' . get_class( $e ) ); // phpcs:ignore WordPress.PHP.DevelopmentFunctions
			self::send_json( 500, array( 'error' => 'internal_error' ) );
		}
	}

	private static function route( $name, $spec ) {
		$method = strtoupper( (string) self::server( 'REQUEST_METHOD' ) );
		if ( $method !== $spec['method'] ) {
			throw new Webinar_Platform_Http_Error( 405, 'method_not_allowed' );
		}
		if ( ! self::origin_ok( $method ) ) {
			throw new Webinar_Platform_Http_Error( 403, 'origin_not_allowed' );
		}
		if ( 'calendar.ics' === $name ) {
			self::proxy_calendar( $spec );
			return;
		}

		$cookies = self::cookies();
		$session = Webinar_Platform_Settings::session_cookie();
		$headers = array(
			'Accept'            => 'application/json',
			'Authorization'     => 'Bearer ' . Webinar_Platform_Settings::proxy_key(),
			'X-Request-Id'      => wp_generate_uuid4(),
			'X-Forwarded-For'   => self::client_ip(),
			'X-Forwarded-Proto' => 'https',
		);
		if ( isset( $cookies[ $session ] ) && preg_match( self::COOKIE_VALUE_RE, $cookies[ $session ] ) ) {
			$headers['Cookie'] = $session . '=' . $cookies[ $session ];
		}

		$set_cookies = array();
		$path        = $spec['upstream'];
		$body        = null;

		if ( 'my-registration' === $name ) {
			// Reads the capability, so it needs the double-submit token like POST (issued by /profile).
			if ( ! self::csrf_ok( $cookies ) ) {
				throw new Webinar_Platform_Http_Error( 403, 'csrf_failed' );
			}
			$path .= '?' . http_build_query( array( 'occurrenceId' => self::single_occurrence_id() ) );
		} elseif ( 'GET' === $spec['method'] ) {
			$query          = self::query_pairs();
			$occurrence_id  = self::first_query_value( $query, 'occurrenceId' );
			$form_version   = self::first_query_value( $query, 'formVersion' );
			if ( ! preg_match( self::QUERY_VALUE_RE, $occurrence_id ) || ! preg_match( self::QUERY_VALUE_RE, $form_version ) ) {
				throw new Webinar_Platform_Http_Error( 400, 'invalid_query' );
			}
			$path .= '?' . http_build_query( array( 'occurrenceId' => $occurrence_id, 'formVersion' => $form_version ) );
			$existing = isset( $cookies[ self::CSRF_COOKIE ] ) ? $cookies[ self::CSRF_COOKIE ] : '';
			if ( ! preg_match( self::CSRF_VALUE_RE, $existing ) ) {
				$token         = rtrim( strtr( base64_encode( random_bytes( 32 ) ), '+/', '-_' ), '=' );
				$set_cookies[] = self::CSRF_COOKIE . '=' . $token . '; Path=/; Secure; SameSite=Strict; Max-Age=86400';
			}
		} else {
			if ( null === self::server( 'HTTP_ORIGIN' ) ) {
				throw new Webinar_Platform_Http_Error( 403, 'origin_not_allowed' );
			}
			if ( ! self::csrf_ok( $cookies ) ) {
				throw new Webinar_Platform_Http_Error( 403, 'csrf_failed' );
			}
			$is_reset = 'reset' === $name;
			if ( ! self::is_json_content_type( self::server( 'CONTENT_TYPE' ) ) ) {
				if ( ! ( $is_reset && '0' === self::server( 'CONTENT_LENGTH' ) ) ) {
					throw new Webinar_Platform_Http_Error( 415, 'unsupported_media_type' );
				}
			}
			$parsed = self::parse_json_object( self::read_body( Webinar_Platform_Settings::body_limit() ), $is_reset );
			if ( ! $is_reset && ( ! isset( $parsed->schemaVersion ) || '1' !== $parsed->schemaVersion ) ) { // phpcs:ignore WordPress.NamingConventions
				throw new Webinar_Platform_Http_Error( 400, 'unsupported_schema_version' );
			}
			$body                    = wp_json_encode( $parsed );
			$headers['Content-Type'] = 'application/json';
		}

		$response = self::upstream( $spec['method'], $path, $headers, $body, self::MAX_UPSTREAM_BODY );

		foreach ( self::raw_set_cookies( $response ) as $raw ) {
			$rewritten = self::rewrite_set_cookie( $raw, $session );
			if ( null !== $rewritten ) {
				$set_cookies[] = $rewritten;
			}
		}

		$status = (int) wp_remote_retrieve_response_code( $response );
		$text   = (string) wp_remote_retrieve_body( $response );
		if ( $status >= 500 || $status < 200 ) {
			throw new Webinar_Platform_Http_Error( 502, 'bad_gateway' );
		}
		if ( 204 === $status || '' === $text ) {
			self::emit_cookies( $set_cookies );
			status_header( $status );
			exit;
		}
		$type = wp_remote_retrieve_header( $response, 'content-type' );
		$type = is_string( $type ) ? preg_replace( '/;\s*charset=utf-8/i', '', $type ) : '';
		if ( ! self::is_json_content_type( $type ) ) {
			throw new Webinar_Platform_Http_Error( 502, 'bad_gateway' );
		}

		if ( 'registrations' === $name && $status < 400 ) {
			// Passed through (including 202 payment_required with order.checkoutUrl), as the Node proxy does;
			// decoded as objects so empty JSON objects stay objects.
			$payload = json_decode( $text, false );
			if ( JSON_ERROR_NONE !== json_last_error() ) {
				throw new Webinar_Platform_Http_Error( 502, 'bad_gateway' );
			}
			self::send_json( $status, self::rewrite_registration_body( $payload ), $set_cookies );
		}

		$payload = json_decode( $text, true );
		if ( JSON_ERROR_NONE !== json_last_error() ) {
			throw new Webinar_Platform_Http_Error( 502, 'bad_gateway' );
		}
		if ( $status >= 400 ) {
			$payload = self::project_error( $payload );
		} elseif ( 'profile' === $name ) {
			if ( ! is_array( $payload ) ) {
				throw new Webinar_Platform_Http_Error( 502, 'bad_gateway' );
			}
			$payload = self::project_profile( $payload );
		} elseif ( 'my-registration' === $name ) {
			if ( ! self::is_plain( $payload ) ) {
				throw new Webinar_Platform_Http_Error( 502, 'bad_gateway' );
			}
			$payload = self::project_my_registration( $payload );
		}
		self::send_json( $status, $payload, $set_cookies );
	}

	/** GET calendar.ics?occurrenceId=<uuid>: public ICS, no cookies or key forwarded. */
	private static function proxy_calendar( $spec ) {
		$path     = str_replace( '{id}', self::single_occurrence_id(), $spec['upstream'] );
		$response = self::upstream(
			'GET',
			$path,
			array(
				'Accept'       => 'text/calendar',
				'X-Request-Id' => wp_generate_uuid4(),
			),
			null,
			self::MAX_ICS_BODY
		);
		$status = (int) wp_remote_retrieve_response_code( $response );
		if ( 404 === $status ) {
			throw new Webinar_Platform_Http_Error( 404, 'not_found' );
		}
		if ( 200 !== $status ) {
			throw new Webinar_Platform_Http_Error( 502, 'bad_gateway' );
		}
		$type = wp_remote_retrieve_header( $response, 'content-type' );
		$type = is_string( $type ) ? strtolower( trim( explode( ';', $type )[0] ) ) : '';
		$body = (string) wp_remote_retrieve_body( $response );
		if ( 'text/calendar' !== $type ) {
			throw new Webinar_Platform_Http_Error( 502, 'bad_gateway' );
		}
		status_header( 200 );
		header( 'Content-Type: text/calendar; charset=utf-8' );
		header( 'Content-Disposition: attachment; filename="webinar.ics"' );
		header( 'Content-Length: ' . strlen( $body ) );
		echo $body; // phpcs:ignore WordPress.Security.EscapeOutput -- verbatim text/calendar from the fixed backend route
		exit;
	}

	// ---------------------------------------------------------------------
	// Request helpers
	// ---------------------------------------------------------------------

	/** An unslashed $_SERVER value, or null when absent. */
	private static function server( $key ) {
		if ( ! isset( $_SERVER[ $key ] ) || ! is_string( $_SERVER[ $key ] ) ) { // phpcs:ignore WordPress.Security.ValidatedSanitizedInput
			return null;
		}
		return wp_unslash( $_SERVER[ $key ] ); // phpcs:ignore WordPress.Security.ValidatedSanitizedInput -- validated by each caller
	}

	private static function origin_ok( $method ) {
		$site = self::server( 'HTTP_SEC_FETCH_SITE' );
		if ( null !== $site && 'same-origin' !== $site && ! ( 'GET' === $method && 'none' === $site ) ) {
			return false;
		}
		$origin = self::server( 'HTTP_ORIGIN' );
		if ( null === $origin ) {
			return 'GET' === $method;
		}
		return in_array( $origin, Webinar_Platform_Settings::allowed_origins(), true );
	}

	private static function csrf_ok( $cookies ) {
		$header = self::server( 'HTTP_X_WEBINAR_CSRF' );
		$cookie = isset( $cookies[ self::CSRF_COOKIE ] ) ? $cookies[ self::CSRF_COOKIE ] : '';
		if ( null === $header || '' === $cookie ) {
			return false;
		}
		if ( ! preg_match( self::CSRF_VALUE_RE, $header ) || ! preg_match( self::CSRF_VALUE_RE, $cookie ) ) {
			return false;
		}
		return hash_equals( $cookie, $header );
	}

	/** Raw Cookie header parse (PHP's $_COOKIE URL-decodes values and rewrites names). First value wins. */
	private static function cookies() {
		$out    = array();
		$header = self::server( 'HTTP_COOKIE' );
		if ( null === $header ) {
			return $out;
		}
		foreach ( explode( ';', $header ) as $part ) {
			$eq = strpos( $part, '=' );
			if ( false === $eq ) {
				continue;
			}
			$name  = trim( substr( $part, 0, $eq ) );
			$value = trim( substr( $part, $eq + 1 ) );
			if ( preg_match( self::TOKEN_RE, $name ) && ! isset( $out[ $name ] ) ) {
				$out[ $name ] = $value;
			}
		}
		return $out;
	}

	/** Query string as ordered [key, value] pairs (duplicates kept, unlike $_GET). */
	private static function query_pairs() {
		$qs    = (string) self::server( 'QUERY_STRING' );
		$pairs = array();
		if ( '' === $qs ) {
			return $pairs;
		}
		foreach ( explode( '&', $qs ) as $part ) {
			if ( '' === $part ) {
				continue;
			}
			$kv      = explode( '=', $part, 2 );
			$pairs[] = array( urldecode( $kv[0] ), isset( $kv[1] ) ? urldecode( $kv[1] ) : '' );
		}
		return $pairs;
	}

	private static function first_query_value( $pairs, $key ) {
		foreach ( $pairs as $pair ) {
			if ( $pair[0] === $key ) {
				return $pair[1];
			}
		}
		return '';
	}

	/** The single occurrenceId UUID query parameter (lower case), or a 400. */
	private static function single_occurrence_id() {
		$pairs = self::query_pairs();
		if ( 1 !== count( $pairs ) || 'occurrenceId' !== $pairs[0][0] || ! preg_match( self::UUID_RE, $pairs[0][1] ) ) {
			throw new Webinar_Platform_Http_Error( 400, 'invalid_query' );
		}
		return strtolower( $pairs[0][1] );
	}

	private static function client_ip() {
		/**
		 * Client IP sent to the backend as X-Forwarded-For. Default: REMOTE_ADDR. Only return a
		 * forwarded value when your edge overwrites (not appends to) that header.
		 */
		$ip = apply_filters( 'webinar_platform_client_ip', (string) self::server( 'REMOTE_ADDR' ) );
		return is_string( $ip ) && preg_match( '/^[0-9A-Fa-f:.]{1,64}$/D', $ip ) ? $ip : '';
	}

	private static function is_json_content_type( $value ) {
		if ( ! is_string( $value ) ) {
			return false;
		}
		$parts = array_map( 'trim', explode( ';', strtolower( $value ) ) );
		if ( 'application/json' !== array_shift( $parts ) ) {
			return false;
		}
		foreach ( $parts as $p ) {
			if ( '' !== $p && 'charset=utf-8' !== $p ) {
				return false;
			}
		}
		return true;
	}

	private static function read_body( $limit ) {
		$declared = self::server( 'CONTENT_LENGTH' );
		if ( null !== $declared && '' !== $declared && ! ( ctype_digit( $declared ) && (int) $declared <= $limit ) ) {
			throw new Webinar_Platform_Http_Error( 413, 'payload_too_large' );
		}
		$handle = fopen( 'php://input', 'rb' ); // phpcs:ignore WordPress.WP.AlternativeFunctions
		if ( false === $handle ) {
			throw new Webinar_Platform_Http_Error( 400, 'bad_request' );
		}
		$raw = stream_get_contents( $handle, $limit + 1 );
		fclose( $handle ); // phpcs:ignore WordPress.WP.AlternativeFunctions
		if ( false === $raw ) {
			throw new Webinar_Platform_Http_Error( 400, 'bad_request' );
		}
		if ( strlen( $raw ) > $limit ) {
			throw new Webinar_Platform_Http_Error( 413, 'payload_too_large' );
		}
		return $raw;
	}

	private static function parse_json_object( $raw, $allow_empty ) {
		if ( '' === trim( $raw ) ) {
			if ( $allow_empty ) {
				return new stdClass();
			}
			throw new Webinar_Platform_Http_Error( 400, 'invalid_json' );
		}
		$value = json_decode( $raw, false );
		if ( JSON_ERROR_NONE !== json_last_error() || ! is_object( $value ) ) {
			throw new Webinar_Platform_Http_Error( 400, 'invalid_json' );
		}
		return $value;
	}

	// ---------------------------------------------------------------------
	// Upstream
	// ---------------------------------------------------------------------

	private static function upstream( $method, $path, $headers, $body, $max_bytes ) {
		$args = array(
			'method'              => $method,
			'headers'             => $headers,
			'timeout'             => Webinar_Platform_Settings::timeout(),
			'redirection'         => 0,
			'sslverify'           => true,
			'limit_response_size' => $max_bytes + 1,
			'user-agent'          => 'webinar-platform-wordpress/' . WEBINAR_PLATFORM_VERSION,
		);
		if ( null !== $body ) {
			$args['body'] = $body;
		}
		$response = wp_remote_request( Webinar_Platform_Settings::backend_origin() . $path, $args );
		if ( is_wp_error( $response ) ) {
			$timed_out = false !== stripos( $response->get_error_message(), 'timed out' );
			throw new Webinar_Platform_Http_Error( $timed_out ? 504 : 502, $timed_out ? 'gateway_timeout' : 'bad_gateway' );
		}
		if ( strlen( (string) wp_remote_retrieve_body( $response ) ) > $max_bytes ) {
			throw new Webinar_Platform_Http_Error( 502, 'bad_gateway' );
		}
		return $response;
	}

	/** Raw Set-Cookie lines from the Requests response object (WP_Http_Cookie would decode values). */
	private static function raw_set_cookies( $response ) {
		if ( isset( $response['http_response'] ) && $response['http_response'] instanceof WP_HTTP_Requests_Response ) {
			$object = $response['http_response']->get_response_object();
			if ( isset( $object->headers ) && is_object( $object->headers ) && method_exists( $object->headers, 'getValues' ) ) {
				$values = $object->headers->getValues( 'set-cookie' );
				return is_array( $values ) ? $values : array();
			}
		}
		return array(); // Unknown transport: no cookie rather than a mangled one.
	}

	/** Re-issues the backend session cookie with fixed safe attributes; drops anything else. */
	public static function rewrite_set_cookie( $raw, $session_name ) {
		$parts = array_values( array_filter( array_map( 'trim', explode( ';', (string) $raw ) ), 'strlen' ) );
		if ( ! $parts ) {
			return null;
		}
		$first = array_shift( $parts );
		$eq    = strpos( $first, '=' );
		if ( false === $eq ) {
			return null;
		}
		$name  = trim( substr( $first, 0, $eq ) );
		$value = trim( substr( $first, $eq + 1 ) );
		if ( $name !== $session_name ) {
			return null;
		}
		// RFC 6265 allows a DQUOTE-wrapped cookie-value; Django's delete_cookie emits name="".
		if ( strlen( $value ) >= 2 && '"' === $value[0] && '"' === substr( $value, -1 ) ) {
			$value = substr( $value, 1, -1 );
		}
		if ( '' !== $value && ! preg_match( self::COOKIE_VALUE_RE, $value ) ) {
			return null;
		}
		$kept = array();
		foreach ( $parts as $p ) {
			$kv  = explode( '=', $p, 2 );
			$key = strtolower( trim( $kv[0] ) );
			$v   = isset( $kv[1] ) ? trim( $kv[1] ) : '';
			if ( 'max-age' === $key && preg_match( '/^-?\d{1,10}$/D', $v ) ) {
				$kept[] = 'Max-Age=' . $v;
			}
			if ( 'expires' === $key && preg_match( '/^[A-Za-z0-9 ,:-]{1,64}$/D', $v ) ) {
				$kept[] = 'Expires=' . $v;
			}
		}
		return implode( '; ', array_merge( array( $name . '=' . $value, 'Path=/' ), $kept, array( 'HttpOnly', 'Secure', 'SameSite=Lax' ) ) );
	}

	// ---------------------------------------------------------------------
	// Projections (defence in depth; mirror sdk/proxy/server.js)
	// ---------------------------------------------------------------------

	private static function is_plain( $value ) {
		return is_array( $value ) && ( array() === $value || array_keys( $value ) !== range( 0, count( $value ) - 1 ) );
	}

	private static function bounded( $array, $key, $max ) {
		return isset( $array[ $key ] ) && is_string( $array[ $key ] ) && strlen( $array[ $key ] ) <= $max ? $array[ $key ] : null;
	}

	private static function project_profile( $body ) {
		$missing = array();
		if ( isset( $body['missingFields'] ) && is_array( $body['missingFields'] ) ) {
			foreach ( $body['missingFields'] as $f ) {
				if ( is_string( $f ) && strlen( $f ) <= 128 ) {
					$missing[] = $f;
				}
			}
		}
		return array(
			'recognized'    => isset( $body['recognized'] ) && true === $body['recognized'],
			'missingFields' => $missing,
			'formVersion'   => isset( $body['formVersion'] ) && is_string( $body['formVersion'] ) ? $body['formVersion'] : '',
		);
	}

	/** Only a machine code and offending field keys; server text never reaches the browser. */
	private static function project_error( $body ) {
		$out = new stdClass();
		if ( ! self::is_plain( $body ) ) {
			return $out;
		}
		$err  = isset( $body['error'] ) ? $body['error'] : null;
		$code = is_array( $err ) ? ( isset( $err['code'] ) ? $err['code'] : null ) : $err;
		if ( is_string( $code ) && preg_match( '/^[a-z0-9_]{1,64}$/D', $code ) ) {
			$out->error = array( 'code' => $code );
		}
		$fields  = array();
		$sources = array(
			isset( $body['fieldErrors'] ) ? $body['fieldErrors'] : null,
			is_array( $err ) && isset( $err['fields'] ) ? $err['fields'] : null,
			isset( $body['errors'] ) ? $body['errors'] : null,
		);
		foreach ( $sources as $src ) {
			if ( ! is_array( $src ) ) {
				continue;
			}
			$keys = array();
			if ( self::is_plain( $src ) ) {
				$keys = array_keys( $src );
			} else {
				foreach ( $src as $e ) {
					if ( is_array( $e ) && isset( $e['field'] ) ) {
						$keys[] = $e['field'];
					}
				}
			}
			foreach ( $keys as $k ) {
				if ( is_string( $k ) && preg_match( '/^[A-Za-z0-9_.]{1,128}$/D', $k ) && count( $fields ) < 64 ) {
					$fields[ $k ] = 'invalid';
				}
			}
		}
		if ( $fields ) {
			$out->fieldErrors = $fields; // phpcs:ignore WordPress.NamingConventions
		}
		return $out;
	}

	/** Proxy-relative ICS link for a backend ICS URL, or null for an unknown shape. */
	private static function proxied_ics_url( $ics ) {
		$path = $ics;
		if ( preg_match( '#^https?://#i', $ics ) ) {
			$parsed = wp_parse_url( $ics, PHP_URL_PATH );
			$path   = is_string( $parsed ) ? $parsed : '';
		}
		if ( preg_match( self::BACKEND_ICS_RE, $path, $m ) && preg_match( self::UUID_RE, $m[1] ) ) {
			return self::base_path() . 'calendar.ics?' . http_build_query( array( 'occurrenceId' => strtolower( $m[1] ) ) );
		}
		return null;
	}

	/** Points the backend's ICS link at this proxy's calendar route; everything else passes through. */
	private static function rewrite_registration_body( $body ) {
		if ( ! is_object( $body ) || ! isset( $body->calendar ) || ! is_object( $body->calendar ) ) {
			return $body;
		}
		if ( ! isset( $body->calendar->icsUrl ) || ! is_string( $body->calendar->icsUrl ) ) { // phpcs:ignore WordPress.NamingConventions
			return $body;
		}
		$url = self::proxied_ics_url( $body->calendar->icsUrl ); // phpcs:ignore WordPress.NamingConventions
		if ( null === $url ) {
			unset( $body->calendar->icsUrl ); // phpcs:ignore WordPress.NamingConventions
		} else {
			$body->calendar->icsUrl = $url; // phpcs:ignore WordPress.NamingConventions
		}
		return $body;
	}

	/** The link when it is https on the expected calendar service path, else null. */
	private static function web_calendar_link( $value, $expected ) {
		if ( ! is_string( $value ) || strlen( $value ) > self::MAX_WEB_LINK || preg_match( '/[\x00-\x20\x7f]/', $value ) ) {
			return null;
		}
		$u = wp_parse_url( $value );
		if ( ! is_array( $u ) || ! isset( $u['scheme'], $u['host'] ) || 'https' !== strtolower( $u['scheme'] ) ) {
			return null;
		}
		if ( isset( $u['user'] ) || isset( $u['pass'] ) || isset( $u['port'] ) || isset( $u['fragment'] ) ) {
			return null;
		}
		$path = isset( $u['path'] ) ? $u['path'] : '/';
		return 'https://' . strtolower( $u['host'] ) . $path === $expected ? $value : null;
	}

	private static function project_my_registration( $body ) {
		$out = array(
			'occurrenceId' => isset( $body['occurrenceId'] ) && is_string( $body['occurrenceId'] ) && preg_match( self::UUID_RE, $body['occurrenceId'] ) ? $body['occurrenceId'] : '',
			'recognized'   => isset( $body['recognized'] ) && true === $body['recognized'],
			'registered'   => isset( $body['registered'] ) && true === $body['registered'],
			'status'       => isset( $body['status'] ) && is_string( $body['status'] ) && preg_match( '/^[a-z_]{1,32}$/D', $body['status'] ) ? $body['status'] : null,
		);
		if ( $out['registered'] && isset( $body['thankYou'] ) && self::is_plain( $body['thankYou'] ) ) {
			$src      = $body['thankYou'];
			$thank_you = array( 'mode' => isset( $src['mode'] ) && 'redirect' === $src['mode'] ? 'redirect' : 'message' );
			$message  = self::bounded( $src, 'message', 4000 );
			if ( null !== $message ) {
				$thank_you['message'] = $message;
			}
			$redirect = self::bounded( $src, 'redirectUrl', 2048 );
			if ( null !== $redirect && preg_match( '#^https://#i', $redirect ) ) {
				$thank_you['redirectUrl'] = $redirect;
			}
			$out['thankYou'] = $thank_you;
		}
		if ( $out['registered'] && isset( $body['calendar'] ) && self::is_plain( $body['calendar'] ) ) {
			$src      = $body['calendar'];
			$calendar = array();
			$limits   = array( 'title' => 1000, 'startsAt' => 64, 'endsAt' => 64, 'timezone' => 64, 'icsUrl' => 2048, 'uid' => 255 );
			foreach ( $limits as $key => $max ) {
				$value = self::bounded( $src, $key, $max );
				if ( null !== $value ) {
					$calendar[ $key ] = $value;
				}
			}
			foreach ( self::CALENDAR_WEB_LINKS as $key => $expected ) {
				$calendar[ $key ] = self::web_calendar_link( isset( $src[ $key ] ) ? $src[ $key ] : null, $expected );
			}
			if ( isset( $src['sequence'] ) && is_int( $src['sequence'] ) && $src['sequence'] >= 0 ) {
				$calendar['sequence'] = $src['sequence'];
			}
			if ( isset( $calendar['icsUrl'] ) ) {
				$url = self::proxied_ics_url( $calendar['icsUrl'] );
				if ( null === $url ) {
					unset( $calendar['icsUrl'] );
				} else {
					$calendar['icsUrl'] = $url;
				}
			}
			$out['calendar'] = $calendar;
		}
		return $out;
	}

	// ---------------------------------------------------------------------
	// Output
	// ---------------------------------------------------------------------

	private static function emit_cookies( $cookies ) {
		foreach ( $cookies as $cookie ) {
			header( 'Set-Cookie: ' . $cookie, false );
		}
	}

	private static function send_json( $status, $payload, $cookies = array() ) {
		self::emit_cookies( $cookies );
		$body = wp_json_encode( $payload );
		status_header( $status );
		header( 'Content-Type: application/json; charset=utf-8' );
		header( 'Content-Length: ' . strlen( $body ) );
		echo $body; // phpcs:ignore WordPress.Security.EscapeOutput -- JSON response body
		exit;
	}
}

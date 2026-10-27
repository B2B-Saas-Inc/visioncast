<?php
/**
 * Settings → Webinar Platform. Requires manage_options; saved through options.php,
 * which verifies the Settings API nonce. Neither option is exposed over REST.
 *
 * The proxy key is write-only: it is never echoed into the page. Prefer defining
 * WEBINAR_PLATFORM_PROXY_KEY in wp-config.php (or the environment) instead.
 *
 * @package WebinarPlatform
 */

defined( 'ABSPATH' ) || exit;

final class Webinar_Platform_Settings {
	const GROUP      = 'webinar_platform';
	const OPTION     = 'webinar_platform_settings';
	const KEY_OPTION = 'webinar_platform_proxy_key';
	const PAGE       = 'webinar-platform';
	const SDK_PATH   = '/static/webinar-sdk/webinar-form.js';

	public static function register() {
		add_action( 'admin_init', array( __CLASS__, 'register_settings' ) );
		add_action( 'admin_menu', array( __CLASS__, 'add_page' ) );
		add_action( 'admin_notices', array( __CLASS__, 'notice' ) );
	}

	public static function register_settings() {
		register_setting(
			self::GROUP,
			self::OPTION,
			array(
				'type'              => 'array',
				'sanitize_callback' => array( __CLASS__, 'sanitize' ),
				'show_in_rest'      => false,
				'default'           => array(),
			)
		);
		register_setting(
			self::GROUP,
			self::KEY_OPTION,
			array(
				'type'              => 'string',
				'sanitize_callback' => array( __CLASS__, 'sanitize_key' ),
				'show_in_rest'      => false,
				'default'           => '',
			)
		);
	}

	public static function add_page() {
		add_options_page(
			__( 'Webinar Platform', 'webinar-platform' ),
			__( 'Webinar Platform', 'webinar-platform' ),
			'manage_options',
			self::PAGE,
			array( __CLASS__, 'render_page' )
		);
	}

	// ---------------------------------------------------------------------
	// Accessors
	// ---------------------------------------------------------------------

	private static function get( $key, $fallback = '' ) {
		$all = get_option( self::OPTION, array() );
		return is_array( $all ) && isset( $all[ $key ] ) ? $all[ $key ] : $fallback;
	}

	/** True when http backends are acceptable (local / development environments only). */
	private static function allow_insecure() {
		return in_array( wp_get_environment_type(), array( 'local', 'development' ), true );
	}

	private static function key_from_environment() {
		if ( defined( 'WEBINAR_PLATFORM_PROXY_KEY' ) ) {
			return (string) WEBINAR_PLATFORM_PROXY_KEY;
		}
		$env = getenv( 'WEBINAR_PLATFORM_PROXY_KEY' );
		return is_string( $env ) && '' !== $env ? $env : null;
	}

	public static function proxy_key() {
		$key = self::key_from_environment();
		return null !== $key ? $key : (string) get_option( self::KEY_OPTION, '' );
	}

	public static function backend_origin() {
		return (string) self::get( 'backend_url' );
	}

	public static function session_cookie() {
		return (string) self::get( 'session_cookie', 'webinar_session' );
	}

	public static function timeout() {
		return (int) self::get( 'timeout', 10 );
	}

	public static function body_limit() {
		return 32 * 1024;
	}

	/** scheme://host[:port] of the site, which is always allowed. */
	public static function home_origin() {
		return self::origin_of( home_url( '/' ) );
	}

	public static function allowed_origins() {
		$extra = self::get( 'allowed_origins', array() );
		return array_values( array_unique( array_merge( array( self::home_origin() ), is_array( $extra ) ? $extra : array() ) ) );
	}

	/**
	 * The SDK script URL: the override, else the backend's published file. A path override is
	 * resolved against home_url(), so "/webinar-form.js" is "<home>/webinar-form.js" on a
	 * subdirectory install too (wp_register_script would otherwise prefix the site URL itself,
	 * turning "/blog/webinar-form.js" into "/blog/blog/webinar-form.js").
	 */
	public static function sdk_url() {
		$override = (string) self::get( 'sdk_url' );
		if ( '' !== $override ) {
			return '/' === $override[0] ? home_url( $override ) : $override;
		}
		$origin = self::backend_origin();
		return '' === $origin ? '' : $origin . self::SDK_PATH;
	}

	public static function is_configured() {
		$key = self::proxy_key();
		return '' !== self::backend_origin() && strlen( $key ) >= 32 && ! preg_match( '/\s/', $key );
	}

	// ---------------------------------------------------------------------
	// Validation
	// ---------------------------------------------------------------------

	/** scheme://host[:port] for an absolute URL, or '' when it is not a bare origin. */
	private static function origin_of( $url, $origin_only = false ) {
		$u = wp_parse_url( trim( (string) $url ) );
		if ( ! is_array( $u ) || ! isset( $u['scheme'], $u['host'] ) || isset( $u['user'] ) || isset( $u['pass'] ) ) {
			return '';
		}
		if ( $origin_only && ( isset( $u['query'] ) || isset( $u['fragment'] ) || ( isset( $u['path'] ) && '/' !== $u['path'] && '' !== $u['path'] ) ) ) {
			return '';
		}
		$origin = strtolower( $u['scheme'] ) . '://' . strtolower( $u['host'] );
		return isset( $u['port'] ) ? $origin . ':' . (int) $u['port'] : $origin;
	}

	private static function is_local_host( $origin ) {
		$host = wp_parse_url( $origin, PHP_URL_HOST );
		return in_array( $host, array( 'localhost', '127.0.0.1' ), true );
	}

	public static function sanitize( $input ) {
		$old = get_option( self::OPTION, array() );
		$old = is_array( $old ) ? $old : array();
		if ( ! is_array( $input ) ) {
			return $old;
		}
		$out = $old;

		$backend = self::origin_of( isset( $input['backend_url'] ) ? $input['backend_url'] : '', true );
		$scheme  = '' === $backend ? '' : wp_parse_url( $backend, PHP_URL_SCHEME );
		if ( '' !== $backend && ( 'https' === $scheme || ( 'http' === $scheme && self::allow_insecure() ) ) ) {
			$out['backend_url'] = $backend;
		} else {
			add_settings_error( self::OPTION, 'backend_url', __( 'Backend URL must be an https origin (scheme://host[:port], no path). http is accepted only when WP_ENVIRONMENT_TYPE is local or development.', 'webinar-platform' ) );
		}

		$sdk = trim( isset( $input['sdk_url'] ) ? (string) $input['sdk_url'] : '' );
		if ( '' === $sdk ) {
			$out['sdk_url'] = '';
		} elseif ( preg_match( '#^/(?![/\\\\])[A-Za-z0-9._~/%-]{1,512}$#D', $sdk ) || ( 'https' === wp_parse_url( $sdk, PHP_URL_SCHEME ) && '' !== self::origin_of( $sdk ) && ! preg_match( '/[\s"\'<>\\\\]/', $sdk ) ) ) {
			$out['sdk_url'] = $sdk;
		} else {
			add_settings_error( self::OPTION, 'sdk_url', __( 'SDK script URL must be an https URL or a path on this site.', 'webinar-platform' ) );
		}

		$origins = array();
		$lines   = preg_split( '/[\r\n,]+/', isset( $input['allowed_origins'] ) ? (string) $input['allowed_origins'] : '' );
		foreach ( $lines as $line ) {
			$line = trim( $line );
			if ( '' === $line ) {
				continue;
			}
			$origin = self::origin_of( $line, true );
			$ok     = '' !== $origin && $origin === rtrim( $line, '/' ) && ( 0 === strpos( $origin, 'https://' ) || self::is_local_host( $origin ) );
			if ( $ok ) {
				$origins[] = $origin;
			} else {
				add_settings_error( self::OPTION, 'allowed_origins', __( 'Ignored an allowed origin that is not an exact https origin.', 'webinar-platform' ) );
			}
		}
		$out['allowed_origins'] = array_values( array_unique( $origins ) );

		$cookie                = isset( $input['session_cookie'] ) ? trim( (string) $input['session_cookie'] ) : '';
		$out['session_cookie'] = preg_match( Webinar_Platform_Proxy::TOKEN_RE, $cookie ) ? $cookie : 'webinar_session';

		$timeout        = isset( $input['timeout'] ) ? (int) $input['timeout'] : 10;
		$out['timeout'] = max( 1, min( 60, $timeout ) );
		return $out;
	}

	/** Write-only: blank keeps the stored key; the key is never rendered back. */
	public static function sanitize_key( $input ) {
		$old = (string) get_option( self::KEY_OPTION, '' );
		if ( null !== self::key_from_environment() || ! is_string( $input ) ) {
			return $old;
		}
		$key = trim( $input );
		if ( '' === $key ) {
			return $old;
		}
		if ( strlen( $key ) < 32 || preg_match( '/\s/', $key ) ) {
			add_settings_error( self::KEY_OPTION, 'proxy_key', __( 'Proxy key must be at least 32 characters with no whitespace. The previous key was kept.', 'webinar-platform' ) );
			return $old;
		}
		return $key;
	}

	// ---------------------------------------------------------------------
	// Admin UI
	// ---------------------------------------------------------------------

	public static function notice() {
		if ( ! current_user_can( 'manage_options' ) || self::is_configured() ) {
			return;
		}
		$url = admin_url( 'options-general.php?page=' . self::PAGE );
		printf(
			'<div class="notice notice-warning"><p>%s <a href="%s">%s</a></p></div>',
			esc_html__( 'Webinar Platform: the registration proxy is not configured, so forms cannot submit.', 'webinar-platform' ),
			esc_url( $url ),
			esc_html__( 'Configure', 'webinar-platform' )
		);
	}

	public static function render_page() {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( esc_html__( 'You do not have permission to manage these settings.', 'webinar-platform' ) );
		}
		$from_env  = null !== self::key_from_environment();
		$has_key   = '' !== self::proxy_key();
		$origins   = self::get( 'allowed_origins', array() );
		$proxy_url = home_url( '/webinar-api/' );
		?>
		<div class="wrap">
			<h1><?php esc_html_e( 'Webinar Platform', 'webinar-platform' ); ?></h1>
			<p><?php esc_html_e( 'Registration forms submit to this site’s first-party proxy, which forwards a fixed set of routes to your webinar backend with the proxy key. The key never reaches the browser.', 'webinar-platform' ); ?></p>
			<p><?php esc_html_e( 'Proxy base:', 'webinar-platform' ); ?> <code><?php echo esc_html( $proxy_url ); ?></code></p>
			<form method="post" action="options.php">
				<?php settings_fields( self::GROUP ); ?>
				<table class="form-table" role="presentation">
					<tr>
						<th scope="row"><label for="wp-backend-url"><?php esc_html_e( 'Backend URL', 'webinar-platform' ); ?></label></th>
						<td><input id="wp-backend-url" class="regular-text" type="url" name="<?php echo esc_attr( self::OPTION ); ?>[backend_url]" value="<?php echo esc_attr( self::backend_origin() ); ?>" placeholder="https://webinars.example.com" required>
						<p class="description"><?php esc_html_e( 'Origin only, https.', 'webinar-platform' ); ?></p></td>
					</tr>
					<tr>
						<th scope="row"><label for="wp-proxy-key"><?php esc_html_e( 'Proxy API key', 'webinar-platform' ); ?></label></th>
						<td>
							<?php if ( $from_env ) : ?>
								<p><?php esc_html_e( 'Set by WEBINAR_PLATFORM_PROXY_KEY (wp-config.php or environment).', 'webinar-platform' ); ?></p>
							<?php else : ?>
								<input id="wp-proxy-key" class="regular-text" type="password" name="<?php echo esc_attr( self::KEY_OPTION ); ?>" value="" autocomplete="new-password" spellcheck="false">
								<p class="description">
									<?php echo $has_key ? esc_html__( 'A key is stored. Leave blank to keep it.', 'webinar-platform' ) : esc_html__( 'No key stored.', 'webinar-platform' ); ?>
									<?php esc_html_e( 'Use a key with the proxy scope only: python manage.py create_api_key --name wordpress --scope proxy', 'webinar-platform' ); ?>
								</p>
							<?php endif; ?>
						</td>
					</tr>
					<tr>
						<th scope="row"><label for="wp-sdk-url"><?php esc_html_e( 'SDK script URL', 'webinar-platform' ); ?></label></th>
						<td><input id="wp-sdk-url" class="regular-text" type="text" name="<?php echo esc_attr( self::OPTION ); ?>[sdk_url]" value="<?php echo esc_attr( (string) self::get( 'sdk_url' ) ); ?>" placeholder="<?php echo esc_attr( self::backend_origin() . self::SDK_PATH ); ?>">
						<p class="description"><?php esc_html_e( 'Optional. Blank uses the backend’s /static/webinar-sdk/webinar-form.js. For a self-hosted copy, enter a path under this site’s home URL (for example /webinar-form.js; on a subdirectory install that is /blog/webinar-form.js).', 'webinar-platform' ); ?></p></td>
					</tr>
					<tr>
						<th scope="row"><label for="wp-origins"><?php esc_html_e( 'Extra allowed origins', 'webinar-platform' ); ?></label></th>
						<td><textarea id="wp-origins" class="large-text code" rows="3" name="<?php echo esc_attr( self::OPTION ); ?>[allowed_origins]"><?php echo esc_textarea( implode( "\n", is_array( $origins ) ? $origins : array() ) ); ?></textarea>
						<p class="description"><?php echo esc_html( sprintf( /* translators: %s: site origin */ __( 'One exact origin per line. %s is always allowed.', 'webinar-platform' ), self::home_origin() ) ); ?></p></td>
					</tr>
					<tr>
						<th scope="row"><label for="wp-session-cookie"><?php esc_html_e( 'Session cookie name', 'webinar-platform' ); ?></label></th>
						<td><input id="wp-session-cookie" type="text" name="<?php echo esc_attr( self::OPTION ); ?>[session_cookie]" value="<?php echo esc_attr( self::session_cookie() ); ?>">
						<p class="description"><?php esc_html_e( 'Must equal the backend’s CAPABILITY_COOKIE_NAME.', 'webinar-platform' ); ?></p></td>
					</tr>
					<tr>
						<th scope="row"><label for="wp-timeout"><?php esc_html_e( 'Upstream timeout (seconds)', 'webinar-platform' ); ?></label></th>
						<td><input id="wp-timeout" type="number" min="1" max="60" name="<?php echo esc_attr( self::OPTION ); ?>[timeout]" value="<?php echo esc_attr( (string) self::timeout() ); ?>"></td>
					</tr>
				</table>
				<?php submit_button(); ?>
			</form>
		</div>
		<?php
	}
}

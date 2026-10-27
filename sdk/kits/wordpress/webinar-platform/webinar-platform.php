<?php
/**
 * Plugin Name:       Webinar Platform
 * Description:       Webinar registration forms (block and [webinar_form] shortcode) with a first-party proxy that keeps the API key on the server.
 * Version:           0.1.0
 * Requires at least: 6.3
 * Requires PHP:      7.4
 * License:           Apache-2.0
 * License URI:       https://www.apache.org/licenses/LICENSE-2.0
 * Text Domain:       webinar-platform
 *
 * @package WebinarPlatform
 */

defined( 'ABSPATH' ) || exit;

define( 'WEBINAR_PLATFORM_VERSION', '0.1.0' );
define( 'WEBINAR_PLATFORM_DIR', plugin_dir_path( __FILE__ ) );

require_once WEBINAR_PLATFORM_DIR . 'includes/class-settings.php';
require_once WEBINAR_PLATFORM_DIR . 'includes/class-proxy.php';
require_once WEBINAR_PLATFORM_DIR . 'includes/class-form.php';

Webinar_Platform_Settings::register();
Webinar_Platform_Proxy::register();
Webinar_Platform_Form::register();

register_activation_hook(
	__FILE__,
	static function () {
		// The key option is never autoloaded, so it is not in every page's alloptions cache.
		add_option( Webinar_Platform_Settings::KEY_OPTION, '', '', false );
	}
);

add_filter(
	'plugin_action_links_' . plugin_basename( __FILE__ ),
	static function ( $links ) {
		$url = admin_url( 'options-general.php?page=' . Webinar_Platform_Settings::PAGE );
		array_unshift( $links, '<a href="' . esc_url( $url ) . '">' . esc_html__( 'Settings', 'webinar-platform' ) . '</a>' );
		return $links;
	}
);

<?php
/**
 * Removes the plugin's options (including the stored proxy key) on uninstall.
 *
 * @package WebinarPlatform
 */

defined( 'WP_UNINSTALL_PLUGIN' ) || exit;

delete_option( 'webinar_platform_settings' );
delete_option( 'webinar_platform_proxy_key' );

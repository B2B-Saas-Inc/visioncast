<?php
// Enables WP Super Cache page caching from the CLI (run inside the WordPress container as www-data).
$_SERVER['HTTP_HOST'] = 'localhost:8080';
require '/var/www/html/wp-load.php';
require_once WP_PLUGIN_DIR . '/wp-super-cache/wp-cache.php';
if ( function_exists( 'wp_cache_enable' ) ) {
	wp_cache_enable();
}
if ( function_exists( 'wp_cache_create_advanced_cache' ) ) {
	wp_cache_create_advanced_cache();
}
if ( function_exists( 'wp_cache_verify_cache_dir' ) ) {
	wp_cache_verify_cache_dir();
}
echo defined( 'WP_CACHE' ) && WP_CACHE ? "WP_CACHE on\n" : "WP_CACHE off\n";
echo file_exists( WP_CONTENT_DIR . '/advanced-cache.php' ) ? "advanced-cache.php present\n" : "advanced-cache.php missing\n";
echo preg_match( '/cache_enabled\s*=\s*true/', (string) file_get_contents( WP_CONTENT_DIR . '/wp-cache-config.php' ) ) ? "cache_enabled=true\n" : "cache_enabled=false\n";

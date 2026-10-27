<?php
/**
 * The registration form: a dynamic block (webinar-platform/webinar-form) and the
 * [webinar_form] shortcode render the same SDK markup. Every attribute is
 * validated against a fixed pattern or allowlist and escaped on output; field
 * names come from the SDK's canonical list only.
 *
 * Paid sessions need no extra code: the SDK handles 202 payment_required by
 * navigating to order.checkoutUrl on checkout.stripe.com (docs/tickets.md).
 *
 * @package WebinarPlatform
 */

defined( 'ABSPATH' ) || exit;

final class Webinar_Platform_Form {
	const HANDLE = 'webinar-platform-sdk';

	/** Canonical SDK field names (webinar-form.js CANONICAL_FIELDS) the kit can render. */
	const FIELDS = array(
		'email'     => array( 'label' => 'Email', 'type' => 'email', 'autocomplete' => 'email' ),
		'firstName' => array( 'label' => 'First name', 'type' => 'text', 'autocomplete' => 'given-name' ),
		'lastName'  => array( 'label' => 'Last name', 'type' => 'text', 'autocomplete' => 'family-name' ),
		'name'      => array( 'label' => 'Full name', 'type' => 'text', 'autocomplete' => 'name' ),
		'phone'     => array( 'label' => 'Phone', 'type' => 'tel', 'autocomplete' => 'tel' ),
		'company'   => array( 'label' => 'Company', 'type' => 'text', 'autocomplete' => 'organization' ),
		'jobTitle'  => array( 'label' => 'Job title', 'type' => 'text', 'autocomplete' => 'organization-title' ),
	);

	const DEFAULT_FIELDS = array( 'email', 'firstName', 'lastName', 'company' );

	public static function register() {
		add_action( 'init', array( __CLASS__, 'register_block' ) );
		add_shortcode( 'webinar_form', array( __CLASS__, 'shortcode' ) );
	}

	public static function register_block() {
		register_block_type(
			WEBINAR_PLATFORM_DIR . 'blocks/webinar-form',
			array( 'render_callback' => array( __CLASS__, 'render_block' ) )
		);
	}

	public static function render_block( $attributes ) {
		$html = self::render( is_array( $attributes ) ? $attributes : array() );
		return '' === $html ? '' : '<div ' . get_block_wrapper_attributes() . '>' . $html . '</div>';
	}

	/**
	 * [webinar_form occurrence="<uuid>" form_version="v1" fields="email,firstName,lastName"
	 *  required="email" submit_label="Register" confirm_label="Confirm my RSVP"]
	 */
	public static function shortcode( $atts ) {
		$atts = shortcode_atts(
			array(
				'occurrence'    => '',
				'form_version'  => '',
				'fields'        => implode( ',', self::DEFAULT_FIELDS ),
				'required'      => 'email',
				'submit_label'  => '',
				'confirm_label' => '',
			),
			$atts,
			'webinar_form'
		);
		return self::render(
			array(
				'occurrenceId' => $atts['occurrence'],
				'formVersion'  => $atts['form_version'],
				'fields'       => explode( ',', (string) $atts['fields'] ),
				'required'     => explode( ',', (string) $atts['required'] ),
				'submitLabel'  => $atts['submit_label'],
				'confirmLabel' => $atts['confirm_label'],
			)
		);
	}

	private static function list_attr( $attributes, $key, $fallback ) {
		$value = isset( $attributes[ $key ] ) && is_array( $attributes[ $key ] ) ? $attributes[ $key ] : $fallback;
		$out   = array();
		foreach ( $value as $item ) {
			$item = trim( (string) $item );
			if ( isset( self::FIELDS[ $item ] ) && ! in_array( $item, $out, true ) ) {
				$out[] = $item;
			}
		}
		return $out;
	}

	private static function label_attr( $attributes, $key, $fallback ) {
		$value = isset( $attributes[ $key ] ) ? sanitize_text_field( (string) $attributes[ $key ] ) : '';
		$value = mb_substr( $value, 0, 60 );
		return '' === $value ? $fallback : $value;
	}

	public static function render( $attributes ) {
		$occurrence_id = strtolower( trim( isset( $attributes['occurrenceId'] ) ? (string) $attributes['occurrenceId'] : '' ) );
		$form_version  = trim( isset( $attributes['formVersion'] ) ? (string) $attributes['formVersion'] : '' );
		if ( ! preg_match( Webinar_Platform_Proxy::UUID_RE, $occurrence_id ) || ! preg_match( Webinar_Platform_Proxy::QUERY_VALUE_RE, $form_version ) ) {
			// Editors see why nothing renders; visitors see nothing.
			return current_user_can( 'edit_posts' )
				? '<p class="webinar-platform-notice">' . esc_html__( 'Webinar form: set a valid occurrence ID (UUID) and form version.', 'webinar-platform' ) . '</p>'
				: '';
		}

		$fields = self::list_attr( $attributes, 'fields', self::DEFAULT_FIELDS );
		if ( ! in_array( 'email', $fields, true ) ) {
			array_unshift( $fields, 'email' ); // the backend identifies registrants by email
		}
		$required = self::list_attr( $attributes, 'required', array( 'email' ) );
		$required = array_unique( array_merge( array( 'email' ), $required ) );
		$submit   = self::label_attr( $attributes, 'submitLabel', __( 'Register', 'webinar-platform' ) );
		$confirm  = self::label_attr( $attributes, 'confirmLabel', '' );

		self::enqueue_sdk();

		$prefix = wp_unique_id( 'wpf-' );
		$html   = '<form data-webinar-form data-occurrence-id="' . esc_attr( $occurrence_id ) . '" data-form-version="' . esc_attr( $form_version ) . '"';
		if ( '' !== $confirm ) {
			$html .= ' data-confirm-label="' . esc_attr( $confirm ) . '"';
		}
		$html .= '>';
		foreach ( $fields as $name ) {
			$def   = self::FIELDS[ $name ];
			$id    = $prefix . '-' . $name;
			$html .= '<div data-webinar-field><label for="' . esc_attr( $id ) . '">' . esc_html( $def['label'] ) . '</label>';
			$html .= '<input id="' . esc_attr( $id ) . '" name="' . esc_attr( $name ) . '" type="' . esc_attr( $def['type'] ) . '" autocomplete="' . esc_attr( $def['autocomplete'] ) . '"';
			$html .= in_array( $name, $required, true ) ? ' required' : '';
			$html .= '></div>';
		}
		$html .= '<button type="submit">' . esc_html( $submit ) . '</button>';
		$html .= '<div data-webinar-status></div></form>';
		$html .= '<noscript>' . esc_html__( 'Registration needs JavaScript.', 'webinar-platform' ) . '</noscript>';
		return $html;
	}

	/** Loads the SDK once, in the footer, after a config that pins it to this site's proxy. */
	public static function enqueue_sdk() {
		if ( wp_script_is( self::HANDLE, 'enqueued' ) ) {
			return;
		}
		$src = Webinar_Platform_Settings::sdk_url();
		if ( '' === $src ) {
			return;
		}
		wp_register_script( self::HANDLE, $src, array(), null, array( 'in_footer' => true ) ); // phpcs:ignore WordPress.WP.EnqueuedResourceParameters -- version is the backend's
		/**
		 * Filters window.WebinarFormConfig (JSON-serialisable values only; see
		 * docs/browser-integration.md). Wire a consent provider from your CMP in a
		 * separate inline script that calls WebinarForm.setConsent().
		 */
		$config = apply_filters(
			'webinar_platform_sdk_config',
			array(
				'proxyBase' => rtrim( Webinar_Platform_Proxy::base_path(), '/' ),
				'consent'   => array( 'analytics' => false, 'advertising' => false ),
			)
		);
		$config['proxyBase'] = rtrim( Webinar_Platform_Proxy::base_path(), '/' ); // not overridable: first-party only
		wp_add_inline_script(
			self::HANDLE,
			'window.WebinarFormConfig = ' . wp_json_encode( $config, JSON_HEX_TAG | JSON_HEX_AMP | JSON_HEX_APOS | JSON_HEX_QUOT ) . ';',
			'before'
		);
		wp_enqueue_script( self::HANDLE );
	}
}

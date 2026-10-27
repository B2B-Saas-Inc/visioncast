/*
 * Editor UI for webinar-platform/webinar-form. Plain ES5 on the wp.* globals, no build step.
 * The block is dynamic (save returns null): PHP validates and renders every attribute.
 */
( function ( wp ) {
	'use strict';
	var el = wp.element.createElement;
	var __ = wp.i18n.__;
	var InspectorControls = wp.blockEditor.InspectorControls;
	var useBlockProps = wp.blockEditor.useBlockProps;
	var PanelBody = wp.components.PanelBody;
	var TextControl = wp.components.TextControl;
	var CheckboxControl = wp.components.CheckboxControl;
	var ServerSideRender = wp.serverSideRender;

	var FIELDS = [
		[ 'email', __( 'Email (always shown)', 'webinar-platform' ) ],
		[ 'firstName', __( 'First name', 'webinar-platform' ) ],
		[ 'lastName', __( 'Last name', 'webinar-platform' ) ],
		[ 'name', __( 'Full name', 'webinar-platform' ) ],
		[ 'phone', __( 'Phone', 'webinar-platform' ) ],
		[ 'company', __( 'Company', 'webinar-platform' ) ],
		[ 'jobTitle', __( 'Job title', 'webinar-platform' ) ]
	];

	function toggle( list, key, on ) {
		var next = ( list || [] ).filter( function ( k ) { return k !== key; } );
		if ( on ) next.push( key );
		return next;
	}

	wp.blocks.registerBlockType( 'webinar-platform/webinar-form', {
		edit: function ( props ) {
			var a = props.attributes;
			var set = props.setAttributes;
			var fieldControls = FIELDS.map( function ( f ) {
				var shown = f[ 0 ] === 'email' || ( a.fields || [] ).indexOf( f[ 0 ] ) !== -1;
				return el(
					'div',
					{ key: f[ 0 ] },
					el( CheckboxControl, {
						label: f[ 1 ],
						checked: shown,
						disabled: f[ 0 ] === 'email',
						onChange: function ( on ) { set( { fields: toggle( a.fields, f[ 0 ], on ) } ); }
					} ),
					shown && f[ 0 ] !== 'email'
						? el( CheckboxControl, {
							label: __( 'Required', 'webinar-platform' ),
							checked: ( a.required || [] ).indexOf( f[ 0 ] ) !== -1,
							onChange: function ( on ) { set( { required: toggle( a.required, f[ 0 ], on ) } ); }
						} )
						: null
				);
			} );
			return el(
				wp.element.Fragment,
				null,
				el(
					InspectorControls,
					null,
					el(
						PanelBody,
						{ title: __( 'Session', 'webinar-platform' ) },
						el( TextControl, {
							label: __( 'Occurrence ID (UUID)', 'webinar-platform' ),
							help: __( 'Studio → session → Embed on your site.', 'webinar-platform' ),
							value: a.occurrenceId,
							onChange: function ( v ) { set( { occurrenceId: String( v ).trim() } ); }
						} ),
						el( TextControl, {
							label: __( 'Form version', 'webinar-platform' ),
							value: a.formVersion,
							onChange: function ( v ) { set( { formVersion: String( v ).trim() } ); }
						} ),
						el( TextControl, {
							label: __( 'Submit label', 'webinar-platform' ),
							value: a.submitLabel,
							onChange: function ( v ) { set( { submitLabel: v } ); }
						} ),
						el( TextControl, {
							label: __( 'Returning-visitor label', 'webinar-platform' ),
							value: a.confirmLabel,
							onChange: function ( v ) { set( { confirmLabel: v } ); }
						} )
					),
					el( PanelBody, { title: __( 'Fields', 'webinar-platform' ), initialOpen: false }, fieldControls )
				),
				el( 'div', useBlockProps(), el( ServerSideRender, { block: 'webinar-platform/webinar-form', attributes: a } ) )
			);
		},
		save: function () {
			return null;
		}
	} );
} )( window.wp );

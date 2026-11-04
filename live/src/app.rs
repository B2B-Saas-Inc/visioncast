//! The HTTP surface: `/v1/*` for VisionCast (bearer secret), `/join/*` for browsers holding a
//! signed link, `/webhooks/livekit` for the LiveKit server (signed by LiveKit).
use axum::{
    body::Bytes,
    extract::{DefaultBodyLimit, Path, Query, Request, State},
    http::{header, HeaderMap, HeaderName, HeaderValue, StatusCode},
    middleware::{self, Next},
    response::{Html, IntoResponse, Response},
    routing::{get, post, put},
    Json, Router,
};
use chrono::{DateTime, Utc};
use livekit_api::access_token::TokenVerifier;
use livekit_api::webhooks::WebhookReceiver;
use livekit_protocol as proto;
use serde::Deserialize;
use serde_json::json;
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use subtle::ConstantTimeEq;
use tower_http::set_header::SetResponseHeaderLayer;

use crate::admin;
use crate::backend::Backend;
use crate::broadcast;
use crate::captions;
use crate::config::Config;
use crate::crew;
use crate::store::{Recording, Room, Store};
use crate::studio;
use crate::ticket::{self, Role};

/// How long before the start attendees and hosts can enter; hosts come early to rehearse.
const ATTENDEE_EARLY_SECONDS: i64 = 2 * 3600;
const HOST_EARLY_SECONDS: i64 = 48 * 3600;
/// How long after the scheduled end a room stays joinable: sessions overrun.
const LATE_SECONDS: i64 = 4 * 3600;
const MAX_NAME: usize = 80;
const MAX_TITLE: usize = 300;
/// Attendee identities can't start with this: it's how the crew is told apart.
pub const CREW_MARK: &str = "host";
pub const HOST_PREFIX: &str = "host-";

pub struct AppState {
    pub config: Config,
    pub store: Store,
    pub backend: Arc<dyn Backend>,
    pub webhooks: WebhookReceiver,
    /// One recording start per room at a time, without one slow room holding up the others.
    recording_locks: Mutex<HashMap<String, Arc<tokio::sync::Mutex<()>>>>,
    pub login_slots: Arc<tokio::sync::Semaphore>,
    pub account_lock: tokio::sync::Mutex<()>,
    pub hubs: std::sync::Mutex<std::collections::HashMap<String, Arc<studio::Hub>>>,
    pub http: reqwest::Client,
    pub login_failures: std::sync::Mutex<admin::Failures>,
}

impl AppState {
    pub fn new(config: Config, store: Store, backend: Arc<dyn Backend>) -> Arc<Self> {
        let webhooks = WebhookReceiver::new(TokenVerifier::with_api_key(&config.livekit_api_key, &config.livekit_api_secret));
        Arc::new(Self { config, store, backend, webhooks, recording_locks: Default::default(), login_slots: Arc::new(tokio::sync::Semaphore::new(4)), account_lock: tokio::sync::Mutex::new(()), hubs: Default::default(), http: reqwest::Client::new(), login_failures: Default::default() })
    }
}

pub struct ApiError(pub StatusCode, pub &'static str);

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.0, Json(json!({ "error": self.1 }))).into_response()
    }
}

impl From<rusqlite::Error> for ApiError {
    fn from(err: rusqlite::Error) -> Self {
        tracing::error!(error = %err, "store error");
        ApiError(StatusCode::INTERNAL_SERVER_ERROR, "internal_error")
    }
}

pub type ApiResult<T> = Result<T, ApiError>;

pub fn now() -> i64 {
    Utc::now().timestamp()
}

pub fn iso(ts: i64) -> String {
    DateTime::<Utc>::from_timestamp(ts, 0).unwrap_or_default().format("%Y-%m-%dT%H:%M:%SZ").to_string()
}

fn parse_time(value: &str) -> Option<i64> {
    DateTime::parse_from_rfc3339(value).ok().map(|d| d.timestamp())
}

/// Addresses a session can't have, because something else lives there.
const RESERVED: [&str; 11] = ["admin", "login", "logout", "join", "program", "assets", "brand", "captions", "healthz", "v1", "webhooks"];

fn valid_slug(slug: &str) -> bool {
    (1..=180).contains(&slug.len())
        && slug.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
        && !slug.starts_with('-')
        && !slug.ends_with('-')
        && !RESERVED.contains(&slug)
}

/// What VisionCast and the settings page both manage, behind their own kind of sign-in.
fn settings_routes() -> Router<Arc<AppState>> {
    Router::new()
        .route("/rooms/{room}/destinations", put(broadcast::put_destinations).get(broadcast::get_destinations))
        .route("/saved-destinations", put(broadcast::put_saved).get(broadcast::get_saved))
        .route("/brand", put(broadcast::put_brand).get(broadcast::get_brand))
        .route("/brand/logo", put(broadcast::put_logo).layer(DefaultBodyLimit::max(broadcast::MAX_LOGO_BYTES)))
        .route("/brand/logo/remove", post(broadcast::delete_logo))
        .route("/brand/font", put(broadcast::put_font).layer(DefaultBodyLimit::max(broadcast::MAX_FONT_BYTES)))
        .route("/brand/font/remove", post(broadcast::delete_font))
        .route("/rooms/{room}/crew", get(crew::list).post(crew::add))
        .route("/rooms/{room}/settings", get(studio::get_settings).patch(studio::patch_settings))
        .route("/rooms/{room}/engagement", get(studio::engagement))
        .route("/rooms/{room}/crew/{id}", axum::routing::delete(crew::remove))
        .route("/rooms/{room}/crew/{id}/new-link", post(crew::new_link))
        .route("/rooms/{room}/crew/{id}/remove", post(crew::remove))
        .route("/rooms/{room}/people/{identity}/forget", post(studio::forget_person_route))
}

pub fn router(state: Arc<AppState>) -> Router {
    let private = settings_routes()
        .route("/rooms/{room}", put(put_room).get(get_room))
        .route("/rooms/{room}/tickets", post(create_ticket))
        .route("/rooms/{room}/attendance", get(get_attendance))
        .route("/rooms/{room}/recordings", get(get_recordings))
        .route("/admin-link", post(admin::create_link))
        .route_layer(middleware::from_fn_with_state(state.clone(), require_service_secret));
    let settings = settings_routes()
        .route("/rooms", get(admin::list_rooms))
        .route("/rooms/{room}", get(admin::room_detail))
        .route("/me", get(admin::me))
        .route("/me/password", post(admin::change_password))
        .route("/accounts", get(admin::list_accounts).post(admin::add_account))
        .route("/accounts/{id}", axum::routing::delete(admin::delete_account))
        .route("/accounts/{id}/reset", post(admin::reset_account))
        .route("/activity", get(admin::activity))
        .route_layer(middleware::from_fn_with_state(state.clone(), admin::require_admin));

    let csp = {
        let ws = &state.config.livekit_public_url;
        let http = ws.replacen("wss://", "https://", 1).replacen("ws://", "http://", 1);
        format!(
            "default-src 'none'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; font-src 'self'; \
             connect-src 'self' {ws} {http}; worker-src 'self' blob:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
        )
    };
    let public = Router::new()
        .route("/join/{ticket}", get(join_page))
        .route("/{slug}", get(session_page))
        .route("/{slug}/lobby", get(session_page))
        .route("/{slug}/green-room", get(session_page))
        .route("/{slug}/public", get(session_public))
        // The bare domain goes to /admin, which sends anyone signed out to /login.
        .route("/", get(|| async { axum::response::Redirect::temporary("/admin") }))
        .route("/login", get(admin::login_page).post(admin::login))
        .route("/logout", post(admin::logout))
        .route("/login/link/{token}", get(admin::use_link))
        .route("/login/set/{token}", get(admin::set_password_page).post(admin::set_password))
        .route("/admin", get(admin::admin_page))
        .route("/assets/admin.js", get(|| asset("text/javascript; charset=utf-8", include_str!("../assets/admin.js"))))
        .route("/join/{ticket}/captions/{file}", get(captions::download))
        .route("/join/{ticket}/info", get(join_info))
        .route("/join/{ticket}/enter", post(studio::enter))
        .route("/join/{ticket}/events", get(studio::events))
        .route("/join/{ticket}/act", post(studio::act))
        .route("/join/{ticket}/share-token", post(studio::share_token))
        .route("/join/{ticket}/files", put(studio::upload).layer(DefaultBodyLimit::disable()))
        .route("/join/{ticket}/files/{id}", get(studio::download))
        .route("/join/{ticket}/files/{id}/pages/{page}", get(studio::deck_page))
        .route("/join/{ticket}/local/{id}/{seq}", put(studio::local_chunk).layer(DefaultBodyLimit::disable()))
        .route("/join/{ticket}/local/{id}", get(studio::local_download))
        .route("/program/{ticket}", get(program_page))
        .route("/brand/logo", get(broadcast::get_logo))
        .route("/brand/icon", get(broadcast::get_icon))
        .route("/brand/font", get(broadcast::get_font))
        .route("/assets/stage.js", get(|| asset("text/javascript; charset=utf-8", include_str!("../assets/stage.js"))))
        .route("/assets/room.js", get(|| asset("text/javascript; charset=utf-8", include_str!("../assets/room.js"))))
        .route("/assets/room.css", get(|| asset("text/css; charset=utf-8", include_str!("../assets/room.css"))))
        .route("/assets/mark.svg", get(|| asset("image/svg+xml", include_str!("../assets/mark.svg"))))
        .route("/assets/vbg/vbg.js", get(|| asset("text/javascript; charset=utf-8", include_str!("../assets/vendor/vbg/vbg.js"))))
        .route("/assets/vbg/vision_wasm_internal.js", get(|| asset("text/javascript; charset=utf-8", include_str!("../assets/vendor/vbg/vision_wasm_internal.js"))))
        .route("/assets/vbg/vision_wasm_internal.wasm", get(|| binary_asset("application/wasm", include_bytes!("../assets/vendor/vbg/vision_wasm_internal.wasm"))))
        .route("/assets/vbg/selfie_segmenter.tflite", get(|| binary_asset("application/octet-stream", include_bytes!("../assets/vendor/vbg/selfie_segmenter.tflite"))))
        .route("/assets/livekit-client.js", get(|| asset("text/javascript; charset=utf-8", include_str!("../assets/vendor/livekit-client.umd.js"))))
        .layer(SetResponseHeaderLayer::if_not_present(
            header::CONTENT_SECURITY_POLICY,
            HeaderValue::from_str(&csp).expect("the CSP is valid header text"),
        ))
        .layer(SetResponseHeaderLayer::overriding(header::REFERRER_POLICY, HeaderValue::from_static("no-referrer")))
        .layer(SetResponseHeaderLayer::overriding(HeaderName::from_static("x-robots-tag"), HeaderValue::from_static("noindex")));

    Router::new()
        .nest("/v1", private)
        .nest("/admin/api", settings)
        .merge(public)
        .route("/captions/in/{ticket}", get(captions::ingest))
        .route("/webhooks/livekit", post(livekit_webhook))
        .route("/healthz", get(|| async { Json(json!({ "status": "ok", "version": env!("CARGO_PKG_VERSION") })) }))
        .layer(SetResponseHeaderLayer::overriding(header::X_CONTENT_TYPE_OPTIONS, HeaderValue::from_static("nosniff")))
        .layer(DefaultBodyLimit::max(256 * 1024))
        .with_state(state)
}

async fn asset(content_type: &'static str, body: &'static str) -> impl IntoResponse {
    ([(header::CONTENT_TYPE, content_type), (header::CACHE_CONTROL, "public, max-age=3600")], body)
}

async fn binary_asset(content_type: &'static str, body: &'static [u8]) -> impl IntoResponse {
    ([(header::CONTENT_TYPE, content_type), (header::CACHE_CONTROL, "public, max-age=86400")], body)
}

async fn require_service_secret(State(state): State<Arc<AppState>>, headers: HeaderMap, request: Request, next: Next) -> Response {
    let given = headers.get(header::AUTHORIZATION).and_then(|v| v.to_str().ok()).and_then(|v| v.strip_prefix("Bearer ")).unwrap_or("");
    if bool::from(given.as_bytes().ct_eq(state.config.service_secret.as_bytes())) {
        next.run(request).await
    } else {
        ApiError(StatusCode::UNAUTHORIZED, "unauthorized").into_response()
    }
}

// ---- /v1: VisionCast

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct PutRoom {
    title: String,
    starts_at: String,
    ends_at: String,
    #[serde(default)]
    auto_record: bool,
    /// The address, `/{slug}`. Defaults to the room's ID.
    #[serde(default)]
    slug: String,
    #[serde(default)]
    entry_url: String,
}

/// Creates or reschedules a room. Repeating the same request is safe.
async fn put_room(State(state): State<Arc<AppState>>, Path(room): Path<String>, Json(body): Json<PutRoom>) -> ApiResult<Json<serde_json::Value>> {
    if !ticket::valid_id(&room) {
        return Err(ApiError(StatusCode::BAD_REQUEST, "invalid_room_id"));
    }
    let (Some(starts_at), Some(ends_at)) = (parse_time(&body.starts_at), parse_time(&body.ends_at)) else {
        return Err(ApiError(StatusCode::BAD_REQUEST, "invalid_time"));
    };
    if ends_at <= starts_at {
        return Err(ApiError(StatusCode::BAD_REQUEST, "invalid_time"));
    }
    let title: String = body.title.chars().take(MAX_TITLE).collect();
    let previous = state.store.room(&room)?;
    let slug = if body.slug.is_empty() { previous.as_ref().map_or_else(|| room.to_lowercase().replace('_', "-"), |p| p.slug.clone()) } else { body.slug };
    if !valid_slug(&slug) {
        return Err(ApiError(StatusCode::BAD_REQUEST, "invalid_slug"));
    }
    if state.store.room_by_slug(&slug)?.is_some_and(|other| other.id != room) {
        return Err(ApiError(StatusCode::CONFLICT, "slug_taken"));
    }
    if !body.entry_url.is_empty() && !(body.entry_url.starts_with("https://") || body.entry_url.starts_with("http://")) {
        return Err(ApiError(StatusCode::BAD_REQUEST, "invalid_entry_url"));
    }
    state.store.upsert_room(&Room { id: room.clone(), title, starts_at, ends_at, auto_record: body.auto_record, slug: slug.clone(), entry_url: body.entry_url }, now())?;
    // A rescheduled session is a new broadcast: it starts again from "not live".
    // Not while it's on the air: that would leave its broadcasts and stage running behind a "not live".
    if previous.is_some_and(|p| p.starts_at != starts_at) {
        state.hub(&room)?.reset_phase_after_end(&state)?;
    }
    Ok(Json(json!({ "id": room, "slug": slug })))
}

async fn get_room(State(state): State<Arc<AppState>>, Path(room): Path<String>) -> ApiResult<Json<serde_json::Value>> {
    let room = state.store.room(&room)?.ok_or(ApiError(StatusCode::NOT_FOUND, "not_found"))?;
    Ok(Json(json!({ "id": room.id, "slug": room.slug, "startsAt": iso(room.starts_at), "endsAt": iso(room.ends_at), "autoRecord": room.auto_record })))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct CreateTicket {
    identity: String,
}

/// One attendee's personal link. The link is derived from the identity, so repeating the
/// request returns the same link rather than creating anything.
async fn create_ticket(State(state): State<Arc<AppState>>, Path(room): Path<String>, Json(body): Json<CreateTicket>) -> ApiResult<Json<serde_json::Value>> {
    if !ticket::valid_id(&body.identity) || body.identity.starts_with(CREW_MARK) {
        return Err(ApiError(StatusCode::BAD_REQUEST, "invalid_identity"));
    }
    state.store.room(&room)?.ok_or(ApiError(StatusCode::NOT_FOUND, "not_found"))?;
    let signed = ticket::sign(&state.config.ticket_key, Role::Attendee, &room, &body.identity);
    Ok(Json(json!({ "id": body.identity, "joinUrl": format!("{}/join/{signed}", state.config.public_url) })))
}

#[derive(Deserialize)]
struct AttendancePage {
    #[serde(default)]
    after: String,
}

async fn get_attendance(
    State(state): State<Arc<AppState>>,
    Path(room): Path<String>,
    Query(page): Query<AttendancePage>,
) -> ApiResult<Json<serde_json::Value>> {
    state.store.room(&room)?.ok_or(ApiError(StatusCode::NOT_FOUND, "not_found"))?;
    let (rows, complete, more) = state.store.attendance(&room, &page.after)?;
    let next = rows.last().filter(|_| more).map(|r| r.external_id.clone());
    let rows: Vec<_> = rows
        .iter()
        .map(|r| json!({
            "externalId": r.external_id,
            "joinedAt": iso(r.joined_at),
            "leftAt": r.left_at.map(iso),
            "durationSeconds": r.duration_seconds,
        }))
        .collect();
    Ok(Json(json!({ "rows": rows, "complete": complete, "next": next })))
}

async fn get_recordings(State(state): State<Arc<AppState>>, Path(room): Path<String>) -> ApiResult<Json<serde_json::Value>> {
    state.store.room(&room)?.ok_or(ApiError(StatusCode::NOT_FOUND, "not_found"))?;
    Ok(Json(json!({ "recordings": recordings_json(&state, &room)? })))
}

pub fn recordings_json(state: &AppState, room: &str) -> ApiResult<serde_json::Value> {
    let base = state.config.recording.as_ref().and_then(|r| r.public_base_url.clone());
    let rows: Vec<_> = state
        .store
        .recordings(room)?
        .into_iter()
        .map(|recording| {
            let public_url = match (&base, recording.status.as_str(), recording.filename.is_empty()) {
                (Some(base), "complete", false) => Some(format!("{base}/{}", recording.filename)),
                _ => None,
            };
            json!({
                "egressId": recording.egress_id, "status": recording.status, "startedAt": recording.started_at, "endedAt": recording.ended_at,
                "durationSeconds": recording.duration_seconds, "sizeBytes": recording.size_bytes, "filename": recording.filename,
                "location": recording.location, "error": recording.error, "publicUrl": public_url,
                "captions": captions::recording_files(state, room, &recording.egress_id),
            })
        })
        .collect();
    Ok(json!(rows))
}

// ---- /join: a signed link, and the clean address it leads to

pub fn check_ticket(state: &AppState, value: &str) -> ApiResult<(ticket::Ticket, Room)> {
    let not_found = ApiError(StatusCode::NOT_FOUND, "not_found");
    let parsed = ticket::verify(&state.config.ticket_key, value).ok_or(ApiError(StatusCode::NOT_FOUND, "not_found"))?;
    let room = state.store.room(&parsed.room)?.ok_or(not_found)?;
    // A crew link is only as good as the person it belongs to: taken away or replaced, it's dead.
    if matches!(parsed.role, Role::Host | Role::Producer | Role::Guest) && crew::member_for(state, parsed.role, &room.id, &parsed.identity)?.is_none() {
        return Err(ApiError(StatusCode::NOT_FOUND, "not_found"));
    }
    Ok((parsed, room))
}

async fn join_page(State(state): State<Arc<AppState>>, Path(value): Path<String>) -> Response {
    let Ok((parsed, room)) = check_ticket(&state, &value) else { return not_found_page() };
    // The link goes in the fragment: browsers never send that to a server or in a referrer, and
    // the page takes it out of the address bar straight away.
    let promoted = state.store.person_flags(&room.id, &parsed.identity).map(|f| f.0).unwrap_or(false);
    let place = if studio::effective_role(parsed.role, promoted) == studio::Crew::Attendee { "lobby" } else { "green-room" };
    let mut response = StatusCode::SEE_OTHER.into_response();
    if let Ok(location) = HeaderValue::from_str(&format!("/{}/{place}#t={value}", room.slug)) {
        response.headers_mut().insert(header::LOCATION, location);
    }
    response.headers_mut().insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    response
}

fn not_found_page() -> Response {
    (StatusCode::NOT_FOUND, [(header::CACHE_CONTROL, "no-store")], Html(include_str!("../assets/not-found.html"))).into_response()
}

/// `/{slug}`, `/{slug}/lobby` and `/{slug}/green-room`: one page, which finds out who you are from
/// the link you came in on.
async fn session_page(State(state): State<Arc<AppState>>, Path(slug): Path<String>) -> Response {
    match state.store.room_by_slug(&slug) {
        Ok(Some(_)) => ([(header::CACHE_CONTROL, "no-store")], Html(include_str!("../assets/room.html"))).into_response(),
        _ => not_found_page(),
    }
}

/// What anyone can know about a session: enough to tell a visitor without a link where to go.
async fn session_public(State(state): State<Arc<AppState>>, Path(slug): Path<String>) -> ApiResult<Response> {
    let room = state.store.room_by_slug(&slug)?.ok_or(ApiError(StatusCode::NOT_FOUND, "not_found"))?;
    let body = json!({ "title": room.title, "startsAt": iso(room.starts_at), "entryUrl": room.entry_url });
    Ok(([(header::CACHE_CONTROL, "no-store")], Json(body)).into_response())
}

#[derive(Debug, PartialEq, Eq)]
pub enum Window {
    Open,
    NotYet,
    Over,
}

pub fn room_window(role: Role, room: &Room, at: i64) -> Window {
    let early = if role == Role::Attendee { ATTENDEE_EARLY_SECONDS } else { HOST_EARLY_SECONDS };
    if at < room.starts_at - early {
        Window::NotYet
    } else if at > room.ends_at + LATE_SECONDS {
        Window::Over
    } else {
        Window::Open
    }
}

async fn join_info(State(state): State<Arc<AppState>>, Path(value): Path<String>) -> ApiResult<Response> {
    let (parsed, room) = check_ticket(&state, &value)?;
    let window = match room_window(parsed.role, &room, now()) {
        Window::Open => "open",
        Window::NotYet => "not_yet",
        Window::Over => "over",
    };
    let promoted = state.store.person_flags(&room.id, &parsed.identity)?.0;
    let member = crew::member_for(&state, parsed.role, &room.id, &parsed.identity)?;
    let body = json!({
        "you": member.as_ref().map(|m| json!({ "name": m.name, "title": m.title })),
        "title": room.title,
        "slug": room.slug,
        "role": studio::effective_role(parsed.role, promoted).name(),
        "startsAt": iso(room.starts_at),
        "endsAt": iso(room.ends_at),
        "window": window,
        "canRecord": state.config.recording.is_some(),
    });
    Ok(([(header::CACHE_CONTROL, "no-store")], Json(body)).into_response())
}

/// The recorder's page: the audience's view, from a link only this service hands out. It reaches
/// LiveKit at whatever address the recorder gives it, so its CSP is wider than the public page's.
async fn program_page(State(state): State<Arc<AppState>>, Path(value): Path<String>) -> Response {
    let ok = matches!(check_ticket(&state, &value), Ok((t, _)) if t.role == Role::Recorder);
    if !ok {
        return (StatusCode::NOT_FOUND, [(header::CACHE_CONTROL, "no-store")], Html(include_str!("../assets/not-found.html"))).into_response();
    }
    let csp = "default-src 'none'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; font-src 'self'; \
               connect-src 'self' ws: wss: http: https:; worker-src 'self' blob:; base-uri 'none'; form-action 'none'";
    ([(header::CACHE_CONTROL, "no-store"), (header::CONTENT_SECURITY_POLICY, csp)], Html(include_str!("../assets/room.html"))).into_response()
}

pub fn clean_name(name: &str, fallback: &str) -> String {
    let cleaned: String = name.chars().filter(|c| !c.is_control()).take(MAX_NAME).collect();
    let cleaned = cleaned.trim();
    if cleaned.is_empty() { fallback.to_string() } else { cleaned.to_string() }
}

/// Starts the room's recording unless one is running.
pub async fn start_recording(state: &AppState, room: &str) -> ApiResult<()> {
    let lock = state.recording_locks.lock().unwrap_or_else(|e| e.into_inner()).entry(room.to_string()).or_default().clone();
    let _guard = lock.lock().await;
    if state.store.active_recording(room)?.is_some() {
        return Ok(());
    }
    // Make sure the room exists: LiveKit refuses to record one that doesn't.
    state.backend.ensure_room(room).await.map_err(|e| {
        tracing::warn!(error = %e.0, "could not prepare the room");
        ApiError(StatusCode::BAD_GATEWAY, "room_unavailable")
    })?;
    let filepath = format!("recordings/{room}/{}.mp4", Utc::now().format("%Y%m%d-%H%M%S"));
    let signed = ticket::sign(&state.config.ticket_key, Role::Recorder, room, "program");
    let page = format!("{}/program/{signed}", state.config.internal_url);
    let egress_id = state.backend.start_recording(room, &filepath, &page).await.map_err(|e| {
        tracing::warn!(error = %e.0, "could not start recording");
        ApiError(StatusCode::BAD_GATEWAY, "recording_failed")
    })?;
    let row = Recording { egress_id, status: "starting".into(), started_at: now(), filename: filepath, ..Default::default() };
    state.store.upsert_recording(room, &row)?;
    Ok(())
}

pub async fn stop_recording(state: &AppState, room: &str) -> ApiResult<()> {
    if let Some(egress_id) = state.store.active_recording(room)? {
        // If the recorder has already gone, the answer to a stop is an error. That shouldn't leave the
        // session stuck thinking it's still recording, with no way to start another.
        let status = match state.backend.stop_egress(&egress_id).await {
            Ok(()) => "ending",
            Err(e) => {
                tracing::warn!(error = %e.0, "could not stop recording");
                "failed"
            }
        };
        let row = Recording { egress_id, status: status.into(), ..Default::default() };
        state.store.upsert_recording(room, &row)?;
    }
    Ok(())
}

// ---- /webhooks/livekit: who joined, left and what was recorded

/// Answers 200 only after the event is stored, so LiveKit retries if storage failed.
async fn livekit_webhook(State(state): State<Arc<AppState>>, headers: HeaderMap, body: Bytes) -> ApiResult<StatusCode> {
    let auth = headers.get(header::AUTHORIZATION).and_then(|v| v.to_str().ok()).unwrap_or("");
    let text = std::str::from_utf8(&body).map_err(|_| ApiError(StatusCode::BAD_REQUEST, "invalid_body"))?;
    let event = state.webhooks.receive(text, auth).map_err(|err| {
        tracing::warn!(error = %err, "rejected a webhook");
        ApiError(StatusCode::UNAUTHORIZED, "invalid_signature")
    })?;
    // A broadcast isn't a recording: it has no file and its own status per destination.
    if let Some(info) = event.egress_info.as_ref().filter(|i| event.event.starts_with("egress_") && broadcast::is_broadcast(&state, i)) {
        if event.id.is_empty() || state.store.first_delivery(&event.id, now())? {
            broadcast::on_egress_update(&state, info).await?;
        }
        return Ok(StatusCode::OK);
    }
    apply_event(&state.store, &event, now())?;
    if event.event.starts_with("egress_") {
        if let Some(info) = event.egress_info.as_ref().filter(|i| !i.room_name.is_empty()) {
            if state.store.room(&info.room_name)?.is_some() && !is_caption_stream(info) {
                state.hub(&info.room_name)?.sync_recording(&state)?;
                if event.event == "egress_ended" && info.status == proto::EgressStatus::EgressComplete as i32 {
                    if let Some(file) = info.file_results.first() {
                        // The file starts when the recorder's page is ready, a few seconds after the
                        // recording did. Captions are timed from the file's first frame.
                        let (mut start_ms, end_ms) = (info.started_at / 1_000_000, info.ended_at / 1_000_000);
                        if file.duration > 0 {
                            start_ms = start_ms.max(end_ms - file.duration / 1_000_000);
                        }
                        captions::after_recording(state.clone(), info.room_name.clone(), info.egress_id.clone(), file.filename.clone(), start_ms, end_ms);
                    }
                }
            }
        }
    }
    // A credential can outlive someone's right to use it, so check everyone who turns up.
    studio::enforce_webhook(&state, &event).await;
    // An encoder's stream arrived or stopped: it's on stage for exactly that long.
    if matches!(event.event.as_str(), "participant_joined" | "participant_left" | "participant_connection_aborted") {
        if let (Some(p), Some(room)) = (event.participant.as_ref().filter(|p| p.identity.starts_with("host-i-")), event.room.as_ref().map(|r| r.name.clone())) {
            if state.store.room(&room)?.is_some() {
                studio::encoder_presence(&state, &room, &p.identity, &p.name, event.event == "participant_joined")?;
            }
        }
    }
    // Someone started or stopped their microphone: captions follow.
    if matches!(event.event.as_str(), "track_published" | "track_unpublished") {
        if let Some(room) = event.room.as_ref().map(|r| r.name.clone()) {
            if state.store.room(&room)?.is_some() {
                let app = state.clone();
                tokio::spawn(async move { captions::sync(&app, &room).await });
            }
        }
    }
    Ok(StatusCode::OK)
}

fn recording_status(status: i32) -> &'static str {
    match proto::EgressStatus::try_from(status) {
        Ok(proto::EgressStatus::EgressStarting) => "starting",
        Ok(proto::EgressStatus::EgressActive) => "active",
        Ok(proto::EgressStatus::EgressEnding) => "ending",
        Ok(proto::EgressStatus::EgressComplete) => "complete",
        Ok(proto::EgressStatus::EgressFailed) => "failed",
        Ok(proto::EgressStatus::EgressAborted) => "aborted",
        Ok(proto::EgressStatus::EgressLimitReached) => "limit_reached",
        Err(_) => "unknown",
    }
}

const NANOS: i64 = 1_000_000_000;

/// The recorder sending one microphone to captions: not a recording of anything.
fn is_caption_stream(info: &proto::EgressInfo) -> bool {
    matches!(info.request, Some(proto::egress_info::Request::Track(_)))
}

/// Applies one verified event. Events for rooms VisionCast didn't schedule are ignored.
pub fn apply_event(store: &Store, event: &proto::WebhookEvent, received_at: i64) -> rusqlite::Result<()> {
    if !event.id.is_empty() && !store.first_delivery(&event.id, received_at)? {
        return Ok(());
    }
    let at = if event.created_at > 0 { event.created_at } else { received_at };
    let room_name = event
        .room
        .as_ref()
        .map(|r| r.name.as_str())
        .or_else(|| event.egress_info.as_ref().map(|e| e.room_name.as_str()))
        .unwrap_or("");
    if room_name.is_empty() || store.room(room_name)?.is_none() {
        return Ok(());
    }
    match event.event.as_str() {
        "participant_joined" | "participant_left" | "participant_connection_aborted" => {
            let Some(p) = event.participant.as_ref() else { return Ok(()) };
            // Attendance counts the audience: hosts and LiveKit's recorder are not attendees.
            // An encoder on an RTMP input is on stage from the moment its stream arrives.
            if p.identity.starts_with("host-i-") {
                return Ok(());
            }
            let standard = proto::participant_info::Kind::try_from(p.kind) == Ok(proto::participant_info::Kind::Standard);
            if !standard || p.identity.starts_with(HOST_PREFIX) || p.identity.is_empty() || p.sid.is_empty() {
                return Ok(());
            }
            let joined = if p.joined_at > 0 { p.joined_at } else { at };
            if event.event == "participant_joined" {
                store.participant_joined(room_name, &p.identity, &p.sid, joined)?;
            } else {
                store.participant_left(room_name, &p.identity, &p.sid, joined, at)?;
            }
        }
        "room_finished" => store.close_open_visits(room_name, at)?,
        "egress_started" | "egress_updated" | "egress_ended" => {
            let Some(info) = event.egress_info.as_ref() else { return Ok(()) };
            if is_caption_stream(info) {
                return Ok(());
            }
            let file = info.file_results.first();
            let row = Recording {
                egress_id: info.egress_id.clone(),
                status: recording_status(info.status).to_string(),
                started_at: info.started_at / NANOS,
                ended_at: info.ended_at / NANOS,
                duration_seconds: file.map_or(0, |f| f.duration / NANOS),
                size_bytes: file.map_or(0, |f| f.size),
                filename: file.map_or(String::new(), |f| f.filename.clone()),
                location: file.map_or(String::new(), |f| f.location.clone()),
                error: info.error.clone(),
            };
            store.upsert_recording(room_name, &row)?;
        }
        _ => {}
    }
    Ok(())
}

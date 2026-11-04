//! Sending the stage to other platforms, and the brand a room is drawn in.
//!
//! Stream keys are set on `/admin`, and this is the only place that keeps them, because this
//! service has to start the broadcast the moment someone goes live. A stream key never leaves:
//! no API, event stream or log carries one, and they're encrypted at rest. Other pages only ever
//! see a platform's name, its server and how it's doing.
use axum::{
    body::Bytes,
    extract::{Path, State},
    http::{header, StatusCode},
    response::{IntoResponse, Response},
    Json,
};
use base64::{engine::general_purpose::STANDARD, Engine};
use chacha20poly1305::{aead::Aead, ChaCha20Poly1305, KeyInit, Nonce};
use livekit_protocol as proto;
use rand::Rng;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::net::IpAddr;
use std::sync::Arc;

use crate::app::{ApiError, ApiResult, AppState};
use crate::studio::{valid_theme, Audience, Theme};
use crate::ticket::{self, Role};

pub const MAX_LOGO_BYTES: usize = 2 * 1024 * 1024;
const PNG_MAGIC: [u8; 8] = [0x89, b'P', b'N', b'G', 0x0d, 0x0a, 0x1a, 0x0a];

/// One platform for one session. The key is sealed; a session without its own uses the saved one.
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(default)]
struct Destination {
    platform: String,
    server: String,
    sealed: String,
    /// The address as LiveKit reports it, with the key masked to its first and last few
    /// characters. For matching LiveKit's reports back to a platform.
    masked: String,
    /// Goes out when the session goes live.
    armed: bool,
    /// off, starting, live, failed.
    status: String,
    error: String,
}

/// A server and key kept for every session. LinkedIn isn't here: it issues one per stream.
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(default)]
struct Saved {
    server: String,
    sealed: String,
}

struct Platform {
    id: &'static str,
    label: &'static str,
    server: &'static str,
    reusable: bool,
    help: &'static str,
}

const CATALOGUE: [Platform; 4] = [
    Platform { id: "youtube", label: "YouTube", server: "rtmps://a.rtmps.youtube.com/live2", reusable: true,
        help: "From YouTube Studio's Go live page, under Stream settings. YouTube starts by itself only if auto-start is on there." },
    Platform { id: "meta", label: "Meta", server: "rtmps://live-api-s.facebook.com:443/rtmp", reusable: true,
        help: "From Live Producer, under Streaming software. You press Go live there once the stream shows." },
    Platform { id: "x", label: "X", server: "", reusable: true,
        help: "From Media Studio's Producer, under Sources: create an RTMP source near your server. You press Go live there. X may require a paid plan." },
    Platform { id: "linkedin", label: "LinkedIn", server: "", reusable: false,
        help: "From Live Studio: Prepare to go live, then Get URL. LinkedIn only gives these out one or two hours before a scheduled stream, for that stream. You press Go live there. Your Page or Profile needs LinkedIn Live access." },
];

fn saved(app: &AppState, platform: &str) -> Saved {
    app.store.setting(&format!("saved-destination:{platform}")).ok().flatten().and_then(|j| serde_json::from_str(&j).ok()).unwrap_or_default()
}

fn default_server(platform: &str) -> &'static str {
    CATALOGUE.iter().find(|p| p.id == platform).map_or("", |p| p.server)
}

/// The address to send to: server and key, from the session or else what's saved.
fn full_url(app: &AppState, d: &Destination) -> Option<String> {
    let kept = saved(app, &d.platform);
    let server = [d.server.as_str(), kept.server.as_str(), default_server(&d.platform)].into_iter().find(|s| !s.is_empty())?.to_string();
    let sealed = if d.sealed.is_empty() { &kept.sealed } else { &d.sealed };
    let key = unseal(app, sealed)?;
    Some(format!("{}/{key}", server.trim_end_matches('/')))
}

fn has_key(app: &AppState, d: &Destination) -> bool {
    !d.sealed.is_empty() || !saved(app, &d.platform).sealed.is_empty()
}

// ---- stream keys at rest

fn cipher(app: &AppState) -> ChaCha20Poly1305 {
    let key = Sha256::digest(format!("visioncast-live-stream-keys|{}", app.config.ticket_key).as_bytes());
    ChaCha20Poly1305::new_from_slice(&key).expect("a SHA-256 digest is a valid key")
}

fn seal(app: &AppState, url: &str) -> String {
    let nonce: [u8; 12] = rand::rng().random();
    let body = cipher(app).encrypt(Nonce::from_slice(&nonce), url.as_bytes()).expect("encrypting in memory cannot fail");
    STANDARD.encode([nonce.as_slice(), body.as_slice()].concat())
}

fn unseal(app: &AppState, sealed: &str) -> Option<String> {
    let raw = STANDARD.decode(sealed).ok()?;
    if raw.len() < 12 {
        return None;
    }
    let plain = cipher(app).decrypt(Nonce::from_slice(&raw[..12]), &raw[12..]).ok()?;
    String::from_utf8(plain).ok()
}

// ---- which addresses are allowed

fn host_of(url: &str) -> Option<String> {
    let rest = url.strip_prefix("rtmps://").or_else(|| url.strip_prefix("rtmp://"))?;
    let authority = rest.split('/').next()?;
    if authority.is_empty() || authority.contains('@') {
        return None;
    }
    let host = if authority.starts_with('[') { authority.split(']').next()?.trim_start_matches('[') } else { authority.split(':').next()? };
    (!host.is_empty()).then(|| host.to_ascii_lowercase())
}

/// Up to the last path segment, then that segment's ends: enough to tell destinations apart
/// without keeping a key in the clear.
fn masked(url: &str) -> String {
    let (prefix, key) = url.rsplit_once('/').unwrap_or((url, ""));
    let chars: Vec<char> = key.chars().collect();
    let start: String = chars.iter().take(3).collect();
    let end: String = chars.iter().rev().take(3).collect::<Vec<_>>().into_iter().rev().collect();
    format!("{prefix}/{start}|{end}")
}

fn same_destination(reported: &str, ours: &str) -> bool {
    let Some((prefix, ends)) = ours.rsplit_once('/') else { return false };
    let Some((start, end)) = ends.split_once('|') else { return false };
    let Some((report_prefix, report_key)) = reported.rsplit_once('/') else { return false };
    if report_prefix != prefix {
        return false;
    }
    match report_key.strip_prefix('{').and_then(|k| k.strip_suffix('}')).and_then(|k| k.split_once("...")) {
        Some((a, b)) => start.starts_with(a) && end.ends_with(b),
        // A short key isn't masked at all.
        None => report_key.starts_with(start) && report_key.ends_with(end),
    }
}

fn valid_url(url: &str) -> bool {
    url.len() <= 2000 && host_of(url).is_some() && !url.chars().any(|c| c.is_whitespace() || c.is_control())
}

fn is_private(ip: IpAddr) -> bool {
    // An IPv4 address written as IPv6 is still that IPv4 address.
    if let IpAddr::V6(v) = ip {
        if let Some(mapped) = v.to_ipv4_mapped() {
            return is_private(IpAddr::V4(mapped));
        }
    }
    match ip {
        IpAddr::V4(v) => v.is_private() || v.is_loopback() || v.is_link_local() || v.is_unspecified() || v.is_broadcast() || v.is_documentation() || v.octets()[0] == 100 && (64..128).contains(&v.octets()[1]),
        IpAddr::V6(v) => v.is_loopback() || v.is_unspecified() || (v.segments()[0] & 0xfe00) == 0xfc00 || (v.segments()[0] & 0xffc0) == 0xfe80,
    }
}

/// The recorder sits inside the server's network, so a typed address must not reach it.
async fn reachable(app: &AppState, url: &str) -> bool {
    if app.config.rtmp_allow_private {
        return true;
    }
    let Some(host) = host_of(url) else { return false };
    if let Ok(ip) = host.parse::<IpAddr>() {
        return !is_private(ip);
    }
    let found = tokio::net::lookup_host((host.as_str(), 443)).await;
    let Ok(addresses) = found else { return false };
    let all: Vec<_> = addresses.collect();
    !all.is_empty() && all.iter().all(|a| !is_private(a.ip()))
}

// ---- the list

fn destinations(app: &AppState, room: &str) -> ApiResult<Vec<Destination>> {
    Ok(app.store.docs(room, "destination", 20)?.iter().filter_map(|j| serde_json::from_str(j).ok()).collect())
}

fn save(app: &AppState, room: &str, d: &Destination) -> ApiResult<()> {
    app.store.put_doc(room, "destination", &d.platform, &serde_json::to_string(d).unwrap_or_default())?;
    Ok(())
}

/// What the crew sees in the room: platforms that are ready. Never a server or key.
pub fn status_json(app: &AppState, room: &str) -> ApiResult<Value> {
    let rows: Vec<Value> = destinations(app, room)?
        .iter()
        .filter(|d| has_key(app, d))
        .map(|d| json!({ "platform": d.platform, "armed": d.armed, "status": if d.status.is_empty() { "off" } else { &d.status }, "error": d.error }))
        .collect();
    Ok(json!(rows))
}

/// What the settings page shows for a session: every platform, and whether a key is set.
pub fn admin_rows(app: &AppState, room: &str) -> ApiResult<Value> {
    let rows = destinations(app, room)?;
    let out: Vec<Value> = CATALOGUE
        .iter()
        .map(|p| {
            let d = rows.iter().find(|d| d.platform == p.id).cloned().unwrap_or_else(|| Destination { platform: p.id.to_string(), ..Default::default() });
            let kept = saved(app, p.id);
            let server = [d.server.as_str(), kept.server.as_str(), p.server].into_iter().find(|s| !s.is_empty()).unwrap_or("");
            json!({
                "platform": p.id, "label": p.label, "help": p.help, "server": server, "armed": d.armed,
                "status": if d.status.is_empty() { "off" } else { &d.status }, "error": d.error,
                "key": if !d.sealed.is_empty() { "own" } else if !kept.sealed.is_empty() { "saved" } else { "none" },
            })
        })
        .collect();
    Ok(json!(out))
}

fn tell_crew(app: &Arc<AppState>, room: &str) -> ApiResult<()> {
    app.hub(room)?.send(Audience::Crew, "broadcast", status_json(app, room)?);
    Ok(())
}

fn problem(field: String, message: String) -> ApiError {
    tracing::debug!(%field, %message, "a destination was refused");
    ApiError(StatusCode::BAD_REQUEST, "invalid_destination")
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct DestinationIn {
    platform: String,
    #[serde(default)]
    server: String,
    /// Blank keeps the key that's there.
    #[serde(default)]
    key: String,
    #[serde(default)]
    armed: bool,
    #[serde(default)]
    clear_key: bool,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DestinationsIn {
    destinations: Vec<DestinationIn>,
}

fn valid_row(d: &DestinationIn) -> bool {
    CATALOGUE.iter().any(|p| p.id == d.platform)
        && (d.server.is_empty() || valid_url(&d.server))
        && !d.key.chars().any(|c| c.is_whitespace() || c.is_control())
        && d.key.len() <= 512
}

/// Saves a session's settings for the platforms named. Changes reach a broadcast that's running
/// the next time that platform is started.
pub async fn put_destinations(State(app): State<Arc<AppState>>, Path(room): Path<String>, Json(body): Json<DestinationsIn>) -> ApiResult<Json<Value>> {
    app.store.room(&room)?.ok_or(ApiError(StatusCode::NOT_FOUND, "not_found"))?;
    if !body.destinations.iter().all(valid_row) {
        return Err(ApiError(StatusCode::BAD_REQUEST, "invalid_destination"));
    }
    let existing = destinations(&app, &room)?;
    let mut changes = vec![];
    for d in body.destinations {
        let mut row = existing.iter().find(|e| e.platform == d.platform).cloned().unwrap_or_else(|| Destination { platform: d.platform.clone(), status: "off".into(), ..Default::default() });
        // Holding the default isn't a choice: a later change to the saved server should apply.
        row.server = if d.server == default_server(&d.platform) || d.server == saved(&app, &d.platform).server { String::new() } else { d.server.clone() };
        if d.clear_key {
            row.sealed.clear();
        } else if !d.key.is_empty() {
            row.sealed = seal(&app, &d.key);
        }
        row.armed = d.armed;
        if row.armed && (!has_key(&app, &row) || full_url(&app, &row).is_none()) {
            return Err(problem(d.platform, "needs a server and a key".into()));
        }
        changes.push(row);
    }
    for row in &changes {
        save(&app, &room, row)?;
    }
    tell_crew(&app, &room)?;
    Ok(Json(json!({ "destinations": admin_rows(&app, &room)? })))
}

pub async fn get_destinations(State(app): State<Arc<AppState>>, Path(room): Path<String>) -> ApiResult<Json<Value>> {
    app.store.room(&room)?.ok_or(ApiError(StatusCode::NOT_FOUND, "not_found"))?;
    Ok(Json(json!({ "destinations": admin_rows(&app, &room)? })))
}

/// The servers and keys kept for every session.
pub async fn get_saved(State(app): State<Arc<AppState>>) -> Json<Value> {
    let rows: Vec<Value> = CATALOGUE
        .iter()
        .filter(|p| p.reusable)
        .map(|p| {
            let kept = saved(&app, p.id);
            json!({ "platform": p.id, "label": p.label, "help": p.help, "server": if kept.server.is_empty() { p.server } else { &kept.server }, "keySet": !kept.sealed.is_empty() })
        })
        .collect();
    Json(json!({ "destinations": rows }))
}

pub async fn put_saved(State(app): State<Arc<AppState>>, Json(body): Json<DestinationsIn>) -> ApiResult<Json<Value>> {
    let reusable = |d: &DestinationIn| CATALOGUE.iter().any(|p| p.id == d.platform && p.reusable);
    if !body.destinations.iter().all(|d| valid_row(d) && reusable(d)) {
        return Err(ApiError(StatusCode::BAD_REQUEST, "invalid_destination"));
    }
    for d in body.destinations {
        let mut kept = saved(&app, &d.platform);
        kept.server = d.server;
        if d.clear_key {
            kept.sealed.clear();
        } else if !d.key.is_empty() {
            kept.sealed = seal(&app, &d.key);
        }
        app.store.save_setting(&format!("saved-destination:{}", d.platform), &serde_json::to_string(&kept).unwrap_or_default())?;
    }
    Ok(get_saved(State(app)).await)
}

// ---- the broadcast

fn egress_id(app: &AppState, room: &str) -> ApiResult<Option<String>> {
    Ok(app.store.get_doc(room, "broadcast", "egress")?)
}

/// Starts sending to everything marked to go out. Failures are shown to the crew, not raised:
/// a platform that refuses must not stop the session.
pub async fn start(app: &Arc<AppState>, room: &str) {
    if let Err(err) = start_armed(app, room).await {
        tracing::warn!(error = err.1, "the broadcast did not start");
    }
    let _ = tell_crew(app, room);
}

/// One broadcast with every destination in it: the recorder encodes once and sends to them all.
async fn start_armed(app: &Arc<AppState>, room: &str) -> ApiResult<()> {
    let mut urls = vec![];
    let mut going = vec![];
    for mut d in destinations(app, room)?.into_iter().filter(|d| d.armed) {
        match full_url(app, &d) {
            None => {
                d.status = "failed".into();
                d.error = "The stream key can't be read. Enter it again in the settings.".into();
            }
            Some(url) if !reachable(app, &url).await => {
                d.status = "failed".into();
                d.error = "That server address can't be used. Check it in the settings.".into();
            }
            Some(url) => {
                d.masked = masked(&url);
                urls.push(url);
                d.status = "starting".into();
                d.error.clear();
                going.push(d.platform.clone());
            }
        }
        save(app, room, &d)?;
    }
    if urls.is_empty() {
        return Ok(());
    }
    let signed = ticket::sign(&app.config.ticket_key, Role::Recorder, room, "program");
    let page = format!("{}/program/{signed}", app.config.internal_url);
    match app.backend.start_broadcast(room, urls, &page).await {
        Ok(id) => {
            app.store.put_doc(room, "broadcast", "egress", &id)?;
            app.store.save_setting(&format!("broadcast-egress:{id}"), &json!(room).to_string())?;
        }
        Err(err) => {
            tracing::warn!(error = %err.0, "the recorder refused the broadcast");
            for mut d in destinations(app, room)?.into_iter().filter(|d| going.contains(&d.platform)) {
                d.status = "failed".into();
                d.error = "The broadcast couldn't start. The server may be out of capacity.".into();
                save(app, room, &d)?;
            }
        }
    }
    Ok(())
}

async fn send_to(app: &Arc<AppState>, room: &str, platform: &str) -> ApiResult<()> {
    let mut rows = destinations(app, room)?;
    let Some(d) = rows.iter_mut().find(|d| d.platform == platform) else {
        return Err(ApiError(StatusCode::NOT_FOUND, "not_found"));
    };
    let fail = |d: &mut Destination, message: &str| {
        d.status = "failed".into();
        d.error = message.into();
    };
    let Some(url) = full_url(app, d) else {
        fail(d, "The stream key can't be read. Enter it again in the settings.");
        save(app, room, d)?;
        return Ok(());
    };
    if !reachable(app, &url).await {
        fail(d, "That server address can't be used. Check it in the settings.");
        save(app, room, d)?;
        return Ok(());
    }
    d.masked = masked(&url);
    d.status = "starting".into();
    d.error.clear();
    save(app, room, d)?;
    let result = match egress_id(app, room)? {
        Some(id) => app.backend.update_broadcast(&id, vec![url], vec![]).await,
        None => {
            let signed = ticket::sign(&app.config.ticket_key, Role::Recorder, room, "program");
            let page = format!("{}/program/{signed}", app.config.internal_url);
            match app.backend.start_broadcast(room, vec![url], &page).await {
                Ok(id) => {
                    app.store.put_doc(room, "broadcast", "egress", &id)?;
                    app.store.save_setting(&format!("broadcast-egress:{id}"), &json!(room).to_string())?;
                    Ok(())
                }
                Err(err) => Err(err),
            }
        }
    };
    if let Err(err) = result {
        tracing::warn!(error = %err.0, "the recorder refused the broadcast");
        let d = rows.iter_mut().find(|d| d.platform == platform).expect("found above");
        fail(d, "The broadcast couldn't start. The server may be out of capacity.");
        save(app, room, d)?;
    }
    Ok(())
}

async fn stop_sending(app: &Arc<AppState>, room: &str, platform: &str) -> ApiResult<()> {
    let mut rows = destinations(app, room)?;
    let others_on = rows.iter().any(|d| d.platform != platform && matches!(d.status.as_str(), "starting" | "live"));
    let Some(d) = rows.iter_mut().find(|d| d.platform == platform) else {
        return Err(ApiError(StatusCode::NOT_FOUND, "not_found"));
    };
    if let Some(id) = egress_id(app, room)? {
        let result = if others_on {
            match full_url(app, d) {
                Some(url) => app.backend.update_broadcast(&id, vec![], vec![url]).await,
                None => Ok(()),
            }
        } else {
            app.store.del_doc(room, "broadcast", "egress")?;
            app.backend.stop_egress(&id).await
        };
        if let Err(err) = result {
            tracing::warn!(error = %err.0, "could not stop sending");
        }
    }
    d.status = "off".into();
    d.error.clear();
    save(app, room, d)
}

/// From the room: while live this starts or stops sending right away; before that it decides
/// what goes out when the session does.
pub async fn toggle(app: &Arc<AppState>, room: &str, platform: &str, on: bool, live: bool) -> ApiResult<()> {
    let mut rows = destinations(app, room)?;
    let Some(d) = rows.iter_mut().find(|d| d.platform == platform) else {
        return Err(ApiError(StatusCode::NOT_FOUND, "not_found"));
    };
    d.armed = on;
    save(app, room, d)?;
    if live {
        if on {
            send_to(app, room, platform).await?;
        } else {
            stop_sending(app, room, platform).await?;
        }
    }
    tell_crew(app, room)
}

/// The session ended: stop everything.
pub async fn stop(app: &Arc<AppState>, room: &str) {
    if let Ok(Some(id)) = egress_id(app, room) {
        let _ = app.store.del_doc(room, "broadcast", "egress");
        if let Err(err) = app.backend.stop_egress(&id).await {
            tracing::warn!(error = %err.0, "could not stop the broadcast");
        }
    }
    if let Ok(rows) = destinations(app, room) {
        for mut d in rows {
            if d.status == "starting" || d.status == "live" {
                d.status = "off".into();
                let _ = save(app, room, &d);
            }
        }
    }
    let _ = tell_crew(app, room);
}

pub fn is_broadcast(app: &AppState, info: &proto::EgressInfo) -> bool {
    !info.stream_results.is_empty() || app.store.setting(&format!("broadcast-egress:{}", info.egress_id)).ok().flatten().is_some()
}

/// LiveKit reports each destination with its key masked: `rtmps://host/live2/{abc...xyz}`.
pub async fn on_egress_update(app: &Arc<AppState>, info: &proto::EgressInfo) -> ApiResult<()> {
    let room = info.room_name.as_str();
    if app.store.room(room)?.is_none() {
        return Ok(());
    }
    let over = matches!(
        proto::EgressStatus::try_from(info.status),
        Ok(proto::EgressStatus::EgressComplete | proto::EgressStatus::EgressFailed | proto::EgressStatus::EgressAborted | proto::EgressStatus::EgressLimitReached)
    );
    let current = egress_id(app, room)?;
    let is_current = current.as_deref() == Some(info.egress_id.as_str());
    for mut d in destinations(app, room)? {
        let report = info.stream_results.iter().rev().find(|r| same_destination(&r.url, &d.masked));
        if let Some(r) = report {
            match proto::stream_info::Status::try_from(r.status) {
                Ok(proto::stream_info::Status::Active) if d.status != "off" => d.status = "live".into(),
                Ok(proto::stream_info::Status::Failed) if d.status != "off" => {
                    d.status = "failed".into();
                    d.error = "The platform didn't accept the stream. Check its stream key, and that its event is ready.".into();
                }
                Ok(proto::stream_info::Status::Finished) if d.status != "failed" => d.status = "off".into(),
                _ => {}
            }
        }
        if over && is_current && matches!(d.status.as_str(), "starting" | "live") {
            if info.error.is_empty() {
                d.status = "off".into();
            } else {
                d.status = "failed".into();
                d.error = "The broadcast stopped unexpectedly.".into();
            }
        }
        save(app, room, &d)?;
    }
    if over && is_current {
        app.store.del_doc(room, "broadcast", "egress")?;
    }
    tell_crew(app, room)
}

// ---- the brand: set at /admin, used by every room

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", default)]
pub struct Brand {
    #[serde(flatten)]
    pub theme: Theme,
    pub logo: bool,
    /// A font file was uploaded. Theme `font: custom` uses it.
    pub font_file: bool,
}

fn brand(app: &AppState) -> Brand {
    app.store.setting("brand").ok().flatten().and_then(|j| serde_json::from_str(&j).ok()).unwrap_or_default()
}

pub async fn get_brand(State(app): State<Arc<AppState>>) -> Json<Value> {
    Json(brand_json(&app))
}

pub fn brand_json(app: &AppState) -> Value {
    json!(brand(app))
}

fn logo_path(app: &AppState) -> std::path::PathBuf {
    std::path::Path::new(&app.config.upload_dir).join("_brand").join("logo.png")
}

fn save_brand(app: &Arc<AppState>, value: &Brand) -> ApiResult<()> {
    app.store.save_setting("brand", &serde_json::to_string(value).unwrap_or_default())?;
    // Rooms that are open pick it up straight away.
    let hubs: Vec<_> = app.hubs.lock().unwrap_or_else(|e| e.into_inner()).values().cloned().collect();
    for hub in hubs {
        hub.send(Audience::Everyone, "brand", json!(value));
    }
    Ok(())
}

pub async fn put_brand(State(app): State<Arc<AppState>>, Json(theme): Json<Theme>) -> ApiResult<Json<Value>> {
    if !valid_theme(&theme) {
        return Err(ApiError(StatusCode::BAD_REQUEST, "invalid_input"));
    }
    let value = Brand { theme, ..brand(&app) };
    save_brand(&app, &value)?;
    Ok(Json(json!(value)))
}

pub async fn put_logo(State(app): State<Arc<AppState>>, body: Bytes) -> ApiResult<Json<Value>> {
    if body.len() > MAX_LOGO_BYTES {
        return Err(ApiError(StatusCode::PAYLOAD_TOO_LARGE, "file_too_large"));
    }
    if !body.starts_with(&PNG_MAGIC) {
        return Err(ApiError(StatusCode::UNSUPPORTED_MEDIA_TYPE, "file_type_not_allowed"));
    }
    let path = logo_path(&app);
    let storage = |_| ApiError(StatusCode::INTERNAL_SERVER_ERROR, "storage_unavailable");
    tokio::fs::create_dir_all(path.parent().expect("has a parent")).await.map_err(storage)?;
    tokio::fs::write(&path, &body).await.map_err(storage)?;
    let value = Brand { logo: true, ..brand(&app) };
    save_brand(&app, &value)?;
    Ok(Json(json!(value)))
}

pub async fn delete_logo(State(app): State<Arc<AppState>>) -> ApiResult<Json<Value>> {
    let _ = tokio::fs::remove_file(logo_path(&app)).await;
    let value = Brand { logo: false, ..brand(&app) };
    save_brand(&app, &value)?;
    Ok(Json(json!(value)))
}

pub const MAX_FONT_BYTES: usize = 8 * 1024 * 1024;

fn font_path(app: &AppState) -> std::path::PathBuf {
    std::path::Path::new(&app.config.upload_dir).join("_brand").join("font")
}

/// What a font file really is, from its first bytes.
fn font_mime(head: &[u8]) -> Option<&'static str> {
    match head.get(..4)? {
        b"wOF2" => Some("font/woff2"),
        b"wOFF" => Some("font/woff"),
        b"OTTO" => Some("font/otf"),
        [0, 1, 0, 0] | b"true" => Some("font/ttf"),
        _ => None,
    }
}

pub async fn put_font(State(app): State<Arc<AppState>>, body: Bytes) -> ApiResult<Json<Value>> {
    if body.len() > MAX_FONT_BYTES {
        return Err(ApiError(StatusCode::PAYLOAD_TOO_LARGE, "file_too_large"));
    }
    if font_mime(&body).is_none() {
        return Err(ApiError(StatusCode::UNSUPPORTED_MEDIA_TYPE, "file_type_not_allowed"));
    }
    let path = font_path(&app);
    let storage = |_| ApiError(StatusCode::INTERNAL_SERVER_ERROR, "storage_unavailable");
    tokio::fs::create_dir_all(path.parent().expect("has a parent")).await.map_err(storage)?;
    tokio::fs::write(&path, &body).await.map_err(storage)?;
    let value = Brand { font_file: true, ..brand(&app) };
    save_brand(&app, &value)?;
    Ok(Json(json!(value)))
}

pub async fn delete_font(State(app): State<Arc<AppState>>) -> ApiResult<Json<Value>> {
    let _ = tokio::fs::remove_file(font_path(&app)).await;
    let mut value = brand(&app);
    value.font_file = false;
    if value.theme.font == "custom" {
        value.theme.font.clear();
    }
    save_brand(&app, &value)?;
    Ok(Json(json!(value)))
}

/// Public, and read for every room, so cached hard by browsers and streamed rather than read into memory.
pub async fn get_font(State(app): State<Arc<AppState>>, request: axum::extract::Request) -> Response {
    let path = font_path(&app);
    let mut head = [0u8; 4];
    let read = async {
        use tokio::io::AsyncReadExt;
        tokio::fs::File::open(&path).await?.read_exact(&mut head).await
    };
    if read.await.is_err() {
        return StatusCode::NOT_FOUND.into_response();
    }
    let mime: axum::http::HeaderValue = axum::http::HeaderValue::from_static(font_mime(&head).unwrap_or("application/octet-stream"));
    let Ok(mime_parsed) = mime.to_str().unwrap_or("application/octet-stream").parse() else { return StatusCode::NOT_FOUND.into_response() };
    let Ok(served) = tower::ServiceExt::oneshot(tower_http::services::ServeFile::new_with_mime(path, &mime_parsed), request).await;
    let (mut parts, body) = served.into_parts();
    parts.headers.insert(header::CACHE_CONTROL, axum::http::HeaderValue::from_static("public, max-age=3600"));
    Response::from_parts(parts, axum::body::Body::new(body))
}

/// Public: a logo is on every page of the room, including before anyone has joined.
pub async fn get_logo(State(app): State<Arc<AppState>>) -> Response {
    match tokio::fs::read(logo_path(&app)).await {
        Ok(bytes) => ([(header::CONTENT_TYPE, "image/png"), (header::CACHE_CONTROL, "public, max-age=60")], bytes).into_response(),
        Err(_) => StatusCode::NOT_FOUND.into_response(),
    }
}

/// The tab icon every page links: the brand's logo once one is set, else the built-in mark.
pub async fn get_icon(State(app): State<Arc<AppState>>) -> Response {
    match tokio::fs::read(logo_path(&app)).await {
        Ok(bytes) => ([(header::CONTENT_TYPE, "image/png"), (header::CACHE_CONTROL, "public, max-age=60")], bytes).into_response(),
        Err(_) => ([(header::CONTENT_TYPE, "image/svg+xml"), (header::CACHE_CONTROL, "public, max-age=60")], include_str!("../assets/mark.svg")).into_response(),
    }
}

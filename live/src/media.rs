//! Recordings and other files, handed out by signed links that expire, and recordings published
//! for viewers on a page of their own.
//!
//! The bucket can stay private. A signed link (`/files/...`) names the room, what the file is and
//! when the link stops working; it sends the browser on to a short-lived signed bucket address,
//! or serves a speaker's own recording from this server's disk. A published recording gets a
//! stable page (`/watch/...`) that works until it's unpublished.

use axum::{
    extract::{Path, Query, Request, State},
    http::{header, HeaderValue, StatusCode},
    response::{Html, IntoResponse, Redirect, Response},
    Json,
};
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
use hmac::{Hmac, Mac};
use rusty_s3::{Bucket, Credentials, S3Action, UrlStyle};
use serde::Deserialize;
use serde_json::{json, Value};
use sha2::Sha256;
use std::sync::Arc;
use std::time::Duration;
use subtle::ConstantTimeEq;
use tower::ServiceExt;
use tower_http::services::ServeFile;

use crate::app::{now, ApiError, ApiResult, AppState};

type HmacSha256 = Hmac<Sha256>;

/// How long a signed link works: long enough to watch or download, short enough not to matter if it leaks.
pub const LINK_SECONDS: i64 = 6 * 3600;
/// How long the bucket address a link sends you on to works. A download that has started carries on.
const BUCKET_SECONDS: u64 = 3600;

const NOT_FOUND: ApiError = ApiError(StatusCode::NOT_FOUND, "not_found");

fn tag(key: &str, purpose: &str, payload: &str) -> Vec<u8> {
    let mut mac = HmacSha256::new_from_slice(key.as_bytes()).expect("HMAC accepts any key length");
    mac.update(format!("visioncast-live-{purpose}-v1|{payload}").as_bytes());
    mac.finalize().into_bytes().to_vec()
}

fn token(key: &str, purpose: &str, payload: &str) -> String {
    format!("{}.{}", URL_SAFE_NO_PAD.encode(payload), URL_SAFE_NO_PAD.encode(tag(key, purpose, payload)))
}

fn open_token(key: &str, purpose: &str, value: &str) -> Option<String> {
    let (payload, mac) = value.split_once('.')?;
    let payload = String::from_utf8(URL_SAFE_NO_PAD.decode(payload).ok()?).ok()?;
    let mac = URL_SAFE_NO_PAD.decode(mac).ok()?;
    bool::from(tag(key, purpose, &payload).ct_eq(&mac)).then_some(payload)
}

/// What a signed link points at.
enum Target {
    /// An object in the recordings bucket, under this room's prefix.
    Bucket(String),
    /// A speaker's own recording, on this server's disk.
    Local(String),
}

/// A link to a file of this room's that works for [`LINK_SECONDS`].
fn signed(app: &AppState, room: &str, target: &Target) -> String {
    let (kind, reference) = match target {
        Target::Bucket(key) => ("b", key.as_str()),
        Target::Local(id) => ("l", id.as_str()),
    };
    let expires = now() + LINK_SECONDS;
    format!("{}/files/{}", app.config.public_url, token(&app.config.ticket_key, "file", &format!("{room}|{kind}|{reference}|{expires}")))
}

/// Where LiveKit's ingress fetches a video to play on the stage: this server, on its internal
/// address, signed for as long as the video plays and then some.
pub fn stage_video_link(app: &AppState, room: &str, id: &str, duration: f64) -> String {
    let expires = now() + duration.max(0.0) as i64 + 12 * 3600;
    format!("{}/files/{}", app.config.internal_url, token(&app.config.ticket_key, "file", &format!("{room}|u|{id}|{expires}")))
}

pub fn bucket_link(app: &AppState, room: &str, key: &str) -> String {
    signed(app, room, &Target::Bucket(key.to_string()))
}

fn bucket(app: &AppState, internal: bool) -> Option<(Bucket, Credentials)> {
    let storage = app.config.recording.as_ref()?;
    let public = if storage.endpoint.is_empty() { format!("https://s3.{}.amazonaws.com", storage.region) } else { storage.endpoint.clone() };
    let endpoint = if internal { app.config.recording_internal_endpoint.clone().unwrap_or(public) } else { public };
    let style = if storage.force_path_style { UrlStyle::Path } else { UrlStyle::VirtualHost };
    let bucket = Bucket::new(endpoint.parse().ok()?, style, storage.bucket.clone(), storage.region.clone()).ok()?;
    Some((bucket, Credentials::new(storage.access_key.clone(), storage.secret_key.clone())))
}

/// Where a browser can fetch a bucket object: the public base when the bucket is public, else a signed address.
fn bucket_address(app: &AppState, key: &str, disposition: Option<&str>) -> Option<String> {
    if let Some(base) = app.config.recording.as_ref().and_then(|r| r.public_base_url.clone()) {
        return Some(format!("{base}/{key}"));
    }
    let (bucket, credentials) = bucket(app, false)?;
    let mut action = bucket.get_object(Some(&credentials), key);
    if let Some(disposition) = disposition {
        action.query_mut().insert("response-content-disposition", disposition.to_string());
    }
    Some(action.sign(Duration::from_secs(BUCKET_SECONDS)).to_string())
}

/// The origin the bucket answers on, for the watch page's media policy.
fn bucket_origin(app: &AppState) -> Option<String> {
    let address = bucket_address(app, "x", None)?;
    let url = reqwest::Url::parse(&address).ok()?;
    Some(url.origin().ascii_serialization())
}

fn attachment(name: &str) -> String {
    let clean: String = name.chars().filter(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '-' | '_')).take(120).collect();
    format!("attachment; filename=\"{}\"", if clean.is_empty() { "file".to_string() } else { clean })
}

#[derive(Deserialize, Default)]
pub struct FileQuery {
    #[serde(default)]
    download: Option<String>,
}

/// `GET /files/{signed}`: plays in the browser, or downloads with `?download=1`.
pub async fn file(State(app): State<Arc<AppState>>, Path(value): Path<String>, Query(q): Query<FileQuery>, request: Request) -> ApiResult<Response> {
    let payload = open_token(&app.config.ticket_key, "file", &value).ok_or(NOT_FOUND)?;
    let mut parts = payload.splitn(4, '|');
    let (Some(room), Some(kind), Some(reference), Some(expires)) = (parts.next(), parts.next(), parts.next(), parts.next()) else { return Err(NOT_FOUND) };
    if expires.parse::<i64>().map_or(true, |e| e < now()) {
        return Err(ApiError(StatusCode::GONE, "link_expired"));
    }
    let download = q.download.is_some();
    match kind {
        "b" => {
            if !reference.starts_with(&format!("recordings/{room}/")) || reference.contains("..") {
                return Err(NOT_FOUND);
            }
            let name = reference.rsplit('/').next().unwrap_or("file");
            let disposition = if download { attachment(name) } else { "inline".to_string() };
            let address = bucket_address(&app, reference, Some(&disposition)).ok_or(NOT_FOUND)?;
            Ok(no_store(Redirect::temporary(&address).into_response()))
        }
        "l" => {
            if !crate::ticket::valid_id(reference) {
                return Err(NOT_FOUND);
            }
            let rec = crate::studio::local_recordings(&app, room)?.into_iter().find(|r| r.id == reference).ok_or(NOT_FOUND)?;
            let path = std::path::Path::new(&app.config.upload_dir).join(room).join(format!("{}.local", rec.id));
            let (mime, ext) = if rec.mp4 { ("video/mp4", "mp4") } else { ("video/webm", "webm") };
            let served = ServeFile::new_with_mime(path, &mime.parse().map_err(|_| NOT_FOUND)?)
                .oneshot(request)
                .await
                .map_err(|_| ApiError(StatusCode::INTERNAL_SERVER_ERROR, "storage_unavailable"))?;
            let (mut parts, body) = served.into_parts();
            let disposition = if download { attachment(&format!("{}.{ext}", rec.name.replace(' ', "-"))) } else { "inline".to_string() };
            parts.headers.insert(header::CONTENT_DISPOSITION, disposition.parse().map_err(|_| NOT_FOUND)?);
            parts.headers.insert(header::CACHE_CONTROL, HeaderValue::from_static("private, no-store"));
            parts.headers.insert(header::CONTENT_SECURITY_POLICY, HeaderValue::from_static("default-src 'none'; media-src 'self'; sandbox"));
            Ok(Response::from_parts(parts, axum::body::Body::new(body)))
        }
        "u" => {
            // A video uploaded for the stage, fetched by the media server.
            if !crate::ticket::valid_id(reference) {
                return Err(NOT_FOUND);
            }
            let found = app.store.get_doc(room, "file", reference)?.and_then(|j| serde_json::from_str::<Value>(&j).ok()).ok_or(NOT_FOUND)?;
            if found["kind"] != "media" {
                return Err(NOT_FOUND);
            }
            let path = std::path::Path::new(&app.config.upload_dir).join(room).join(reference);
            let served = ServeFile::new_with_mime(path, &"video/mp4".parse().map_err(|_| NOT_FOUND)?)
                .oneshot(request)
                .await
                .map_err(|_| ApiError(StatusCode::INTERNAL_SERVER_ERROR, "storage_unavailable"))?;
            let (mut parts, body) = served.into_parts();
            parts.headers.insert(header::CACHE_CONTROL, HeaderValue::from_static("private, no-store"));
            parts.headers.insert(header::CONTENT_SECURITY_POLICY, HeaderValue::from_static("default-src 'none'; sandbox"));
            Ok(Response::from_parts(parts, axum::body::Body::new(body)))
        }
        _ => Err(NOT_FOUND),
    }
}

fn no_store(mut response: Response) -> Response {
    response.headers_mut().insert(header::CACHE_CONTROL, HeaderValue::from_static("private, no-store"));
    response
}

/// Speakers' own recordings, with links, for `/v1` and `/admin`.
pub fn local_json(app: &AppState, room: &str) -> ApiResult<Value> {
    Ok(json!(crate::studio::local_recordings(app, room)?
        .into_iter()
        .filter(|r| r.finished || r.size > 0)
        .map(|r| json!({
            "id": r.id, "name": r.name, "sizeBytes": r.size, "startedAt": crate::app::iso(r.at), "finished": r.finished,
            "screen": r.screen, "inBucket": r.bucket_key.is_some(), "filename": r.bucket_key.clone(),
            "url": match &r.bucket_key { Some(key) => signed(app, room, &Target::Bucket(key.clone())), None => signed(app, room, &Target::Local(r.id.clone())) },
        }))
        .collect::<Vec<_>>()))
}

// ---- published recordings: a page for viewers that works until it's unpublished

/// The same recording always gets the same page, so publishing twice changes nothing.
fn watch_token(app: &AppState, room: &str, egress_id: &str) -> String {
    token(&app.config.ticket_key, "watch", &format!("{room}|{egress_id}"))
}

fn is_published(app: &AppState, room: &str, egress_id: &str) -> bool {
    app.store.get_doc(room, "published", egress_id).ok().flatten().is_some()
}

pub fn watch_url(app: &AppState, room: &str, egress_id: &str) -> Option<String> {
    is_published(app, room, egress_id).then(|| format!("{}/watch/{}", app.config.public_url, watch_token(app, room, egress_id)))
}

fn finished_recording(app: &AppState, room: &str, egress_id: &str) -> ApiResult<crate::store::Recording> {
    app.store
        .recordings(room)?
        .into_iter()
        .find(|r| r.egress_id == egress_id && r.status == "complete" && !r.filename.is_empty())
        .ok_or(NOT_FOUND)
}

pub async fn publish(State(app): State<Arc<AppState>>, Path((room, egress_id)): Path<(String, String)>) -> ApiResult<Json<Value>> {
    finished_recording(&app, &room, &egress_id)?;
    app.store.put_doc(&room, "published", &egress_id, &json!({ "at": now() }).to_string())?;
    let page = format!("{}/watch/{}", app.config.public_url, watch_token(&app, &room, &egress_id));
    Ok(Json(json!({ "watchUrl": page, "videoUrl": format!("{page}/video.mp4") })))
}

pub async fn unpublish(State(app): State<Arc<AppState>>, Path((room, egress_id)): Path<(String, String)>) -> ApiResult<Json<Value>> {
    app.store.del_doc(&room, "published", &egress_id)?;
    Ok(Json(json!({})))
}

/// The room and recording a watch page is for, while it's published.
fn watched(app: &AppState, value: &str) -> ApiResult<(crate::store::Room, crate::store::Recording)> {
    let payload = open_token(&app.config.ticket_key, "watch", value).ok_or(NOT_FOUND)?;
    let (room, egress_id) = payload.split_once('|').ok_or(NOT_FOUND)?;
    if !is_published(app, room, egress_id) {
        return Err(NOT_FOUND);
    }
    let found = app.store.room(room)?.ok_or(NOT_FOUND)?;
    let recording = finished_recording(app, room, egress_id)?;
    Ok((found, recording))
}

fn everyone_srt(app: &AppState, room: &str, egress_id: &str) -> Option<String> {
    let files = crate::captions::recording_files(app, room, egress_id);
    files.as_array()?.iter().find(|f| f["speaker"] == "Everyone" && f["kind"] == "srt").and_then(|f| f["filename"].as_str().map(str::to_string))
}

fn escape(text: &str) -> String {
    text.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;").replace('"', "&quot;").replace('\'', "&#39;")
}

/// `GET /watch/{token}`: the recording, its captions and a download, on the brand.
pub async fn watch_page(State(app): State<Arc<AppState>>, Path(value): Path<String>) -> Response {
    let Ok((room, recording)) = watched(&app, &value) else { return crate::app::not_found_page() };
    let brand = crate::broadcast::brand_json(&app);
    let logo = if brand["logo"] == true { r#"<img class="watch-logo" src="/brand/logo" alt="">"# } else { "" };
    let track = if everyone_srt(&app, &room.id, &recording.egress_id).is_some() {
        format!(r#"<track kind="captions" srclang="en" label="Captions" src="/watch/{value}/captions.vtt">"#)
    } else {
        String::new()
    };
    let minutes = (recording.duration_seconds.max(60) + 30) / 60;
    let title = escape(&room.title);
    let html = format!(
        r#"<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="icon" href="/brand/icon">
<link rel="stylesheet" href="/assets/room.css">
</head>
<body class="watch">
<main class="watch-main">
<header class="watch-head">{logo}<div><h1>{title}</h1><p class="muted">Recording · {minutes} min</p></div></header>
<video class="watch-video" controls playsinline preload="metadata" src="/watch/{value}/video.mp4">{track}</video>
<p class="watch-actions"><a class="btn" href="/watch/{value}/video.mp4?download=1">Download the video</a></p>
</main>
</body>
</html>"#
    );
    let media = bucket_origin(&app).map(|o| format!(" {o}")).unwrap_or_default();
    let csp = format!("default-src 'none'; img-src 'self'; style-src 'self'; font-src 'self'; media-src 'self'{media}; base-uri 'none'; form-action 'none'; frame-ancestors *");
    let mut response = Html(html).into_response();
    if let Ok(value) = HeaderValue::from_str(&csp) {
        response.headers_mut().insert(header::CONTENT_SECURITY_POLICY, value);
    }
    response.headers_mut().insert(header::CACHE_CONTROL, HeaderValue::from_static("no-cache"));
    response
}

/// `GET /watch/{token}/video.mp4`: on to the video itself, or a download with `?download=1`.
pub async fn watch_video(State(app): State<Arc<AppState>>, Path(value): Path<String>, Query(q): Query<FileQuery>) -> ApiResult<Response> {
    let (room, recording) = watched(&app, &value)?;
    let disposition = if q.download.is_some() { attachment(&format!("{}.mp4", room.slug)) } else { "inline".to_string() };
    let address = bucket_address(&app, &recording.filename, Some(&disposition)).ok_or(NOT_FOUND)?;
    Ok(no_store(Redirect::temporary(&address).into_response()))
}

/// `GET /watch/{token}/captions.vtt`: everyone's captions, as WebVTT for the player.
pub async fn watch_captions(State(app): State<Arc<AppState>>, Path(value): Path<String>) -> ApiResult<Response> {
    let (room, recording) = watched(&app, &value)?;
    let key = everyone_srt(&app, &room.id, &recording.egress_id).ok_or(NOT_FOUND)?;
    let (bucket, credentials) = bucket(&app, true).ok_or(NOT_FOUND)?;
    let address = bucket.get_object(Some(&credentials), &key).sign(Duration::from_secs(60));
    let res = app.http.get(address).send().await.map_err(|_| ApiError(StatusCode::BAD_GATEWAY, "storage_unavailable"))?;
    if !res.status().is_success() {
        return Err(NOT_FOUND);
    }
    let srt = res.text().await.map_err(|_| ApiError(StatusCode::BAD_GATEWAY, "storage_unavailable"))?;
    Ok(([(header::CONTENT_TYPE, "text/vtt; charset=utf-8"), (header::CACHE_CONTROL, "public, max-age=300")], srt_to_vtt(&srt)).into_response())
}

/// SRT and WebVTT differ in the header, the decimal comma, and VTT not needing cue numbers.
pub fn srt_to_vtt(srt: &str) -> String {
    let mut out = String::from("WEBVTT\n\n");
    for line in srt.replace("\r\n", "\n").lines() {
        if line.contains(" --> ") {
            out.push_str(&line.replace(',', "."));
        } else {
            out.push_str(line);
        }
        out.push('\n');
    }
    out
}

// ---- speakers' own recordings, copied to the bucket once they're finished

/// Parts of a multipart upload: at least 5 MiB each except the last, and at most 10,000 of them.
const PART_BYTES: usize = 64 * 1024 * 1024;

/// Where a speaker's own recording goes in the bucket, next to the session's recordings.
pub fn local_key(room: &str, rec: &crate::studio::LocalRecording) -> String {
    let name: String = rec.name.to_lowercase().chars().map(|c| if c.is_ascii_alphanumeric() { c } else { '-' }).collect();
    let name = name.split('-').filter(|p| !p.is_empty()).collect::<Vec<_>>().join("-");
    let ext = if rec.mp4 { "mp4" } else { "webm" };
    format!("recordings/{room}/speakers/{}-{}{}.{ext}", crate::app::iso(rec.at).replace([':', '-'], "").get(..15).unwrap_or("x"), if name.is_empty() { "speaker".to_string() } else { name }, if rec.screen { "-screen" } else { "" })
}

/// Copies a finished speaker's recording to the bucket, in parts, so files of several GB never sit in
/// memory. The copy on this server stays until the room's records are removed. Failing leaves it there.
pub fn upload_local(app: Arc<AppState>, room: String, id: String) {
    if app.config.recording.is_none() {
        return;
    }
    tokio::spawn(async move {
        let Some(mut rec) = crate::studio::local_recordings(&app, &room).ok().and_then(|all| all.into_iter().find(|r| r.id == id)) else { return };
        if rec.bucket_key.is_some() {
            return;
        }
        let key = local_key(&room, &rec);
        match put_file(&app, &room, &rec, &key).await {
            Ok(()) => {
                rec.bucket_key = Some(key);
                let _ = app.store.put_doc(&room, "local", &rec.id, &serde_json::to_string(&rec).unwrap_or_default());
            }
            Err(err) => tracing::warn!(error = %err, "could not copy a speaker's own recording to the bucket"),
        }
    });
}

/// Deletes everything a session has in the bucket: its recordings, their captions and the
/// speakers' own copies, all kept under `recordings/{room}/`. Returns how many files went.
pub async fn delete_room_files(app: &AppState, room: &str) -> Result<usize, String> {
    let Some((bucket, credentials)) = bucket(app, true) else { return Ok(0) };
    let prefix = format!("recordings/{room}/");
    let signed = |d: u64| Duration::from_secs(d);
    let mut deleted = 0;
    let mut next: Option<String> = None;
    loop {
        let mut list = bucket.list_objects_v2(Some(&credentials));
        list.with_prefix(prefix.as_str());
        if let Some(token) = next.as_deref() {
            list.with_continuation_token(token.to_string());
        }
        let res = app.http.get(list.sign(signed(300))).send().await.map_err(|e| e.to_string())?;
        if !res.status().is_success() {
            return Err(format!("list: http {}", res.status()));
        }
        let page = rusty_s3::actions::ListObjectsV2::parse_response(&res.text().await.map_err(|e| e.to_string())?).map_err(|e| e.to_string())?;
        for object in page.contents.iter().filter(|o| o.key.starts_with(&prefix)) {
            let res = app.http.delete(bucket.delete_object(Some(&credentials), &object.key).sign(signed(300))).send().await.map_err(|e| e.to_string())?;
            if !res.status().is_success() && res.status() != StatusCode::NOT_FOUND {
                return Err(format!("delete: http {}", res.status()));
            }
            deleted += 1;
        }
        match page.next_continuation_token {
            Some(token) if !token.is_empty() => next = Some(token),
            _ => return Ok(deleted),
        }
    }
}

async fn put_file(app: &AppState, room: &str, rec: &crate::studio::LocalRecording, key: &str) -> Result<(), String> {
    use tokio::io::AsyncReadExt;
    let (bucket, credentials) = bucket(app, true).ok_or("no storage")?;
    let path = std::path::Path::new(&app.config.upload_dir).join(room).join(format!("{}.local", rec.id));
    let mut file = tokio::fs::File::open(&path).await.map_err(|e| e.to_string())?;
    let content_type = if rec.mp4 { "video/mp4" } else { "video/webm" };
    let signed = |d: u64| Duration::from_secs(d);
    let created = app.http.post(bucket.create_multipart_upload(Some(&credentials), key).sign(signed(600))).header(header::CONTENT_TYPE, content_type).send().await.map_err(|e| e.to_string())?;
    if !created.status().is_success() {
        return Err(format!("create: http {}", created.status()));
    }
    let body = created.text().await.map_err(|e| e.to_string())?;
    let upload = rusty_s3::actions::CreateMultipartUpload::parse_response(&body).map_err(|e| e.to_string())?;
    let upload_id = upload.upload_id().to_string();
    let mut etags = vec![];
    let result: Result<(), String> = async {
        let mut buffer = vec![0u8; PART_BYTES];
        for part in 1u16.. {
            let mut filled = 0;
            while filled < PART_BYTES {
                let n = file.read(&mut buffer[filled..]).await.map_err(|e| e.to_string())?;
                if n == 0 {
                    break;
                }
                filled += n;
            }
            if filled == 0 && part > 1 {
                break;
            }
            let url = bucket.upload_part(Some(&credentials), key, part, &upload_id).sign(signed(3600));
            let res = app.http.put(url).body(buffer[..filled].to_vec()).send().await.map_err(|e| e.to_string())?;
            if !res.status().is_success() {
                return Err(format!("part {part}: http {}", res.status()));
            }
            let etag = res.headers().get(header::ETAG).and_then(|v| v.to_str().ok()).ok_or("no etag")?.to_string();
            etags.push(etag);
            if filled < PART_BYTES {
                break;
            }
        }
        let complete = bucket.complete_multipart_upload(Some(&credentials), key, &upload_id, etags.iter().map(String::as_str));
        let url = complete.sign(signed(600));
        let res = app.http.post(url).body(complete.body()).send().await.map_err(|e| e.to_string())?;
        if res.status().is_success() { Ok(()) } else { Err(format!("complete: http {}", res.status())) }
    }
    .await;
    if result.is_err() {
        let _ = app.http.delete(bucket.abort_multipart_upload(Some(&credentials), key, &upload_id).sign(signed(600))).send().await;
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tokens_check_their_purpose_and_contents() {
        let t = token("k", "file", "room|b|recordings/room/a.mp4|9");
        assert_eq!(open_token("k", "file", &t).as_deref(), Some("room|b|recordings/room/a.mp4|9"));
        assert_eq!(open_token("k", "watch", &t), None, "a file link isn't a watch page");
        assert_eq!(open_token("other", "file", &t), None);
        let (payload, mac) = t.split_once('.').unwrap();
        let forged = format!("{}.{mac}", URL_SAFE_NO_PAD.encode("room|b|recordings/other/x.mp4|9"));
        assert_ne!(payload, forged.split_once('.').unwrap().0);
        assert_eq!(open_token("k", "file", &forged), None);
    }

    #[test]
    fn srt_becomes_vtt() {
        let vtt = srt_to_vtt("1\r\n00:00:01,500 --> 00:00:03,000\r\nEJ: Hello, everyone\r\n\r\n");
        assert!(vtt.starts_with("WEBVTT\n\n"));
        assert!(vtt.contains("00:00:01.500 --> 00:00:03.000"));
        assert!(vtt.contains("EJ: Hello, everyone"), "commas in the words stay");
    }
}

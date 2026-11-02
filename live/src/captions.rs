//! Live captions, per speaker. LiveKit's recorder sends each on-stage microphone here as raw
//! audio over a WebSocket, so who said what is known, not guessed. Speech is cut into short
//! segments and sent to Whisper, which runs in its own container so that a crash or a memory
//! spike there can't touch a live room. Results go to everyone in the room and are kept; when a
//! recording finishes, its SRT and transcript files go in the bucket next to it.
//!
//! Only the stage is ever transcribed, and only while live: never the green room.
use axum::{
    extract::{
        ws::{Message, WebSocket, WebSocketUpgrade},
        Path, Query, State,
    },
    http::{header, StatusCode},
    response::{IntoResponse, Response},
};
use rusty_s3::{Bucket, Credentials, S3Action, UrlStyle};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::mpsc;

use crate::app::{check_ticket, ApiError, ApiResult, AppState};
use crate::studio::{effective_role, Audience, Crew};
use crate::ticket::{self, Role};

const RATE: usize = 16_000;
const FRAME: usize = RATE * 30 / 1000;
/// The recorder sends 48 kHz stereo, with the same sound on both sides: its docs don't say so, but
/// that's what arrives (measured on egress v1.15.0). Averaging three stereo pairs gets to 16 kHz mono,
/// which is a crude filter and enough for speech.
const DECIMATE: usize = 6;
const VOICE_LEVEL: f64 = 0.006;
const PAUSE_MS: i64 = 600;
const MIN_MS: i64 = 900;
const SOFT_MAX_MS: i64 = 6_000;
const HARD_MAX_MS: i64 = 9_000;
const PRE_ROLL_FRAMES: usize = 10;
/// How long after a recording ends its last words may still be on their way back from Whisper.
const SETTLE: Duration = Duration::from_secs(12);
const QUEUE: usize = 12;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Caption {
    pub id: String,
    pub at_ms: i64,
    pub end_ms: i64,
    pub identity: String,
    pub name: String,
    pub text: String,
}

pub fn available(app: &AppState) -> bool {
    app.config.captions_url.is_some()
}

fn rows(app: &AppState, room: &str) -> ApiResult<Vec<Caption>> {
    let mut rows: Vec<Caption> = app.store.docs(room, "caption", 100_000)?.iter().filter_map(|j| serde_json::from_str(j).ok()).collect();
    rows.sort_by_key(|c| c.at_ms);
    Ok(rows)
}

// ---- which microphones are being listened to

/// Starts listening to every microphone on stage that isn't being listened to yet, or stops
/// them all when captions shouldn't be running. Safe to call as often as things change.
pub async fn sync(app: &Arc<AppState>, room: &str) {
    if !available(app) {
        return;
    }
    let Ok(hub) = app.hub(room) else { return };
    let state = hub.state();
    if state.phase != "live" || !state.settings.captions {
        let running: Vec<String> = hub.captioning.lock().unwrap_or_else(|e| e.into_inner()).drain().map(|(_, id)| id).collect();
        for id in running {
            let _ = app.backend.stop_egress(&id).await;
        }
        return;
    }
    let mics = match app.backend.stage_mics(room).await {
        Ok(mics) => mics,
        Err(err) => {
            tracing::warn!(error = %err.0, "could not list the stage's microphones");
            return;
        }
    };
    let base = app.config.internal_url.replacen("https://", "wss://", 1).replacen("http://", "ws://", 1);
    for (identity, track) in mics {
        if hub.captioning.lock().unwrap_or_else(|e| e.into_inner()).contains_key(&track) {
            continue;
        }
        // Claimed before the slow call, so two webhooks can't start it twice.
        hub.captioning.lock().unwrap_or_else(|e| e.into_inner()).insert(track.clone(), String::new());
        let signed = ticket::sign(&app.config.ticket_key, Role::CaptionSource, room, &identity);
        match app.backend.start_track_stream(room, &track, &format!("{base}/captions/in/{signed}")).await {
            Ok(id) => {
                hub.captioning.lock().unwrap_or_else(|e| e.into_inner()).insert(track, id);
            }
            Err(err) => {
                tracing::warn!(error = %err.0, "could not start captions for a speaker");
                hub.captioning.lock().unwrap_or_else(|e| e.into_inner()).remove(&track);
            }
        }
    }
}

// ---- audio in

pub async fn ingest(State(app): State<Arc<AppState>>, Path(value): Path<String>, ws: WebSocketUpgrade) -> ApiResult<Response> {
    let (parsed, room) = check_ticket(&app, &value)?;
    if parsed.role != Role::CaptionSource {
        return Err(ApiError(StatusCode::NOT_FOUND, "not_found"));
    }
    Ok(ws.max_message_size(1024 * 1024).on_upgrade(move |socket| receive(app, room.id, parsed.identity, socket)))
}

struct Segment {
    at_ms: i64,
    end_ms: i64,
    samples: Vec<i16>,
}

/// Cuts a speaker's audio at their pauses.
struct Cutter {
    pending: Vec<i32>,
    frame: Vec<i16>,
    pre_roll: Vec<Vec<i16>>,
    speech: Vec<i16>,
    started_ms: i64,
    quiet_ms: i64,
}

impl Cutter {
    fn new() -> Self {
        Self { pending: vec![], frame: Vec::with_capacity(FRAME), pre_roll: vec![], speech: vec![], started_ms: 0, quiet_ms: 0 }
    }

    fn feed(&mut self, bytes: &[u8], now_ms: i64, out: &mut Vec<Segment>) {
        for pair in bytes.chunks_exact(2) {
            self.pending.push(i16::from_le_bytes([pair[0], pair[1]]) as i32);
            if self.pending.len() == DECIMATE {
                let sample = (self.pending.iter().sum::<i32>() / DECIMATE as i32) as i16;
                self.pending.clear();
                self.frame.push(sample);
                if self.frame.len() == FRAME {
                    let frame = std::mem::replace(&mut self.frame, Vec::with_capacity(FRAME));
                    self.frame_done(frame, now_ms, out);
                }
            }
        }
    }

    fn frame_done(&mut self, frame: Vec<i16>, now_ms: i64, out: &mut Vec<Segment>) {
        let energy = frame.iter().map(|s| (*s as f64 / 32768.0).powi(2)).sum::<f64>() / frame.len() as f64;
        let voiced = energy.sqrt() > VOICE_LEVEL;
        if self.speech.is_empty() {
            if voiced {
                let held: usize = self.pre_roll.iter().map(|f| f.len()).sum();
                self.started_ms = now_ms - (held * 1000 / RATE) as i64;
                self.speech.extend(self.pre_roll.drain(..).flatten());
                self.speech.extend(frame);
                self.quiet_ms = 0;
            } else {
                self.pre_roll.push(frame);
                if self.pre_roll.len() > PRE_ROLL_FRAMES {
                    self.pre_roll.remove(0);
                }
            }
            return;
        }
        self.speech.extend(frame);
        self.quiet_ms = if voiced { 0 } else { self.quiet_ms + 30 };
        let length = (self.speech.len() * 1000 / RATE) as i64;
        if (self.quiet_ms >= PAUSE_MS && length >= MIN_MS) || (self.quiet_ms >= 90 && length >= SOFT_MAX_MS) || length >= HARD_MAX_MS {
            self.cut(now_ms, out);
        } else if self.quiet_ms >= PAUSE_MS * 2 {
            self.speech.clear(); // a cough, not speech
        }
    }

    fn cut(&mut self, now_ms: i64, out: &mut Vec<Segment>) {
        if self.speech.is_empty() {
            return;
        }
        let samples = std::mem::take(&mut self.speech);
        out.push(Segment { at_ms: self.started_ms, end_ms: now_ms, samples });
        self.quiet_ms = 0;
    }
}

async fn receive(app: Arc<AppState>, room: String, identity: String, mut socket: WebSocket) {
    let (tx, rx) = mpsc::channel::<Segment>(QUEUE);
    let worker = tokio::spawn(transcribe_in_order(app, room.clone(), identity.clone(), rx));
    let mut cutter = Cutter::new();
    let mut ready = vec![];
    let (mut total, started) = (0usize, std::time::Instant::now());
    while let Some(Ok(message)) = socket.recv().await {
        let now_ms = chrono::Utc::now().timestamp_millis();
        match message {
            Message::Binary(bytes) => {
                total += bytes.len();
                cutter.feed(&bytes, now_ms, &mut ready);
            }
            // Muted: whatever they were saying is finished.
            Message::Text(_) => cutter.cut(now_ms, &mut ready),
            Message::Close(_) => break,
            _ => {}
        }
        for segment in ready.drain(..) {
            // A captions server that's falling behind loses a sentence, not the room.
            if tx.try_send(segment).is_err() {
                tracing::warn!("captions are falling behind: skipped a segment");
            }
        }
    }
    cutter.cut(chrono::Utc::now().timestamp_millis(), &mut ready);
    for segment in ready.drain(..) {
        let _ = tx.send(segment).await;
    }
    tracing::info!(bytes = total, seconds = started.elapsed().as_secs(), "a speaker's audio ended");
    drop(tx);
    let _ = worker.await;
}

#[derive(Deserialize)]
struct Whisper {
    text: String,
}

async fn transcribe_in_order(app: Arc<AppState>, room: String, identity: String, mut rx: mpsc::Receiver<Segment>) {
    let Some(url) = app.config.captions_url.clone() else { return };
    let mut previous = String::new();
    while let Some(segment) = rx.recv().await {
        let body: Vec<u8> = segment.samples.iter().flat_map(|s| s.to_le_bytes()).collect();
        let answer = app
            .http
            .post(format!("{url}/transcribe"))
            .query(&[("prompt", previous.as_str())])
            .timeout(Duration::from_secs(45))
            .body(body)
            .send()
            .await;
        let text = match answer {
            Ok(res) if res.status().is_success() => res.json::<Whisper>().await.map(|w| w.text).unwrap_or_default(),
            Ok(res) => {
                tracing::warn!(status = %res.status(), "Whisper refused a segment");
                continue;
            }
            Err(err) => {
                tracing::warn!(error = %err, "could not reach Whisper");
                continue;
            }
        };
        let text = clean(&text);
        if text.is_empty() {
            continue;
        }
        let Ok(hub) = app.hub(&room) else { continue };
        let caption = Caption {
            id: format!("{:013}-{}", segment.at_ms, crate::studio::new_id()),
            at_ms: segment.at_ms,
            end_ms: segment.end_ms,
            name: hub.person_name(&identity),
            identity: identity.clone(),
            text: text.clone(),
        };
        if app.store.put_doc(&room, "caption", &caption.id, &serde_json::to_string(&caption).unwrap_or_default()).is_err() {
            tracing::error!("could not save a caption");
        }
        hub.send(Audience::Everyone, "caption", json!({ "name": caption.name, "text": caption.text }));
        translate(&app, &hub, &caption);
        previous = text.chars().rev().take(200).collect::<Vec<_>>().into_iter().rev().collect();
    }
}

/// Sends this caption in each language someone in the room has asked for. Slow or failed
/// translations are dropped: the original has already gone out.
fn translate(app: &Arc<AppState>, hub: &Arc<crate::studio::Hub>, caption: &Caption) {
    let Some(url) = app.config.translate_url.clone() else { return };
    let languages: Vec<String> = hub.caption_languages.lock().unwrap_or_else(|e| e.into_inner()).iter().cloned().collect();
    for language in languages {
        let (app, hub, url, name, text) = (app.clone(), hub.clone(), url.clone(), caption.name.clone(), caption.text.clone());
        tokio::spawn(async move {
            let body = json!({ "q": text, "source": app.config.captions_language, "target": language, "format": "text" });
            let sent = app.http.post(format!("{url}/translate")).timeout(Duration::from_secs(8)).json(&body).send().await;
            let Ok(res) = sent else { return };
            let Ok(reply) = res.json::<Value>().await else { return };
            if let Some(translated) = reply["translatedText"].as_str() {
                hub.send(Audience::Everyone, "caption_tr", json!({ "name": name, "text": translated, "language": language }));
            }
        });
    }
}

/// Whisper describes silence and noise in brackets rather than saying nothing.
fn clean(text: &str) -> String {
    let t = text.trim();
    let bracketed = |open: char, close: char| t.len() > 1 && t.starts_with(open) && t.ends_with(close) && !t[1..t.len() - 1].contains([open, close]);
    if t.is_empty() || bracketed('[', ']') || bracketed('(', ')') || bracketed('*', '*') {
        return String::new();
    }
    t.to_string()
}

// ---- files

fn clock(ms: i64, separator: char) -> String {
    let ms = ms.max(0);
    format!("{:02}:{:02}:{:02}{separator}{:03}", ms / 3_600_000, ms / 60_000 % 60, ms / 1000 % 60, ms % 1000)
}

/// With `names`, each line says who's speaking: for the file that has everyone in it.
pub fn srt(rows: &[Caption], base_ms: i64, names: bool) -> String {
    let mut out = String::new();
    for (i, c) in rows.iter().enumerate() {
        let start = c.at_ms - base_ms;
        let end = (c.end_ms - base_ms).max(start + 500);
        let line = if names { format!("{}: {}", c.name, c.text) } else { c.text.clone() };
        out.push_str(&format!("{}\n{} --> {}\n{}\n\n", i + 1, clock(start, ','), clock(end, ','), line));
    }
    out
}

pub fn transcript(rows: &[Caption], base_ms: i64) -> String {
    let mut out = String::new();
    let mut speaker = "";
    for c in rows {
        if c.identity != speaker {
            speaker = &c.identity;
            out.push_str(&format!("\n[{}] {}\n", clock(c.at_ms - base_ms, '.').split('.').next().unwrap_or(""), c.name));
        }
        out.push_str(&c.text);
        out.push('\n');
    }
    out.trim_start().to_string()
}

fn file_slug(name: &str) -> String {
    let slug: String = name.to_lowercase().chars().map(|c| if c.is_ascii_alphanumeric() { c } else { '-' }).collect();
    let slug = slug.trim_matches('-').to_string();
    if slug.is_empty() { "speaker".to_string() } else { slug }
}

/// A file name for each speaker in turn. Two people can share a name, so the second one gets a number.
fn speaker_slugs<'a>(names: impl Iterator<Item = &'a str>) -> Vec<String> {
    let mut taken = std::collections::HashSet::new();
    names
        .map(|name| {
            let base = file_slug(name);
            let mut slug = base.clone();
            let mut number = 1;
            while !taken.insert(slug.clone()) {
                number += 1;
                slug = format!("{base}-{number}");
            }
            slug
        })
        .collect()
}

/// One SRT and one transcript for everyone, and one of each per speaker, timed from `base_ms`.
fn files_for(rows: &[Caption], base_ms: i64) -> Vec<(String, String, String)> {
    let mut out = vec![("Everyone".to_string(), "srt".to_string(), srt(rows, base_ms, true)), ("Everyone".to_string(), "txt".to_string(), transcript(rows, base_ms))];
    let mut speakers: Vec<(&str, &str)> = rows.iter().map(|c| (c.identity.as_str(), c.name.as_str())).collect();
    speakers.sort();
    speakers.dedup();
    for (identity, name) in speakers {
        let theirs: Vec<Caption> = rows.iter().filter(|c| c.identity == identity).cloned().collect();
        out.push((name.to_string(), "srt".to_string(), srt(&theirs, base_ms, false)));
        out.push((name.to_string(), "txt".to_string(), transcript(&theirs, base_ms)));
    }
    out
}

async fn put_in_bucket(app: &AppState, key: &str, body: String) -> Result<(), String> {
    let storage = app.config.recording.as_ref().ok_or("no storage")?;
    let endpoint = app.config.recording_internal_endpoint.clone().unwrap_or_else(|| {
        if storage.endpoint.is_empty() { format!("https://s3.{}.amazonaws.com", storage.region) } else { storage.endpoint.clone() }
    });
    let style = if storage.force_path_style { UrlStyle::Path } else { UrlStyle::VirtualHost };
    let url = endpoint.parse().map_err(|_| "bad endpoint".to_string())?;
    let bucket = Bucket::new(url, style, storage.bucket.clone(), storage.region.clone()).map_err(|e| e.to_string())?;
    let credentials = Credentials::new(storage.access_key.clone(), storage.secret_key.clone());
    let signed = bucket.put_object(Some(&credentials), key).sign(Duration::from_secs(300));
    let content_type = if key.ends_with(".srt") { "application/x-subrip" } else { "text/plain; charset=utf-8" };
    let res = app.http.put(signed).header(header::CONTENT_TYPE, content_type).body(body).send().await.map_err(|e| e.to_string())?;
    if res.status().is_success() { Ok(()) } else { Err(format!("http {}", res.status())) }
}

/// A recording finished: put its captions next to it. Waits for the last words to come back
/// from Whisper. Timed from the recording's own start, so they line up with the video.
pub fn after_recording(app: Arc<AppState>, room: String, egress_id: String, filename: String, start_ms: i64, end_ms: i64) {
    tokio::spawn(async move {
        tokio::time::sleep(SETTLE).await;
        let Ok(all) = rows(&app, &room) else { return };
        let chosen: Vec<Caption> = all.into_iter().filter(|c| c.at_ms >= start_ms - 1000 && c.at_ms <= end_ms).collect();
        if chosen.is_empty() {
            return;
        }
        let stem = filename.strip_suffix(".mp4").unwrap_or(&filename).to_string();
        let mut saved = vec![];
        let files = files_for(&chosen, start_ms);
        let slugs = speaker_slugs(files.iter().skip(2).step_by(2).map(|(speaker, _, _)| speaker.as_str()));
        for (index, (speaker, kind, body)) in files.into_iter().enumerate() {
            // The first pair is always everyone's. Someone called Everyone gets a file of their own.
            let key = if index < 2 { format!("{stem}.{kind}") } else { format!("{stem}.{}.{kind}", slugs[index / 2 - 1]) };
            match put_in_bucket(&app, &key, body).await {
                Ok(()) => saved.push(json!({ "speaker": speaker, "kind": kind, "filename": key })),
                Err(err) => tracing::warn!(error = %err, "could not save a captions file"),
            }
        }
        let _ = app.store.put_doc(&room, "recording_captions", &egress_id, &json!(saved).to_string());
    });
}

pub fn recording_files(app: &AppState, room: &str, egress_id: &str) -> Value {
    let public = app.config.recording.as_ref().and_then(|r| r.public_base_url.clone());
    let saved: Vec<Value> = app.store.get_doc(room, "recording_captions", egress_id).ok().flatten().and_then(|j| serde_json::from_str(&j).ok()).unwrap_or_default();
    json!(saved
        .into_iter()
        .map(|mut f| {
            if let (Some(base), Some(name)) = (public.as_ref(), f["filename"].as_str().map(str::to_string)) {
                f["url"] = json!(format!("{base}/{name}"));
            }
            f
        })
        .collect::<Vec<_>>())
}

// ---- for hosts and producers, with or without a recording

#[derive(Deserialize)]
pub struct DownloadQuery {
    #[serde(default)]
    speaker: String,
}

/// `all.srt` or `all.txt`, or one speaker's with `?speaker=`, timed from going live.
pub async fn download(State(app): State<Arc<AppState>>, Path((value, file)): Path<(String, String)>, Query(q): Query<DownloadQuery>) -> ApiResult<Response> {
    let (parsed, room) = check_ticket(&app, &value)?;
    if !matches!(effective_role(parsed.role, false), Crew::Host | Crew::Producer) {
        return Err(ApiError(StatusCode::NOT_FOUND, "not_found"));
    }
    let hub = app.hub(&room.id)?;
    let mut chosen = rows(&app, &room.id)?;
    let base = hub.state().live_since.map(|s| s * 1000).or(chosen.first().map(|c| c.at_ms)).unwrap_or(0);
    let names = q.speaker.is_empty();
    if !names {
        chosen.retain(|c| c.identity == q.speaker);
    }
    let (body, content_type) = match file.as_str() {
        "all.srt" => (srt(&chosen, base, names), "application/x-subrip"),
        "all.txt" => (transcript(&chosen, base), "text/plain; charset=utf-8"),
        _ => return Err(ApiError(StatusCode::NOT_FOUND, "not_found")),
    };
    let speaker = chosen.first().filter(|_| !names).map(|c| file_slug(&c.name)).unwrap_or_else(|| "everyone".to_string());
    let extension = file.rsplit('.').next().unwrap_or("txt");
    let disposition = format!("attachment; filename=\"{}-{speaker}.{extension}\"", room.slug);
    Ok(([(header::CONTENT_TYPE, content_type.to_string()), (header::CONTENT_DISPOSITION, disposition), (header::CACHE_CONTROL, "no-store".to_string())], body).into_response())
}

/// Who has spoken, for the crew's download list.
pub fn speakers_json(app: &AppState, room: &str) -> ApiResult<Value> {
    let all = rows(app, room)?;
    let mut seen: Vec<(String, String)> = all.iter().map(|c| (c.identity.clone(), c.name.clone())).collect();
    seen.sort();
    seen.dedup();
    Ok(json!(seen.into_iter().map(|(identity, name)| json!({ "identity": identity, "name": name })).collect::<Vec<_>>()))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn caption(identity: &str, name: &str, at: i64, end: i64, text: &str) -> Caption {
        Caption { id: format!("{at}"), at_ms: at, end_ms: end, identity: identity.into(), name: name.into(), text: text.into() }
    }

    #[test]
    fn srt_is_timed_from_the_recording_and_numbered() {
        let rows = vec![caption("a", "Maya", 11_500, 13_250, "Welcome."), caption("b", "Gus", 3_725_000, 3_726_000, "Thanks.")];
        assert_eq!(
            srt(&rows, 10_000, false),
            "1\n00:00:01,500 --> 00:00:03,250\nWelcome.\n\n2\n01:01:55,000 --> 01:01:56,000\nThanks.\n\n"
        );
        assert!(srt(&rows, 10_000, true).contains("Maya: Welcome."));
    }

    #[test]
    fn a_caption_before_the_recording_never_has_a_negative_time() {
        assert!(srt(&[caption("a", "M", 900, 950, "x")], 1000, false).starts_with("1\n00:00:00,000 --> 00:00:00,400"));
    }

    #[test]
    fn transcripts_group_by_who_is_speaking() {
        let rows = vec![caption("a", "Maya", 1000, 2000, "One."), caption("a", "Maya", 2000, 3000, "Two."), caption("b", "Gus", 65_000, 66_000, "Three.")];
        assert_eq!(transcript(&rows, 0), "[00:00:01] Maya\nOne.\nTwo.\n\n[00:01:05] Gus\nThree.\n");
    }

    #[test]
    fn each_speaker_gets_their_own_pair_of_files_as_well_as_the_shared_pair() {
        let rows = vec![caption("a", "Maya H", 0, 1, "One."), caption("b", "Gus", 2, 3, "Two.")];
        let files = files_for(&rows, 0);
        assert_eq!(files.len(), 6);
        let maya = files.iter().find(|f| f.0 == "Maya H" && f.1 == "srt").unwrap();
        assert!(maya.2.contains("One.") && !maya.2.contains("Two."));
        assert_eq!(file_slug("Maya H."), "maya-h");
    }

    #[test]
    fn people_with_the_same_name_do_not_overwrite_each_other() {
        let slugs = speaker_slugs(["Maya Host", "Gus", "Maya Host", "Maya-Host", "maya host 2"].into_iter());
        assert_eq!(slugs, ["maya-host", "gus", "maya-host-2", "maya-host-3", "maya-host-2-2"]);
    }

    #[test]
    fn descriptions_of_silence_are_not_captions() {
        for noise in ["[BLANK_AUDIO]", " (music) ", "*coughs*", "  "] {
            assert_eq!(clean(noise), "", "{noise}");
        }
        assert_eq!(clean(" Hello there (really). "), "Hello there (really).");
    }

    #[test]
    fn speech_is_cut_at_pauses_and_silence_is_ignored() {
        let mut cutter = Cutter::new();
        let mut out = vec![];
        // Stereo at 48 kHz: 96 samples per millisecond.
        let tone = |ms: usize, level: i16| -> Vec<u8> { (0..96 * ms).flat_map(|_| level.to_le_bytes()).collect() };
        // Averaging would cancel a fast tone, so speech here is a slow square wave.
        let speech = |ms: usize| -> Vec<u8> { (0..96 * ms).flat_map(|i| (if (i / 96) % 2 == 0 { 8000i16 } else { -8000i16 }).to_le_bytes()).collect() };
        cutter.feed(&tone(2000, 0), 2000, &mut out);
        assert!(out.is_empty(), "silence makes nothing");
        cutter.feed(&speech(1500), 3500, &mut out);
        assert!(out.is_empty(), "still talking");
        cutter.feed(&tone(800, 0), 4300, &mut out);
        assert_eq!(out.len(), 1, "a pause ends the segment");
        let length = out[0].samples.len() * 1000 / RATE;
        assert!((1500..3000).contains(&length), "{length}");
        // Someone who never pauses is cut anyway.
        out.clear();
        cutter.feed(&speech(10_000), 15_000, &mut out);
        assert_eq!(out.len(), 1);
    }
}

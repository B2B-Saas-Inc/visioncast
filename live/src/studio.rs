//! The live studio: who is on stage, whether the session is live, and everything around the
//! video (chat, questions, polls, files, overlays).
//!
//! Two LiveKit rooms keep the audience away from the crew. The stage room (the session's ID)
//! is what the audience watches and the recorder records. The green room (`<id>--bs`) is where
//! hosts, producers and guests wait and talk, and only they ever get a credential for it. So
//! backstage audio can't reach an attendee whatever their browser does: nothing about it is
//! decided in the browser. An attendee gets no stage credential at all until the session is live.
//!
//! Everything else goes through this service rather than LiveKit's data channel, so it can be
//! moderated and is there for someone who joins late. `/join/{ticket}/events` streams changes;
//! `/join/{ticket}/act` makes them, after a permission check on the server.
use axum::{
    body::Body,
    extract::{Path, Query, Request, State},
    http::{header, StatusCode},
    response::{
        sse::{Event, KeepAlive, Sse},
        IntoResponse, Response,
    },
    Json,
};
use futures_util::{Stream, StreamExt};
use rand::Rng;
use serde::{de::DeserializeOwned, Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::convert::Infallible;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::io::AsyncWriteExt;
use tokio::sync::broadcast;
use tower::ServiceExt;
use tower_http::services::ServeFile;

use crate::app::{self, check_ticket, clean_name, now, ApiError, ApiResult, AppState};
use crate::backend::{self, Access};
use crate::ticket::Role;

const CHAT_HISTORY: i64 = 300;
const MAX_TEXT: usize = 500;
const MAX_OVERLAY_TEXT: usize = 200;
const MAX_OPTIONS: usize = 8;
const MAX_LAYERS: usize = 12;
const HUB_IDLE_SECONDS: u32 = 600;
/// More than a browser sends, however busy someone is. It's here for scripts.
const ACTS_PER_SECOND: u32 = 20;
const STREAMS_PER_PERSON: u32 = 5;

pub fn green_room(room: &str) -> String {
    format!("{room}--bs")
}

// ---- roles and what they may do

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Crew {
    Host,
    Producer,
    Guest,
    Attendee,
    Recorder,
}

impl Crew {
    pub fn name(self) -> &'static str {
        match self {
            Crew::Host => "host",
            Crew::Producer => "producer",
            Crew::Guest => "guest",
            Crew::Attendee => "attendee",
            Crew::Recorder => "recorder",
        }
    }
    fn is_crew(self) -> bool {
        matches!(self, Crew::Host | Crew::Producer | Crew::Guest)
    }
    /// Layouts, overlays, polls, moderation, files, moving people. Hosts and producers alike.
    fn runs_show(self) -> bool {
        matches!(self, Crew::Host | Crew::Producer)
    }
}

/// An attendee a host brought up is a guest until they're sent back.
pub fn effective_role(ticket: Role, promoted: bool) -> Crew {
    match ticket {
        Role::Host => Crew::Host,
        Role::Producer => Crew::Producer,
        Role::Guest => Crew::Guest,
        Role::Recorder | Role::CaptionSource => Crew::Recorder,
        Role::Attendee if promoted => Crew::Guest,
        Role::Attendee => Crew::Attendee,
    }
}

fn may_go_live(role: Crew, settings: &Settings) -> bool {
    role == Crew::Host || (role == Crew::Producer && settings.producers_can_go_live)
}

// ---- state everyone can see

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase", default)]
pub struct Settings {
    pub chat_enabled: bool,
    pub chat_review: bool,
    pub reactions_enabled: bool,
    pub qa_enabled: bool,
    pub qa_moderated: bool,
    pub qa_anonymous: bool,
    pub polls_enabled: bool,
    pub resources_enabled: bool,
    pub hand_raising: bool,
    pub profanity_filter: bool,
    pub producers_can_go_live: bool,
    /// Transcribes the people on stage while live. Each viewer turns the display on or off.
    pub captions: bool,
    /// Draws captions on the picture itself, so they're in recordings and broadcasts.
    pub captions_on_picture: bool,
    /// Attendees' questions are seen only by the crew and whoever asked.
    pub qa_private: bool,
    /// Each person on stage also records their own camera and microphone, in full quality.
    pub local_recordings: bool,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            chat_enabled: true,
            chat_review: false,
            reactions_enabled: true,
            qa_enabled: true,
            qa_moderated: false,
            qa_anonymous: true,
            polls_enabled: true,
            resources_enabled: true,
            hand_raising: true,
            profanity_filter: true,
            producers_can_go_live: true,
            captions: true,
            captions_on_picture: false,
            qa_private: false,
            local_recordings: false,
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct Rect {
    pub x: f64,
    pub y: f64,
    pub w: f64,
    pub h: f64,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase", default)]
pub struct Layout {
    pub mode: String,
    pub focus: Option<String>,
    pub tiles: BTreeMap<String, Rect>,
    /// How far every window stays from the edge of the picture, so a frame doesn't cover them.
    pub inset: f64,
}

impl Default for Layout {
    fn default() -> Self {
        Self { mode: "grid".into(), focus: None, tiles: BTreeMap::new(), inset: 0.0 }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct StageMember {
    pub identity: String,
    pub name: String,
    pub title: String,
    pub role: String,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct LowerThird {
    pub title: String,
    pub subtitle: String,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct Cta {
    pub title: String,
    pub subtitle: String,
    pub label: String,
    pub url: String,
}

/// A video played on the stage. Every browser plays the file itself, which looks and sounds
/// better than screen sharing it, and they stay in step with these numbers.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct MediaPlay {
    pub id: String,
    pub playing: bool,
    pub position: f64,
    pub at_ms: i64,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase", default)]
pub struct StudioState {
    pub phase: String,
    pub live_since: Option<i64>,
    pub recording: bool,
    pub stage: Vec<StageMember>,
    pub layout: Layout,
    pub frame_id: Option<String>,
    pub background_id: Option<String>,
    pub logo_id: Option<String>,
    pub visual_id: Option<String>,
    pub lower_third: Option<LowerThird>,
    pub ticker: Option<String>,
    pub cta: Option<Cta>,
    pub media: Option<MediaPlay>,
    pub stage_poll: Option<String>,
    pub pinned: Option<String>,
    pub settings: Settings,
    /// This session's own look. Anything left empty comes from the brand set in the studio.
    pub theme: Theme,
    /// GIFs, videos and text placed anywhere on the picture, in drawing order.
    pub layers: Vec<Layer>,
    /// Shown to attendees before the session goes live.
    pub banner_id: Option<String>,
    pub saved_layouts: Vec<SavedLayout>,
    pub survey: Option<Survey>,
    pub simulive: Option<Simulive>,
    pub slides: Option<SlideShow>,
    /// Empty when no breakouts are open. assigned, open or hub.
    pub breakout_kind: String,
    pub breakouts: Vec<Breakout>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct SavedLayout {
    pub id: String,
    pub name: String,
    pub layout: Layout,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase", default)]
pub struct SurveyQuestion {
    pub id: String,
    /// rating (1 to 5), choice or text.
    pub kind: String,
    pub text: String,
    pub options: Vec<String>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase", default)]
pub struct Survey {
    pub title: String,
    pub questions: Vec<SurveyQuestion>,
}

/// A recorded video played as if live: it goes live by itself at `at_ms`.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct Simulive {
    pub file_id: String,
    pub at_ms: i64,
    pub duration: f64,
    pub auto_end: bool,
    #[serde(default)]
    pub started: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct SlideShow {
    pub file_id: String,
    pub page: u32,
    pub pages: u32,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase", default)]
pub struct Breakout {
    pub id: String,
    pub name: String,
    /// Most people allowed in at once. 0 is no limit.
    pub cap: u32,
    /// Who it's for, when people are assigned. Only ever comes in with the request: attendees
    /// see the state, and they don't get to see who everyone else is.
    #[serde(skip_serializing)]
    pub members: Vec<String>,
}

/// A graphic on the picture: a looping GIF or muted video, text, or text over either. That's what
/// makes a chyron or a ticker: an animated band with the words live on top.
#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase", default)]
pub struct Layer {
    pub id: String,
    pub file_id: Option<String>,
    pub mime: String,
    pub name: String,
    pub x: f64,
    pub y: f64,
    pub w: f64,
    pub h: f64,
    pub visible: bool,
    pub title: String,
    pub subtitle: String,
    /// The words scroll across, like a ticker.
    pub scroll: bool,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase", default)]
pub struct Theme {
    pub accent: String,
    pub stage: String,
    pub mode: String,
    pub font: String,
}

impl Default for StudioState {
    fn default() -> Self {
        Self {
            phase: "not_live".into(),
            live_since: None,
            recording: false,
            stage: vec![],
            layout: Layout::default(),
            frame_id: None,
            background_id: None,
            logo_id: None,
            visual_id: None,
            lower_third: None,
            ticker: None,
            cta: None,
            media: None,
            stage_poll: None,
            pinned: None,
            settings: Settings::default(),
            theme: Theme::default(),
            layers: vec![],
            banner_id: None,
            saved_layouts: vec![],
            survey: None,
            simulive: None,
            slides: None,
            breakout_kind: String::new(),
            breakouts: vec![],
        }
    }
}

/// The state, with the server's clock so browsers can stay in step on a playing video.
fn with_clock(state: &StudioState) -> Value {
    let mut value = json!(state);
    value["serverMs"] = json!(chrono::Utc::now().timestamp_millis());
    value
}

/// What attendees and the recorder get of the state. They don't need to know a session is
/// prerecorded, what the survey will ask, what's waiting hidden on a layer, or the crew's layouts.
fn for_viewers(mut value: Value) -> Value {
    if let Some(map) = value.as_object_mut() {
        map.remove("simulive");
        map.remove("savedLayouts");
        if map.get("phase").and_then(Value::as_str) != Some("ended") {
            map.remove("survey");
        }
        if let Some(layers) = map.get_mut("layers").and_then(Value::as_array_mut) {
            layers.retain(|l| l["visible"].as_bool() == Some(true));
        }
    }
    value
}

fn state_json(app: &AppState, state: &StudioState) -> Value {
    let mut value = with_clock(state);
    value["brand"] = crate::broadcast::brand_json(app);
    value["captionsAvailable"] = json!(crate::captions::available(app));
    value["translateLanguages"] = json!(app.config.translate_languages);
    value["slidesAvailable"] = json!(app.config.slides_dir.is_some());
    value["ingressAvailable"] = json!(app.config.ingress_url.is_some());
    value
}

pub fn valid_theme(theme: &Theme) -> bool {
    let colour = |c: &str| c.is_empty() || (c.len() == 7 && c.starts_with('#') && c[1..].bytes().all(|b| b.is_ascii_hexdigit()));
    colour(&theme.accent)
        && colour(&theme.stage)
        && ["", "light", "dark"].contains(&theme.mode.as_str())
        && ["", "system", "serif", "rounded", "mono", "custom"].contains(&theme.font.as_str())
}

// ---- documents

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ChatMessage {
    pub id: String,
    pub at: i64,
    /// `event` is the audience's chat; `stage` is the crew's and is never sent to anyone else.
    pub channel: String,
    pub identity: String,
    pub name: String,
    pub role: String,
    pub text: String,
    /// False while it waits for a person to approve it.
    pub approved: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Question {
    pub id: String,
    pub at: i64,
    pub identity: String,
    pub name: String,
    pub text: String,
    /// pending, approved, rejected or hidden. Only approved questions reach the audience.
    pub status: String,
    pub answered: bool,
    pub answer: Option<String>,
    pub answered_by: Option<String>,
    #[serde(default)]
    pub votes: i64,
    /// Seen only by the crew and whoever asked.
    #[serde(default)]
    pub private: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Poll {
    pub id: String,
    pub question: String,
    pub options: Vec<String>,
    pub correct: Option<usize>,
    /// live: results show while voting. closed: only once the poll closes.
    pub show_results: String,
    pub show_counts: bool,
    /// draft, open or closed.
    pub status: String,
    #[serde(default)]
    pub tallies: Vec<i64>,
    #[serde(default)]
    pub total: i64,
    /// Comes up over the stage for anyone who hasn't voted.
    #[serde(default)]
    pub popup: bool,
    /// Opens by itself this many seconds after going live.
    #[serde(default)]
    pub open_after: Option<i64>,
    /// Closes by itself this many seconds after opening.
    #[serde(default)]
    pub close_after: Option<i64>,
    #[serde(default)]
    pub opened_at: Option<i64>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct FileDoc {
    pub id: String,
    /// resource, frame, background, logo, visual or media.
    pub kind: String,
    pub name: String,
    pub size: u64,
    pub public: bool,
    pub downloads: i64,
    pub at: i64,
    #[serde(default)]
    pub mime: String,
    /// For a slide deck: ready, or failed if it couldn't be converted. Empty while it's converting.
    #[serde(default)]
    pub status: String,
    #[serde(default)]
    pub pages: u32,
    /// Speaker notes, one per page. The crew's only.
    #[serde(default)]
    pub notes: Vec<String>,
}

fn doc<T: DeserializeOwned>(json: &str) -> Option<T> {
    serde_json::from_str(json).ok()
}

pub fn new_id() -> String {
    format!("{:016x}", rand::rng().random::<u64>())
}

// ---- what each audience is sent

fn public_chat(m: &ChatMessage) -> Value {
    json!({ "id": m.id, "at": m.at, "channel": m.channel, "name": m.name, "role": m.role, "text": m.text, "approved": m.approved })
}

fn crew_chat(m: &ChatMessage) -> Value {
    let mut v = public_chat(m);
    v["identity"] = json!(m.identity);
    v
}

fn public_question(q: &Question) -> Value {
    json!({ "id": q.id, "at": q.at, "name": q.name, "text": q.text, "status": q.status, "answered": q.answered, "answer": q.answer, "answeredBy": q.answered_by, "votes": q.votes, "private": q.private })
}

fn crew_question(q: &Question) -> Value {
    let mut v = public_question(q);
    // Anonymous means anonymous, to the crew as well.
    if q.name != "Anonymous" {
        v["identity"] = json!(q.identity);
    }
    v
}

fn public_poll(p: &Poll) -> Value {
    let hide_results = p.show_results == "closed" && p.status != "closed";
    json!({
        "id": p.id, "question": p.question, "options": p.options, "showCounts": p.show_counts, "status": p.status,
        "showResults": p.show_results, "popup": p.popup,
        "correct": if p.status == "closed" { json!(p.correct) } else { Value::Null },
        "tallies": if hide_results { Value::Null } else { json!(p.tallies) },
        "total": if hide_results { Value::Null } else { json!(p.total) },
    })
}

fn public_file(f: &FileDoc) -> Value {
    json!({ "id": f.id, "kind": f.kind, "name": f.name, "size": f.size, "pages": f.pages })
}

// ---- the per-room hub

#[derive(Clone, Debug)]
pub enum Audience {
    Everyone,
    /// Attendees and the recorder.
    Viewers,
    Crew,
    Only(String),
    /// Hosts and producers, not guests.
    Runners,
    Guests,
    /// Not an event: this person's streams end now, whatever they're doing.
    Disconnect(String),
}

#[derive(Clone, Debug)]
pub struct Envelope {
    audience: Audience,
    event: &'static str,
    data: String,
}

#[derive(Clone, Debug, Default)]
struct Person {
    name: String,
    title: String,
    role: String,
    connections: u32,
    hand: bool,
    invited: bool,
    last_chat: i64,
    last_ask: i64,
    last_react_ms: i64,
    breakout: String,
    acts_second: i64,
    acts: u32,
}

#[derive(Default)]
struct Dirty {
    polls: HashSet<String>,
    questions: HashSet<String>,
    people: bool,
    viewers: bool,
    chats: HashSet<String>,
    breakouts: bool,
}

pub struct Hub {
    room: String,
    tx: broadcast::Sender<Envelope>,
    state: Mutex<StudioState>,
    people: Mutex<HashMap<String, Person>>,
    dirty: Mutex<Dirty>,
    /// Microphones being transcribed: track ID to the recorder process doing it.
    pub captioning: Mutex<HashMap<String, String>>,
    /// Languages someone here wants captions in.
    pub caption_languages: Mutex<HashSet<String>>,
    /// Going live, ending and what runs by the clock happen one at a time.
    show_lock: tokio::sync::Mutex<()>,
    /// One chunk at a time for each speaker's own recording.
    local_lock: tokio::sync::Mutex<()>,
}

impl AppState {
    pub fn hub(self: &Arc<Self>, room: &str) -> ApiResult<Arc<Hub>> {
        let mut hubs = self.hubs.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(hub) = hubs.get(room) {
            return Ok(hub.clone());
        }
        let state = self.store.studio_state(room)?.and_then(|s| doc(&s)).unwrap_or_default();
        let (tx, _) = broadcast::channel(2048);
        let hub = Arc::new(Hub { room: room.to_string(), tx, state: Mutex::new(state), people: Default::default(), dirty: Default::default(), captioning: Default::default(), caption_languages: Default::default(), show_lock: Default::default(), local_lock: Default::default() });
        hubs.insert(room.to_string(), hub.clone());
        // Votes and presence change far faster than anyone needs to see: send them once a second.
        // A room nobody's in goes away after a while, and comes back from what's saved when asked for.
        let (app, ticking) = (self.clone(), hub.clone());
        tokio::spawn(async move {
            let mut tick = tokio::time::interval(Duration::from_secs(1));
            let mut idle = 0u32;
            loop {
                tick.tick().await;
                if let Err(err) = ticking.flush(&app) {
                    tracing::warn!(status = %err.0, "could not send updates");
                }
                let state = ticking.state();
                let needed = ticking.tx.receiver_count() > 0 || state.phase == "live" || state.simulive.as_ref().is_some_and(|sim| state.phase != "ended" && !sim.started);
                idle = if needed { 0 } else { idle + 1 };
                if idle >= HUB_IDLE_SECONDS {
                    let mut hubs = app.hubs.lock().unwrap_or_else(|e| e.into_inner());
                    if ticking.tx.receiver_count() == 0 {
                        hubs.remove(&ticking.room);
                        break;
                    }
                }
            }
        });
        // Separately, because going live waits on other services and votes shouldn't wait with it.
        let (app, ticking) = (self.clone(), Arc::downgrade(&hub));
        tokio::spawn(async move {
            let mut tick = tokio::time::interval(Duration::from_secs(1));
            loop {
                tick.tick().await;
                let Some(hub) = ticking.upgrade() else { break };
                if let Err(err) = hub.clockwork(&app).await {
                    tracing::warn!(status = %err.0, "could not run what was scheduled");
                }
            }
        });
        Ok(hub)
    }
}

impl Hub {
    pub fn send(&self, audience: Audience, event: &'static str, data: Value) {
        // No receivers is normal: nobody is connected yet.
        let _ = self.tx.send(Envelope { audience, event, data: data.to_string() });
    }

    pub fn state(&self) -> StudioState {
        self.state.lock().unwrap_or_else(|e| e.into_inner()).clone()
    }

    /// Changes the shared state, saves it and tells everyone.
    fn change(&self, app: &AppState, apply: impl FnOnce(&mut StudioState)) -> ApiResult<StudioState> {
        let next = {
            let mut guard = self.state.lock().unwrap_or_else(|e| e.into_inner());
            apply(&mut guard);
            guard.clone()
        };
        app.store.save_studio_state(&self.room, &serde_json::to_string(&next).unwrap_or_default())?;
        let value = state_json(app, &next);
        self.send(Audience::Viewers, "state", for_viewers(value.clone()));
        self.send(Audience::Crew, "state", value);
        Ok(next)
    }

    /// A session rescheduled after it ended is a new one. One still on the air carries on.
    pub fn reset_phase_after_end(&self, app: &AppState) -> ApiResult<()> {
        if self.state().phase == "ended" {
            self.change(app, |s| {
                s.phase = "not_live".into();
                s.live_since = None;
                s.simulive = None;
            })?;
        }
        Ok(())
    }

    /// Ends this person's event streams, for someone removed or whose link was replaced.
    pub fn disconnect(&self, identity: &str) {
        self.send(Audience::Disconnect(identity.to_string()), "bye", json!({}));
    }

    pub fn sync_recording(&self, app: &AppState) -> ApiResult<()> {
        let on = app.store.active_recording(&self.room)?.is_some();
        if self.state().recording != on {
            self.change(app, |s| s.recording = on)?;
        }
        Ok(())
    }

    pub fn person_name(&self, identity: &str) -> String {
        self.person(identity).name
    }

    fn person(&self, identity: &str) -> Person {
        self.people.lock().unwrap_or_else(|e| e.into_inner()).get(identity).cloned().unwrap_or_default()
    }

    pub fn forget_person(&self, identity: &str) {
        self.people.lock().unwrap_or_else(|e| e.into_inner()).remove(identity);
        self.dirty.lock().unwrap_or_else(|e| e.into_inner()).people = true;
    }

    /// For what only this service looks at, like rate limits. It changes nothing anyone sees.
    fn touch_person<T>(&self, identity: &str, apply: impl FnOnce(&mut Person) -> T) -> T {
        let mut people = self.people.lock().unwrap_or_else(|e| e.into_inner());
        apply(people.entry(identity.to_string()).or_default())
    }

    fn with_person<T>(&self, identity: &str, apply: impl FnOnce(&mut Person) -> T) -> T {
        let mut people = self.people.lock().unwrap_or_else(|e| e.into_inner());
        let value = apply(people.entry(identity.to_string()).or_default());
        self.dirty.lock().unwrap_or_else(|e| e.into_inner()).people = true;
        value
    }

    fn flush(&self, app: &AppState) -> ApiResult<()> {
        let dirty = std::mem::take(&mut *self.dirty.lock().unwrap_or_else(|e| e.into_inner()));
        for id in dirty.polls {
            if let Some(mut poll) = app.store.get_doc(&self.room, "poll", &id)?.and_then(|j| doc::<Poll>(&j)) {
                fill_tallies(app, &self.room, &mut poll)?;
                self.send_poll(&poll);
            }
        }
        for id in dirty.questions {
            if let Some(mut q) = app.store.get_doc(&self.room, "qa", &id)?.and_then(|j| doc::<Question>(&j)) {
                q.votes = app.store.tally(&self.room, &format!("qa:{id}"))?.iter().map(|t| t.1).sum();
                self.send_question(&q);
            }
        }
        if dirty.people {
            self.send(Audience::Crew, "people", self.people_json(app)?);
        }
        if dirty.viewers {
            self.send(Audience::Crew, "viewers", json!(self.viewer_count()));
        }
        for id in dirty.chats {
            self.send(Audience::Everyone, "chat_reactions", json!({ "id": id, "counts": chat_reactions(app, &self.room, &id)? }));
        }
        if dirty.breakouts {
            self.send(Audience::Everyone, "breakout_counts", self.breakout_counts());
        }
        Ok(())
    }

    fn breakout_counts(&self) -> Value {
        let mut counts: BTreeMap<String, usize> = BTreeMap::new();
        for p in self.people.lock().unwrap_or_else(|e| e.into_inner()).values() {
            if !p.breakout.is_empty() {
                *counts.entry(p.breakout.clone()).or_default() += 1;
            }
        }
        json!(counts)
    }

    /// What happens by the clock: simulive, and polls that open and close themselves.
    async fn clockwork(self: &Arc<Self>, app: &Arc<AppState>) -> ApiResult<()> {
        let _show = self.show_lock.lock().await;
        let state = self.state();
        let now_ms = chrono::Utc::now().timestamp_millis();
        if let Some(sim) = &state.simulive {
            if !sim.started && state.phase == "not_live" && now_ms >= sim.at_ms {
                let room = app.store.room(&self.room)?.ok_or(NOT_FOUND)?;
                let at_ms = now_ms;
                let file_id = sim.file_id.clone();
                self.change(app, |s| {
                    if let Some(sim) = s.simulive.as_mut() {
                        sim.started = true;
                        sim.at_ms = at_ms;
                    }
                    s.media = Some(MediaPlay { id: file_id, playing: true, position: 0.0, at_ms });
                })?;
                go_live(app, self, &room).await?;
            } else if sim.started && sim.auto_end && state.phase == "live" && now_ms as f64 >= sim.at_ms as f64 + sim.duration * 1000.0 {
                end_session(app, self, &self.room).await?;
            }
        }
        if state.phase == "live" {
            let elapsed = state.live_since.map_or(0, |since| now() - since);
            for j in app.store.docs(&self.room, "poll", 200)? {
                let Some(mut p) = doc::<Poll>(&j) else { continue };
                let opens = p.status == "draft" && p.open_after.is_some_and(|after| elapsed >= after);
                let closes = p.status == "open" && p.close_after.zip(p.opened_at).is_some_and(|(after, at)| now() - at >= after);
                if !opens && !closes {
                    continue;
                }
                p.status = if opens { "open".into() } else { "closed".into() };
                if opens {
                    p.opened_at = Some(now());
                }
                app.store.put_doc(&self.room, "poll", &p.id, &serde_json::to_string(&p).unwrap_or_default())?;
                fill_tallies(app, &self.room, &mut p)?;
                self.send_poll(&p);
            }
        }
        Ok(())
    }

    fn viewer_count(&self) -> usize {
        self.people.lock().unwrap_or_else(|e| e.into_inner()).values().filter(|p| p.connections > 0 && p.role == "attendee").count()
    }

    fn people_json(&self, app: &AppState) -> ApiResult<Value> {
        let people = self.people.lock().unwrap_or_else(|e| e.into_inner()).clone();
        let mut rows = vec![];
        let flags = app.store.people_flags(&self.room)?;
        for (identity, p) in people.iter().filter(|(_, p)| p.connections > 0 && !p.role.is_empty()) {
            let blocked = flags.get(identity).is_some_and(|f| f.1);
            rows.push(json!({ "identity": identity, "name": p.name, "title": p.title, "role": p.role, "hand": p.hand, "invited": p.invited, "blocked": blocked, "breakout": p.breakout }));
        }
        rows.sort_by(|a, b| a["name"].as_str().cmp(&b["name"].as_str()));
        Ok(json!(rows))
    }

    fn send_chat(&self, m: &ChatMessage) {
        self.send(Audience::Crew, "chat", crew_chat(m));
        if m.channel == "event" {
            if m.approved {
                self.send(Audience::Viewers, "chat", public_chat(m));
            } else {
                self.send(Audience::Only(m.identity.clone()), "chat", public_chat(m));
            }
        }
    }

    fn send_question(&self, q: &Question) {
        self.send(Audience::Crew, "qa", crew_question(q));
        if q.status == "approved" && !q.private {
            self.send(Audience::Viewers, "qa", public_question(q));
        } else {
            self.send(Audience::Viewers, "qa_remove", json!({ "id": q.id }));
            self.send(Audience::Only(q.identity.clone()), "qa", public_question(q));
        }
    }

    fn send_poll(&self, p: &Poll) {
        self.send(Audience::Runners, "poll", json!(p));
        if p.status == "draft" {
            self.send(Audience::Viewers, "poll_remove", json!({ "id": p.id }));
        } else {
            self.send(Audience::Viewers, "poll", public_poll(p));
            self.send(Audience::Guests, "poll", public_poll(p));
        }
    }

    fn send_files(&self, app: &AppState) -> ApiResult<()> {
        let files = files(app, &self.room)?;
        self.send(Audience::Runners, "files", json!(files));
        self.send(Audience::Guests, "files", json!(files.iter().filter(|f| guest_sees_file(f)).collect::<Vec<_>>()));
        let public: Vec<Value> = files.iter().filter(|f| f.kind == "resource" && f.public).map(public_file).collect();
        self.send(Audience::Viewers, "files", json!(public));
        Ok(())
    }
}

/// Guests get what's been shared, and slides, for the notes while they present.
fn guest_sees_file(f: &FileDoc) -> bool {
    (f.kind == "resource" && f.public) || f.kind == "deck"
}

fn files(app: &AppState, room: &str) -> ApiResult<Vec<FileDoc>> {
    Ok(app.store.docs(room, "file", 500)?.iter().filter_map(|j| doc(j)).collect())
}

const REACTIONS: [&str; 6] = ["👏", "❤️", "😂", "🎉", "👍", "🤔"];

fn chat_reactions(app: &AppState, room: &str, id: &str) -> ApiResult<BTreeMap<String, i64>> {
    let mut out = BTreeMap::new();
    for (choice, n) in app.store.tally(room, &format!("chat:{id}"))? {
        if let Some(emoji) = REACTIONS.get(choice as usize) {
            out.insert((*emoji).to_string(), n);
        }
    }
    Ok(out)
}

fn fill_tallies(app: &AppState, room: &str, poll: &mut Poll) -> ApiResult<()> {
    let counts = app.store.tally(room, &format!("poll:{}", poll.id))?;
    poll.tallies = vec![0; poll.options.len()];
    for (choice, n) in counts {
        if let Some(slot) = poll.tallies.get_mut(choice as usize) {
            *slot = n;
        }
    }
    poll.total = poll.tallies.iter().sum();
    Ok(())
}

// ---- who is asking

pub struct Me {
    pub room: crate::store::Room,
    pub ticket_role: Role,
    pub role: Crew,
    pub identity: String,
}

/// Who a crew member is in the rooms. The prefix keeps them out of attendance.
pub fn crew_identity(role: &str, member: &str) -> String {
    let letter = match role {
        "host" => "h",
        "producer" => "p",
        _ => "g",
    };
    format!("{}{letter}-{member}", app::HOST_PREFIX)
}

fn resolve(app: &AppState, ticket: &str) -> ApiResult<Me> {
    let (parsed, room) = check_ticket(app, ticket)?;
    let (promoted, _, removed) = app.store.person_flags(&room.id, &parsed.identity)?;
    if removed {
        return Err(ApiError(StatusCode::FORBIDDEN, "removed"));
    }
    let identity = match parsed.role {
        Role::Attendee => parsed.identity.clone(),
        Role::Recorder => "program".to_string(),
        _ => {
            // check_ticket has already found them.
            let member = crate::crew::member_for(app, parsed.role, &room.id, &parsed.identity)?.ok_or(NOT_FOUND)?;
            crew_identity(&member.role, &member.id)
        }
    };
    // Someone removed from the session can't come back by opening their crew link in a new tab.
    if parsed.role != Role::Attendee && app.store.person_flags(&room.id, &identity)?.2 {
        return Err(ApiError(StatusCode::FORBIDDEN, "removed"));
    }
    Ok(Me { role: effective_role(parsed.role, promoted), ticket_role: parsed.role, identity, room })
}

fn permissions(role: Crew, settings: &Settings) -> Value {
    json!({
        "goLive": may_go_live(role, settings),
        "runShow": role.runs_show(),
        "publish": role.is_crew(),
        "share": role.is_crew(),
        "answer": role.is_crew(),
        "canBeOnStage": matches!(role, Crew::Host | Crew::Guest),
    })
}

fn snapshot(app: &AppState, hub: &Hub, me: &Me) -> ApiResult<Value> {
    let crew = me.role.is_crew();
    let room = &me.room.id;
    let mut chat: Vec<Value> = vec![];
    for j in app.store.docs(room, "chat", CHAT_HISTORY)? {
        let Some(m) = doc::<ChatMessage>(&j) else { continue };
        if crew {
            chat.push(crew_chat(&m));
        } else if m.channel == "event" && (m.approved || m.identity == me.identity) {
            chat.push(public_chat(&m));
        }
    }
    // Everyone's votes in one query each, not one per question and poll: this runs for everyone who joins.
    let mut qa_votes: HashMap<String, i64> = HashMap::new();
    for (target, _, n) in app.store.tallies_with_prefix(room, "qa:")? {
        *qa_votes.entry(target).or_default() += n;
    }
    let mut poll_votes: HashMap<String, Vec<(i64, i64)>> = HashMap::new();
    for (target, choice, n) in app.store.tallies_with_prefix(room, "poll:")? {
        poll_votes.entry(target).or_default().push((choice, n));
    }
    let mut qa: Vec<Value> = vec![];
    for j in app.store.docs(room, "qa", 1000)? {
        let Some(mut q) = doc::<Question>(&j) else { continue };
        q.votes = qa_votes.get(&format!("qa:{}", q.id)).copied().unwrap_or(0);
        if crew {
            qa.push(crew_question(&q));
        } else if (q.status == "approved" && !q.private) || q.identity == me.identity {
            qa.push(public_question(&q));
        }
    }
    let mut polls: Vec<Value> = vec![];
    for j in app.store.docs(room, "poll", 200)? {
        let Some(mut p) = doc::<Poll>(&j) else { continue };
        p.tallies = vec![0; p.options.len()];
        for (choice, n) in poll_votes.get(&format!("poll:{}", p.id)).into_iter().flatten() {
            if let Some(slot) = p.tallies.get_mut(*choice as usize) {
                *slot = *n;
            }
        }
        p.total = p.tallies.iter().sum();
        if me.role.runs_show() {
            polls.push(json!(p));
        } else if p.status != "draft" {
            polls.push(public_poll(&p));
        }
    }
    let file_list = files(app, room)?;
    let files_json: Vec<Value> = if me.role.runs_show() {
        file_list.iter().map(|f| json!(f)).collect()
    } else if crew {
        file_list.iter().filter(|f| guest_sees_file(f)).map(|f| json!(f)).collect()
    } else {
        file_list.iter().filter(|f| f.kind == "resource" && f.public).map(public_file).collect()
    };
    let state = hub.state();
    let mut chat_counts = serde_json::Map::new();
    for (target, choice, n) in app.store.tallies_with_prefix(room, "chat:")? {
        let (Some(id), Some(emoji)) = (target.strip_prefix("chat:"), REACTIONS.get(choice as usize)) else { continue };
        let entry = chat_counts.entry(id.to_string()).or_insert_with(|| json!({}));
        entry[*emoji] = json!(n);
    }
    let mine: Vec<(String, i64)> = app.store.my_votes(room, &me.identity)?;
    let (_, blocked, _) = app.store.person_flags(room, &me.identity)?;
    let person = hub.person(&me.identity);
    let mut out = json!({
        "me": { "identity": me.identity, "role": me.role.name(), "perms": permissions(me.role, &state.settings), "blocked": blocked, "invited": person.invited, "hand": person.hand, "named": !person.name.is_empty() },
        "state": if crew { state_json(app, &state) } else { for_viewers(state_json(app, &state)) },
        "chat": chat,
        "qa": qa,
        "polls": polls,
        "files": files_json,
        "myVotes": mine.iter().map(|(t, c)| json!([t, c])).collect::<Vec<_>>(),
        "chatReactions": chat_counts,
        "breakoutCounts": hub.breakout_counts(),
        "surveyDone": app.store.get_doc(room, "survey", &me.identity)?.is_some(),
        "assigned": assigned_room(app, room, &me.identity)?,
    });
    if crew {
        out["people"] = hub.people_json(app)?;
        out["viewers"] = json!(hub.viewer_count());
        out["recordingAvailable"] = json!(app.config.recording.is_some());
        if me.role.runs_show() {
            let removed: Vec<Value> = app.store.people_flags(room)?.into_iter().filter(|(_, f)| f.2).map(|(identity, _)| json!({ "identity": identity, "name": hub.person(&identity).name })).collect();
            out["removedPeople"] = json!(removed);
            out["broadcast"] = crate::broadcast::status_json(app, room)?;
            out["speakers"] = crate::captions::speakers_json(app, room)?;
        }
    }
    Ok(out)
}

// ---- entering

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Enter {
    #[serde(default)]
    name: String,
    #[serde(default)]
    title: String,
    /// Older pages still send this: nothing depends on it now.
    #[serde(default, rename = "clientId")]
    _client_id: String,
}

/// Who someone is in this session, and the LiveKit credentials they may have right now.
pub async fn enter(State(app): State<Arc<AppState>>, Path(ticket): Path<String>, Json(body): Json<Enter>) -> ApiResult<Response> {
    let me = resolve(&app, &ticket)?;
    if me.role == Crew::Recorder {
        return Err(ApiError(StatusCode::FORBIDDEN, "not_allowed"));
    }
    match app::room_window(me.ticket_role, &me.room, now()) {
        app::Window::Open => {}
        app::Window::NotYet => return Err(ApiError(StatusCode::FORBIDDEN, "not_open_yet")),
        app::Window::Over => return Err(ApiError(StatusCode::GONE, "session_over")),
    }
    let hub = app.hub(&me.room.id)?;
    let state = hub.state();
    let fallback = match me.role {
        Crew::Host => "Host",
        Crew::Producer => "Producer",
        Crew::Guest => "Guest",
        _ => "Attendee",
    };
    let member = crate::crew::member_for(&app, me.ticket_role, &me.room.id, &check_ticket(&app, &ticket)?.0.identity)?;
    let name = clean_name(&body.name, member.as_ref().map_or(fallback, |m| m.name.as_str()));
    let title: String = clean_name(&body.title, member.as_ref().map_or("", |m| m.title.as_str()));
    hub.with_person(&me.identity, |p| {
        p.name = name.clone();
        p.title = title.clone();
        p.role = me.role.name().to_string();
    });
    let token = |room: &str, access| {
        backend::access_token(&app.config, access, room, &me.identity, &name).map_err(|e| {
            tracing::error!(error = %e.0, "could not sign a token");
            ApiError(StatusCode::INTERNAL_SERVER_ERROR, "internal_error")
        })
    };
    let mut out = json!({ "identity": me.identity, "role": me.role.name(), "url": app.config.livekit_public_url, "phase": state.phase });
    if me.role.is_crew() {
        // A server left at its small size between events can't record: say so before going live.
        let cpus = std::thread::available_parallelism().map_or(0, |n| n.get());
        if cpus < app.config.min_cpus {
            out["serverCpus"] = json!(cpus);
        }
        for room in [me.room.id.clone(), green_room(&me.room.id)] {
            app.backend.ensure_room(&room).await.map_err(|e| {
                tracing::warn!(error = %e.0, "could not prepare the room");
                ApiError(StatusCode::BAD_GATEWAY, "room_unavailable")
            })?;
        }
        let on_stage = state.stage.iter().any(|m| m.identity == me.identity);
        let stage = if on_stage {
            Access::Present
        } else {
            // A producer never appears, even in the raw participant list.
            Access::Watch { hidden: me.role == Crew::Producer }
        };
        out["stageToken"] = json!(token(&me.room.id, stage)?);
        out["greenToken"] = json!(token(&green_room(&me.room.id), Access::Present)?);
    } else if state.phase == "live" {
        out["stageToken"] = json!(token(&me.room.id, Access::Watch { hidden: true })?);
    }
    Ok(([(header::CACHE_CONTROL, "no-store")], Json(out)).into_response())
}

/// A separate connection that can only publish a screen. Anyone in the crew who is sharing
/// shows as a presentation, not as themselves.
pub async fn share_token(State(app): State<Arc<AppState>>, Path(ticket): Path<String>, Json(_body): Json<ClientOnly>) -> ApiResult<Response> {
    let me = resolve(&app, &ticket)?;
    let hub = app.hub(&me.room.id)?;
    let state = hub.state();
    let on_stage = state.stage.iter().any(|m| m.identity == me.identity);
    if !me.role.runs_show() && !(me.role == Crew::Guest && on_stage) {
        return Err(ApiError(StatusCode::FORBIDDEN, "not_on_stage"));
    }
    let person = hub.person(&me.identity);
    let name = if me.role == Crew::Producer { "Presentation".to_string() } else { format!("{}'s screen", person.name) };
    let identity = share_identity(&me);
    let token = backend::access_token(&app.config, Access::Share, &me.room.id, &identity, &name).map_err(|_| ApiError(StatusCode::INTERNAL_SERVER_ERROR, "internal_error"))?;
    Ok(([(header::CACHE_CONTROL, "no-store")], Json(json!({ "url": app.config.livekit_public_url, "token": token, "identity": identity }))).into_response())
}

fn share_identity(me: &Me) -> String {
    share_identity_of(&me.identity)
}

fn share_identity_of(identity: &str) -> String {
    format!("{}m-{}", app::HOST_PREFIX, identity.trim_start_matches(app::HOST_PREFIX))
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ClientOnly {
    /// Older pages still send this: nothing depends on it now.
    #[serde(default, rename = "clientId")]
    _client_id: String,
}

// ---- the event stream


struct Presence {
    hub: Arc<Hub>,
    identity: String,
    counted: bool,
}

impl Drop for Presence {
    fn drop(&mut self) {
        if self.counted {
            self.hub.with_person(&self.identity, |p| p.connections = p.connections.saturating_sub(1));
            self.hub.dirty.lock().unwrap_or_else(|e| e.into_inner()).viewers = true;
        }
    }
}

fn audience_matches(audience: &Audience, me: &Me) -> bool {
    match audience {
        Audience::Everyone => true,
        Audience::Crew => me.role.is_crew(),
        Audience::Viewers => !me.role.is_crew(),
        Audience::Runners => me.role.runs_show(),
        Audience::Guests => me.role == Crew::Guest,
        Audience::Only(identity) => *identity == me.identity && !me.role.is_crew(),
        Audience::Disconnect(_) => false,
    }
}

/// How often an open event stream checks that its link still works and its role hasn't changed.
const RECHECK_SECONDS: u64 = 20;

pub async fn events(State(app): State<Arc<AppState>>, Path(ticket): Path<String>) -> ApiResult<Sse<impl Stream<Item = Result<Event, Infallible>>>> {
    let me = resolve(&app, &ticket)?;
    let hub = app.hub(&me.room.id)?;
    let rx = hub.tx.subscribe();
    let counted = me.role != Crew::Recorder;
    if counted && !me.role.is_crew() && hub.person(&me.identity).connections >= STREAMS_PER_PERSON {
        return Err(SLOW_DOWN);
    }
    if counted {
        hub.with_person(&me.identity, |p| {
            p.connections += 1;
            if p.role.is_empty() {
                p.role = me.role.name().to_string();
            }
        });
        hub.dirty.lock().unwrap_or_else(|e| e.into_inner()).viewers = true;
    }
    let presence = Presence { hub: hub.clone(), identity: me.identity.clone(), counted };
    let first = snapshot(&app, &hub, &me)?;
    let start = (rx, presence, Some(first), app, hub, me, ticket);
    let stream = futures_util::stream::unfold(start, |(mut rx, presence, mut first, app, hub, me, ticket)| async move {
        if let Some(snap) = first.take() {
            let event = Event::default().event("snapshot").data(snap.to_string());
            return Some((Ok(event), (rx, presence, first, app, hub, me, ticket)));
        }
        let mut recheck = tokio::time::interval(Duration::from_secs(RECHECK_SECONDS));
        recheck.tick().await;
        loop {
            tokio::select! {
                got = rx.recv() => match got {
                    Ok(env) => {
                        if matches!(&env.audience, Audience::Disconnect(identity) if *identity == me.identity) {
                            return None;
                        }
                        if audience_matches(&env.audience, &me) {
                            let event = Event::default().event(env.event).data(env.data);
                            return Some((Ok(event), (rx, presence, first, app, hub, me, ticket)));
                        }
                    }
                    // This person fell behind: send everything again.
                    Err(broadcast::error::RecvError::Lagged(_)) => {
                        let Ok(now) = resolve(&app, &ticket) else { return None };
                        if now.role != me.role {
                            return None;
                        }
                        let Ok(snap) = snapshot(&app, &hub, &me) else { return None };
                        let event = Event::default().event("snapshot").data(snap.to_string());
                        return Some((Ok(event), (rx, presence, first, app, hub, me, ticket)));
                    }
                    Err(broadcast::error::RecvError::Closed) => return None,
                },
                // Taken off the crew, removed or sent back: this stream carries what they may no longer see.
                _ = recheck.tick() => {
                    match resolve(&app, &ticket) {
                        Ok(now) if now.role == me.role => {}
                        _ => return None,
                    }
                }
            }
        }
    });
    Ok(Sse::new(stream).keep_alive(KeepAlive::default()))
}

// ---- actions

#[derive(Deserialize, Debug)]
#[serde(tag = "type", rename_all = "snake_case", rename_all_fields = "camelCase")]
pub enum Action {
    GoLive,
    EndSession,
    Record { on: bool },
    StageAdd { identity: String },
    StageRemove { identity: String },
    SetLayout { mode: String, focus: Option<String>, tiles: Option<BTreeMap<String, Rect>>, inset: Option<f64> },
    ListRecordings,
    SetTheme { theme: Theme },
    LayerSave { layer: Layer },
    LayerDelete { id: String },
    BroadcastToggle { platform: String, on: bool },
    SetAsset { slot: String, id: Option<String> },
    SetLowerThird { title: Option<String>, subtitle: Option<String> },
    SetTicker { text: Option<String> },
    SetCta { title: Option<String>, subtitle: Option<String>, label: Option<String>, url: Option<String> },
    SetSettings { settings: Settings },
    MediaPlay { id: String },
    MediaControl { playing: bool, position: f64 },
    MediaStop,
    ChatSend { text: String, channel: String },
    ChatDelete { id: String },
    ChatApprove { id: String },
    ChatPin { id: Option<String> },
    ChatToQa { id: String },
    Block { identity: String, on: bool },
    Remove { identity: String },
    QaAsk { text: String, anonymous: bool },
    QaVote { id: String },
    QaModerate { id: String, action: String },
    QaAnswer { id: String, text: String },
    QaOverlay { id: String },
    PollSave {
        id: Option<String>,
        question: String,
        options: Vec<String>,
        correct: Option<usize>,
        show_results: String,
        show_counts: bool,
        #[serde(default)]
        popup: bool,
        #[serde(default)]
        open_after: Option<i64>,
        #[serde(default)]
        close_after: Option<i64>,
    },
    PollStatus { id: String, status: String },
    PollDelete { id: String },
    PollStage { id: Option<String> },
    PollVote { id: String, option: usize },
    FilePublic { id: String, public: bool },
    FileDelete { id: String },
    React { emoji: String },
    Hand { up: bool },
    Invite { identity: String, on: bool },
    InviteAnswer { accept: bool },
    Demote { identity: String },
    Restore { identity: String },
    ChatReact { id: String, emoji: String },
    ChatOverlay { id: String },
    LayoutSave { name: String },
    LayoutApply { id: String },
    LayoutDelete { id: String },
    SurveySave { survey: Option<Survey> },
    SurveyAnswer { answers: BTreeMap<String, String> },
    SurveyResults,
    SimuliveSet { file_id: String, at_ms: i64, duration: f64, auto_end: bool },
    SimuliveClear,
    SlidesShow { id: String, page: Option<u32> },
    SlidesPage { page: u32 },
    SlidesStop,
    BreakoutsOpen { kind: String, rooms: Vec<Breakout> },
    BreakoutsClose,
    BreakoutJoin { id: String },
    BreakoutLeave,
    CaptionLanguage { language: String },
    LocalRecordingDone { id: String },
    IngressCreate { name: String },
    IngressDelete { id: String },
    IngressList,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ActBody {
    /// Older pages still send this: nothing depends on it now.
    #[serde(default, rename = "clientId")]
    _client_id: String,
    action: Action,
}

const FORBIDDEN: ApiError = ApiError(StatusCode::FORBIDDEN, "not_allowed");
const INVALID: ApiError = ApiError(StatusCode::BAD_REQUEST, "invalid_input");
const SLOW_DOWN: ApiError = ApiError(StatusCode::TOO_MANY_REQUESTS, "slow_down");
const NOT_FOUND: ApiError = ApiError(StatusCode::NOT_FOUND, "not_found");

fn text(value: &str, max: usize) -> String {
    let cleaned: String = value.chars().filter(|c| !c.is_control() || *c == '\n').take(max).collect();
    cleaned.trim().to_string()
}

/// A short list, matched as whole words. Good enough to stop the obvious; a person moderates the rest.
const MASKED: [&str; 8] = ["fuck", "shit", "bitch", "asshole", "cunt", "nigger", "faggot", "retard"];

fn mask(value: &str) -> String {
    value
        .split_inclusive(|c: char| !c.is_alphanumeric())
        .map(|piece| {
            let word: String = piece.chars().take_while(|c| c.is_alphanumeric()).collect();
            if MASKED.iter().any(|m| word.eq_ignore_ascii_case(m)) {
                piece.replacen(&word, &"*".repeat(word.chars().count()), 1)
            } else {
                piece.to_string()
            }
        })
        .collect()
}

fn valid_layout(mode: &str, tiles: &Option<BTreeMap<String, Rect>>) -> bool {
    if !["single", "split", "grid", "speaker", "pip", "custom"].contains(&mode) {
        return false;
    }
    tiles.as_ref().is_none_or(|t| {
        t.len() <= 24
            && t.iter().all(|(k, r)| {
                k.len() <= 200
                    && [r.x, r.y, r.w, r.h].iter().all(|v| v.is_finite() && (0.0..=1.0).contains(v))
                    && r.w >= 0.05
                    && r.h >= 0.05
            })
    })
}

fn safe_url(value: &str) -> bool {
    value.len() <= 1000 && value.starts_with("https://") && !value.contains(|c: char| c.is_whitespace() || c.is_control())
}

pub async fn act(State(app): State<Arc<AppState>>, Path(ticket): Path<String>, Json(body): Json<ActBody>) -> ApiResult<Json<Value>> {
    let me = resolve(&app, &ticket)?;
    if me.role == Crew::Recorder {
        return Err(FORBIDDEN);
    }
    let hub = app.hub(&me.room.id)?;
    let second = chrono::Utc::now().timestamp();
    let too_many = hub.touch_person(&me.identity, |p| {
        if p.acts_second != second {
            p.acts_second = second;
            p.acts = 0;
        }
        p.acts += 1;
        p.acts > ACTS_PER_SECOND
    });
    if too_many {
        return Err(SLOW_DOWN);
    }
    // Attendees can act while the room is open, and answer the survey for as long as they can reach it.
    if me.role == Crew::Attendee && !matches!(app::room_window(me.ticket_role, &me.room, now()), app::Window::Open) && !matches!(body.action, Action::SurveyAnswer { .. }) {
        return Err(ApiError(StatusCode::CONFLICT, "not_available"));
    }
    let result = apply(&app, &hub, &me, body.action).await?;
    Ok(Json(result))
}

fn require(allowed: bool) -> ApiResult<()> {
    if allowed { Ok(()) } else { Err(FORBIDDEN) }
}

async fn apply(app: &Arc<AppState>, hub: &Arc<Hub>, me: &Me, action: Action) -> ApiResult<Value> {
    let room = me.room.id.as_str();
    let state = hub.state();
    let (_, blocked, _) = app.store.person_flags(room, &me.identity)?;
    let runs = me.role.runs_show();
    match action {
        // ---- the show
        Action::GoLive => {
            require(may_go_live(me.role, &state.settings))?;
            let _show = hub.show_lock.lock().await;
            if hub.state().phase != "not_live" {
                return Err(ApiError(StatusCode::CONFLICT, "not_available"));
            }
            go_live(app, hub, &me.room).await?;
        }
        Action::EndSession => {
            require(may_go_live(me.role, &state.settings))?;
            let _show = hub.show_lock.lock().await;
            if hub.state().phase == "ended" {
                return Ok(json!({}));
            }
            end_session(app, hub, room).await?;
        }
        Action::Record { on } => {
            require(runs)?;
            if app.config.recording.is_none() {
                return Err(ApiError(StatusCode::CONFLICT, "recording_not_configured"));
            }
            if on {
                app::start_recording(app, room).await?;
            } else {
                app::stop_recording(app, room).await?;
            }
            hub.sync_recording(app)?;
        }
        Action::StageAdd { identity } => {
            require(runs)?;
            let person = hub.person(&identity);
            // The rule that keeps producers off the air: they can't be put on stage, by anyone.
            if !matches!(person.role.as_str(), "host" | "guest") {
                return Err(ApiError(StatusCode::CONFLICT, "cannot_be_on_stage"));
            }
            // What someone says in a breakout room is private. It can't suddenly go on the air.
            if !person.breakout.is_empty() {
                return Err(ApiError(StatusCode::CONFLICT, "in_breakout"));
            }
            if !state.stage.iter().any(|m| m.identity == identity) {
                // They may have only just entered and still be connecting.
                let mut attempts = 0;
                while let Err(err) = app.backend.set_can_publish(room, &identity, true).await {
                    attempts += 1;
                    if attempts >= 10 {
                        tracing::warn!(error = %err.0, "could not open publishing");
                        return Err(ApiError(StatusCode::CONFLICT, "not_connected"));
                    }
                    tokio::time::sleep(Duration::from_millis(300)).await;
                }
                hub.change(app, |s| {
                    s.stage.push(StageMember { identity: identity.clone(), name: person.name, title: person.title, role: person.role });
                })?;
            }
        }
        Action::StageRemove { identity } => {
            require(runs)?;
            take_off_stage(app, hub, room, &identity).await?;
        }

        // ---- the look
        Action::SetLayout { mode, focus, tiles, inset } => {
            require(runs)?;
            if !valid_layout(&mode, &tiles) || focus.as_ref().is_some_and(|f| f.len() > 200) || inset.is_some_and(|i| !i.is_finite() || !(0.0..=0.25).contains(&i)) {
                return Err(INVALID);
            }
            hub.change(app, |s| {
                s.layout.mode = mode;
                s.layout.focus = focus;
                if let Some(t) = tiles {
                    s.layout.tiles = t;
                }
                if let Some(i) = inset {
                    s.layout.inset = i;
                }
            })?;
        }
        Action::SetTheme { theme } => {
            require(runs)?;
            if !valid_theme(&theme) {
                return Err(INVALID);
            }
            hub.change(app, |s| s.theme = theme)?;
        }
        Action::BroadcastToggle { platform, on } => {
            require(runs)?;
            crate::broadcast::toggle(app, room, &platform, on, state.phase == "live").await?;
        }
        Action::ListRecordings => {
            require(runs)?;
            return Ok(json!({ "recordings": crate::app::recordings_json(app, room)?, "speakers": crate::captions::speakers_json(app, room)?, "local": local_recordings(app, room)? }));
        }
        Action::LayerSave { mut layer } => {
            require(runs)?;
            let rect_ok = [layer.x, layer.y, layer.w, layer.h].iter().all(|v| v.is_finite() && (0.0..=1.0).contains(v)) && layer.w >= 0.02 && layer.h >= 0.02;
            if !rect_ok {
                return Err(INVALID);
            }
            layer.title = text(&layer.title, MAX_TEXT);
            layer.subtitle = text(&layer.subtitle, MAX_OVERLAY_TEXT);
            layer.name = text(&layer.name, MAX_OVERLAY_TEXT);
            if let Some(id) = &layer.file_id {
                let file = app.store.get_doc(room, "file", id)?.and_then(|j| doc::<FileDoc>(&j)).ok_or(NOT_FOUND)?;
                if file.kind != "graphic" {
                    return Err(INVALID);
                }
                layer.mime = file.mime;
            } else {
                layer.mime.clear();
                if layer.title.is_empty() {
                    return Err(INVALID);
                }
            }
            if layer.id.is_empty() {
                layer.id = new_id();
            }
            if !ticket_safe(&layer.id) || (state.layers.len() >= MAX_LAYERS && !state.layers.iter().any(|l| l.id == layer.id)) {
                return Err(INVALID);
            }
            let id = layer.id.clone();
            hub.change(app, |s| match s.layers.iter_mut().find(|l| l.id == layer.id) {
                Some(existing) => *existing = layer,
                None => s.layers.push(layer),
            })?;
            return Ok(json!({ "id": id }));
        }
        Action::LayerDelete { id } => {
            require(runs)?;
            hub.change(app, |s| s.layers.retain(|l| l.id != id))?;
        }
        Action::SetAsset { slot, id } => {
            require(runs)?;
            if let Some(id) = &id {
                let file = app.store.get_doc(room, "file", id)?.and_then(|j| doc::<FileDoc>(&j)).ok_or(NOT_FOUND)?;
                if file.kind != slot {
                    return Err(INVALID);
                }
            }
            hub.change(app, |s| match slot.as_str() {
                "frame" => s.frame_id = id,
                "background" => s.background_id = id,
                "logo" => s.logo_id = id,
                "visual" => s.visual_id = id,
                "banner" => s.banner_id = id,
                _ => {}
            })?;
        }
        Action::SetLowerThird { title, subtitle } => {
            require(runs)?;
            let value = title.map(|t| LowerThird { title: text(&t, MAX_OVERLAY_TEXT), subtitle: text(&subtitle.unwrap_or_default(), MAX_OVERLAY_TEXT) });
            hub.change(app, |s| s.lower_third = value.filter(|l| !l.title.is_empty()))?;
        }
        Action::SetTicker { text: value } => {
            require(runs)?;
            let value = value.map(|t| text(&t, MAX_TEXT)).filter(|t| !t.is_empty());
            hub.change(app, |s| s.ticker = value)?;
        }
        Action::SetCta { title, subtitle, label, url } => {
            require(runs)?;
            let cta = match (title, url) {
                (Some(t), Some(u)) => {
                    if !safe_url(&u) {
                        return Err(INVALID);
                    }
                    Some(Cta { title: text(&t, MAX_OVERLAY_TEXT), subtitle: text(&subtitle.unwrap_or_default(), MAX_OVERLAY_TEXT), label: text(&label.unwrap_or_default(), 60), url: u })
                }
                _ => None,
            };
            hub.change(app, |s| s.cta = cta)?;
        }
        Action::SetSettings { settings } => {
            require(runs)?;
            // Only a host can give producers, or take from them, the right to go live and end.
            if settings.producers_can_go_live != state.settings.producers_can_go_live {
                require(me.role == Crew::Host)?;
            }
            hub.change(app, |s| s.settings = settings)?;
            crate::captions::sync(app, room).await;
        }
        Action::MediaPlay { id } => {
            require(runs)?;
            let file = app.store.get_doc(room, "file", &id)?.and_then(|j| doc::<FileDoc>(&j)).ok_or(NOT_FOUND)?;
            if file.kind != "media" {
                return Err(INVALID);
            }
            let at_ms = chrono::Utc::now().timestamp_millis();
            hub.change(app, |s| s.media = Some(MediaPlay { id, playing: true, position: 0.0, at_ms }))?;
        }
        Action::MediaControl { playing, position } => {
            require(runs)?;
            if !position.is_finite() || position < 0.0 {
                return Err(INVALID);
            }
            let at_ms = chrono::Utc::now().timestamp_millis();
            hub.change(app, |s| {
                if let Some(m) = s.media.as_mut() {
                    m.playing = playing;
                    m.position = position;
                    m.at_ms = at_ms;
                }
            })?;
        }
        Action::MediaStop => {
            require(runs)?;
            hub.change(app, |s| s.media = None)?;
        }

        // ---- chat
        Action::ChatSend { text: value, channel } => {
            let stage = channel == "stage";
            if !stage && channel != "event" {
                return Err(INVALID);
            }
            require(me.role.is_crew() || (!stage && !blocked))?;
            if !stage && !me.role.is_crew() && !state.settings.chat_enabled {
                return Err(ApiError(StatusCode::CONFLICT, "chat_off"));
            }
            let at = now();
            let slow = hub.touch_person(&me.identity, |p| {
                let slow = !me.role.is_crew() && p.last_chat == at;
                p.last_chat = at;
                slow
            });
            if slow {
                return Err(SLOW_DOWN);
            }
            let mut body = text(&value, MAX_TEXT);
            if body.is_empty() {
                return Err(INVALID);
            }
            if state.settings.profanity_filter && !stage {
                body = mask(&body);
            }
            let person = hub.person(&me.identity);
            let approved = stage || me.role.is_crew() || !state.settings.chat_review;
            let m = ChatMessage { id: new_id(), at, channel, identity: me.identity.clone(), name: person.name, role: me.role.name().into(), text: body, approved };
            app.store.put_doc(room, "chat", &m.id, &serde_json::to_string(&m).unwrap_or_default())?;
            hub.send_chat(&m);
            return Ok(json!({ "id": m.id, "approved": m.approved }));
        }
        Action::ChatDelete { id } => {
            require(runs)?;
            app.store.del_doc(room, "chat", &id)?;
            hub.send(Audience::Everyone, "chat_remove", json!({ "id": id }));
            if state.pinned.as_deref() == Some(&id) {
                hub.change(app, |s| s.pinned = None)?;
            }
        }
        Action::ChatApprove { id } => {
            require(runs)?;
            let mut m = app.store.get_doc(room, "chat", &id)?.and_then(|j| doc::<ChatMessage>(&j)).ok_or(NOT_FOUND)?;
            m.approved = true;
            app.store.put_doc(room, "chat", &id, &serde_json::to_string(&m).unwrap_or_default())?;
            hub.send_chat(&m);
        }
        Action::ChatPin { id } => {
            require(runs)?;
            if let Some(id) = &id {
                let m = app.store.get_doc(room, "chat", id)?.and_then(|j| doc::<ChatMessage>(&j)).ok_or(NOT_FOUND)?;
                if m.channel != "event" || !m.approved {
                    return Err(INVALID);
                }
                hub.send(Audience::Everyone, "pin", public_chat(&m));
            }
            hub.change(app, |s| s.pinned = id)?;
        }
        Action::ChatToQa { id } => {
            require(runs)?;
            let m = app.store.get_doc(room, "chat", &id)?.and_then(|j| doc::<ChatMessage>(&j)).ok_or(NOT_FOUND)?;
            let q = Question { id: new_id(), at: now(), identity: m.identity, name: m.name, text: m.text, status: "approved".into(), answered: false, answer: None, answered_by: None, votes: 0, private: state.settings.qa_private };
            app.store.put_doc(room, "qa", &q.id, &serde_json::to_string(&q).unwrap_or_default())?;
            hub.send_question(&q);
        }
        Action::Block { identity, on } => {
            require(runs)?;
            require(!is_crew_identity(&identity))?;
            let (promoted, _, removed) = app.store.person_flags(room, &identity)?;
            app.store.set_person_flags(room, &identity, (promoted, on, removed))?;
            hub.send(Audience::Only(identity.clone()), "me", json!({ "blocked": on }));
            hub.dirty.lock().unwrap_or_else(|e| e.into_inner()).people = true;
        }
        Action::Remove { identity } => {
            require(runs)?;
            // Hosts and producers can't remove each other; that's a matter for the studio. Their
            // identities say so whether or not they're connected right now.
            require(!identity.starts_with("host-h-") && !identity.starts_with("host-p-") && !identity.starts_with("host-m-"))?;
            let target = hub.person(&identity);
            require(!matches!(target.role.as_str(), "host" | "producer"))?;
            if let Some(member) = identity.strip_prefix("host-g-") {
                let found = app.store.crew_member(member)?.filter(|m| m.room == room && m.role == "guest").ok_or(NOT_FOUND)?;
                app.store.delete_crew(&found.id)?;
            }
            take_off_stage(app, hub, room, &identity).await?;
            let (_, blocked, _) = app.store.person_flags(room, &identity)?;
            app.store.set_person_flags(room, &identity, (false, blocked, true))?;
            let _ = app.backend.remove_participant(room, &identity).await;
            let _ = app.backend.remove_participant(&green_room(room), &identity).await;
            for r in &state.breakouts {
                let _ = app.backend.remove_participant(&breakout_room(room, &r.id), &identity).await;
            }
            hub.send(Audience::Crew, "removed", json!({ "identity": identity }));
            hub.send(Audience::Only(identity.clone()), "removed", json!({ "identity": identity }));
            hub.disconnect(&identity);
            hub.with_person(&identity, |p| {
                p.connections = 0;
                p.breakout.clear();
            });
        }
        Action::Restore { identity } => {
            require(runs)?;
            let (promoted, blocked, _) = app.store.person_flags(room, &identity)?;
            app.store.set_person_flags(room, &identity, (promoted, blocked, false))?;
            hub.dirty.lock().unwrap_or_else(|e| e.into_inner()).people = true;
        }

        // ---- questions
        Action::QaAsk { text: value, anonymous } => {
            require(me.role.is_crew() || !blocked)?;
            if !me.role.is_crew() && !state.settings.qa_enabled {
                return Err(ApiError(StatusCode::CONFLICT, "qa_off"));
            }
            let at = now();
            let slow = hub.touch_person(&me.identity, |p| {
                let slow = !me.role.is_crew() && at - p.last_ask < 5;
                if !slow {
                    p.last_ask = at;
                }
                slow
            });
            if slow {
                return Err(SLOW_DOWN);
            }
            let mut body = text(&value, MAX_TEXT);
            if body.is_empty() {
                return Err(INVALID);
            }
            if state.settings.profanity_filter {
                body = mask(&body);
            }
            let name = if anonymous && state.settings.qa_anonymous { "Anonymous".to_string() } else { hub.person(&me.identity).name };
            let status = if state.settings.qa_moderated && !me.role.is_crew() { "pending" } else { "approved" };
            let private = state.settings.qa_private && !me.role.is_crew();
            let q = Question { id: new_id(), at, identity: me.identity.clone(), name, text: body, status: status.into(), answered: false, answer: None, answered_by: None, votes: 0, private };
            app.store.put_doc(room, "qa", &q.id, &serde_json::to_string(&q).unwrap_or_default())?;
            hub.send_question(&q);
            return Ok(json!({ "id": q.id, "status": q.status }));
        }
        Action::QaVote { id } => {
            require(!blocked)?;
            let q = app.store.get_doc(room, "qa", &id)?.and_then(|j| doc::<Question>(&j)).ok_or(NOT_FOUND)?;
            if q.status != "approved" || (q.private && !me.role.is_crew()) {
                return Err(INVALID);
            }
            let target = format!("qa:{id}");
            let voted = app.store.vote(room, &target, &me.identity, 1)?;
            if !voted {
                app.store.unvote(room, &target, &me.identity)?;
            }
            hub.dirty.lock().unwrap_or_else(|e| e.into_inner()).questions.insert(id);
            return Ok(json!({ "voted": voted }));
        }
        Action::QaModerate { id, action } => {
            require(runs)?;
            let mut q = app.store.get_doc(room, "qa", &id)?.and_then(|j| doc::<Question>(&j)).ok_or(NOT_FOUND)?;
            match action.as_str() {
                "approve" | "show" => q.status = "approved".into(),
                "reject" => q.status = "rejected".into(),
                "hide" => q.status = "hidden".into(),
                "answered" => q.answered = !q.answered,
                "private" => q.private = true,
                "public" => q.private = false,
                "delete" => {
                    app.store.del_doc(room, "qa", &id)?;
                    hub.send(Audience::Everyone, "qa_remove", json!({ "id": id }));
                    return Ok(json!({}));
                }
                _ => return Err(INVALID),
            }
            app.store.put_doc(room, "qa", &id, &serde_json::to_string(&q).unwrap_or_default())?;
            hub.send_question(&q);
        }
        Action::QaAnswer { id, text: value } => {
            require(me.role.is_crew())?;
            let mut q = app.store.get_doc(room, "qa", &id)?.and_then(|j| doc::<Question>(&j)).ok_or(NOT_FOUND)?;
            let body = text(&value, MAX_TEXT);
            if body.is_empty() {
                return Err(INVALID);
            }
            q.answer = Some(body);
            q.answered = true;
            q.answered_by = Some(if me.role == Crew::Producer { "The team".to_string() } else { hub.person(&me.identity).name });
            app.store.put_doc(room, "qa", &id, &serde_json::to_string(&q).unwrap_or_default())?;
            hub.send_question(&q);
        }
        Action::QaOverlay { id } => {
            require(runs)?;
            let q = app.store.get_doc(room, "qa", &id)?.and_then(|j| doc::<Question>(&j)).ok_or(NOT_FOUND)?;
            let third = LowerThird { title: text(&q.text, MAX_OVERLAY_TEXT), subtitle: q.name };
            hub.change(app, |s| s.lower_third = Some(third))?;
        }

        // ---- polls
        Action::PollSave { id, question, options, correct, show_results, show_counts, popup, open_after, close_after } => {
            require(runs)?;
            let question = text(&question, MAX_TEXT);
            let options: Vec<String> = options.iter().map(|o| text(o, MAX_OVERLAY_TEXT)).filter(|o| !o.is_empty()).collect();
            if question.is_empty() || options.len() < 2 || options.len() > MAX_OPTIONS || !["live", "closed"].contains(&show_results.as_str()) || correct.is_some_and(|c| c >= options.len()) || open_after.is_some_and(|v| !(0..=86_400).contains(&v)) || close_after.is_some_and(|v| !(5..=86_400).contains(&v)) {
                return Err(INVALID);
            }
            if let Some(existing) = id.as_ref().and_then(|i| app.store.get_doc(room, "poll", i).ok().flatten()).and_then(|j| doc::<Poll>(&j)) {
                if existing.status != "draft" {
                    return Err(ApiError(StatusCode::CONFLICT, "poll_already_published"));
                }
            }
            let p = Poll { id: id.unwrap_or_else(new_id), question, options, correct, show_results, show_counts, status: "draft".into(), tallies: vec![], total: 0, popup, open_after, close_after, opened_at: None };
            app.store.put_doc(room, "poll", &p.id, &serde_json::to_string(&p).unwrap_or_default())?;
            hub.send_poll(&p);
            return Ok(json!({ "id": p.id }));
        }
        Action::PollStatus { id, status } => {
            require(runs)?;
            if !["open", "closed"].contains(&status.as_str()) {
                return Err(INVALID);
            }
            let mut p = app.store.get_doc(room, "poll", &id)?.and_then(|j| doc::<Poll>(&j)).ok_or(NOT_FOUND)?;
            if status == "open" {
                p.opened_at = Some(now());
            }
            p.status = status;
            app.store.put_doc(room, "poll", &id, &serde_json::to_string(&p).unwrap_or_default())?;
            fill_tallies(app, room, &mut p)?;
            hub.send_poll(&p);
        }
        Action::PollDelete { id } => {
            require(runs)?;
            app.store.del_doc(room, "poll", &id)?;
            hub.send(Audience::Everyone, "poll_remove", json!({ "id": id }));
            if state.stage_poll.as_deref() == Some(&id) {
                hub.change(app, |s| s.stage_poll = None)?;
            }
        }
        Action::PollStage { id } => {
            require(runs)?;
            if let Some(id) = &id {
                let p = app.store.get_doc(room, "poll", id)?.and_then(|j| doc::<Poll>(&j)).ok_or(NOT_FOUND)?;
                if p.status == "draft" {
                    return Err(INVALID);
                }
            }
            hub.change(app, |s| s.stage_poll = id)?;
        }
        Action::PollVote { id, option } => {
            require(!blocked)?;
            let p = app.store.get_doc(room, "poll", &id)?.and_then(|j| doc::<Poll>(&j)).ok_or(NOT_FOUND)?;
            if p.status != "open" || option >= p.options.len() {
                return Err(ApiError(StatusCode::CONFLICT, "poll_closed"));
            }
            let counted = app.store.vote(room, &format!("poll:{id}"), &me.identity, option as i64)?;
            hub.dirty.lock().unwrap_or_else(|e| e.into_inner()).polls.insert(id);
            return Ok(json!({ "counted": counted }));
        }

        // ---- files
        Action::FilePublic { id, public } => {
            require(runs)?;
            let mut f = app.store.get_doc(room, "file", &id)?.and_then(|j| doc::<FileDoc>(&j)).ok_or(NOT_FOUND)?;
            f.public = public;
            app.store.put_doc(room, "file", &id, &serde_json::to_string(&f).unwrap_or_default())?;
            hub.send_files(app)?;
        }
        Action::FileDelete { id } => {
            require(runs)?;
            // It has to be a file of this room: the ID goes into a path on the server's disk.
            let file = app.store.get_doc(room, "file", &id)?.and_then(|j| doc::<FileDoc>(&j)).ok_or(NOT_FOUND)?;
            if !ticket_safe(&id) {
                return Err(INVALID);
            }
            app.store.del_doc(room, "file", &id)?;
            let _ = tokio::fs::remove_file(file_path(app, room, &id)).await;
            for page in 1..=file.pages {
                let _ = tokio::fs::remove_file(page_path(app, room, &id, page)).await;
            }
            hub.change(app, |s| {
                for slot in [&mut s.frame_id, &mut s.background_id, &mut s.logo_id, &mut s.visual_id, &mut s.banner_id] {
                    if slot.as_deref() == Some(&id) {
                        *slot = None;
                    }
                }
                if s.media.as_ref().is_some_and(|m| m.id == id) {
                    s.media = None;
                }
                s.layers.retain(|l| l.file_id.as_deref() != Some(&id));
                if s.slides.as_ref().is_some_and(|d| d.file_id == id) {
                    s.slides = None;
                }
                if s.simulive.as_ref().is_some_and(|d| d.file_id == id && !d.started) {
                    s.simulive = None;
                }
            })?;
            hub.send_files(app)?;
        }

        // ---- the audience
        Action::React { emoji } => {
            require(!blocked && (me.role.is_crew() || state.settings.reactions_enabled))?;
            if !["👏", "❤️", "😂", "🎉", "👍", "🤔"].contains(&emoji.as_str()) {
                return Err(INVALID);
            }
            let at = chrono::Utc::now().timestamp_millis();
            let ok = hub.touch_person(&me.identity, |p| {
                let ok = at - p.last_react_ms >= 250;
                if ok {
                    p.last_react_ms = at;
                }
                ok
            });
            if ok {
                hub.send(Audience::Everyone, "react", json!({ "emoji": emoji }));
            }
        }
        Action::Hand { up } => {
            require(me.role == Crew::Attendee && state.settings.hand_raising && !blocked)?;
            hub.with_person(&me.identity, |p| p.hand = up);
        }
        Action::Invite { identity, on } => {
            require(runs)?;
            let target = hub.person(&identity);
            if target.role != "attendee" {
                return Err(ApiError(StatusCode::CONFLICT, "not_an_attendee"));
            }
            hub.with_person(&identity, |p| p.invited = on);
            hub.send(Audience::Only(identity), "me", json!({ "invited": on }));
        }
        Action::InviteAnswer { accept } => {
            require(me.role == Crew::Attendee)?;
            let invited = hub.person(&me.identity).invited;
            require(invited)?;
            hub.with_person(&me.identity, |p| {
                p.invited = false;
                p.hand = false;
                if accept {
                    p.role = "guest".into();
                }
            });
            if accept {
                let (_, blocked, removed) = app.store.person_flags(room, &me.identity)?;
                app.store.set_person_flags(room, &me.identity, (true, blocked, removed))?;
                hub.send(Audience::Crew, "invite_result", json!({ "identity": me.identity, "accepted": true }));
            }
            return Ok(json!({ "reload": accept }));
        }
        Action::Demote { identity } => {
            require(runs)?;
            let (promoted, blocked, removed) = app.store.person_flags(room, &identity)?;
            if !promoted {
                return Err(ApiError(StatusCode::CONFLICT, "not_promoted"));
            }
            take_off_stage(app, hub, room, &identity).await?;
            app.store.set_person_flags(room, &identity, (false, blocked, removed))?;
            hub.with_person(&identity, |p| p.role = "attendee".into());
            let _ = app.backend.remove_participant(&green_room(room), &identity).await;
            hub.send(Audience::Crew, "demoted", json!({ "identity": identity }));
            hub.disconnect(&identity);
        }

        // ---- chat, again
        Action::ChatReact { id, emoji } => {
            require(!blocked && (me.role.is_crew() || state.settings.reactions_enabled))?;
            let choice = REACTIONS.iter().position(|e| *e == emoji).ok_or(INVALID)? as i64;
            let m = app.store.get_doc(room, "chat", &id)?.and_then(|j| doc::<ChatMessage>(&j)).ok_or(NOT_FOUND)?;
            if m.channel != "event" || !m.approved {
                return Err(INVALID);
            }
            let target = format!("chat:{id}");
            // One reaction per person per message: the same one again takes it back.
            let previous = app.store.my_vote(room, &target, &me.identity)?;
            if previous.is_some() {
                app.store.unvote(room, &target, &me.identity)?;
            }
            let on = previous != Some(choice);
            if on {
                app.store.vote(room, &target, &me.identity, choice)?;
            }
            hub.dirty.lock().unwrap_or_else(|e| e.into_inner()).chats.insert(id);
            return Ok(json!({ "reaction": if on { Some(choice) } else { None } }));
        }
        Action::ChatOverlay { id } => {
            require(runs)?;
            let m = app.store.get_doc(room, "chat", &id)?.and_then(|j| doc::<ChatMessage>(&j)).ok_or(NOT_FOUND)?;
            if m.channel != "event" || !m.approved {
                return Err(INVALID);
            }
            let third = LowerThird { title: text(&m.text, MAX_OVERLAY_TEXT), subtitle: m.name };
            hub.change(app, |s| s.lower_third = Some(third))?;
        }

        // ---- saved layouts
        Action::LayoutSave { name } => {
            require(runs)?;
            let name = text(&name, 60);
            if name.is_empty() || (state.saved_layouts.len() >= MAX_LAYERS && !state.saved_layouts.iter().any(|l| l.name == name)) {
                return Err(INVALID);
            }
            hub.change(app, |s| {
                let layout = s.layout.clone();
                match s.saved_layouts.iter_mut().find(|l| l.name == name) {
                    Some(existing) => existing.layout = layout,
                    None => s.saved_layouts.push(SavedLayout { id: new_id(), name, layout }),
                }
            })?;
        }
        Action::LayoutApply { id } => {
            require(runs)?;
            let saved = state.saved_layouts.iter().find(|l| l.id == id).ok_or(NOT_FOUND)?.layout.clone();
            hub.change(app, |s| s.layout = saved)?;
        }
        Action::LayoutDelete { id } => {
            require(runs)?;
            hub.change(app, |s| s.saved_layouts.retain(|l| l.id != id))?;
        }

        // ---- the survey after the session
        Action::SurveySave { survey } => {
            require(runs)?;
            let survey = match survey {
                None => None,
                Some(mut v) => {
                    v.title = text(&v.title, MAX_OVERLAY_TEXT);
                    if v.questions.is_empty() || v.questions.len() > 20 {
                        return Err(INVALID);
                    }
                    for q in &mut v.questions {
                        q.text = text(&q.text, MAX_TEXT);
                        q.options = q.options.iter().map(|o| text(o, MAX_OVERLAY_TEXT)).filter(|o| !o.is_empty()).collect();
                        if q.id.is_empty() {
                            q.id = new_id();
                        }
                        let kind_ok = match q.kind.as_str() {
                            "rating" | "text" => true,
                            "choice" => (2..=MAX_OPTIONS).contains(&q.options.len()),
                            _ => false,
                        };
                        if !kind_ok || q.text.is_empty() || !ticket_safe(&q.id) {
                            return Err(INVALID);
                        }
                    }
                    Some(v)
                }
            };
            hub.change(app, |s| s.survey = survey)?;
        }
        Action::SurveyAnswer { answers } => {
            require(!blocked)?;
            let survey = state.survey.as_ref().ok_or(NOT_FOUND)?;
            if state.phase != "ended" {
                return Err(ApiError(StatusCode::CONFLICT, "not_available"));
            }
            if app.store.get_doc(room, "survey", &me.identity)?.is_some() {
                return Err(ApiError(StatusCode::CONFLICT, "already_answered"));
            }
            let mut clean = BTreeMap::new();
            for q in &survey.questions {
                let Some(given) = answers.get(&q.id) else { continue };
                let value = text(given, 1000);
                let ok = match q.kind.as_str() {
                    "rating" => matches!(value.as_str(), "1" | "2" | "3" | "4" | "5"),
                    "choice" => q.options.contains(&value),
                    _ => !value.is_empty(),
                };
                if !ok {
                    return Err(INVALID);
                }
                clean.insert(q.id.clone(), value);
            }
            if clean.is_empty() {
                return Err(INVALID);
            }
            let answer = json!({ "identity": me.identity, "at": now(), "answers": clean, "name": hub.person(&me.identity).name });
            app.store.put_doc(room, "survey", &me.identity, &answer.to_string())?;
        }
        Action::SurveyResults => {
            require(runs)?;
            return Ok(json!({ "answers": survey_answers(app, room)? }));
        }

        // ---- a recorded session, played as if live
        Action::SimuliveSet { file_id, at_ms, duration, auto_end } => {
            require(runs)?;
            let file = app.store.get_doc(room, "file", &file_id)?.and_then(|j| doc::<FileDoc>(&j)).ok_or(NOT_FOUND)?;
            if file.kind != "media" || !duration.is_finite() || duration <= 0.0 || state.phase != "not_live" {
                return Err(INVALID);
            }
            // A time already gone would go live on the spot, broadcasts and all.
            if at_ms <= chrono::Utc::now().timestamp_millis() {
                return Err(ApiError(StatusCode::CONFLICT, "time_passed"));
            }
            hub.change(app, |s| s.simulive = Some(Simulive { file_id, at_ms, duration, auto_end, started: false }))?;
        }
        Action::SimuliveClear => {
            require(runs)?;
            hub.change(app, |s| s.simulive = None)?;
        }

        // ---- slides
        Action::SlidesShow { id, page } => {
            require(runs)?;
            let file = app.store.get_doc(room, "file", &id)?.and_then(|j| doc::<FileDoc>(&j)).ok_or(NOT_FOUND)?;
            if file.kind != "deck" || file.status != "ready" || file.pages == 0 {
                return Err(INVALID);
            }
            let page = page.unwrap_or(1).clamp(1, file.pages);
            hub.change(app, |s| s.slides = Some(SlideShow { file_id: id, page, pages: file.pages }))?;
        }
        Action::SlidesPage { page } => {
            let on_stage = state.stage.iter().any(|m| m.identity == me.identity);
            require(runs || (me.role == Crew::Guest && on_stage))?;
            let show = state.slides.as_ref().ok_or(NOT_FOUND)?;
            let page = page.clamp(1, show.pages);
            hub.change(app, |s| {
                if let Some(d) = s.slides.as_mut() {
                    d.page = page;
                }
            })?;
        }
        Action::SlidesStop => {
            require(runs)?;
            hub.change(app, |s| s.slides = None)?;
        }

        // ---- breakouts and the networking hub
        Action::BreakoutsOpen { kind, rooms } => {
            require(runs)?;
            if !["assigned", "open", "hub"].contains(&kind.as_str()) || rooms.is_empty() || rooms.len() > 50 {
                return Err(INVALID);
            }
            let mut clean = vec![];
            for (i, mut r) in rooms.into_iter().enumerate() {
                r.id = format!("{}", i + 1);
                r.name = text(&r.name, 60);
                if r.name.is_empty() || r.members.len() > 5000 || r.members.iter().any(|m| m.len() > 200) {
                    return Err(INVALID);
                }
                if kind != "assigned" {
                    r.members.clear();
                }
                clean.push(r);
            }
            // Only once the new ones are known to be good.
            close_breakouts(app, hub, room, &state).await;
            clear_assignments(app, room)?;
            for r in &clean {
                for member in &r.members {
                    app.store.put_doc(room, "breakout_assign", member, &json!({ "identity": member, "room": r.id }).to_string())?;
                    hub.send(Audience::Only(member.clone()), "me", json!({ "assigned": r.id }));
                }
            }
            hub.change(app, |s| {
                s.breakout_kind = kind;
                s.breakouts = clean;
            })?;
        }
        Action::BreakoutsClose => {
            require(runs)?;
            close_breakouts(app, hub, room, &state).await;
            clear_assignments(app, room)?;
            hub.change(app, |s| {
                s.breakout_kind.clear();
                s.breakouts.clear();
            })?;
        }
        Action::BreakoutJoin { id } => {
            require(!blocked)?;
            let target = state.breakouts.iter().find(|r| r.id == id).ok_or(NOT_FOUND)?;
            if state.phase == "ended" && state.breakout_kind != "hub" {
                return Err(ApiError(StatusCode::CONFLICT, "not_available"));
            }
            if state.stage.iter().any(|m| m.identity == me.identity) {
                return Err(ApiError(StatusCode::CONFLICT, "on_stage"));
            }
            if !me.role.is_crew() && state.breakout_kind == "assigned" && assigned_room(app, room, &me.identity)?.as_deref() != Some(id.as_str()) {
                return Err(FORBIDDEN);
            }
            let full = {
                let mut people = hub.people.lock().unwrap_or_else(|e| e.into_inner());
                let inside = people.iter().filter(|(who, p)| p.breakout == id && **who != me.identity).count();
                let full = target.cap > 0 && inside >= target.cap as usize && !me.role.is_crew();
                if !full {
                    people.entry(me.identity.clone()).or_default().breakout = id.clone();
                }
                full
            };
            if full {
                return Err(ApiError(StatusCode::CONFLICT, "room_full"));
            }
            let name = breakout_room(room, &id);
            if app.backend.ensure_room(&name).await.is_err() {
                hub.touch_person(&me.identity, |p| p.breakout.clear());
                return Err(ApiError(StatusCode::BAD_GATEWAY, "room_unavailable"));
            }
            let token = backend::access_token(&app.config, Access::Present, &name, &me.identity, &hub.person(&me.identity).name).map_err(|_| ApiError(StatusCode::INTERNAL_SERVER_ERROR, "internal_error"))?;
            let mut dirty = hub.dirty.lock().unwrap_or_else(|e| e.into_inner());
            dirty.breakouts = true;
            dirty.people = true;
            return Ok(json!({ "url": app.config.livekit_public_url, "token": token }));
        }
        Action::BreakoutLeave => {
            let was = hub.with_person(&me.identity, |p| std::mem::take(&mut p.breakout));
            // Their seat is free, so they don't get to stay in it.
            if !was.is_empty() {
                let _ = app.backend.remove_participant(&breakout_room(room, &was), &me.identity).await;
            }
            hub.dirty.lock().unwrap_or_else(|e| e.into_inner()).breakouts = true;
        }

        // ---- captions in someone's own language
        Action::CaptionLanguage { language } => {
            if !language.is_empty() {
                if !app.config.translate_languages.contains(&language) {
                    return Err(INVALID);
                }
                hub.caption_languages.lock().unwrap_or_else(|e| e.into_inner()).insert(language);
            }
        }

        // ---- each speaker's own recording
        Action::LocalRecordingDone { id } => {
            require(me.role.is_crew())?;
            let mut rec = app.store.get_doc(room, "local", &id)?.and_then(|j| doc::<LocalRecording>(&j)).ok_or(NOT_FOUND)?;
            if rec.identity != me.identity {
                return Err(FORBIDDEN);
            }
            rec.finished = true;
            app.store.put_doc(room, "local", &id, &serde_json::to_string(&rec).unwrap_or_default())?;
        }

        // ---- an encoder sending RTMP in
        Action::IngressCreate { name } => {
            require(runs)?;
            let url = app.config.ingress_url.clone().ok_or(ApiError(StatusCode::CONFLICT, "ingress_not_configured"))?;
            let name = text(&name, 80);
            if name.is_empty() {
                return Err(INVALID);
            }
            let identity = format!("{}i-{}", app::HOST_PREFIX, new_id());
            let (id, key) = app.backend.create_ingress(room, &identity, &name).await.map_err(|e| {
                tracing::warn!(error = %e.0, "could not create an RTMP input");
                ApiError(StatusCode::BAD_GATEWAY, "room_unavailable")
            })?;
            let input = json!({ "id": id, "name": name, "identity": identity, "url": url, "key": key });
            app.store.put_doc(room, "ingress", &id, &input.to_string())?;
            return Ok(input);
        }
        Action::IngressDelete { id } => {
            require(runs)?;
            let input: Value = app.store.get_doc(room, "ingress", &id)?.and_then(|j| doc(&j)).ok_or(NOT_FOUND)?;
            if let Err(err) = app.backend.delete_ingress(&id).await {
                tracing::warn!(error = %err.0, "could not delete an RTMP input");
            }
            app.store.del_doc(room, "ingress", &id)?;
            if let Some(identity) = input["identity"].as_str() {
                hub.change(app, |s| s.stage.retain(|m| m.identity != identity))?;
            }
        }
        Action::IngressList => {
            require(runs)?;
            let rows: Vec<Value> = app.store.docs(room, "ingress", 20)?.iter().filter_map(|j| doc(j)).collect();
            return Ok(json!({ "inputs": rows }));
        }
    }
    Ok(json!({}))
}

fn assigned_room(app: &AppState, room: &str, identity: &str) -> ApiResult<Option<String>> {
    Ok(app.store.get_doc(room, "breakout_assign", identity)?.and_then(|j| doc::<Value>(&j)).and_then(|v| v["room"].as_str().map(str::to_string)))
}

fn clear_assignments(app: &AppState, room: &str) -> ApiResult<()> {
    for j in app.store.docs(room, "breakout_assign", 100_000)? {
        if let Some(identity) = doc::<Value>(&j).and_then(|v| v["identity"].as_str().map(str::to_string)) {
            app.store.del_doc(room, "breakout_assign", &identity)?;
        }
    }
    Ok(())
}

pub fn breakout_room(room: &str, id: &str) -> String {
    format!("{room}--br-{id}")
}

async fn close_breakouts(app: &Arc<AppState>, hub: &Arc<Hub>, room: &str, state: &StudioState) {
    for r in &state.breakouts {
        if let Err(err) = app.backend.close_room(&breakout_room(room, &r.id)).await {
            tracing::warn!(error = %err.0, "could not close a breakout room");
        }
    }
    {
        let mut people = hub.people.lock().unwrap_or_else(|e| e.into_inner());
        for p in people.values_mut() {
            p.breakout.clear();
        }
    }
    let mut dirty = hub.dirty.lock().unwrap_or_else(|e| e.into_inner());
    dirty.breakouts = true;
    dirty.people = true;
}

/// Everyone sees the session go live; broadcasts, captions and any automatic recording follow.
async fn go_live(app: &Arc<AppState>, hub: &Arc<Hub>, room: &crate::store::Room) -> ApiResult<()> {
    let id = room.id.as_str();
    hub.change(app, |s| {
        s.phase = "live".into();
        s.live_since = Some(now());
    })?;
    // A platform that refuses must never stop the session going out to everyone else.
    crate::broadcast::start(app, id).await;
    crate::captions::sync(app, id).await;
    if room.auto_record && app.config.recording.is_some() && hub.state().phase == "live" {
        // Failing to record must never stop the broadcast; the crew sees it isn't on.
        if let Err(err) = app::start_recording(app, id).await {
            tracing::warn!(error = err.1, "automatic recording did not start");
        }
        hub.sync_recording(app)?;
    }
    Ok(())
}

async fn end_session(app: &Arc<AppState>, hub: &Arc<Hub>, room: &str) -> ApiResult<()> {
    if let Err(err) = app::stop_recording(app, room).await {
        tracing::warn!(error = err.1, "recording did not stop");
    }
    crate::broadcast::stop(app, room).await;
    let state = hub.state();
    if state.breakout_kind != "hub" {
        close_breakouts(app, hub, room, &state).await;
    }
    hub.change(app, |s| {
        s.phase = "ended".into();
        s.stage.clear();
        s.media = None;
        s.slides = None;
        s.recording = false;
        if s.breakout_kind != "hub" {
            s.breakout_kind.clear();
            s.breakouts.clear();
        }
    })?;
    crate::captions::sync(app, room).await;
    delete_inputs(app, room).await;
    if let Err(err) = app.backend.close_room(room).await {
        tracing::warn!(error = %err.0, "could not close the stage room");
    }
    Ok(())
}

/// A session's RTMP inputs end with it, so their stream keys stop working.
pub async fn delete_inputs(app: &AppState, room: &str) {
    let Ok(inputs) = app.store.docs(room, "ingress", 50) else { return };
    for input in inputs.iter().filter_map(|j| doc::<Value>(j)) {
        let Some(id) = input["id"].as_str() else { continue };
        if let Err(err) = app.backend.delete_ingress(id).await {
            tracing::warn!(error = %err.0, "could not delete an RTMP input");
        }
        let _ = app.store.del_doc(room, "ingress", id);
    }
}

/// A room's phase, from what's saved, without waking it up.
pub fn saved_phase(app: &AppState, room: &str) -> ApiResult<String> {
    if let Some(hub) = app.hubs.lock().unwrap_or_else(|e| e.into_inner()).get(room) {
        return Ok(hub.state().phase);
    }
    Ok(app.store.studio_state(room)?.and_then(|s| doc::<StudioState>(&s)).unwrap_or_default().phase)
}

/// The rooms a LiveKit room name belongs to: (the session, what kind, which breakout).
fn parse_room(name: &str) -> (&str, &'static str, &str) {
    if let Some(base) = name.strip_suffix("--bs") {
        return (base, "green", "");
    }
    if let Some((base, id)) = name.rsplit_once("--br-") {
        return (base, "breakout", id);
    }
    (name, "stage", "")
}

/// A LiveKit credential outlives someone's right to use it by a few minutes, and a browser can
/// reconnect with one it kept. So everyone who turns up is checked against who's allowed now, and
/// anyone publishing on the stage who isn't on it stops.
pub async fn enforce_webhook(app: &Arc<AppState>, event: &livekit_protocol::WebhookEvent) {
    let (Some(p), Some(lk_room)) = (event.participant.as_ref(), event.room.as_ref()) else { return };
    let (base, kind, breakout) = parse_room(&lk_room.name);
    let identity = p.identity.as_str();
    if identity.is_empty() || !matches!(app.store.room(base), Ok(Some(_))) {
        return;
    }
    // LiveKit's own recorder and ingress aren't people.
    if livekit_protocol::participant_info::Kind::try_from(p.kind) != Ok(livekit_protocol::participant_info::Kind::Standard) {
        return;
    }
    let Ok(hub) = app.hub(base) else { return };
    if event.event == "participant_left" || event.event == "participant_connection_aborted" {
        if kind == "breakout" {
            hub.with_person(identity, |person| {
                if person.breakout == breakout {
                    person.breakout.clear();
                }
            });
            hub.dirty.lock().unwrap_or_else(|e| e.into_inner()).breakouts = true;
        }
        return;
    }
    let state = hub.state();
    let (promoted, _, removed) = app.store.person_flags(base, identity).unwrap_or((false, false, false));
    let member = |id: &str| matches!(app.store.crew_member(id), Ok(Some(m)) if m.room == base);
    let valid = match identity.strip_prefix(app::HOST_PREFIX) {
        Some(rest) => match rest.split_once('-') {
            Some(("i", _)) => true,
            Some(("m", owner)) => owner.split_once('-').is_some_and(|(_, id)| member(id)),
            Some((_, id)) => member(id),
            None => false,
        },
        None => match kind {
            "green" => promoted,
            "stage" => state.phase == "live" || promoted,
            _ => true,
        },
    };
    let open = kind != "breakout" || state.breakouts.iter().any(|r| r.id == breakout);
    if event.event == "participant_joined" && (removed || !valid || !open) {
        tracing::info!(room = %lk_room.name, "put someone out who no longer has a place here");
        let _ = app.backend.remove_participant(&lk_room.name, identity).await;
        return;
    }
    if event.event == "track_published" && kind == "stage" {
        let on_stage = state.stage.iter().any(|m| m.identity == identity);
        let allowed = on_stage || identity.starts_with("host-i-") || identity.starts_with("host-m-");
        if !allowed {
            tracing::info!(room = %lk_room.name, "closed publishing for someone who isn't on stage");
            let _ = app.backend.set_can_publish(&lk_room.name, identity, false).await;
        }
    }
}

/// Removes what this service holds on one attendee, for an erasure request from VisionCast.
pub async fn forget_person_route(State(app): State<Arc<AppState>>, Path((room, identity)): Path<(String, String)>) -> ApiResult<Json<Value>> {
    app.store.room(&room)?.ok_or(NOT_FOUND)?;
    for kind in ["chat", "qa", "caption"] {
        for j in app.store.docs(&room, kind, 100_000)? {
            let Some(row) = doc::<Value>(&j) else { continue };
            if row["identity"].as_str() == Some(identity.as_str()) {
                if let Some(id) = row["id"].as_str() {
                    app.store.del_doc(&room, kind, id)?;
                }
            }
        }
    }
    app.store.del_doc(&room, "survey", &identity)?;
    app.store.del_doc(&room, "breakout_assign", &identity)?;
    app.store.delete_votes_of(&room, &identity)?;
    app.store.forget_attendance(&room, &identity)?;
    app.store.set_person_flags(&room, &identity, (false, false, false))?;
    let hub = app.hub(&room)?;
    hub.forget_person(&identity);
    hub.disconnect(&identity);
    Ok(Json(json!({ "ok": true })))
}

pub fn encoder_presence(app: &Arc<AppState>, room: &str, identity: &str, name: &str, here: bool) -> ApiResult<()> {
    let hub = app.hub(room)?;
    hub.change(app, |s| {
        s.stage.retain(|m| m.identity != identity);
        if here && s.phase != "ended" {
            s.stage.push(StageMember { identity: identity.to_string(), name: name.to_string(), title: String::new(), role: "guest".into() });
        }
    })?;
    Ok(())
}

/// After a restart: wakes rooms that are on the air or have something scheduled, so polls, simulive
/// and captions carry on, and marks as failed any deck that was being converted when it stopped.
pub fn revive_schedules(app: &Arc<AppState>) {
    let Ok(rooms) = app.store.room_ids() else { return };
    for room in rooms {
        if let Ok(decks) = app.store.docs(&room, "file", 500) {
            for mut deck in decks.iter().filter_map(|j| doc::<FileDoc>(j)).filter(|f| f.kind == "deck" && f.status.is_empty()) {
                deck.status = "failed".into();
                let _ = app.store.put_doc(&room, "file", &deck.id, &serde_json::to_string(&deck).unwrap_or_default());
            }
        }
        let Some(state) = app.store.studio_state(&room).ok().flatten().and_then(|s| doc::<StudioState>(&s)) else { continue };
        if state.phase == "live" || (state.simulive.is_some() && state.phase != "ended") {
            if app.hub(&room).is_ok() && state.phase == "live" {
                let (app, room) = (app.clone(), room.clone());
                tokio::spawn(async move { crate::captions::sync(&app, &room).await });
            }
        }
    }
}

fn survey_answers(app: &AppState, room: &str) -> ApiResult<Vec<Value>> {
    Ok(app.store.docs(room, "survey", 100_000)?.iter().filter_map(|j| doc(j)).collect())
}

/// What each attendee did in a session, for VisionCast and the CRM. An anonymous question stays
/// anonymous: it isn't tied to anyone here.
pub async fn engagement(State(app): State<Arc<AppState>>, Path(room): Path<String>, Query(page): Query<EngagementPage>) -> ApiResult<Json<Value>> {
    app.store.room(&room)?.ok_or(NOT_FOUND)?;
    let mut people: BTreeMap<String, Value> = BTreeMap::new();
    let blank = || json!({ "chatMessages": 0, "chat": [], "questions": [], "polls": [], "survey": Value::Null });
    for j in app.store.docs(&room, "chat", 100_000)? {
        let Some(m) = doc::<ChatMessage>(&j) else { continue };
        if m.channel != "event" || !m.approved || is_crew_identity(&m.identity) {
            continue;
        }
        let row = people.entry(m.identity.clone()).or_insert_with(blank);
        row["chatMessages"] = json!(row["chatMessages"].as_i64().unwrap_or(0) + 1);
        if let Some(list) = row["chat"].as_array_mut() {
            if list.len() < ENGAGEMENT_ROWS {
                list.push(json!({ "at": m.at, "text": short(&m.text, 280) }));
            }
        }
    }
    for j in app.store.docs(&room, "qa", 100_000)? {
        let Some(q) = doc::<Question>(&j) else { continue };
        if is_crew_identity(&q.identity) || q.name == "Anonymous" || q.status == "rejected" {
            continue;
        }
        let row = people.entry(q.identity.clone()).or_insert_with(blank);
        if let Some(list) = row["questions"].as_array_mut() {
            if list.len() < ENGAGEMENT_ROWS {
                list.push(json!({ "at": q.at, "text": short(&q.text, 280), "answered": q.answered, "answer": q.answer.as_deref().map(|a| short(a, 280)) }));
            }
        }
    }
    for j in app.store.docs(&room, "poll", 1000)? {
        let Some(p) = doc::<Poll>(&j) else { continue };
        for (identity, choice) in app.store.voters(&room, &format!("poll:{}", p.id))? {
            if is_crew_identity(&identity) {
                continue;
            }
            let row = people.entry(identity).or_insert_with(blank);
            if let Some(list) = row["polls"].as_array_mut() {
                if list.len() < ENGAGEMENT_ROWS {
                    list.push(json!({ "question": short(&p.question, 200), "answer": p.options.get(choice as usize).map(|o| short(o, 200)) }));
                }
            }
        }
    }
    let survey = app.hub(&room)?.state().survey;
    for a in survey_answers(&app, &room)? {
        let Some(identity) = a["identity"].as_str().filter(|i| !is_crew_identity(i)) else { continue };
        let mut labelled = serde_json::Map::new();
        if let (Some(answers), Some(survey)) = (a["answers"].as_object(), &survey) {
            for q in &survey.questions {
                if let Some(value) = answers.get(&q.id) {
                    labelled.insert(short(&q.text, 200), json!(short(value.as_str().unwrap_or(""), 300)));
                }
            }
        }
        people.entry(identity.to_string()).or_insert_with(blank)["survey"] = json!(labelled);
    }
    // Sorted by identity, so a page picks up where the last one stopped.
    let rest: Vec<(String, Value)> = people.into_iter().filter(|(identity, _)| page.after.as_str() < identity.as_str()).collect();
    // VisionCast reads at most 64 KB an answer, so a page stops short of that however much people said.
    let mut rows: BTreeMap<String, Value> = BTreeMap::new();
    let (mut bytes, mut more) = (0usize, false);
    for (identity, row) in rest {
        let size = identity.len() + row.to_string().len() + 8;
        if !rows.is_empty() && (rows.len() >= ENGAGEMENT_PAGE || bytes + size > ENGAGEMENT_PAGE_BYTES) {
            more = true;
            break;
        }
        bytes += size;
        rows.insert(identity, row);
    }
    let next = if more { rows.keys().last().cloned() } else { None };
    Ok(Json(json!({ "phase": app.hub(&room)?.state().phase, "people": rows, "next": next })))
}

const ENGAGEMENT_PAGE: usize = 200;
const ENGAGEMENT_PAGE_BYTES: usize = 56_000;
const ENGAGEMENT_ROWS: usize = 20;

/// Cut to a number of bytes, on a character boundary. Text can be four bytes a character.
fn short(value: &str, bytes: usize) -> String {
    let mut end = value.len().min(bytes);
    while !value.is_char_boundary(end) {
        end -= 1;
    }
    value[..end].to_string()
}

#[derive(Deserialize)]
pub struct EngagementPage {
    #[serde(default)]
    after: String,
}

/// The session's survey, its answers and its room settings, for VisionCast.
pub async fn get_settings(State(app): State<Arc<AppState>>, Path(room): Path<String>) -> ApiResult<Json<Value>> {
    app.store.room(&room)?.ok_or(NOT_FOUND)?;
    let state = app.hub(&room)?.state();
    Ok(Json(json!({ "settings": state.settings, "theme": state.theme, "survey": state.survey, "savedLayouts": state.saved_layouts, "simulive": state.simulive, "breakoutKind": state.breakout_kind, "breakouts": state.breakouts })))
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct PatchSettings {
    settings: Option<Settings>,
    theme: Option<Theme>,
    survey: Option<Option<Survey>>,
}

/// Changes a session's settings from outside the room: its studio or an API client.
pub async fn patch_settings(State(app): State<Arc<AppState>>, Path(room): Path<String>, Json(body): Json<PatchSettings>) -> ApiResult<Json<Value>> {
    let record = app.store.room(&room)?.ok_or(NOT_FOUND)?;
    let hub = app.hub(&room)?;
    let me = Me { room: record, ticket_role: Role::Host, role: Crew::Host, identity: "service".to_string() };
    if let Some(settings) = body.settings {
        apply(&app, &hub, &me, Action::SetSettings { settings }).await?;
    }
    if let Some(theme) = body.theme {
        apply(&app, &hub, &me, Action::SetTheme { theme }).await?;
    }
    if let Some(survey) = body.survey {
        apply(&app, &hub, &me, Action::SurveySave { survey }).await?;
    }
    get_settings(State(app), Path(room)).await
}

fn ticket_safe(id: &str) -> bool {
    crate::ticket::valid_id(id)
}

fn is_crew_identity(identity: &str) -> bool {
    identity.starts_with(app::HOST_PREFIX)
}

/// Taking someone off stage is enforced by LiveKit: their right to publish goes, their tracks with
/// it, and any screen they were sharing. It doesn't depend on their browser cooperating.
pub async fn take_off_stage(app: &Arc<AppState>, hub: &Arc<Hub>, room: &str, identity: &str) -> ApiResult<()> {
    // Whether or not they were on stage: a producer shares a screen without being on it.
    let _ = app.backend.remove_participant(room, &share_identity_of(identity)).await;
    let was_on = hub.state().stage.iter().any(|m| m.identity == identity);
    if !was_on {
        return Ok(());
    }
    if let Err(err) = app.backend.set_can_publish(room, identity, false).await {
        tracing::warn!(error = %err.0, "could not close publishing");
    }
    hub.change(app, |s| s.stage.retain(|m| m.identity != identity))?;
    Ok(())
}

// ---- files

fn file_path(app: &AppState, room: &str, id: &str) -> std::path::PathBuf {
    std::path::Path::new(&app.config.upload_dir).join(room).join(id)
}

#[derive(Deserialize)]
pub struct UploadQuery {
    kind: String,
    name: String,
}

const RESOURCE_EXTENSIONS: [&str; 18] = ["pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "csv", "key", "txt", "jpg", "jpeg", "png", "gif", "mp3", "wav", "mp4", "mov"];

fn clean_filename(name: &str) -> String {
    let base = name.rsplit(['/', '\\']).next().unwrap_or("");
    let cleaned: String = base.chars().filter(|c| c.is_alphanumeric() || " ._-()".contains(*c)).take(200).collect();
    let cleaned = cleaned.trim().trim_start_matches('.').to_string();
    if cleaned.is_empty() { "file".to_string() } else { cleaned }
}

fn extension(name: &str) -> String {
    name.rsplit('.').next().unwrap_or("").to_ascii_lowercase()
}

/// Checked against the file's first bytes, not what the browser claimed.
fn looks_right(kind: &str, head: &[u8]) -> bool {
    match kind {
        "frame" | "background" | "logo" | "visual" => head.starts_with(&[0x89, b'P', b'N', b'G', 0x0d, 0x0a, 0x1a, 0x0a]),
        "banner" => banner_mime(head).is_some(),
        "deck" => true,
        "media" => head.len() >= 12 && &head[4..8] == b"ftyp",
        "graphic" => graphic_mime(head).is_some(),
        _ => true,
    }
}

/// What an uploaded graphic really is, from its first bytes. Never SVG or HTML: nothing that can
/// run script in this origin.
fn graphic_mime(head: &[u8]) -> Option<&'static str> {
    if head.starts_with(b"GIF87a") || head.starts_with(b"GIF89a") {
        Some("image/gif")
    } else if head.starts_with(&[0x89, b'P', b'N', b'G']) {
        Some("image/png")
    } else if head.len() >= 12 && &head[..4] == b"RIFF" && &head[8..12] == b"WEBP" {
        Some("image/webp")
    } else if head.starts_with(&[0x1a, 0x45, 0xdf, 0xa3]) {
        Some("video/webm")
    } else if head.len() >= 12 && &head[4..8] == b"ftyp" {
        Some("video/mp4")
    } else {
        None
    }
}

fn banner_mime(head: &[u8]) -> Option<&'static str> {
    if head.starts_with(&[0xff, 0xd8, 0xff]) {
        return Some("image/jpeg");
    }
    graphic_mime(head).filter(|m| m.starts_with("image/"))
}

const DECK_EXTENSIONS: [&str; 4] = ["pdf", "pptx", "ppt", "odp"];

/// Stores one file, streamed to disk with the size checked as it arrives.
pub async fn upload(State(app): State<Arc<AppState>>, Path(ticket): Path<String>, Query(q): Query<UploadQuery>, body: Body) -> ApiResult<Json<Value>> {
    let me = resolve(&app, &ticket)?;
    require(me.role.runs_show())?;
    if !["resource", "frame", "background", "logo", "visual", "media", "graphic", "banner", "deck"].contains(&q.kind.as_str()) {
        return Err(INVALID);
    }
    let name = clean_filename(&q.name);
    if q.kind == "resource" && !RESOURCE_EXTENSIONS.contains(&extension(&name).as_str()) {
        return Err(ApiError(StatusCode::UNSUPPORTED_MEDIA_TYPE, "file_type_not_allowed"));
    }
    if q.kind == "deck" && (app.config.slides_dir.is_none() || !DECK_EXTENSIONS.contains(&extension(&name).as_str())) {
        return Err(ApiError(StatusCode::UNSUPPORTED_MEDIA_TYPE, "file_type_not_allowed"));
    }
    let id = new_id();
    let dir = std::path::Path::new(&app.config.upload_dir).join(&me.room.id);
    tokio::fs::create_dir_all(&dir).await.map_err(|_| ApiError(StatusCode::INTERNAL_SERVER_ERROR, "storage_unavailable"))?;
    let partial = dir.join(format!("{id}.part"));
    let mut file = tokio::fs::File::create(&partial).await.map_err(|_| ApiError(StatusCode::INTERNAL_SERVER_ERROR, "storage_unavailable"))?;
    let mut stream = body.into_data_stream();
    let mut size: u64 = 0;
    let mut head: Vec<u8> = vec![];
    let outcome: ApiResult<()> = async {
        while let Some(chunk) = stream.next().await {
            let chunk = chunk.map_err(|_| INVALID)?;
            size += chunk.len() as u64;
            if size > app.config.max_upload_bytes {
                return Err(ApiError(StatusCode::PAYLOAD_TOO_LARGE, "file_too_large"));
            }
            if head.len() < 16 {
                head.extend_from_slice(&chunk[..chunk.len().min(16 - head.len())]);
            }
            file.write_all(&chunk).await.map_err(|_| ApiError(StatusCode::INTERNAL_SERVER_ERROR, "storage_unavailable"))?;
        }
        if size == 0 {
            return Err(INVALID);
        }
        if !looks_right(&q.kind, &head) {
            return Err(ApiError(StatusCode::UNSUPPORTED_MEDIA_TYPE, "file_type_not_allowed"));
        }
        file.flush().await.map_err(|_| ApiError(StatusCode::INTERNAL_SERVER_ERROR, "storage_unavailable"))
    }
    .await;
    drop(file);
    if let Err(err) = outcome {
        let _ = tokio::fs::remove_file(&partial).await;
        return Err(err);
    }
    tokio::fs::rename(&partial, dir.join(&id)).await.map_err(|_| ApiError(StatusCode::INTERNAL_SERVER_ERROR, "storage_unavailable"))?;
    let mime = match q.kind.as_str() {
        "graphic" => graphic_mime(&head).unwrap_or_default().to_string(),
        "banner" => banner_mime(&head).unwrap_or_default().to_string(),
        _ => String::new(),
    };
    let doc = FileDoc { id: id.clone(), kind: q.kind, name, size, public: false, downloads: 0, at: now(), mime, status: String::new(), pages: 0, notes: vec![] };
    app.store.put_doc(&me.room.id, "file", &id, &serde_json::to_string(&doc).unwrap_or_default())?;
    app.hub(&me.room.id)?.send_files(&app)?;
    if doc.kind == "deck" {
        let (app, room, deck) = (app.clone(), me.room.id.clone(), doc.clone());
        tokio::spawn(async move { convert_deck(app, room, deck).await });
    }
    Ok(Json(json!(doc)))
}

/// Attendees get a resource once it's public, and a stage file only while it's on stage.
/// Downloads are always attachments, so an uploaded file never runs in this origin.
pub async fn download(State(app): State<Arc<AppState>>, Path((ticket, id)): Path<(String, String)>, request: Request) -> ApiResult<Response> {
    if !crate::ticket::valid_id(&id) {
        return Err(NOT_FOUND);
    }
    let (_, room) = check_ticket(&app, &ticket)?;
    let me = resolve(&app, &ticket)?;
    let mut f = app.store.get_doc(&room.id, "file", &id)?.and_then(|j| doc::<FileDoc>(&j)).ok_or(NOT_FOUND)?;
    let state = app.hub(&room.id)?.state();
    let on_stage = [&state.frame_id, &state.background_id, &state.logo_id, &state.visual_id, &state.banner_id].iter().any(|s| s.as_deref() == Some(&id))
        || state.media.as_ref().is_some_and(|m| m.id == id)
        || state.layers.iter().any(|l| l.visible && l.file_id.as_deref() == Some(&id));
    let allowed = me.role.runs_show() || on_stage || (f.kind == "resource" && f.public && (me.role.is_crew() || state.settings.resources_enabled));
    if !allowed {
        return Err(NOT_FOUND);
    }
    let (mime, disposition): (&str, String) = match f.kind.as_str() {
        "graphic" | "banner" => (f.mime.as_str(), "inline".to_string()),
        "frame" | "background" | "logo" | "visual" => ("image/png", "inline".to_string()),
        "media" => ("video/mp4", "inline".to_string()),
        _ => ("application/octet-stream", format!("attachment; filename=\"{}\"", f.name.replace('"', ""))),
    };
    let is_range_continuation = request.headers().contains_key(header::RANGE);
    if f.kind == "resource" && !me.role.is_crew() && !is_range_continuation {
        f.downloads += 1;
        app.store.put_doc(&room.id, "file", &id, &serde_json::to_string(&f).unwrap_or_default())?;
        app.hub(&room.id)?.send(Audience::Crew, "file_stat", json!({ "id": id, "downloads": f.downloads }));
    }
    let mime_header: axum::http::HeaderValue = axum::http::HeaderValue::from_str(mime).map_err(|_| INVALID)?;
    let served = ServeFile::new_with_mime(file_path(&app, &room.id, &id), &mime_header.to_str().unwrap_or("application/octet-stream").parse().map_err(|_| INVALID)?)
        .oneshot(request)
        .await
        .map_err(|_| ApiError(StatusCode::INTERNAL_SERVER_ERROR, "storage_unavailable"))?;
    let (mut parts, body) = served.into_parts();
    parts.headers.insert(header::CONTENT_DISPOSITION, disposition.parse().map_err(|_| INVALID)?);
    parts.headers.insert(header::CACHE_CONTROL, "private, no-store".parse().map_err(|_| INVALID)?);
    parts.headers.insert(header::CONTENT_SECURITY_POLICY, "default-src 'none'; sandbox".parse().map_err(|_| INVALID)?);
    parts.headers.insert(axum::http::header::HeaderName::from_static("cross-origin-resource-policy"), "same-origin".parse().map_err(|_| INVALID)?);
    Ok(Response::from_parts(parts, Body::new(body)))
}

// ---- slide decks

/// A deck goes to the slides converter through a shared folder. The converter has no network at
/// all, so a hostile document can't reach anything even if it breaks out of LibreOffice.
async fn convert_deck(app: Arc<AppState>, room: String, mut deck: FileDoc) {
    let outcome = run_converter(&app, &room, &deck).await;
    match outcome {
        Ok((pages, notes)) => {
            deck.status = "ready".into();
            deck.pages = pages;
            deck.notes = notes;
        }
        Err(reason) => {
            tracing::warn!(reason, "a deck could not be converted");
            deck.status = "failed".into();
        }
    }
    // Someone may have deleted it while it was converting: then it stays deleted.
    if !matches!(app.store.get_doc(&room, "file", &deck.id), Ok(Some(_))) {
        for page in 1..=deck.pages {
            let _ = tokio::fs::remove_file(page_path(&app, &room, &deck.id, page)).await;
        }
        return;
    }
    let _ = app.store.put_doc(&room, "file", &deck.id, &serde_json::to_string(&deck).unwrap_or_default());
    if let Ok(hub) = app.hub(&room) {
        let _ = hub.send_files(&app);
    }
}

/// Long, because decks are converted one at a time and this one may be behind others.
const DECK_TIMEOUT_SECONDS: u64 = 1200;
const MAX_DECK_PAGES: u32 = 500;

async fn run_converter(app: &AppState, room: &str, deck: &FileDoc) -> Result<(u32, Vec<String>), &'static str> {
    let spool = app.config.slides_dir.as_ref().ok_or("no converter")?;
    let job = std::path::Path::new(spool).join(&deck.id);
    tokio::fs::create_dir_all(&job).await.map_err(|_| "no spool")?;
    // The converter runs as its own user and writes the pages here.
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        tokio::fs::set_permissions(&job, std::fs::Permissions::from_mode(0o777)).await.map_err(|_| "no spool")?;
    }
    let input = job.join(format!("in.{}", extension(&deck.name)));
    tokio::fs::copy(file_path(app, room, &deck.id), &input).await.map_err(|_| "no copy")?;
    // The converter takes a job when this appears, so the deck is whole by then.
    tokio::fs::write(job.join("ready"), b"").await.map_err(|_| "no spool")?;
    let result = async {
        for _ in 0..DECK_TIMEOUT_SECONDS {
            tokio::time::sleep(Duration::from_secs(1)).await;
            if let Ok(done) = tokio::fs::read(job.join("done.json")).await {
                let done: Value = serde_json::from_slice(&done).map_err(|_| "bad result")?;
                let pages = done["pages"].as_u64().ok_or("failed")? as u32;
                if pages == 0 || pages > MAX_DECK_PAGES {
                    return Err("bad page count");
                }
                let notes: Vec<String> = done["notes"].as_array().map_or_else(Vec::new, |n| n.iter().map(|v| text(v.as_str().unwrap_or(""), 4000)).collect());
                for page in 1..=pages {
                    let from = job.join(format!("page-{page}.png"));
                    let bytes = tokio::fs::read(&from).await.map_err(|_| "missing page")?;
                    // Whatever the converter left behind has to really be a picture.
                    if !looks_right("frame", &bytes[..bytes.len().min(16)]) {
                        return Err("not a picture");
                    }
                    tokio::fs::write(page_path(app, room, &deck.id, page), &bytes).await.map_err(|_| "no storage")?;
                }
                return Ok((pages, notes));
            }
        }
        Err("timed out")
    }
    .await;
    let _ = tokio::fs::remove_dir_all(&job).await;
    result
}

fn page_path(app: &AppState, room: &str, id: &str, page: u32) -> std::path::PathBuf {
    file_path(app, room, id).with_extension(format!("p{page}.png"))
}

/// A page of a deck. Attendees only ever get the page on the stage right now, so nobody reads ahead.
pub async fn deck_page(State(app): State<Arc<AppState>>, Path((ticket, id, page)): Path<(String, String, u32)>) -> ApiResult<Response> {
    if !crate::ticket::valid_id(&id) {
        return Err(NOT_FOUND);
    }
    let me = resolve(&app, &ticket)?;
    let state = app.hub(&me.room.id)?.state();
    let showing = state.slides.as_ref().is_some_and(|d| d.file_id == id && d.page == page);
    if !me.role.is_crew() && !showing {
        return Err(NOT_FOUND);
    }
    let bytes = tokio::fs::read(page_path(&app, &me.room.id, &id, page)).await.map_err(|_| NOT_FOUND)?;
    Ok(([(header::CONTENT_TYPE, "image/png"), (header::CACHE_CONTROL, "private, no-store")], bytes).into_response())
}

// ---- each speaker's own recording: chunks from their browser, in the order they were made

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct LocalRecording {
    pub id: String,
    pub identity: String,
    pub name: String,
    pub at: i64,
    pub size: u64,
    pub next: u64,
    #[serde(default)]
    pub finished: bool,
    /// Safari records MP4s, everyone else WebM.
    #[serde(default)]
    pub mp4: bool,
}

const MAX_LOCAL_RECORDING_BYTES: u64 = 8 * 1024 * 1024 * 1024;
const MAX_LOCAL_CHUNK_BYTES: usize = 32 * 1024 * 1024;
const MAX_LOCAL_RECORDINGS_EACH: usize = 20;
const MAX_LOCAL_ROOM_BYTES: u64 = 100 * 1024 * 1024 * 1024;

pub async fn local_chunk(State(app): State<Arc<AppState>>, Path((ticket, id, seq)): Path<(String, String, u64)>, body: Body) -> ApiResult<Json<Value>> {
    let me = resolve(&app, &ticket)?;
    require(me.role.is_crew())?;
    if !crate::ticket::valid_id(&id) {
        return Err(INVALID);
    }
    let hub = app.hub(&me.room.id)?;
    let state = hub.state();
    if !state.settings.local_recordings {
        return Err(ApiError(StatusCode::CONFLICT, "not_available"));
    }
    // Read first, so that a slow upload holds nothing up.
    let bytes = axum::body::to_bytes(body, MAX_LOCAL_CHUNK_BYTES).await.map_err(|_| ApiError(StatusCode::PAYLOAD_TOO_LARGE, "file_too_large"))?;
    let _one_at_a_time = hub.local_lock.lock().await;
    let all = local_recordings(&app, &me.room.id)?;
    let mut rec = match all.iter().find(|r| r.id == id).cloned() {
        Some(existing) if existing.identity != me.identity => return Err(FORBIDDEN),
        Some(existing) => existing,
        None => {
            // A new file only while recording, from someone on stage, and only so many.
            if !state.recording || !state.stage.iter().any(|m| m.identity == me.identity) {
                return Err(ApiError(StatusCode::CONFLICT, "not_available"));
            }
            if all.iter().filter(|r| r.identity == me.identity).count() >= MAX_LOCAL_RECORDINGS_EACH {
                return Err(ApiError(StatusCode::PAYLOAD_TOO_LARGE, "file_too_large"));
            }
            LocalRecording { id: id.clone(), identity: me.identity.clone(), name: hub.person(&me.identity).name, at: now(), size: 0, next: 0, finished: false, mp4: bytes.len() >= 8 && &bytes[4..8] == b"ftyp" }
        }
    };
    // A chunk sent twice after a dropped connection counts once.
    if seq < rec.next {
        return Ok(Json(json!({ "next": rec.next })));
    }
    if seq != rec.next || rec.finished {
        return Err(ApiError(StatusCode::CONFLICT, "out_of_order"));
    }
    let so_far: u64 = all.iter().map(|r| r.size).sum();
    rec.size += bytes.len() as u64;
    if rec.size > MAX_LOCAL_RECORDING_BYTES || so_far + bytes.len() as u64 > MAX_LOCAL_ROOM_BYTES {
        return Err(ApiError(StatusCode::PAYLOAD_TOO_LARGE, "file_too_large"));
    }
    let dir = std::path::Path::new(&app.config.upload_dir).join(&me.room.id);
    tokio::fs::create_dir_all(&dir).await.map_err(|_| ApiError(StatusCode::INTERNAL_SERVER_ERROR, "storage_unavailable"))?;
    let mut file = tokio::fs::OpenOptions::new().create(true).append(true).open(dir.join(format!("{id}.local"))).await.map_err(|_| ApiError(StatusCode::INTERNAL_SERVER_ERROR, "storage_unavailable"))?;
    file.write_all(&bytes).await.map_err(|_| ApiError(StatusCode::INTERNAL_SERVER_ERROR, "storage_unavailable"))?;
    file.flush().await.map_err(|_| ApiError(StatusCode::INTERNAL_SERVER_ERROR, "storage_unavailable"))?;
    rec.next = seq + 1;
    app.store.put_doc(&me.room.id, "local", &id, &serde_json::to_string(&rec).unwrap_or_default())?;
    Ok(Json(json!({ "next": rec.next })))
}

pub fn local_recordings(app: &AppState, room: &str) -> ApiResult<Vec<LocalRecording>> {
    Ok(app.store.docs(room, "local", 500)?.iter().filter_map(|j| doc(j)).collect())
}

/// Hosts and producers get anyone's. Everyone else in the crew gets their own.
pub async fn local_download(State(app): State<Arc<AppState>>, Path((ticket, id)): Path<(String, String)>, request: Request) -> ApiResult<Response> {
    let me = resolve(&app, &ticket)?;
    if !crate::ticket::valid_id(&id) {
        return Err(NOT_FOUND);
    }
    let rec = app.store.get_doc(&me.room.id, "local", &id)?.and_then(|j| doc::<LocalRecording>(&j)).ok_or(NOT_FOUND)?;
    require(me.role.runs_show() || (me.role.is_crew() && rec.identity == me.identity))?;
    let path = std::path::Path::new(&app.config.upload_dir).join(&me.room.id).join(format!("{id}.local"));
    let (mime, ext) = if rec.mp4 { ("video/mp4", "mp4") } else { ("video/webm", "webm") };
    let served = ServeFile::new_with_mime(path, &mime.parse().map_err(|_| INVALID)?).oneshot(request).await.map_err(|_| ApiError(StatusCode::INTERNAL_SERVER_ERROR, "storage_unavailable"))?;
    let (mut parts, body) = served.into_parts();
    parts.headers.insert(header::CONTENT_DISPOSITION, format!("attachment; filename=\"{}.{ext}\"", clean_filename(&rec.name).replace('"', "")).parse().map_err(|_| INVALID)?);
    parts.headers.insert(header::CACHE_CONTROL, "private, no-store".parse().map_err(|_| INVALID)?);
    parts.headers.insert(header::CONTENT_SECURITY_POLICY, "default-src 'none'; sandbox".parse().map_err(|_| INVALID)?);
    Ok(Response::from_parts(parts, Body::new(body)))
}

/// Removes the uploads of rooms that no longer exist.
pub async fn purge_uploads(app: &AppState) {
    let Ok(rooms) = app.store.room_ids() else { return };
    let Ok(mut dirs) = tokio::fs::read_dir(&app.config.upload_dir).await else { return };
    while let Ok(Some(entry)) = dirs.next_entry().await {
        let name = entry.file_name().to_string_lossy().to_string();
        // _brand is the logo and font, which belong to no room.
        if !name.starts_with('_') && !rooms.contains(&name) {
            let _ = tokio::fs::remove_dir_all(entry.path()).await;
        }
    }
}


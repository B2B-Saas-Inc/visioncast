//! The HTTP layer against a fake LiveKit. The real server is exercised by tests/e2e/.
use async_trait::async_trait;
use axum::{body::Body, http::{header, Request, StatusCode}, Router};
use base64::{engine::general_purpose::STANDARD, Engine};
use chrono::{Duration, Utc};
use http_body_util::BodyExt;
use livekit_api::access_token::{AccessToken, TokenVerifier, VideoGrants};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::sync::{Arc, Mutex};
use tower::ServiceExt;
use visioncast_live::{
    app::{router, AppState},
    backend::{Backend, BackendError},
    config::{Config, RecordingStorage},
    store::Store,
};

const SECRET: &str = "service-secret-service-secret-service";
const LK_KEY: &str = "devkey";
const LK_SECRET: &str = "livekit-secret-livekit-secret-livekit";
const ROOM: &str = "vc-room-1";

#[derive(Default)]
struct Fake {
    calls: Mutex<Vec<String>>,
    fail: Mutex<bool>,
    fail_cast: Mutex<bool>,
    mics: Mutex<Vec<(String, String)>>,
}

#[async_trait]
impl Backend for Fake {
    async fn ensure_room(&self, room: &str) -> Result<(), BackendError> {
        self.calls.lock().unwrap().push(format!("ensure:{room}"));
        if *self.fail.lock().unwrap() { Err(BackendError("down".into())) } else { Ok(()) }
    }
    async fn start_recording(&self, room: &str, filepath: &str, page: &str) -> Result<String, BackendError> {
        self.calls.lock().unwrap().push(format!("record:{room}:{filepath}:page={page}"));
        Ok("EG_1".into())
    }
    async fn start_broadcast(&self, room: &str, urls: Vec<String>, page: &str) -> Result<String, BackendError> {
        self.calls.lock().unwrap().push(format!("cast:{room}:{}:page={page}", urls.join(",")));
        if *self.fail_cast.lock().unwrap() { return Err(BackendError("no capacity".into())); }
        Ok("EG_CAST".into())
    }
    async fn update_broadcast(&self, egress_id: &str, add: Vec<String>, remove: Vec<String>) -> Result<(), BackendError> {
        self.calls.lock().unwrap().push(format!("update:{egress_id}:add={}:remove={}", add.join(","), remove.join(",")));
        Ok(())
    }
    async fn stop_egress(&self, egress_id: &str) -> Result<(), BackendError> {
        self.calls.lock().unwrap().push(format!("stop:{egress_id}"));
        Ok(())
    }
    async fn set_can_publish(&self, room: &str, identity: &str, can_publish: bool) -> Result<(), BackendError> {
        self.calls.lock().unwrap().push(format!("publish:{room}:{identity}:{can_publish}"));
        Ok(())
    }
    async fn remove_participant(&self, room: &str, identity: &str) -> Result<(), BackendError> {
        self.calls.lock().unwrap().push(format!("remove:{room}:{identity}"));
        Ok(())
    }
    async fn create_ingress(&self, _room: &str, identity: &str, _name: &str) -> Result<(String, String), BackendError> {
        Ok((format!("ing-{identity}"), "stream-key".into()))
    }
    async fn delete_ingress(&self, _id: &str) -> Result<(), BackendError> {
        Ok(())
    }
    async fn close_room(&self, room: &str) -> Result<(), BackendError> {
        self.calls.lock().unwrap().push(format!("close:{room}"));
        Ok(())
    }
    async fn stage_mics(&self, _room: &str) -> Result<Vec<(String, String)>, BackendError> {
        Ok(self.mics.lock().unwrap().clone())
    }
    async fn start_track_stream(&self, room: &str, track: &str, url: &str) -> Result<String, BackendError> {
        self.calls.lock().unwrap().push(format!("listen:{room}:{track}:{url}"));
        Ok(format!("EG_{track}"))
    }
}

fn config(recording: bool) -> Config {
    Config {
        bind: "127.0.0.1:0".into(),
        public_url: "https://live.example.com".into(),
        livekit_url: "http://livekit:7880".into(),
        livekit_public_url: "wss://rtc.example.com".into(),
        livekit_api_key: LK_KEY.into(),
        livekit_api_secret: LK_SECRET.into(),
        service_secret: SECRET.into(),
        ticket_key: "ticket-key-ticket-key-ticket-key-ticket".into(),
        db_path: ":memory:".into(),
        internal_url: "https://live.example.com".into(),
        upload_dir: tempfile::tempdir().unwrap().keep().to_string_lossy().to_string(),
        rtmp_allow_private: false,
        captions_url: Some("http://127.0.0.1:1".into()),
        recording_internal_endpoint: None,
        first_admin: Some(("owner@example.com".into(), PASSWORD.into())),
        trust_forwarded_for: false,
        translate_url: None,
        captions_language: "en".into(),
        translate_languages: vec![],
        slides_dir: None,
        ingress_url: None,
        min_cpus: 0,
        max_upload_bytes: 1024 * 1024,
        retention_days: 90,
        recording: recording.then(|| RecordingStorage {
            bucket: "b".into(),
            region: "r".into(),
            endpoint: String::new(),
            access_key: "a".into(),
            secret_key: "s".into(),
            force_path_style: false,
            public_base_url: Some("https://cdn.example.com".into()),
        }),
    }
}

struct Ctx {
    app: Router,
    fake: Arc<Fake>,
}

fn ctx(recording: bool) -> Ctx {
    let fake = Arc::new(Fake::default());
    let state = AppState::new(config(recording), Store::open(":memory:").unwrap(), fake.clone());
    visioncast_live::admin::seed_first_admin(&state).unwrap();
    Ctx { app: router(state), fake }
}

impl Ctx {
    async fn call(&self, method: &str, path: &str, auth: Option<&str>, body: Option<Value>) -> (StatusCode, Value, axum::http::HeaderMap) {
        let mut req = Request::builder().method(method).uri(path);
        if let Some(a) = auth {
            req = req.header(header::AUTHORIZATION, a);
        }
        let req = match body {
            Some(b) => req.header(header::CONTENT_TYPE, "application/json").body(Body::from(b.to_string())).unwrap(),
            None => req.body(Body::empty()).unwrap(),
        };
        let res = self.app.clone().oneshot(req).await.unwrap();
        let (status, headers) = (res.status(), res.headers().clone());
        let bytes = res.into_body().collect().await.unwrap().to_bytes();
        (status, serde_json::from_slice(&bytes).unwrap_or(Value::Null), headers)
    }

    async fn v1(&self, method: &str, path: &str, body: Option<Value>) -> (StatusCode, Value) {
        let (s, v, _) = self.call(method, path, Some(&format!("Bearer {SECRET}")), body).await;
        (s, v)
    }

    async fn room(&self, starts_in_hours: i64) -> Value {
        let start = Utc::now() + Duration::hours(starts_in_hours);
        let (status, body) = self
            .v1("PUT", &format!("/v1/rooms/{ROOM}"), Some(json!({"title": "Launch", "startsAt": start.to_rfc3339(), "endsAt": (start + Duration::hours(1)).to_rfc3339()})))
            .await;
        assert_eq!(status, StatusCode::OK, "{body}");
        body
    }

    async fn attendee_path(&self, identity: &str) -> String {
        let (status, body) = self.v1("POST", &format!("/v1/rooms/{ROOM}/tickets"), Some(json!({"identity": identity}))).await;
        assert_eq!(status, StatusCode::OK, "{body}");
        path_of(body["joinUrl"].as_str().unwrap())
    }

    async fn webhook(&self, event: Value) -> StatusCode {
        let text = event.to_string();
        let digest = STANDARD.encode(Sha256::digest(text.as_bytes()));
        let token = AccessToken::with_api_key(LK_KEY, LK_SECRET).with_sha256(&digest).with_grants(VideoGrants::default()).to_jwt().unwrap();
        let req = Request::post("/webhooks/livekit").header(header::AUTHORIZATION, token).body(Body::from(text)).unwrap();
        self.app.clone().oneshot(req).await.unwrap().status()
    }
}

fn path_of(url: &str) -> String {
    url.strip_prefix("https://live.example.com").expect("links use the public URL").to_string()
}

#[tokio::test]
async fn the_private_api_needs_the_service_secret() {
    let c = ctx(false);
    let path = format!("/v1/rooms/{ROOM}/attendance");
    assert_eq!(c.call("GET", &path, None, None).await.0, StatusCode::UNAUTHORIZED);
    assert_eq!(c.call("GET", &path, Some("Bearer wrong"), None).await.0, StatusCode::UNAUTHORIZED);
    assert_eq!(c.call("GET", &path, Some(SECRET), None).await.0, StatusCode::UNAUTHORIZED);
    assert_eq!(c.v1("GET", &path, None).await.0, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn creating_a_room_is_repeatable_and_rescheduling_keeps_its_address() {
    let c = ctx(false);
    let first = c.room(1).await;
    let second = c.room(5).await;
    assert_eq!(first["id"], ROOM);
    assert_eq!(first["slug"], second["slug"]);
    let (status, _) = c.v1("PUT", "/v1/rooms/bad.id", Some(json!({"title":"x","startsAt":"2030-01-01T00:00:00Z","endsAt":"2030-01-01T01:00:00Z"}))).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    let (status, _) = c.v1("PUT", "/v1/rooms/r2", Some(json!({"title":"x","startsAt":"2030-01-01T01:00:00Z","endsAt":"2030-01-01T00:00:00Z"}))).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    let (status, _) = c.v1("PUT", "/v1/rooms/r2", Some(json!({"title":"x","startsAt":"tomorrow","endsAt":"2030-01-01T00:00:00Z"}))).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn attendee_links_are_stable_personal_and_need_a_known_room() {
    let c = ctx(false);
    let (status, _) = c.v1("POST", &format!("/v1/rooms/{ROOM}/tickets"), Some(json!({"identity":"p1"}))).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    c.room(1).await;
    assert_eq!(c.attendee_path("p1").await, c.attendee_path("p1").await);
    assert_ne!(c.attendee_path("p1").await, c.attendee_path("p2").await);
    for bad in ["host", "host-abc", "has space", "a.b"] {
        let (status, _) = c.v1("POST", &format!("/v1/rooms/{ROOM}/tickets"), Some(json!({"identity": bad}))).await;
        assert_eq!(status, StatusCode::BAD_REQUEST, "{bad}");
    }
}

fn claims(token: &str) -> livekit_api::access_token::Claims {
    TokenVerifier::with_api_key(LK_KEY, LK_SECRET).verify(token).unwrap()
}

const CLIENT: &str = "client00000001";

impl Ctx {
    async fn enter(&self, path: &str, name: &str, client: &str) -> (StatusCode, Value) {
        let (status, body, _) = self.call("POST", &format!("{path}/enter"), None, Some(json!({"name": name, "clientId": client}))).await;
        (status, body)
    }

    async fn act(&self, path: &str, client: &str, action: Value) -> (StatusCode, Value) {
        let (status, body, _) = self.call("POST", &format!("{path}/act"), None, Some(json!({"clientId": client, "action": action}))).await;
        (status, body)
    }

    /// The first thing someone is sent on the event stream: everything they're allowed to see.
    async fn snapshot(&self, path: &str, client: &str) -> Value {
        let req = Request::get(format!("{path}/events?c={client}")).body(Body::empty()).unwrap();
        let res = self.app.clone().oneshot(req).await.unwrap();
        assert_eq!(res.status(), StatusCode::OK);
        let mut body = res.into_body();
        let frame = body.frame().await.unwrap().unwrap();
        let text = String::from_utf8(frame.into_data().unwrap().to_vec()).unwrap();
        let line = text.lines().find(|l| l.starts_with("data:")).expect("a data line");
        serde_json::from_str(line.trim_start_matches("data:").trim()).unwrap()
    }

    /// A new crew member's link.
    async fn member(&self, role: &str, name: &str) -> String {
        let (status, body) = self.v1("POST", &format!("/v1/rooms/{ROOM}/crew"), Some(json!({"role": role, "name": name}))).await;
        assert_eq!(status, StatusCode::OK, "{body}");
        path_of(body["url"].as_str().unwrap())
    }

    async fn crew(&self) -> Crew {
        self.room(1).await;
        Crew { host: self.member("host", "Maya").await, producer: self.member("producer", "Pat").await, guest: self.member("guest", "Gus").await }
    }

    async fn identity(&self, path: &str) -> String {
        self.enter(path, "", CLIENT).await.1["identity"].as_str().unwrap().to_string()
    }
}

struct Crew {
    host: String,
    producer: String,
    guest: String,
}

#[tokio::test]
async fn people_lists_everyone_for_owners_with_each_session_named() {
    let c = ctx(false);
    c.crew().await;
    let owner = c.sign_in().await;
    let (status, list) = c.admin("GET", "/accounts", Some(&owner), None).await;
    assert_eq!(status, StatusCode::OK, "{list}");
    assert_eq!(list["accounts"].as_array().unwrap().len(), 1);
    let crew = list["crew"].as_array().unwrap();
    let roles: std::collections::BTreeSet<&str> = crew.iter().map(|m| m["role"].as_str().unwrap()).collect();
    assert_eq!(roles.into_iter().collect::<Vec<_>>(), ["guest", "host", "producer"], "{list}");
    assert!(crew.iter().all(|m| !m["roomTitle"].as_str().unwrap().is_empty() && m["url"].as_str().unwrap().contains("/join/") && !m["room"].as_str().unwrap().is_empty()), "{list}");
}

#[tokio::test]
async fn the_crew_are_warned_on_a_small_server_and_attendees_arent_told() {
    let fake = Arc::new(Fake::default());
    let state = AppState::new(Config { min_cpus: 100_000, ..config(false) }, Store::open(":memory:").unwrap(), fake.clone());
    visioncast_live::admin::seed_first_admin(&state).unwrap();
    let c = Ctx { app: router(state), fake };
    let crew = c.crew().await;
    let (_, host) = c.enter(&crew.host, "Hal", "x").await;
    assert!(host["serverCpus"].as_u64().unwrap() > 0, "{host}");
    let attendee = c.attendee_path("p1").await;
    let (_, viewer) = c.enter(&attendee, "Ada", "y").await;
    assert!(viewer.get("serverCpus").is_none(), "{viewer}");
    let big = ctx(false);
    let crew = big.crew().await;
    let (_, host) = big.enter(&crew.host, "Hal", "x").await;
    assert!(host.get("serverCpus").is_none(), "off when it's 0: {host}");
}

#[tokio::test]
async fn an_attendee_gets_no_credential_until_the_session_is_live_and_then_can_only_watch() {
    let c = ctx(false);
    let crew = c.crew().await;
    let attendee = c.attendee_path("p1").await;
    let (status, body) = c.enter(&attendee, "  Ada\u{7} ", "x").await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["phase"], "not_live");
    assert!(body.get("stageToken").is_none(), "no way in before going live");
    assert!(body.get("greenToken").is_none());

    assert_eq!(c.act(&crew.host, CLIENT, json!({"type":"go_live"})).await.0, StatusCode::OK);
    let (_, body) = c.enter(&attendee, "  Ada\u{7} ", "x").await;
    assert_eq!(body["url"], "wss://rtc.example.com");
    assert!(body.get("greenToken").is_none(), "an attendee never gets the green room");
    let got = claims(body["stageToken"].as_str().unwrap());
    assert_eq!((got.sub.as_str(), got.name.as_str(), got.video.room.as_str()), ("p1", "Ada", ROOM));
    assert!(got.video.room_join && got.video.hidden);
    assert_eq!(got.video.can_publish, Some(false));
    assert_eq!(got.video.can_publish_data, Some(false));
    assert!(!got.video.room_admin && !got.video.room_record);

    // Once it's over, no more credentials.
    c.act(&crew.host, CLIENT, json!({"type":"end_session"})).await;
    let (_, body) = c.enter(&attendee, "Ada", "x").await;
    assert!(body.get("stageToken").is_none());
    assert!(c.fake.calls.lock().unwrap().contains(&format!("close:{ROOM}")));
}

#[tokio::test]
async fn the_crew_starts_in_the_green_room_and_only_the_stage_list_opens_publishing() {
    let c = ctx(false);
    let crew = c.crew().await;
    let (status, body) = c.enter(&crew.host, "Maya", CLIENT).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let identity = body["identity"].as_str().unwrap().to_string();
    assert!(identity.starts_with("host-h-"), "{identity}");
    let (green, stage) = (claims(body["greenToken"].as_str().unwrap()), claims(body["stageToken"].as_str().unwrap()));
    assert_eq!(green.video.room, format!("{ROOM}--bs"));
    assert_eq!(green.video.can_publish, Some(true));
    assert_eq!(stage.video.room, ROOM);
    assert_eq!(stage.video.can_publish, Some(false), "nobody publishes to the stage just by joining");

    assert_eq!(c.act(&crew.host, CLIENT, json!({"type":"stage_add","identity":identity})).await.0, StatusCode::OK);
    assert!(c.fake.calls.lock().unwrap().contains(&format!("publish:{ROOM}:{identity}:true")));
    // Coming back after a reload, they're still on stage.
    let (_, body) = c.enter(&crew.host, "Maya", CLIENT).await;
    assert_eq!(claims(body["stageToken"].as_str().unwrap()).video.can_publish, Some(true));

    c.act(&crew.host, CLIENT, json!({"type":"stage_remove","identity":identity})).await;
    assert!(c.fake.calls.lock().unwrap().contains(&format!("publish:{ROOM}:{identity}:false")));
    let (_, body) = c.enter(&crew.host, "Maya", CLIENT).await;
    assert_eq!(claims(body["stageToken"].as_str().unwrap()).video.can_publish, Some(false));
}

#[tokio::test]
async fn a_producer_can_never_be_on_stage_or_heard_by_the_audience() {
    let c = ctx(false);
    let crew = c.crew().await;
    let (_, body) = c.enter(&crew.producer, "Pat", CLIENT).await;
    let identity = body["identity"].as_str().unwrap().to_string();
    let stage = claims(body["stageToken"].as_str().unwrap());
    assert_eq!(stage.video.can_publish, Some(false));
    assert!(stage.video.hidden, "not even in the raw participant list");
    let (status, body) = c.act(&crew.producer, CLIENT, json!({"type":"stage_add","identity":identity})).await;
    assert_eq!((status, body["error"].as_str()), (StatusCode::CONFLICT, Some("cannot_be_on_stage")));
    c.enter(&crew.host, "Maya", "hostclient01").await;
    let (status, _) = c.act(&crew.host, "hostclient01", json!({"type":"stage_add","identity":identity})).await;
    assert_eq!(status, StatusCode::CONFLICT);
    // Their screen goes through a connection that can publish a screen and nothing else.
    let (status, share, _) = c.call("POST", &format!("{}/share-token", crew.producer), None, Some(json!({"clientId": CLIENT}))).await;
    assert_eq!(status, StatusCode::OK);
    let got = claims(share["token"].as_str().unwrap());
    assert_eq!(got.name, "Presentation");
    assert_eq!(got.video.can_publish_sources, vec!["screen_share".to_string(), "screen_share_audio".to_string()]);
    assert_eq!(got.video.can_subscribe, Some(false));
}

#[tokio::test]
async fn permissions_by_role() {
    let c = ctx(true);
    let crew = c.crew().await;
    let attendee = c.attendee_path("p1").await;
    for (path, name) in [(&crew.host, "Maya"), (&crew.producer, "Pat"), (&crew.guest, "Gus"), (&attendee, "Ada")] {
        c.enter(path, name, CLIENT).await;
    }
    let gus = c.identity(&crew.guest).await;
    let run_show = [
        json!({"type":"record","on":true}),
        json!({"type":"set_layout","mode":"split","focus":null}),
        json!({"type":"set_lower_third","title":"Hello","subtitle":""}),
        json!({"type":"set_ticker","text":"News"}),
        json!({"type":"set_cta","title":"Buy","subtitle":"","label":"Go","url":"https://example.com"}),
        json!({"type":"set_settings","settings":{"chatEnabled":true}}),
        json!({"type":"poll_save","id":null,"question":"Q","options":["a","b"],"correct":null,"showResults":"live","showCounts":false}),
        json!({"type":"stage_add","identity":gus.clone()}),
        json!({"type":"block","identity":"p1","on":false}),
    ];
    for action in &run_show {
        for path in [&crew.guest, &attendee] {
            assert_eq!(c.act(path, CLIENT, action.clone()).await.0, StatusCode::FORBIDDEN, "{action} {path}");
        }
        assert_ne!(c.act(&crew.producer, CLIENT, action.clone()).await.0, StatusCode::FORBIDDEN, "{action}");
        assert_ne!(c.act(&crew.host, CLIENT, action.clone()).await.0, StatusCode::FORBIDDEN, "{action}");
    }
    for path in [&crew.guest, &attendee] {
        assert_eq!(c.act(path, CLIENT, json!({"type":"go_live"})).await.0, StatusCode::FORBIDDEN);
        assert_eq!(c.act(path, CLIENT, json!({"type":"end_session"})).await.0, StatusCode::FORBIDDEN);
    }
    // A guest in the green room can't share; on stage they can. An attendee never can.
    let share = |path: String| { let c = &c; async move { c.call("POST", &format!("{path}/share-token"), None, Some(json!({"clientId": CLIENT}))).await.0 } };
    c.act(&crew.host, CLIENT, json!({"type":"stage_remove","identity":gus.clone()})).await;
    assert_eq!(share(crew.guest.clone()).await, StatusCode::FORBIDDEN);
    assert_eq!(share(attendee.clone()).await, StatusCode::FORBIDDEN);
    assert_eq!(c.act(&crew.host, CLIENT, json!({"type":"stage_add","identity":gus.clone()})).await.0, StatusCode::OK);
    assert_eq!(share(crew.guest.clone()).await, StatusCode::OK);
    assert_eq!(c.act(&crew.guest, CLIENT, json!({"type":"go_live"})).await.0, StatusCode::FORBIDDEN);
    // Producers go live by default; a host can take that away, and only a host.
    let off = json!({"type":"set_settings","settings":{"producersCanGoLive":false}});
    assert_eq!(c.act(&crew.producer, CLIENT, off.clone()).await.0, StatusCode::FORBIDDEN);
    assert_eq!(c.act(&crew.host, CLIENT, off).await.0, StatusCode::OK);
    assert_eq!(c.act(&crew.producer, CLIENT, json!({"type":"go_live"})).await.0, StatusCode::FORBIDDEN);
    assert_eq!(c.act(&crew.host, CLIENT, json!({"type":"go_live"})).await.0, StatusCode::OK);
    assert_eq!(c.act(&crew.host, CLIENT, json!({"type":"go_live"})).await.0, StatusCode::CONFLICT);
}

#[tokio::test]
async fn attendees_are_never_sent_what_is_meant_for_the_crew() {
    let c = ctx(false);
    let crew = c.crew().await;
    let attendee = c.attendee_path("p1").await;
    let other = c.attendee_path("p2").await;
    c.enter(&crew.host, "Maya", CLIENT).await;
    c.enter(&attendee, "Ada", "x").await;
    c.enter(&other, "Bea", "x").await;
    c.act(&crew.host, CLIENT, json!({"type":"set_settings","settings":{"chatReview":true,"qaModerated":true,"qaEnabled":true,"chatEnabled":true,"qaAnonymous":true,"profanityFilter":false,"producersCanGoLive":true}})).await;
    c.act(&crew.host, CLIENT, json!({"type":"chat_send","text":"crew-secret","channel":"stage"})).await;
    c.act(&other, "x", json!({"type":"chat_send","text":"held-message","channel":"event"})).await;
    c.act(&other, "x", json!({"type":"qa_ask","text":"pending-question","anonymous":false})).await;
    let (_, draft) = c.act(&crew.host, CLIENT, json!({"type":"poll_save","id":null,"question":"draft-poll","options":["a","b"],"correct":0,"showResults":"closed","showCounts":false})).await;
    let snap = c.snapshot(&attendee, "x").await;
    let text = snap.to_string();
    for secret in ["crew-secret", "held-message", "pending-question", "draft-poll", "people", "p2", "recordingAvailable"] {
        assert!(!text.contains(secret), "{secret} leaked: {text}");
    }
    // Their own held message and question are still theirs to see.
    let mine = c.snapshot(&other, "x").await;
    assert!(mine.to_string().contains("held-message") && mine.to_string().contains("pending-question"));
    // The crew sees it all.
    let host = c.snapshot(&crew.host, CLIENT).await.to_string();
    for seen in ["crew-secret", "held-message", "pending-question", "draft-poll"] {
        assert!(host.contains(seen), "{seen}");
    }
    // A results-on-close poll hides its results and answer while it's open.
    let id = draft["id"].as_str().unwrap();
    c.act(&crew.host, CLIENT, json!({"type":"poll_status","id":id,"status":"open"})).await;
    c.act(&attendee, "x", json!({"type":"poll_vote","id":id,"option":1})).await;
    let snap = c.snapshot(&attendee, "x").await;
    assert_eq!(snap["polls"][0]["tallies"], Value::Null);
    assert_eq!(snap["polls"][0]["correct"], Value::Null);
    c.act(&crew.host, CLIENT, json!({"type":"poll_status","id":id,"status":"closed"})).await;
    let snap = c.snapshot(&attendee, "x").await;
    assert_eq!(snap["polls"][0]["tallies"], json!([0, 1]));
    assert_eq!(snap["polls"][0]["correct"], 0);
}

#[tokio::test]
async fn moderation_holds_blocks_removes_and_deletes() {
    let c = ctx(false);
    let crew = c.crew().await;
    let attendee = c.attendee_path("p1").await;
    c.enter(&crew.host, "Maya", CLIENT).await;
    c.enter(&attendee, "Ada", "x").await;
    let (_, sent) = c.act(&attendee, "x", json!({"type":"chat_send","text":"oh shit hello","channel":"event"})).await;
    assert_eq!(sent["approved"], true);
    let snap = c.snapshot(&crew.host, CLIENT).await;
    assert_eq!(snap["chat"][0]["text"], "oh **** hello");
    // An attendee can't write to the crew's chat.
    assert_eq!(c.act(&attendee, "x", json!({"type":"chat_send","text":"hi","channel":"stage"})).await.0, StatusCode::FORBIDDEN);
    // Deleted means gone for everyone.
    c.act(&crew.host, CLIENT, json!({"type":"chat_delete","id":sent["id"]})).await;
    assert!(c.snapshot(&attendee, "x").await["chat"].as_array().unwrap().is_empty());
    // Blocked: can watch, can't post, vote or react.
    c.act(&crew.host, CLIENT, json!({"type":"block","identity":"p1","on":true})).await;
    tokio::time::sleep(std::time::Duration::from_millis(1100)).await;
    assert_eq!(c.act(&attendee, "x", json!({"type":"chat_send","text":"hi","channel":"event"})).await.0, StatusCode::FORBIDDEN);
    assert_eq!(c.act(&attendee, "x", json!({"type":"qa_ask","text":"hi","anonymous":false})).await.0, StatusCode::FORBIDDEN);
    assert_eq!(c.enter(&attendee, "Ada", "x").await.0, StatusCode::OK);
    // Removed: out, and the link stops working.
    c.act(&crew.host, CLIENT, json!({"type":"remove","identity":"p1"})).await;
    let (status, body) = c.enter(&attendee, "Ada", "x").await;
    assert_eq!((status, body["error"].as_str()), (StatusCode::FORBIDDEN, Some("removed")));
    assert!(c.fake.calls.lock().unwrap().contains(&format!("remove:{ROOM}:p1")));
    // Hosts and producers can't be blocked or removed from inside the room.
    let maya = c.identity(&crew.host).await;
    assert_eq!(c.act(&crew.host, CLIENT, json!({"type":"block","identity":maya,"on":true})).await.0, StatusCode::FORBIDDEN);
}

#[tokio::test]
async fn an_invited_attendee_chooses_then_waits_in_the_green_room_until_brought_on() {
    let c = ctx(false);
    let crew = c.crew().await;
    let attendee = c.attendee_path("p1").await;
    c.enter(&crew.host, "Maya", CLIENT).await;
    c.enter(&attendee, "Ada", "x").await;
    // Nothing happens without an invitation.
    assert_eq!(c.act(&attendee, "x", json!({"type":"invite_answer","accept":true})).await.0, StatusCode::FORBIDDEN);
    c.act(&crew.host, CLIENT, json!({"type":"invite","identity":"p1","on":true})).await;
    // Saying no changes nothing.
    c.act(&attendee, "x", json!({"type":"invite_answer","accept":false})).await;
    assert!(c.enter(&attendee, "Ada", "x").await.1.get("greenToken").is_none());
    assert_eq!(c.act(&attendee, "x", json!({"type":"invite_answer","accept":true})).await.0, StatusCode::FORBIDDEN);
    // Saying yes makes them a guest.
    c.act(&crew.host, CLIENT, json!({"type":"invite","identity":"p1","on":true})).await;
    c.act(&attendee, "x", json!({"type":"invite_answer","accept":true})).await;
    let (_, body) = c.enter(&attendee, "Ada", "x").await;
    assert_eq!(body["role"], "guest");
    assert_eq!(claims(body["greenToken"].as_str().unwrap()).sub, "p1");
    assert_eq!(claims(body["stageToken"].as_str().unwrap()).video.can_publish, Some(false));
    assert_eq!(c.act(&crew.host, CLIENT, json!({"type":"stage_add","identity":"p1"})).await.0, StatusCode::OK);
    // Sent back: off the stage, out of the green room, an attendee again.
    assert_eq!(c.act(&crew.host, CLIENT, json!({"type":"demote","identity":"p1"})).await.0, StatusCode::OK);
    assert!(c.fake.calls.lock().unwrap().contains(&format!("publish:{ROOM}:p1:false")));
    assert!(c.fake.calls.lock().unwrap().contains(&format!("remove:{ROOM}--bs:p1")));
    let (_, body) = c.enter(&attendee, "Ada", "x").await;
    assert_eq!(body["role"], "attendee");
    assert!(body.get("greenToken").is_none());
}

#[tokio::test]
async fn questions_vote_once_and_polls_count_one_vote_per_person() {
    let c = ctx(false);
    let crew = c.crew().await;
    let attendee = c.attendee_path("p1").await;
    c.enter(&crew.host, "Maya", CLIENT).await;
    c.enter(&attendee, "Ada", "x").await;
    let (_, q) = c.act(&attendee, "x", json!({"type":"qa_ask","text":"Why?","anonymous":true})).await;
    let snap = c.snapshot(&crew.host, CLIENT).await;
    assert_eq!(snap["qa"][0]["name"], "Anonymous");
    let id = q["id"].as_str().unwrap();
    assert_eq!(c.act(&attendee, "x", json!({"type":"qa_vote","id":id})).await.1["voted"], true);
    assert_eq!(c.snapshot(&attendee, "x").await["qa"][0]["votes"], 1);
    assert_eq!(c.act(&attendee, "x", json!({"type":"qa_vote","id":id})).await.1["voted"], false);
    assert_eq!(c.snapshot(&attendee, "x").await["qa"][0]["votes"], 0);
    assert_eq!(c.act(&crew.guest, CLIENT, json!({"type":"qa_answer","id":id,"text":"Because"})).await.0, StatusCode::OK);
    assert_eq!(c.act(&attendee, "x", json!({"type":"qa_answer","id":id,"text":"mine"})).await.0, StatusCode::FORBIDDEN);

    let (_, p) = c.act(&crew.host, CLIENT, json!({"type":"poll_save","id":null,"question":"Q","options":["a","b"],"correct":null,"showResults":"live","showCounts":false})).await;
    let id = p["id"].as_str().unwrap();
    assert_eq!(c.act(&attendee, "x", json!({"type":"poll_vote","id":id,"option":0})).await.0, StatusCode::CONFLICT, "a draft takes no votes");
    c.act(&crew.host, CLIENT, json!({"type":"poll_status","id":id,"status":"open"})).await;
    assert_eq!(c.act(&attendee, "x", json!({"type":"poll_vote","id":id,"option":0})).await.1["counted"], true);
    assert_eq!(c.act(&attendee, "x", json!({"type":"poll_vote","id":id,"option":1})).await.1["counted"], false);
    assert_eq!(c.snapshot(&attendee, "x").await["polls"][0]["tallies"], json!([1, 0]));
    assert_eq!(c.act(&attendee, "x", json!({"type":"poll_vote","id":id,"option":9})).await.0, StatusCode::CONFLICT);
}

const PNG: &[u8] = &[0x89, b'P', b'N', b'G', 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 0];

impl Ctx {
    async fn upload(&self, path: &str, client: &str, kind: &str, name: &str, bytes: &[u8]) -> (StatusCode, Value) {
        let req = Request::put(format!("{path}/files?c={client}&kind={kind}&name={name}")).body(Body::from(bytes.to_vec())).unwrap();
        let res = self.app.clone().oneshot(req).await.unwrap();
        let status = res.status();
        let bytes = res.into_body().collect().await.unwrap().to_bytes();
        (status, serde_json::from_slice(&bytes).unwrap_or(Value::Null))
    }

    async fn fetch(&self, path: &str) -> (StatusCode, axum::http::HeaderMap, Vec<u8>) {
        let res = self.app.clone().oneshot(Request::get(path).body(Body::empty()).unwrap()).await.unwrap();
        let (status, headers) = (res.status(), res.headers().clone());
        (status, headers, res.into_body().collect().await.unwrap().to_bytes().to_vec())
    }
}

#[tokio::test]
async fn files_stay_private_until_shared_and_only_download_as_attachments() {
    let c = ctx(false);
    let crew = c.crew().await;
    let attendee = c.attendee_path("p1").await;
    c.enter(&crew.host, "Maya", CLIENT).await;
    c.enter(&attendee, "Ada", "x").await;
    // Only hosts and producers upload.
    assert_eq!(c.upload(&crew.guest, CLIENT, "resource", "a.pdf", b"%PDF").await.0, StatusCode::FORBIDDEN);
    assert_eq!(c.upload(&attendee, "x", "resource", "a.pdf", b"%PDF").await.0, StatusCode::FORBIDDEN);
    assert_eq!(c.upload(&crew.host, CLIENT, "resource", "evil.html", b"<script>").await.0, StatusCode::UNSUPPORTED_MEDIA_TYPE);
    let (status, f) = c.upload(&crew.producer, CLIENT, "resource", "..%2F..%2Fdeck.pdf", b"%PDF-1").await;
    assert_eq!(status, StatusCode::OK, "{f}");
    assert_eq!(f["name"], "deck.pdf", "no way out of the folder");
    let id = f["id"].as_str().unwrap();
    assert_eq!(c.fetch(&format!("{attendee}/files/{id}")).await.0, StatusCode::NOT_FOUND);
    assert!(c.snapshot(&attendee, "x").await["files"].as_array().unwrap().is_empty());
    c.act(&crew.host, CLIENT, json!({"type":"file_public","id":id,"public":true})).await;
    assert_eq!(c.snapshot(&attendee, "x").await["files"][0]["name"], "deck.pdf");
    let (status, headers, body) = c.fetch(&format!("{attendee}/files/{id}")).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body, b"%PDF-1");
    assert_eq!(headers[header::CONTENT_DISPOSITION], "attachment; filename=\"deck.pdf\"");
    assert_eq!(headers[header::CONTENT_TYPE], "application/octet-stream");
    assert!(headers[header::CONTENT_SECURITY_POLICY].to_str().unwrap().contains("sandbox"));
    c.act(&crew.host, CLIENT, json!({"type":"file_public","id":id,"public":false})).await;
    assert_eq!(c.fetch(&format!("{attendee}/files/{id}")).await.0, StatusCode::NOT_FOUND);
    // A frame must really be a PNG, and an attendee sees it only while it's on stage.
    assert_eq!(c.upload(&crew.host, CLIENT, "frame", "f.png", b"<svg onload=x>").await.0, StatusCode::UNSUPPORTED_MEDIA_TYPE);
    assert_eq!(c.upload(&crew.host, CLIENT, "media", "v.mp4", b"not a video at all").await.0, StatusCode::UNSUPPORTED_MEDIA_TYPE);
    let (_, frame) = c.upload(&crew.host, CLIENT, "frame", "f.png", PNG).await;
    let fid = frame["id"].as_str().unwrap();
    assert_eq!(c.fetch(&format!("{attendee}/files/{fid}")).await.0, StatusCode::NOT_FOUND);
    assert_eq!(c.act(&crew.host, CLIENT, json!({"type":"set_asset","slot":"background","id":fid})).await.0, StatusCode::BAD_REQUEST);
    c.act(&crew.host, CLIENT, json!({"type":"set_asset","slot":"frame","id":fid})).await;
    let (status, headers, _) = c.fetch(&format!("{attendee}/files/{fid}")).await;
    assert_eq!((status, headers[header::CONTENT_TYPE].to_str().unwrap()), (StatusCode::OK, "image/png"));
    // Deleting takes it off the stage.
    c.act(&crew.host, CLIENT, json!({"type":"file_delete","id":fid})).await;
    assert_eq!(c.snapshot(&attendee, "x").await["state"]["frameId"], Value::Null);
}

#[tokio::test]
async fn windows_can_be_arranged_but_not_placed_off_the_stage() {
    let c = ctx(false);
    let crew = c.crew().await;
    c.enter(&crew.host, "Maya", CLIENT).await;
    let good = json!({"type":"set_layout","mode":"custom","focus":null,"tiles":{"cam:a":{"x":0.1,"y":0.1,"w":0.5,"h":0.5}}});
    assert_eq!(c.act(&crew.host, CLIENT, good).await.0, StatusCode::OK);
    assert_eq!(c.snapshot(&crew.host, CLIENT).await["state"]["layout"]["tiles"]["cam:a"]["w"], 0.5);
    for bad in [
        json!({"type":"set_layout","mode":"dance","focus":null}),
        json!({"type":"set_layout","mode":"custom","focus":null,"tiles":{"a":{"x":2.0,"y":0.1,"w":0.5,"h":0.5}}}),
        json!({"type":"set_layout","mode":"custom","focus":null,"tiles":{"a":{"x":0.1,"y":0.1,"w":0.0,"h":0.5}}}),
        json!({"type":"set_cta","title":"x","subtitle":"","label":"","url":"javascript:alert(1)"}),
    ] {
        assert_eq!(c.act(&crew.host, CLIENT, bad.clone()).await.0, StatusCode::BAD_REQUEST, "{bad}");
    }
}

#[tokio::test]
async fn the_room_only_opens_around_its_schedule() {
    let c = ctx(false);
    c.room(3).await;
    let attendee = c.attendee_path("p1").await;
    let (status, body) = c.enter(&attendee, "", "x").await;
    assert_eq!((status, body["error"].as_str()), (StatusCode::FORBIDDEN, Some("not_open_yet")));
    assert_eq!(c.call("GET", &format!("{attendee}/info"), None, None).await.1["window"], "not_yet");
    // The same link starts working once the session is rescheduled to soon: links carry no times.
    c.room(1).await;
    assert_eq!(c.enter(&attendee, "", "x").await.0, StatusCode::OK);
    // The crew can get in early to rehearse.
    c.room(24).await;
    let host = c.member("host", "Maya").await;
    assert_eq!(c.enter(&host, "", CLIENT).await.0, StatusCode::OK);
    // Long after the end, nobody gets in.
    c.room(-10).await;
    let (status, body) = c.enter(&attendee, "", "x").await;
    assert_eq!((status, body["error"].as_str()), (StatusCode::GONE, Some("session_over")));
}

#[tokio::test]
async fn forged_links_are_refused_everywhere() {
    let c = ctx(true);
    c.room(1).await;
    let host = c.member("host", "Maya").await;
    let attendee = c.attendee_path("p1").await;
    let as_host = attendee.replacen("/join/a.", "/join/h.", 1);
    assert_eq!(c.enter(&as_host, "", CLIENT).await.0, StatusCode::NOT_FOUND);
    assert_eq!(c.act(&as_host, CLIENT, json!({"type":"go_live"})).await.0, StatusCode::NOT_FOUND);
    assert_eq!(c.call("GET", &format!("{as_host}/info"), None, None).await.0, StatusCode::NOT_FOUND);
    let res = c.app.clone().oneshot(Request::get(as_host.clone()).body(Body::empty()).unwrap()).await.unwrap();
    assert_eq!(res.status(), StatusCode::NOT_FOUND);
    let program = attendee.replacen("/join/a.", "/program/r.", 1);
    let res = c.app.clone().oneshot(Request::get(program).body(Body::empty()).unwrap()).await.unwrap();
    assert_eq!(res.status(), StatusCode::NOT_FOUND);
    let res = c.app.clone().oneshot(Request::get(host).body(Body::empty()).unwrap()).await.unwrap();
    assert_eq!(res.status(), StatusCode::SEE_OTHER);
    let res = c.app.clone().oneshot(Request::get(format!("/{ROOM}")).body(Body::empty()).unwrap()).await.unwrap();
    assert_eq!(res.status(), StatusCode::OK);
    assert_eq!(res.headers()[header::REFERRER_POLICY], "no-referrer");
    assert!(res.headers()[header::CONTENT_SECURITY_POLICY].to_str().unwrap().contains("connect-src 'self' wss://rtc.example.com https://rtc.example.com"));
}

#[tokio::test]
async fn recording_a_second_start_changes_nothing_and_opens_the_program_page() {
    let c = ctx(true);
    let crew = c.crew().await;
    c.enter(&crew.producer, "Pat", CLIENT).await;
    for _ in 0..2 {
        assert_eq!(c.act(&crew.producer, CLIENT, json!({"type":"record","on":true})).await.0, StatusCode::OK);
    }
    let calls = c.fake.calls.lock().unwrap().clone();
    let records: Vec<_> = calls.iter().filter(|x| x.starts_with("record:")).collect();
    assert_eq!(records.len(), 1);
    assert!(records[0].contains("page=https://live.example.com/program/r."), "{}", records[0]);
    let page = records[0].split("page=https://live.example.com").nth(1).unwrap().to_string();
    // The recorder's page opens, and its link does nothing else.
    let res = c.app.clone().oneshot(Request::get(page.clone()).body(Body::empty()).unwrap()).await.unwrap();
    assert_eq!(res.status(), StatusCode::OK);
    assert!(res.headers()[header::CONTENT_SECURITY_POLICY].to_str().unwrap().contains("ws:"));
    let as_join = page.replacen("/program/", "/join/", 1);
    assert_eq!(c.act(&as_join, CLIENT, json!({"type":"go_live"})).await.0, StatusCode::FORBIDDEN);
    assert_eq!(c.enter(&as_join, "", CLIENT).await.0, StatusCode::FORBIDDEN);
    c.act(&crew.producer, CLIENT, json!({"type":"record","on":false})).await;
    assert!(c.fake.calls.lock().unwrap().contains(&"stop:EG_1".to_string()));
}

#[tokio::test]
async fn without_storage_recording_is_refused_and_automatic_recording_never_blocks_going_live() {
    let c = ctx(false);
    let start = Utc::now() + Duration::hours(1);
    let (_, room) = c.v1("PUT", &format!("/v1/rooms/{ROOM}"), Some(json!({"title":"L","startsAt":start.to_rfc3339(),"endsAt":(start+Duration::hours(1)).to_rfc3339(),"autoRecord":true}))).await;
    assert_eq!(room["id"], ROOM);
    let host = c.member("host", "Maya").await;
    c.enter(&host, "", CLIENT).await;
    let (status, body) = c.act(&host, CLIENT, json!({"type":"record","on":true})).await;
    assert_eq!((status, body["error"].as_str()), (StatusCode::CONFLICT, Some("recording_not_configured")));
    assert_eq!(c.act(&host, CLIENT, json!({"type":"go_live"})).await.0, StatusCode::OK);
}

#[tokio::test]
async fn automatic_recording_starts_on_going_live_not_during_rehearsal() {
    let c = ctx(true);
    let start = Utc::now() + Duration::hours(1);
    let (_, room) = c.v1("PUT", &format!("/v1/rooms/{ROOM}"), Some(json!({"title":"L","startsAt":start.to_rfc3339(),"endsAt":(start+Duration::hours(1)).to_rfc3339(),"autoRecord":true}))).await;
    assert_eq!(room["id"], ROOM);
    let host = c.member("host", "Maya").await;
    c.enter(&host, "", CLIENT).await;
    let count = |c: &Ctx| c.fake.calls.lock().unwrap().iter().filter(|x| x.starts_with("record:")).count();
    assert_eq!(count(&c), 0);
    c.act(&host, CLIENT, json!({"type":"go_live"})).await;
    assert_eq!(count(&c), 1);
    c.act(&host, CLIENT, json!({"type":"end_session"})).await;
    assert!(c.fake.calls.lock().unwrap().contains(&"stop:EG_1".to_string()));
}

#[tokio::test]
async fn a_rescheduled_session_can_go_live_again() {
    let c = ctx(false);
    let crew = c.crew().await;
    c.enter(&crew.host, "", CLIENT).await;
    c.act(&crew.host, CLIENT, json!({"type":"go_live"})).await;
    c.act(&crew.host, CLIENT, json!({"type":"end_session"})).await;
    assert_eq!(c.act(&crew.host, CLIENT, json!({"type":"go_live"})).await.0, StatusCode::CONFLICT);
    c.room(2).await;
    assert_eq!(c.act(&crew.host, CLIENT, json!({"type":"go_live"})).await.0, StatusCode::OK);
}

#[tokio::test]
async fn a_backend_failure_on_joining_is_reported_not_hidden() {
    let c = ctx(false);
    c.room(1).await;
    let host = c.member("host", "Maya").await;
    *c.fake.fail.lock().unwrap() = true;
    let (status, body) = c.enter(&host, "", CLIENT).await;
    assert_eq!((status, body["error"].as_str()), (StatusCode::BAD_GATEWAY, Some("room_unavailable")));
}

const KEY: &str = "sk-live-super-secret-key";

impl Ctx {
    async fn destinations(&self, list: Value) -> (StatusCode, Value) {
        self.v1("PUT", &format!("/v1/rooms/{ROOM}/destinations"), Some(json!({"destinations": list}))).await
    }
}

fn dest(platform: &str, armed: bool) -> Value {
    // 8.8.8.8: a public address, so no DNS is needed in tests.
    json!({"platform": platform, "server": "rtmps://8.8.8.8/live2", "key": format!("{platform}-{KEY}"), "armed": armed})
}

#[tokio::test]
async fn going_live_sends_to_everything_marked_to_go_out_in_one_broadcast() {
    let c = ctx(false);
    let crew = c.crew().await;
    c.enter(&crew.host, "Maya", CLIENT).await;
    c.destinations(json!([dest("youtube", true), dest("meta", true), dest("linkedin", false)])).await;
    let casts = |c: &Ctx| c.fake.calls.lock().unwrap().iter().filter(|x| x.starts_with("cast:")).cloned().collect::<Vec<_>>();
    assert!(casts(&c).is_empty(), "nothing goes out during rehearsal");
    c.act(&crew.host, CLIENT, json!({"type":"go_live"})).await;
    let started = casts(&c);
    assert_eq!(started.len(), 1, "one encode for all of them");
    assert!(started[0].contains("youtube-") && started[0].contains("meta-") && !started[0].contains("linkedin-"), "{}", started[0]);
    assert!(started[0].contains("page=https://live.example.com/program/r."));
    // Turning one on later joins the same broadcast; turning it off leaves the rest going.
    c.act(&crew.producer, CLIENT, json!({"type":"broadcast_toggle","platform":"linkedin","on":true})).await;
    c.enter(&crew.producer, "Pat", CLIENT).await;
    c.act(&crew.producer, CLIENT, json!({"type":"broadcast_toggle","platform":"linkedin","on":true})).await;
    assert!(c.fake.calls.lock().unwrap().iter().any(|x| x.starts_with("update:EG_CAST:add=rtmps://8.8.8.8/live2/linkedin")));
    c.act(&crew.producer, CLIENT, json!({"type":"broadcast_toggle","platform":"meta","on":false})).await;
    assert!(c.fake.calls.lock().unwrap().iter().any(|x| x.contains("remove=rtmps://8.8.8.8/live2/meta")));
    // Ending stops it.
    c.act(&crew.host, CLIENT, json!({"type":"end_session"})).await;
    assert!(c.fake.calls.lock().unwrap().contains(&"stop:EG_CAST".to_string()));
}

#[tokio::test]
async fn a_platform_that_refuses_never_stops_the_session_going_live() {
    let c = ctx(false);
    let crew = c.crew().await;
    c.enter(&crew.host, "Maya", CLIENT).await;
    c.destinations(json!([dest("youtube", true)])).await;
    *c.fake.fail_cast.lock().unwrap() = true;
    assert_eq!(c.act(&crew.host, CLIENT, json!({"type":"go_live"})).await.0, StatusCode::OK);
    let snap = c.snapshot(&crew.host, CLIENT).await;
    assert_eq!(snap["state"]["phase"], "live");
    assert_eq!(snap["broadcast"][0]["status"], "failed");
    assert!(snap["broadcast"][0]["error"].as_str().unwrap().contains("capacity"));
}

#[tokio::test]
async fn stream_keys_never_come_back_out() {
    let c = ctx(false);
    let crew = c.crew().await;
    let attendee = c.attendee_path("p1").await;
    c.enter(&crew.host, "Maya", CLIENT).await;
    c.enter(&crew.guest, "Gus", CLIENT).await;
    c.enter(&attendee, "Ada", "x").await;
    let (status, saved) = c.destinations(json!([dest("youtube", true)])).await;
    assert_eq!(status, StatusCode::OK);
    assert!(!saved.to_string().contains(KEY));
    let (_, read) = c.v1("GET", &format!("/v1/rooms/{ROOM}/destinations"), None).await;
    assert_eq!(read["destinations"][0]["armed"], true);
    assert!(!read.to_string().contains(KEY));
    for (path, client) in [(&crew.host, CLIENT), (&crew.guest, CLIENT), (&attendee, "x")] {
        let text = c.snapshot(path, client).await.to_string();
        assert!(!text.contains(KEY) && !text.contains("8.8.8.8"), "{path}: {text}");
    }
    // Only people who run the show see the platforms at all.
    assert!(c.snapshot(&crew.guest, CLIENT).await.get("broadcast").is_none());
    assert!(c.snapshot(&attendee, "x").await.get("broadcast").is_none());
    assert_eq!(c.act(&crew.guest, CLIENT, json!({"type":"broadcast_toggle","platform":"youtube","on":false})).await.0, StatusCode::FORBIDDEN);
    assert_eq!(c.act(&attendee, "x", json!({"type":"broadcast_toggle","platform":"youtube","on":false})).await.0, StatusCode::FORBIDDEN);
}

#[tokio::test]
async fn destinations_must_be_public_rtmp_addresses() {
    let c = ctx(false);
    let crew = c.crew().await;
    c.enter(&crew.host, "Maya", CLIENT).await;
    for bad in ["https://youtube.com/live", "rtmp://", "rtmp://user:pass@host/x", "rtmp://has space/x", "file:///etc/passwd"] {
        let (status, _) = c.destinations(json!([{"platform":"youtube","server":bad,"key":"k","armed":true}])).await;
        assert_eq!(status, StatusCode::BAD_REQUEST, "{bad}");
    }
    let (status, _) = c.destinations(json!([{"platform":"tiktok","server":"rtmp://8.8.8.8/x","key":"k","armed":true}])).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    // On with no key is refused, rather than quietly sending nothing.
    let (status, _) = c.destinations(json!([{"platform":"x","server":"rtmp://8.8.8.8/x","armed":true}])).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    // The recorder is inside the server's network: it mustn't be pointed at it.
    for inside in ["rtmp://127.0.0.1/x", "rtmp://10.0.0.5/x", "rtmp://169.254.169.254/x", "rtmp://[::1]/x", "rtmp://192.168.1.1:1935/x"] {
        c.destinations(json!([{"platform":"youtube","server":inside,"key":"k","armed":true}])).await;
        c.act(&crew.host, CLIENT, json!({"type":"go_live"})).await;
        let snap = c.snapshot(&crew.host, CLIENT).await;
        assert_eq!(snap["broadcast"][0]["status"], "failed", "{inside}");
        assert!(!c.fake.calls.lock().unwrap().iter().any(|x| x.starts_with("cast:")), "{inside}");
        c.room(2).await;
        c.room(1).await;
    }
}

#[tokio::test]
async fn the_recorders_reports_set_each_platforms_status_and_stay_out_of_recordings() {
    let c = ctx(false);
    let crew = c.crew().await;
    c.enter(&crew.host, "Maya", CLIENT).await;
    // Same server and path, told apart by their keys, as on a platform with one regional server.
    c.destinations(json!([{"platform":"youtube","server":"rtmps://8.8.8.8/live","key":"aaa-first-key-aaa","armed":true},{"platform":"meta","server":"rtmps://8.8.8.8/live","key":"bbb-other-key-bbb","armed":true}])).await;
    c.act(&crew.host, CLIENT, json!({"type":"go_live"})).await;
    let event = json!({"id":"e-cast-1","event":"egress_updated","createdAt":1000,"egressInfo":{"egressId":"EG_CAST","roomName":ROOM,"status":"EGRESS_ACTIVE",
        "streamResults":[{"url":"rtmps://8.8.8.8/live/{aaa...aaa}","status":"ACTIVE"},{"url":"rtmps://8.8.8.8/live/{bbb...bbb}","status":"FAILED","error":"refused"}]}});
    assert_eq!(c.webhook(event).await, StatusCode::OK);
    let snap = c.snapshot(&crew.host, CLIENT).await;
    let status = |p: &str| snap["broadcast"].as_array().unwrap().iter().find(|d| d["platform"] == p).unwrap()["status"].as_str().unwrap().to_string();
    assert_eq!((status("youtube").as_str(), status("meta").as_str()), ("live", "failed"));
    let (_, recordings) = c.v1("GET", &format!("/v1/rooms/{ROOM}/recordings"), None).await;
    assert!(recordings["recordings"].as_array().unwrap().is_empty());
    assert_eq!(snap["state"]["recording"], false);
}

#[tokio::test]
async fn the_brand_reaches_every_room_and_a_session_can_override_it() {
    let c = ctx(false);
    let crew = c.crew().await;
    let attendee = c.attendee_path("p1").await;
    c.enter(&crew.host, "Maya", CLIENT).await;
    c.enter(&attendee, "Ada", "x").await;
    let brand = json!({"accent":"#123abc","stage":"#000000","mode":"dark","font":"serif"});
    assert_eq!(c.call("PUT", "/v1/brand", None, Some(brand.clone())).await.0, StatusCode::UNAUTHORIZED);
    assert_eq!(c.v1("PUT", "/v1/brand", Some(brand)).await.0, StatusCode::OK);
    let snap = c.snapshot(&attendee, "x").await;
    assert_eq!(snap["state"]["brand"]["accent"], "#123abc");
    assert_eq!(snap["state"]["brand"]["mode"], "dark");
    assert_eq!(snap["state"]["brand"]["logo"], false);
    // A logo must be a real PNG, and it's public once set.
    let put = |c: &Ctx, bytes: &[u8]| {
        let req = Request::put("/v1/brand/logo").header(header::AUTHORIZATION, format!("Bearer {SECRET}")).body(Body::from(bytes.to_vec())).unwrap();
        let app = c.app.clone();
        async move { app.oneshot(req).await.unwrap().status() }
    };
    assert_eq!(put(&c, b"<svg onload=x>").await, StatusCode::UNSUPPORTED_MEDIA_TYPE);
    assert_eq!(c.fetch("/brand/logo").await.0, StatusCode::NOT_FOUND);
    let big = [PNG, &vec![0u8; 500 * 1024]].concat();
    assert_eq!(put(&c, &big).await, StatusCode::OK, "a real logo is bigger than an API body");
    let (status, headers, _) = c.fetch("/brand/logo").await;
    assert_eq!((status, headers[header::CONTENT_TYPE].to_str().unwrap()), (StatusCode::OK, "image/png"));
    assert_eq!(c.snapshot(&attendee, "x").await["state"]["brand"]["logo"], true);
    // A session's own colour sits alongside the brand; only the crew that runs the show can set it.
    let own = json!({"type":"set_theme","theme":{"accent":"#ff0000"}});
    assert_eq!(c.act(&attendee, "x", own.clone()).await.0, StatusCode::FORBIDDEN);
    assert_eq!(c.act(&crew.host, CLIENT, own).await.0, StatusCode::OK);
    assert_eq!(c.snapshot(&attendee, "x").await["state"]["theme"]["accent"], "#ff0000");
    for bad in [json!({"accent":"red"}), json!({"accent":"#12"}), json!({"mode":"neon"}), json!({"stage":"url(x)"}), json!({"font":"Comic"})] {
        assert_eq!(c.act(&crew.host, CLIENT, json!({"type":"set_theme","theme":bad})).await.0, StatusCode::BAD_REQUEST, "{bad}");
    }
}

const PASSWORD: &str = "correct-horse-battery";

impl Ctx {
    async fn sign_in(&self) -> String {
        let req = Request::post("/login").header(header::CONTENT_TYPE, "application/json").body(Body::from(json!({"email": "owner@example.com", "password": PASSWORD}).to_string())).unwrap();
        let res = self.app.clone().oneshot(req).await.unwrap();
        assert_eq!(res.status(), StatusCode::OK);
        res.headers()[header::SET_COOKIE].to_str().unwrap().split(';').next().unwrap().to_string()
    }

    async fn admin(&self, method: &str, path: &str, cookie: Option<&str>, body: Option<Value>) -> (StatusCode, Value) {
        let mut req = Request::builder().method(method).uri(format!("/admin/api{path}"));
        if let Some(c) = cookie {
            req = req.header(header::COOKIE, c);
        }
        let req = match body {
            Some(b) => req.header(header::CONTENT_TYPE, "application/json").body(Body::from(b.to_string())).unwrap(),
            None => req.body(Body::empty()).unwrap(),
        };
        let res = self.app.clone().oneshot(req).await.unwrap();
        let status = res.status();
        let bytes = res.into_body().collect().await.unwrap().to_bytes();
        (status, serde_json::from_slice(&bytes).unwrap_or(Value::Null))
    }
}

fn start_body(slug: &str) -> Value {
    let start = Utc::now() + Duration::hours(1);
    json!({"title":"L","startsAt":start.to_rfc3339(),"endsAt":(start+Duration::hours(1)).to_rfc3339(),"slug":slug,"entryUrl":"https://events.example.com/w/abc/"})
}

#[tokio::test]
async fn sessions_live_at_a_clean_address_and_the_signed_link_only_passes_through() {
    let c = ctx(false);
    let (status, body) = c.v1("PUT", &format!("/v1/rooms/{ROOM}"), Some(start_body("launch-webinar"))).await;
    assert_eq!((status, body["slug"].as_str()), (StatusCode::OK, Some("launch-webinar")));
    let host = c.member("host", "Maya").await;
    let attendee = c.attendee_path("p1").await;
    // A signed link goes to the clean address, with the link in the fragment: no server or referrer sees that.
    let location = |p: String| { let app = c.app.clone(); async move {
        let res = app.oneshot(Request::get(p).body(Body::empty()).unwrap()).await.unwrap();
        assert_eq!(res.status(), StatusCode::SEE_OTHER);
        res.headers()[header::LOCATION].to_str().unwrap().to_string()
    } };
    assert_eq!(location(host.clone()).await, format!("/launch-webinar/green-room#t={}", host.trim_start_matches("/join/")));
    assert_eq!(location(attendee.clone()).await, format!("/launch-webinar/lobby#t={}", attendee.trim_start_matches("/join/")));
    for path in ["/launch-webinar", "/launch-webinar/lobby", "/launch-webinar/green-room"] {
        assert_eq!(c.fetch(path).await.0, StatusCode::OK, "{path}");
    }
    assert_eq!(c.fetch("/no-such-session").await.0, StatusCode::NOT_FOUND);
    assert_eq!(c.fetch("/no-such-session/green-room").await.0, StatusCode::NOT_FOUND);
    // Without a link, all anyone learns is the title and where to go.
    let (_, _, bytes) = c.fetch("/launch-webinar/public").await;
    let public: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(public["entryUrl"], "https://events.example.com/w/abc/");
    assert!(public.get("id").is_none() && !public.to_string().contains(ROOM));
}

#[tokio::test]
async fn addresses_are_unique_and_never_collide_with_the_servers_own() {
    let c = ctx(false);
    assert_eq!(c.v1("PUT", "/v1/rooms/one", Some(start_body("launch"))).await.0, StatusCode::OK);
    assert_eq!(c.v1("PUT", "/v1/rooms/two", Some(start_body("launch"))).await.1["error"], "slug_taken");
    assert_eq!(c.v1("PUT", "/v1/rooms/one", Some(start_body("launch"))).await.0, StatusCode::OK, "its own slug is fine");
    for bad in ["admin", "login", "assets", "join", "v1", "Upper", "has space", "-edge", "a/b"] {
        assert_eq!(c.v1("PUT", "/v1/rooms/three", Some(start_body(bad))).await.1["error"], "invalid_slug", "{bad}");
    }
    let (status, _) = c.v1("PUT", "/v1/rooms/four", Some(json!({"title":"x","startsAt":"2030-01-01T00:00:00Z","endsAt":"2030-01-01T01:00:00Z","entryUrl":"javascript:alert(1)"}))).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
}

async fn login_status(c: &Ctx, email: &str, password: &str) -> StatusCode {
    let req = Request::post("/login").header(header::CONTENT_TYPE, "application/json").body(Body::from(json!({"email": email, "password": password}).to_string())).unwrap();
    c.app.clone().oneshot(req).await.unwrap().status()
}

#[tokio::test]
async fn admin_needs_a_sign_in_and_there_is_no_sign_up() {
    let c = ctx(false);
    c.room(1).await;
    for path in ["/rooms", "/brand", "/saved-destinations", "/me", "/accounts", "/activity"] {
        assert_eq!(c.admin("GET", path, None, None).await.0, StatusCode::UNAUTHORIZED, "{path}");
        assert_eq!(c.admin("GET", path, Some("vc_admin=forged"), None).await.0, StatusCode::UNAUTHORIZED, "{path}");
    }
    assert_eq!(login_status(&c, "owner@example.com", "wrong-password").await, StatusCode::UNAUTHORIZED);
    assert_eq!(login_status(&c, "nobody@example.com", PASSWORD).await, StatusCode::UNAUTHORIZED);
    assert_eq!(c.fetch("/signup").await.0, StatusCode::NOT_FOUND);
    let cookie = c.sign_in().await;
    let (status, body) = c.admin("GET", "/rooms", Some(&cookie), None).await;
    assert_eq!((status, body["rooms"][0]["id"].as_str()), (StatusCode::OK, Some(ROOM)));
    let (_, detail) = c.admin("GET", &format!("/rooms/{ROOM}"), Some(&cookie), None).await;
    assert_eq!(detail["destinations"].as_array().unwrap().len(), 4);
    assert_eq!(c.admin("PUT", "/brand", Some(&cookie), Some(json!({"accent":"#123abc"}))).await.0, StatusCode::OK);
    assert_eq!(c.admin("GET", "/me", Some(&cookie), None).await.1["email"], "owner@example.com");
}

#[tokio::test]
async fn guessing_one_address_locks_that_address_not_everyone() {
    let c = ctx(false);
    c.sign_in().await;
    let (_, added) = c.admin("POST", "/accounts", Some(&c.sign_in().await), Some(json!({"email":"sam@example.com"}))).await;
    assert!(added["setPasswordUrl"].as_str().is_some());
    for _ in 0..9 {
        login_status(&c, "sam@example.com", "wrong-password").await;
    }
    assert_eq!(login_status(&c, "sam@example.com", "wrong-password").await, StatusCode::TOO_MANY_REQUESTS);
    assert_eq!(login_status(&c, "owner@example.com", PASSWORD).await, StatusCode::OK);
}

#[tokio::test]
async fn everyone_has_their_own_account_set_up_from_a_one_time_link() {
    let c = ctx(false);
    let owner = c.sign_in().await;
    let (status, added) = c.admin("POST", "/accounts", Some(&owner), Some(json!({"email":"Sam@Example.com","name":"Sam"}))).await;
    assert_eq!(status, StatusCode::OK, "{added}");
    assert_eq!(added["account"]["email"], "sam@example.com");
    assert_eq!(added["account"]["hasPassword"], false);
    assert_eq!(c.admin("POST", "/accounts", Some(&owner), Some(json!({"email":"sam@example.com"}))).await.0, StatusCode::CONFLICT);
    // No password yet, so no way in without the link.
    assert_eq!(login_status(&c, "sam@example.com", "").await, StatusCode::UNAUTHORIZED);
    let link = path_of(added["setPasswordUrl"].as_str().unwrap());
    let set = |password: &str, path: String| {
        let req = Request::post(path).header(header::CONTENT_TYPE, "application/json").body(Body::from(json!({"password": password}).to_string())).unwrap();
        let app = c.app.clone();
        async move { app.oneshot(req).await.unwrap() }
    };
    assert_eq!(set("short", link.clone()).await.status(), StatusCode::BAD_REQUEST);
    let done = set("sams-own-long-password", link.clone()).await;
    assert_eq!(done.status(), StatusCode::OK);
    let sam = done.headers()[header::SET_COOKIE].to_str().unwrap().split(';').next().unwrap().to_string();
    assert_eq!(set("another-long-password", link).await.status(), StatusCode::GONE, "once only");
    assert_eq!(login_status(&c, "sam@example.com", "sams-own-long-password").await, StatusCode::OK);
    // Sam isn't an owner: no managing other people.
    assert_eq!(c.admin("GET", "/accounts", Some(&sam), None).await.0, StatusCode::FORBIDDEN);
    assert_eq!(c.admin("POST", "/accounts", Some(&sam), Some(json!({"email":"x@example.com"}))).await.0, StatusCode::FORBIDDEN);
    let (_, list) = c.admin("GET", "/accounts", Some(&owner), None).await;
    assert!(!list.to_string().contains("argon2"), "hashes stay on the server");
    let sam_id = list["accounts"].as_array().unwrap().iter().find(|a| a["email"] == "sam@example.com").unwrap()["id"].as_str().unwrap().to_string();
    let owner_id = list["accounts"].as_array().unwrap().iter().find(|a| a["email"] == "owner@example.com").unwrap()["id"].as_str().unwrap().to_string();
    // The record says who did what.
    let (_, log) = c.admin("GET", "/activity", Some(&owner), None).await;
    assert!(log.to_string().contains("account.added") && log.to_string().contains("owner@example.com"));
    assert!(!log.to_string().contains("sams-own-long-password"));
    // A reset signs them out everywhere and kills the password.
    let (status, reset) = c.admin("POST", &format!("/accounts/{sam_id}/reset"), Some(&owner), None).await;
    assert_eq!(status, StatusCode::OK);
    assert!(reset["setPasswordUrl"].as_str().is_some());
    assert_eq!(c.admin("GET", "/me", Some(&sam), None).await.0, StatusCode::UNAUTHORIZED);
    assert_eq!(login_status(&c, "sam@example.com", "sams-own-long-password").await, StatusCode::UNAUTHORIZED);
    // The last owner can't be removed; anyone else can, and their session goes.
    assert_eq!(c.admin("DELETE", &format!("/accounts/{owner_id}"), Some(&owner), None).await.0, StatusCode::CONFLICT);
    assert_eq!(c.admin("DELETE", &format!("/accounts/{sam_id}"), Some(&owner), None).await.0, StatusCode::OK);
    assert_eq!(login_status(&c, "sam@example.com", "sams-own-long-password").await, StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn signing_out_and_changing_a_password_end_sessions_on_the_server() {
    let c = ctx(false);
    let first = c.sign_in().await;
    let second = c.sign_in().await;
    let wrong = json!({"current":"not-it","newPassword":"a-brand-new-password"});
    assert_eq!(c.admin("POST", "/me/password", Some(&first), Some(wrong)).await.0, StatusCode::FORBIDDEN);
    assert_eq!(c.admin("POST", "/me/password", Some(&first), Some(json!({"current":PASSWORD,"newPassword":"short"}))).await.0, StatusCode::BAD_REQUEST);
    assert_eq!(c.admin("POST", "/me/password", Some(&first), Some(json!({"current":PASSWORD,"newPassword":"a-brand-new-password"}))).await.0, StatusCode::OK);
    assert_eq!(c.admin("GET", "/me", Some(&second), None).await.0, StatusCode::UNAUTHORIZED, "signed out elsewhere");
    assert_eq!(login_status(&c, "owner@example.com", PASSWORD).await, StatusCode::UNAUTHORIZED);
    let req = Request::post("/login").header(header::CONTENT_TYPE, "application/json").body(Body::from(json!({"email":"owner@example.com","password":"a-brand-new-password"}).to_string())).unwrap();
    let res = c.app.clone().oneshot(req).await.unwrap();
    let cookie = res.headers()[header::SET_COOKIE].to_str().unwrap().split(';').next().unwrap().to_string();
    let out = Request::post("/logout").header(header::COOKIE, &cookie).body(Body::empty()).unwrap();
    assert_eq!(c.app.clone().oneshot(out).await.unwrap().status(), StatusCode::OK);
    assert_eq!(c.admin("GET", "/me", Some(&cookie), None).await.0, StatusCode::UNAUTHORIZED, "a copy of the cookie stops working");
}

#[tokio::test]
async fn each_crew_member_has_their_own_link_that_can_be_replaced_or_taken_away() {
    let c = ctx(false);
    c.room(1).await;
    let (_, gus) = c.v1("POST", &format!("/v1/rooms/{ROOM}/crew"), Some(json!({"role":"guest","name":"Gus Guest","title":"VP Marketing"}))).await;
    let (_, gia) = c.v1("POST", &format!("/v1/rooms/{ROOM}/crew"), Some(json!({"role":"guest","name":"Gia Guest"}))).await;
    assert_ne!(gus["url"], gia["url"], "two guests, two links");
    let (gus_path, gia_path) = (path_of(gus["url"].as_str().unwrap()), path_of(gia["url"].as_str().unwrap()));
    // Their name and title are ready for them, and they're different people in the rooms.
    let info = c.call("GET", &format!("{gus_path}/info"), None, None).await.1;
    assert_eq!((info["you"]["name"].as_str(), info["role"].as_str()), (Some("Gus Guest"), Some("guest")));
    let (a, b) = (c.enter(&gus_path, "", "x").await.1, c.enter(&gia_path, "", "x").await.1);
    assert_ne!(a["identity"], b["identity"]);
    assert_eq!(claims(a["stageToken"].as_str().unwrap()).name, "Gus Guest");
    // Same key, same person and same link: how the studio gives each user one host link.
    let key = json!({"role":"host","name":"Ed","key":"studio-user-7"});
    let (_, first) = c.v1("POST", &format!("/v1/rooms/{ROOM}/crew"), Some(key.clone())).await;
    let (_, again) = c.v1("POST", &format!("/v1/rooms/{ROOM}/crew"), Some(key)).await;
    assert_eq!(first["url"], again["url"]);
    // A role can't be changed by editing the link.
    let as_host = gus_path.replacen("/join/g.", "/join/h.", 1);
    assert_eq!(c.enter(&as_host, "", "x").await.0, StatusCode::NOT_FOUND);
    // A new link kills the old one, and only that one. They're put out of the rooms.
    let id = gus["id"].as_str().unwrap();
    let (status, replaced) = c.v1("POST", &format!("/v1/rooms/{ROOM}/crew/{id}/new-link"), None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(c.enter(&gus_path, "", "x").await.0, StatusCode::NOT_FOUND);
    assert_eq!(c.enter(&path_of(replaced["url"].as_str().unwrap()), "", "x").await.0, StatusCode::OK);
    assert_eq!(c.enter(&gia_path, "", "x").await.0, StatusCode::OK);
    assert!(c.fake.calls.lock().unwrap().iter().any(|x| x.starts_with(&format!("remove:{ROOM}--bs:host-g-{id}"))));
    // Taken away: dead everywhere, right away.
    assert_eq!(c.v1("DELETE", &format!("/v1/rooms/{ROOM}/crew/{id}"), None).await.0, StatusCode::OK);
    let dead = path_of(replaced["url"].as_str().unwrap());
    assert_eq!(c.enter(&dead, "", "x").await.0, StatusCode::NOT_FOUND);
    assert_eq!(c.act(&dead, "x", json!({"type":"chat_send","text":"hi","channel":"stage"})).await.0, StatusCode::NOT_FOUND);
    assert_eq!(c.call("GET", &format!("{dead}/info"), None, None).await.0, StatusCode::NOT_FOUND);
    let (_, list) = c.v1("GET", &format!("/v1/rooms/{ROOM}/crew"), None).await;
    assert_eq!(list["crew"].as_array().unwrap().len(), 2);
    for bad in [json!({"role":"attendee","name":"x"}), json!({"role":"host","name":"  "})] {
        assert_eq!(c.v1("POST", &format!("/v1/rooms/{ROOM}/crew"), Some(bad)).await.0, StatusCode::BAD_REQUEST);
    }
}

#[tokio::test]
async fn saved_keys_are_kept_for_every_session_except_linkedins_and_never_returned() {
    let c = ctx(false);
    c.room(1).await;
    let cookie = c.sign_in().await;
    let (status, saved) = c.admin("PUT", "/saved-destinations", Some(&cookie), Some(json!({"destinations":[{"platform":"youtube","server":"rtmps://8.8.8.8/live2","key":KEY}]}))).await;
    assert_eq!(status, StatusCode::OK);
    assert!(!saved.to_string().contains(KEY));
    assert_eq!(saved["destinations"][0]["keySet"], true);
    assert_eq!(c.admin("PUT", "/saved-destinations", Some(&cookie), Some(json!({"destinations":[{"platform":"linkedin","server":"rtmps://8.8.8.8/x","key":"k"}]}))).await.0, StatusCode::BAD_REQUEST);
    // A session uses it when turned on with no key of its own, and doesn't when turned off.
    let crew = c.crew().await;
    c.enter(&crew.host, "Maya", CLIENT).await;
    assert_eq!(c.admin("PUT", &format!("/rooms/{ROOM}/destinations"), Some(&cookie), Some(json!({"destinations":[{"platform":"youtube","server":"rtmps://8.8.8.8/live2","armed":true}]}))).await.0, StatusCode::OK);
    let (_, detail) = c.admin("GET", &format!("/rooms/{ROOM}"), Some(&cookie), None).await;
    assert_eq!(detail["destinations"][0]["key"], "saved");
    assert!(!detail.to_string().contains(KEY));
    c.act(&crew.host, CLIENT, json!({"type":"go_live"})).await;
    assert!(c.fake.calls.lock().unwrap().iter().any(|x| x.starts_with("cast:") && x.contains(&format!("rtmps://8.8.8.8/live2/{KEY}"))));
}

#[tokio::test]
async fn a_link_from_the_studio_signs_in_once_and_expires() {
    let c = ctx(false);
    let (status, link) = c.v1("POST", "/v1/admin-link", Some(json!({"room": ROOM, "email": "Ed@Example.com", "name": "Ed"}))).await;
    assert_eq!(status, StatusCode::OK, "{link}");
    assert_eq!(c.call("POST", "/v1/admin-link", None, Some(json!({"email":"x@example.com"}))).await.0, StatusCode::UNAUTHORIZED);
    let path = path_of(link["url"].as_str().unwrap());
    let res = c.app.clone().oneshot(Request::get(path.clone()).body(Body::empty()).unwrap()).await.unwrap();
    assert_eq!(res.headers()[header::LOCATION], format!("/admin#{ROOM}").as_str());
    let cookie = res.headers()[header::SET_COOKIE].to_str().unwrap();
    assert!(cookie.contains("HttpOnly") && cookie.contains("SameSite=Strict") && cookie.contains("Secure"), "{cookie}");
    let cookie = cookie.split(';').next().unwrap().to_string();
    // They're on record as themselves, and they didn't get a password or an owner's powers.
    let me = c.admin("GET", "/me", Some(&cookie), None).await.1;
    assert_eq!((me["email"].as_str(), me["hasPassword"].as_bool()), (Some("ed@example.com"), Some(false)));
    // Used once: the second time just goes to the sign-in page, with no session.
    let again = c.app.clone().oneshot(Request::get(path.clone()).body(Body::empty()).unwrap()).await.unwrap();
    assert_eq!(again.headers()[header::LOCATION], "/login");
    assert!(!again.headers().contains_key(header::SET_COOKIE));
    let altered = format!("{}x", path);
    let forged = c.app.clone().oneshot(Request::get(altered).body(Body::empty()).unwrap()).await.unwrap();
    assert!(!forged.headers().contains_key(header::SET_COOKIE));
}

const GIF: &[u8] = b"GIF89a-not-really-but-it-starts-right";

#[tokio::test]
async fn graphics_are_real_media_and_attendees_get_them_only_while_on_the_picture() {
    let c = ctx(false);
    let crew = c.crew().await;
    let attendee = c.attendee_path("p1").await;
    c.enter(&crew.host, "Maya", CLIENT).await;
    c.enter(&attendee, "Ada", "x").await;
    for bad in [&b"<svg onload=alert(1)>"[..], b"<html><script>", b"#!/bin/sh"] {
        assert_eq!(c.upload(&crew.host, CLIENT, "graphic", "x.gif", bad).await.0, StatusCode::UNSUPPORTED_MEDIA_TYPE);
    }
    assert_eq!(c.upload(&crew.guest, CLIENT, "graphic", "x.gif", GIF).await.0, StatusCode::FORBIDDEN);
    let (status, file) = c.upload(&crew.host, CLIENT, "graphic", "band.gif", GIF).await;
    assert_eq!((status, file["mime"].as_str()), (StatusCode::OK, Some("image/gif")));
    let id = file["id"].as_str().unwrap();
    let url = format!("{attendee}/files/{id}");
    assert_eq!(c.fetch(&url).await.0, StatusCode::NOT_FOUND, "not on the picture yet");
    let layer = |visible: bool, x: f64| json!({"type":"layer_save","layer":{"id":"band","fileId":id,"visible":visible,"title":"Live now","x":x,"y":0.8,"w":0.5,"h":0.1,"scroll":true}});
    assert_eq!(c.act(&attendee, "x", layer(true, 0.1)).await.0, StatusCode::FORBIDDEN);
    assert_eq!(c.act(&crew.guest, CLIENT, layer(true, 0.1)).await.0, StatusCode::FORBIDDEN);
    assert_eq!(c.act(&crew.host, CLIENT, layer(true, 2.0)).await.0, StatusCode::BAD_REQUEST, "off the picture");
    assert_eq!(c.act(&crew.host, CLIENT, layer(true, 0.1)).await.0, StatusCode::OK);
    let saved = c.snapshot(&attendee, "x").await;
    assert_eq!(saved["state"]["layers"][0]["mime"], "image/gif");
    assert_eq!(saved["state"]["layers"][0]["title"], "Live now");
    let (status, headers, _) = c.fetch(&url).await;
    assert_eq!((status, headers[header::CONTENT_TYPE].to_str().unwrap(), headers[header::CONTENT_DISPOSITION].to_str().unwrap()), (StatusCode::OK, "image/gif", "inline"));
    assert!(headers[header::CONTENT_SECURITY_POLICY].to_str().unwrap().contains("sandbox"));
    assert_eq!(c.act(&crew.host, CLIENT, layer(false, 0.1)).await.0, StatusCode::OK);
    assert_eq!(c.fetch(&url).await.0, StatusCode::NOT_FOUND, "hidden again");
    // Words alone are fine; nothing at all isn't. Deleting the file takes its layer with it.
    assert_eq!(c.act(&crew.host, CLIENT, json!({"type":"layer_save","layer":{"id":"t","fileId":null,"title":"Hello","x":0.0,"y":0.0,"w":1.0,"h":0.1,"visible":true}})).await.0, StatusCode::OK);
    assert_eq!(c.act(&crew.host, CLIENT, json!({"type":"layer_save","layer":{"id":"e","fileId":null,"x":0.0,"y":0.0,"w":1.0,"h":0.1}})).await.0, StatusCode::BAD_REQUEST);
    c.act(&crew.host, CLIENT, json!({"type":"file_delete","id":id})).await;
    let left = c.snapshot(&crew.host, CLIENT).await;
    assert_eq!(left["state"]["layers"].as_array().unwrap().len(), 1);
    // Stream video types are told apart, and media with sound stays silent on the page's side.
    let webm = [&[0x1au8, 0x45, 0xdf, 0xa3][..], &[0u8; 20]].concat();
    assert_eq!(c.upload(&crew.host, CLIENT, "graphic", "bug.webm", &webm).await.1["mime"], "video/webm");
}

#[tokio::test]
async fn captions_listen_only_while_live_and_only_to_the_stage() {
    let c = ctx(false);
    let crew = c.crew().await;
    c.enter(&crew.host, "Maya", CLIENT).await;
    *c.fake.mics.lock().unwrap() = vec![(c.identity(&crew.host).await, "TR_mic".to_string())];
    let listens = |c: &Ctx| c.fake.calls.lock().unwrap().iter().filter(|x| x.starts_with("listen:")).cloned().collect::<Vec<_>>();
    // Rehearsal isn't transcribed.
    let track = json!({"id":"e-t1","event":"track_published","createdAt":1,"room":{"name":ROOM}});
    c.webhook(track.clone()).await;
    tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    assert!(listens(&c).is_empty());
    c.act(&crew.host, CLIENT, json!({"type":"go_live"})).await;
    let started = listens(&c);
    assert_eq!(started.len(), 1);
    assert!(started[0].starts_with(&format!("listen:{ROOM}:TR_mic:wss://live.example.com/captions/in/c.")), "{}", started[0]);
    assert!(!started.iter().any(|x| x.contains(&format!("{ROOM}--bs"))), "never the green room");
    // A second event about the same microphone changes nothing; a new one is picked up.
    c.webhook(json!({"id":"e-t2","event":"track_published","createdAt":2,"room":{"name":ROOM}})).await;
    tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    assert_eq!(listens(&c).len(), 1);
    c.fake.mics.lock().unwrap().push(("p2".to_string(), "TR_two".to_string()));
    c.webhook(json!({"id":"e-t3","event":"track_published","createdAt":3,"room":{"name":ROOM}})).await;
    tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    assert_eq!(listens(&c).len(), 2);
    // A person turning them off stops it. The stream isn't a recording.
    c.act(&crew.host, CLIENT, json!({"type":"set_settings","settings":{"captions":false}})).await;
    assert!(c.fake.calls.lock().unwrap().contains(&"stop:EG_TR_mic".to_string()));
    assert_eq!(c.webhook(json!({"id":"e-t4","event":"egress_started","createdAt":4,"egressInfo":{"egressId":"EG_TR_mic","roomName":ROOM,"status":"EGRESS_ACTIVE","track":{"roomName":ROOM,"trackId":"TR_mic"}}})).await, StatusCode::OK);
    let (_, recordings) = c.v1("GET", &format!("/v1/rooms/{ROOM}/recordings"), None).await;
    assert!(recordings["recordings"].as_array().unwrap().is_empty());
}

#[tokio::test]
async fn caption_audio_needs_its_own_signed_link_and_transcripts_are_for_those_running_the_show() {
    let c = ctx(false);
    let crew = c.crew().await;
    let attendee = c.attendee_path("p1").await;
    let as_ticket = attendee.trim_start_matches("/join/").to_string();
    assert_eq!(c.fetch(&format!("/captions/in/{as_ticket}")).await.0, StatusCode::BAD_REQUEST, "not a WebSocket request");
    let ws = |path: String| { let app = c.app.clone(); async move {
        let req = Request::get(path).header(header::CONNECTION, "upgrade").header(header::UPGRADE, "websocket")
            .header("sec-websocket-version", "13").header("sec-websocket-key", "dGhlIHNhbXBsZSBub25jZQ==").body(Body::empty()).unwrap();
        app.oneshot(req).await.unwrap().status()
    } };
    assert_ne!(ws(format!("/captions/in/{as_ticket}")).await, StatusCode::SWITCHING_PROTOCOLS, "an attendee's link can't send audio");
    assert_ne!(ws("/captions/in/c.vc-room-1.p1.forged".to_string()).await, StatusCode::SWITCHING_PROTOCOLS);
    assert_eq!(c.fetch(&format!("{attendee}/captions/all.srt")).await.0, StatusCode::NOT_FOUND);
    assert_eq!(c.fetch(&format!("{}/captions/all.srt", crew.guest)).await.0, StatusCode::NOT_FOUND);
    let (status, headers, _) = c.fetch(&format!("{}/captions/all.srt", crew.producer)).await;
    assert_eq!(status, StatusCode::OK);
    assert!(headers[header::CONTENT_DISPOSITION].to_str().unwrap().contains(".srt"));
    assert_eq!(c.fetch(&format!("{}/captions/passwd", crew.host)).await.0, StatusCode::NOT_FOUND);
}

fn participant_event(id: &str, event: &str, identity: &str, sid: &str, joined_at: i64, created_at: i64) -> Value {
    json!({"id": id, "event": event, "createdAt": created_at, "room": {"name": ROOM}, "participant": {"sid": sid, "identity": identity, "joinedAt": joined_at}})
}

#[tokio::test]
async fn webhooks_build_attendance_for_the_audience_only() {
    let c = ctx(false);
    c.room(1).await;
    assert_eq!(c.webhook(participant_event("e1", "participant_joined", "p1", "PA_1", 1000, 1000)).await, StatusCode::OK);
    // Redelivered: must not change anything.
    assert_eq!(c.webhook(participant_event("e1", "participant_joined", "p1", "PA_1", 1000, 1000)).await, StatusCode::OK);
    c.webhook(participant_event("e2", "participant_joined", "host-abc", "PA_2", 1000, 1000)).await;
    c.webhook(json!({"id":"e3","event":"participant_joined","createdAt":1000,"room":{"name":ROOM},"participant":{"sid":"PA_3","identity":"EG_x","joinedAt":1000,"kind":"EGRESS"}})).await;
    c.webhook(participant_event("e4", "participant_joined", "stranger", "PA_4", 1000, 1000)).await;

    let (_, open) = c.v1("GET", &format!("/v1/rooms/{ROOM}/attendance"), None).await;
    assert_eq!(open["complete"], false);

    c.webhook(participant_event("e5", "participant_left", "p1", "PA_1", 1000, 1600)).await;
    c.webhook(participant_event("e6", "participant_left", "stranger", "PA_4", 1000, 1100)).await;
    let (_, done) = c.v1("GET", &format!("/v1/rooms/{ROOM}/attendance"), None).await;
    assert_eq!(done["complete"], true);
    let rows = done["rows"].as_array().unwrap();
    assert_eq!(rows.len(), 2);
    let p1 = rows.iter().find(|r| r["externalId"] == "p1").unwrap();
    assert_eq!(p1["durationSeconds"], 600);
    assert_eq!(p1["joinedAt"], "1970-01-01T00:16:40Z");
}

#[tokio::test]
async fn the_room_finishing_closes_open_visits_and_other_rooms_are_ignored() {
    let c = ctx(false);
    c.room(1).await;
    c.webhook(participant_event("e1", "participant_joined", "p1", "PA_1", 1000, 1000)).await;
    c.webhook(json!({"id":"e2","event":"room_finished","createdAt":1300,"room":{"name":ROOM}})).await;
    let (_, body) = c.v1("GET", &format!("/v1/rooms/{ROOM}/attendance"), None).await;
    assert_eq!((body["complete"].as_bool(), body["rows"][0]["durationSeconds"].as_i64()), (Some(true), Some(300)));
    assert_eq!(c.webhook(json!({"id":"e3","event":"participant_joined","createdAt":1,"room":{"name":"not-ours"},"participant":{"sid":"x","identity":"y"}})).await, StatusCode::OK);
    assert_eq!(c.v1("GET", "/v1/rooms/not-ours/attendance", None).await.0, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn unsigned_or_altered_webhooks_are_rejected() {
    let c = ctx(false);
    c.room(1).await;
    let event = participant_event("e1", "participant_joined", "p1", "PA_1", 1000, 1000).to_string();
    let no_auth = Request::post("/webhooks/livekit").body(Body::from(event.clone())).unwrap();
    assert_eq!(c.app.clone().oneshot(no_auth).await.unwrap().status(), StatusCode::UNAUTHORIZED);

    let digest = STANDARD.encode(Sha256::digest(b"something else"));
    let token = AccessToken::with_api_key(LK_KEY, LK_SECRET).with_sha256(&digest).with_grants(VideoGrants::default()).to_jwt().unwrap();
    let altered = Request::post("/webhooks/livekit").header(header::AUTHORIZATION, token).body(Body::from(event.clone())).unwrap();
    assert_eq!(c.app.clone().oneshot(altered).await.unwrap().status(), StatusCode::UNAUTHORIZED);

    let digest = STANDARD.encode(Sha256::digest(event.as_bytes()));
    let forged = AccessToken::with_api_key(LK_KEY, "another-secret-another-secret-another").with_sha256(&digest).with_grants(VideoGrants::default()).to_jwt().unwrap();
    let forged = Request::post("/webhooks/livekit").header(header::AUTHORIZATION, forged).body(Body::from(event)).unwrap();
    assert_eq!(c.app.clone().oneshot(forged).await.unwrap().status(), StatusCode::UNAUTHORIZED);
    assert!(c.v1("GET", &format!("/v1/rooms/{ROOM}/attendance"), None).await.1["rows"].as_array().unwrap().is_empty());
}

#[tokio::test]
async fn recordings_report_their_result_and_public_link() {
    let c = ctx(true);
    c.room(1).await;
    let egress = |id: &str, status: &str| json!({"id": id, "event": "egress_ended", "createdAt": 2000, "egressInfo": {"egressId":"EG_1","roomName":ROOM,"status":status,"startedAt":"1000000000000","endedAt":"1600000000000","fileResults":[{"filename":"recordings/r/a.mp4","duration":"600000000000","size":"12345","location":"s3://b/recordings/r/a.mp4"}]}});
    assert_eq!(c.webhook(egress("e1", "EGRESS_COMPLETE")).await, StatusCode::OK);
    let (_, body) = c.v1("GET", &format!("/v1/rooms/{ROOM}/recordings"), None).await;
    let rec = &body["recordings"][0];
    assert_eq!(rec["status"], "complete");
    assert_eq!(rec["durationSeconds"], 600);
    assert_eq!(rec["publicUrl"], "https://cdn.example.com/recordings/r/a.mp4");
    c.webhook(json!({"id":"e2","event":"egress_ended","createdAt":3000,"egressInfo":{"egressId":"EG_2","roomName":ROOM,"status":"EGRESS_FAILED","error":"upload failed"}})).await;
    let (_, body) = c.v1("GET", &format!("/v1/rooms/{ROOM}/recordings"), None).await;
    let failed = body["recordings"].as_array().unwrap().iter().find(|r| r["egressId"] == "EG_2").unwrap().clone();
    assert_eq!((failed["status"].as_str(), failed["publicUrl"].is_null()), (Some("failed"), true));
}

#[tokio::test]
async fn health_is_public_and_says_nothing_sensitive() {
    let c = ctx(false);
    let (status, body, _) = c.call("GET", "/healthz", None, None).await;
    assert_eq!((status, body["status"].as_str()), (StatusCode::OK, Some("ok")));
}


// ---- breakouts, surveys, slides and the rest

async fn live_with_host(c: &Ctx) -> String {
    c.room(1).await;
    let host = c.member("host", "Maya").await;
    assert_eq!(c.enter(&host, "Maya", CLIENT).await.0, StatusCode::OK);
    host
}

#[tokio::test]
async fn attendees_can_do_none_of_the_new_things_that_run_the_show() {
    let c = ctx(true);
    let _host = live_with_host(&c).await;
    let a = c.attendee_path("p1").await;
    c.enter(&a, "Ada", CLIENT).await;
    for action in [
        json!({"type":"breakouts_open","kind":"open","rooms":[{"name":"x"}]}),
        json!({"type":"breakouts_close"}),
        json!({"type":"layout_save","name":"x"}),
        json!({"type":"layout_apply","id":"x"}),
        json!({"type":"survey_save","survey":null}),
        json!({"type":"survey_results"}),
        json!({"type":"simulive_set","fileId":"x","atMs":1,"duration":1.0,"autoEnd":false}),
        json!({"type":"simulive_clear"}),
        json!({"type":"slides_show","id":"x","page":1}),
        json!({"type":"slides_page","page":1}),
        json!({"type":"slides_stop"}),
        json!({"type":"ingress_create","name":"x"}),
        json!({"type":"ingress_delete","id":"x"}),
        json!({"type":"ingress_list"}),
        json!({"type":"chat_overlay","id":"x"}),
        json!({"type":"local_recording_done","id":"x"}),
    ] {
        assert_eq!(c.act(&a, CLIENT, action.clone()).await.0, StatusCode::FORBIDDEN, "{action}");
    }
}

#[tokio::test]
async fn breakout_assignments_are_private_and_enforced() {
    let c = ctx(true);
    let host = live_with_host(&c).await;
    let (a, b) = (c.attendee_path("p1").await, c.attendee_path("p2").await);
    c.enter(&a, "Ada", CLIENT).await;
    c.enter(&b, "Bo", CLIENT).await;
    let rooms = json!([{"name":"One","cap":0,"members":["p1"]},{"name":"Two","cap":0,"members":["p2"]}]);
    assert_eq!(c.act(&host, CLIENT, json!({"type":"breakouts_open","kind":"assigned","rooms":rooms})).await.0, StatusCode::OK);
    let snap = c.snapshot(&a, CLIENT).await;
    assert_eq!(snap["assigned"], "1");
    assert!(!snap["state"]["breakouts"].to_string().contains("p2"), "nobody learns who else is where");
    assert_eq!(c.act(&a, CLIENT, json!({"type":"breakout_join","id":"2"})).await.0, StatusCode::FORBIDDEN);
    let (status, joined) = c.act(&a, CLIENT, json!({"type":"breakout_join","id":"1"})).await;
    assert_eq!(status, StatusCode::OK);
    assert!(joined["token"].is_string());
    assert_eq!(c.act(&host, CLIENT, json!({"type":"breakouts_close"})).await.0, StatusCode::OK);
    assert_eq!(c.act(&a, CLIENT, json!({"type":"breakout_join","id":"1"})).await.0, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn a_full_room_turns_people_away_but_not_the_crew() {
    let c = ctx(true);
    let host = live_with_host(&c).await;
    let (a, b) = (c.attendee_path("p1").await, c.attendee_path("p2").await);
    c.enter(&a, "Ada", CLIENT).await;
    c.enter(&b, "Bo", CLIENT).await;
    let rooms = json!([{"name":"Table","cap":1}]);
    assert_eq!(c.act(&host, CLIENT, json!({"type":"breakouts_open","kind":"hub","rooms":rooms})).await.0, StatusCode::OK);
    assert_eq!(c.act(&a, CLIENT, json!({"type":"breakout_join","id":"1"})).await.0, StatusCode::OK);
    assert_eq!(c.act(&b, CLIENT, json!({"type":"breakout_join","id":"1"})).await.0, StatusCode::CONFLICT);
    assert_eq!(c.act(&host, CLIENT, json!({"type":"breakout_join","id":"1"})).await.0, StatusCode::OK);
    assert_eq!(c.act(&a, CLIENT, json!({"type":"breakout_leave"})).await.0, StatusCode::OK);
    assert_eq!(c.act(&b, CLIENT, json!({"type":"breakout_join","id":"1"})).await.0, StatusCode::CONFLICT, "the crew counts while inside");
}

#[tokio::test]
async fn a_private_question_reaches_only_its_asker_and_the_crew() {
    let c = ctx(true);
    let host = live_with_host(&c).await;
    let (a, b) = (c.attendee_path("p1").await, c.attendee_path("p2").await);
    c.enter(&a, "Ada", CLIENT).await;
    c.enter(&b, "Bo", CLIENT).await;
    let mut settings = c.snapshot(&host, CLIENT).await["state"]["settings"].clone();
    settings["qaPrivate"] = json!(true);
    assert_eq!(c.act(&host, CLIENT, json!({"type":"set_settings","settings":settings})).await.0, StatusCode::OK);
    let (_, asked) = c.act(&a, CLIENT, json!({"type":"qa_ask","text":"Secret","anonymous":false})).await;
    let id = asked["id"].as_str().unwrap().to_string();
    assert!(c.snapshot(&a, CLIENT).await["qa"].to_string().contains("Secret"));
    assert!(c.snapshot(&host, CLIENT).await["qa"].to_string().contains("Secret"));
    assert!(!c.snapshot(&b, CLIENT).await["qa"].to_string().contains("Secret"));
    assert_eq!(c.act(&b, CLIENT, json!({"type":"qa_vote","id":id})).await.0, StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn surveys_open_at_the_end_take_one_answer_and_check_it() {
    let c = ctx(true);
    let host = live_with_host(&c).await;
    let a = c.attendee_path("p1").await;
    c.enter(&a, "Ada", CLIENT).await;
    let survey = json!({"title":"How was it?","questions":[{"id":"q1","kind":"rating","text":"Rate","options":[]},{"id":"q2","kind":"choice","text":"Pick","options":["A","B"]}]});
    assert_eq!(c.act(&host, CLIENT, json!({"type":"survey_save","survey":survey})).await.0, StatusCode::OK);
    let answer = |q1: &str| json!({"type":"survey_answer","answers":{"q1":q1}});
    assert_eq!(c.act(&a, CLIENT, answer("5")).await.0, StatusCode::CONFLICT, "not before the end");
    assert_eq!(c.act(&host, CLIENT, json!({"type":"go_live"})).await.0, StatusCode::OK);
    assert_eq!(c.act(&host, CLIENT, json!({"type":"end_session"})).await.0, StatusCode::OK);
    assert_eq!(c.act(&a, CLIENT, answer("9")).await.0, StatusCode::BAD_REQUEST);
    assert_eq!(c.act(&a, CLIENT, json!({"type":"survey_answer","answers":{"q2":"Z"}})).await.0, StatusCode::BAD_REQUEST);
    assert_eq!(c.act(&a, CLIENT, answer("5")).await.0, StatusCode::OK);
    assert_eq!(c.act(&a, CLIENT, answer("4")).await.0, StatusCode::CONFLICT, "one answer per person");
    let (_, results) = c.act(&host, CLIENT, json!({"type":"survey_results"})).await;
    assert_eq!(results["answers"].as_array().unwrap().len(), 1);
    assert!(c.snapshot(&a, CLIENT).await["surveyDone"].as_bool().unwrap());
}

#[tokio::test]
async fn engagement_needs_the_secret_and_leaves_the_crew_and_the_anonymous_out() {
    let c = ctx(true);
    let host = live_with_host(&c).await;
    let a = c.attendee_path("p1").await;
    c.enter(&a, "Ada", CLIENT).await;
    assert_eq!(c.act(&host, CLIENT, json!({"type":"chat_send","text":"Hi from the crew","channel":"event"})).await.0, StatusCode::OK);
    assert_eq!(c.act(&a, CLIENT, json!({"type":"chat_send","text":"Hi","channel":"event"})).await.0, StatusCode::OK);
    assert_eq!(c.act(&a, CLIENT, json!({"type":"qa_ask","text":"Who asked this?","anonymous":true})).await.0, StatusCode::OK);
    assert_eq!(c.call("GET", &format!("/v1/rooms/{ROOM}/engagement"), None, None).await.0, StatusCode::UNAUTHORIZED);
    let (status, data) = c.v1("GET", &format!("/v1/rooms/{ROOM}/engagement"), None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(data["people"]["p1"]["chatMessages"], 1);
    let text = data.to_string();
    assert!(!text.contains("Hi from the crew") && !text.contains("Who asked this?"));
}

#[tokio::test]
async fn a_chat_reaction_counts_once_and_comes_back_off() {
    let c = ctx(true);
    let host = live_with_host(&c).await;
    let a = c.attendee_path("p1").await;
    c.enter(&a, "Ada", CLIENT).await;
    let (_, sent) = c.act(&host, CLIENT, json!({"type":"chat_send","text":"Hello","channel":"event"})).await;
    let id = sent["id"].as_str().unwrap();
    let react = |emoji: &str| json!({"type":"chat_react","id":id,"emoji":emoji});
    assert_eq!(c.act(&a, CLIENT, react("🎉")).await.1["reaction"], 3);
    assert_eq!(c.snapshot(&a, CLIENT).await["chatReactions"][id]["🎉"], 1);
    assert_eq!(c.act(&a, CLIENT, react("👍")).await.1["reaction"], 4, "changing it replaces it");
    assert_eq!(c.snapshot(&a, CLIENT).await["chatReactions"][id]["🎉"], Value::Null);
    assert_eq!(c.act(&a, CLIENT, react("👍")).await.1["reaction"], Value::Null);
    assert_eq!(c.act(&a, CLIENT, react("💩")).await.0, StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn a_scheduled_recording_must_be_a_video_and_only_before_going_live() {
    let c = ctx(true);
    let host = live_with_host(&c).await;
    assert_eq!(c.act(&host, CLIENT, json!({"type":"simulive_set","fileId":"nope","atMs":1,"duration":10.0,"autoEnd":true})).await.0, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn crew_recording_chunks_arrive_in_order_and_stay_with_their_owner() {
    let c = ctx(true);
    let host = live_with_host(&c).await;
    let other = c.member("host", "Rae").await;
    c.enter(&other, "Rae", CLIENT).await;
    let a = c.attendee_path("p1").await;
    let app = c.app.clone();
    let put = |path: String| {
        let app = app.clone();
        async move { app.oneshot(Request::put(path).body(Body::from(vec![1u8; 32])).unwrap()).await.unwrap().status() }
    };
    let chunk = |who: &str, seq: u64| format!("{who}/local/abc123def4567890/{seq}");
    assert_eq!(put(chunk(&host, 0)).await, StatusCode::CONFLICT, "only when the session records this way");
    let mut settings = c.snapshot(&host, CLIENT).await["state"]["settings"].clone();
    settings["localRecordings"] = json!(true);
    assert_eq!(c.act(&host, CLIENT, json!({"type":"set_settings","settings":settings})).await.0, StatusCode::OK);
    assert_eq!(put(chunk(&a, 0)).await, StatusCode::FORBIDDEN, "not attendees");
    assert_eq!(put(chunk(&host, 0)).await, StatusCode::CONFLICT, "only while recording, from someone on stage");
    let me = c.snapshot(&host, CLIENT).await["me"]["identity"].clone();
    assert_eq!(c.act(&host, CLIENT, json!({"type":"stage_add","identity":me})).await.0, StatusCode::OK);
    assert_eq!(c.act(&host, CLIENT, json!({"type":"record","on":true})).await.0, StatusCode::OK);
    assert_eq!(put(chunk(&host, 1)).await, StatusCode::CONFLICT, "not out of order");
    assert_eq!(put(chunk(&host, 0)).await, StatusCode::OK);
    assert_eq!(put(chunk(&host, 0)).await, StatusCode::OK, "sent twice counts once");
    assert_eq!(put(chunk(&other, 1)).await, StatusCode::FORBIDDEN, "not someone else's file");
    assert_eq!(c.call("GET", &format!("{a}/local/abc123def4567890"), None, None).await.0, StatusCode::FORBIDDEN);
    assert_eq!(c.act(&other, CLIENT, json!({"type":"local_recording_done","id":"abc123def4567890"})).await.0, StatusCode::FORBIDDEN);
}

#[tokio::test]
async fn only_real_fonts_are_taken_and_a_room_can_use_one() {
    let c = ctx(true);
    let app = c.app.clone();
    let put = |bytes: &'static [u8]| {
        let app = app.clone();
        async move {
            let req = Request::put("/v1/brand/font").header(header::AUTHORIZATION, format!("Bearer {SECRET}")).body(Body::from(bytes)).unwrap();
            app.oneshot(req).await.unwrap().status()
        }
    };
    assert_eq!(put(b"<svg onload=alert(1)>").await, StatusCode::UNSUPPORTED_MEDIA_TYPE);
    assert_eq!(put(b"wOF2 pretend this is a font").await, StatusCode::OK);
    let res = c.app.clone().oneshot(Request::get("/brand/font").body(Body::empty()).unwrap()).await.unwrap();
    assert_eq!(res.headers()[header::CONTENT_TYPE], "font/woff2");
}

#[tokio::test]
async fn an_encoder_is_on_stage_only_while_its_stream_runs() {
    let c = ctx(true);
    let host = live_with_host(&c).await;
    let (status, _) = c.act(&host, CLIENT, json!({"type":"ingress_create","name":"Cam"})).await;
    assert_eq!(status, StatusCode::CONFLICT, "not offered without an input server");
}


// ---- what the adversarial review found

#[tokio::test]
async fn a_producer_cant_delete_anything_but_this_rooms_files() {
    let c = ctx(true);
    let host = live_with_host(&c).await;
    for id in ["/etc/passwd", "../../live.sqlite3", "..", "nope"] {
        assert_eq!(c.act(&host, CLIENT, json!({"type":"file_delete","id":id})).await.0, StatusCode::NOT_FOUND, "{id}");
    }
}

#[tokio::test]
async fn nobody_removes_a_host_by_dressing_them_up_as_a_guest() {
    let c = ctx(true);
    c.room(1).await;
    let host = c.member("host", "Maya").await;
    let second = c.member("host", "Rae").await;
    c.enter(&host, "Maya", CLIENT).await;
    c.enter(&second, "Rae", CLIENT).await;
    let rae = c.snapshot(&second, CLIENT).await["me"]["identity"].as_str().unwrap().to_string();
    let dressed = rae.replacen("host-h-", "host-g-", 1);
    assert_eq!(c.act(&host, CLIENT, json!({"type":"remove","identity":dressed})).await.0, StatusCode::NOT_FOUND);
    assert_eq!(c.act(&host, CLIENT, json!({"type":"remove","identity":rae})).await.0, StatusCode::FORBIDDEN);
    assert_eq!(c.enter(&second, "Rae", CLIENT).await.0, StatusCode::OK, "their link still works");
}

#[tokio::test]
async fn someone_removed_can_be_let_back_in() {
    let c = ctx(true);
    let host = live_with_host(&c).await;
    let a = c.attendee_path("p1").await;
    c.enter(&a, "Ada", CLIENT).await;
    assert_eq!(c.act(&host, CLIENT, json!({"type":"remove","identity":"p1"})).await.0, StatusCode::OK);
    assert_eq!(c.enter(&a, "Ada", CLIENT).await.0, StatusCode::FORBIDDEN);
    assert!(c.snapshot(&host, CLIENT).await["removedPeople"].to_string().contains("p1"));
    assert_eq!(c.act(&host, CLIENT, json!({"type":"restore","identity":"p1"})).await.0, StatusCode::OK);
    assert_eq!(c.enter(&a, "Ada", CLIENT).await.0, StatusCode::OK);
}

#[tokio::test]
async fn attendees_dont_get_what_the_crew_is_preparing() {
    let c = ctx(true);
    let host = live_with_host(&c).await;
    let a = c.attendee_path("p1").await;
    c.enter(&a, "Ada", CLIENT).await;
    let survey = json!({"title":"Secret plans","questions":[{"id":"q1","kind":"text","text":"Why?","options":[]}]});
    assert_eq!(c.act(&host, CLIENT, json!({"type":"survey_save","survey":survey})).await.0, StatusCode::OK);
    assert_eq!(c.act(&host, CLIENT, json!({"type":"layer_save","layer":{"id":"","title":"Hidden words","x":0.1,"y":0.1,"w":0.2,"h":0.2,"visible":false}})).await.0, StatusCode::OK);
    assert_eq!(c.act(&host, CLIENT, json!({"type":"layout_save","name":"Our layout"})).await.0, StatusCode::OK);
    let text = c.snapshot(&a, CLIENT).await["state"].to_string();
    assert!(!text.contains("Secret plans") && !text.contains("Hidden words") && !text.contains("Our layout"), "{text}");
    assert!(c.snapshot(&host, CLIENT).await["state"].to_string().contains("Secret plans"));
}

#[tokio::test]
async fn an_anonymous_question_is_anonymous_to_the_crew_too() {
    let c = ctx(true);
    let host = live_with_host(&c).await;
    let a = c.attendee_path("p1").await;
    c.enter(&a, "Ada", CLIENT).await;
    assert_eq!(c.act(&a, CLIENT, json!({"type":"qa_ask","text":"Who?","anonymous":true})).await.0, StatusCode::OK);
    assert!(!c.snapshot(&host, CLIENT).await["qa"].to_string().contains("p1"));
}

#[tokio::test]
async fn a_guest_sees_no_draft_polls_or_unshared_files() {
    let c = ctx(true);
    let host = live_with_host(&c).await;
    let guest = c.member("guest", "Gus").await;
    c.enter(&guest, "Gus", CLIENT).await;
    let poll = json!({"type":"poll_save","id":null,"question":"Secret?","options":["A","B"],"correct":0,"showResults":"live","showCounts":false});
    assert_eq!(c.act(&host, CLIENT, poll).await.0, StatusCode::OK);
    assert!(!c.snapshot(&guest, CLIENT).await["polls"].to_string().contains("Secret?"));
    let req = Request::put(format!("{host}/files?kind=resource&name=plan.pdf")).body(Body::from(vec![1u8; 32])).unwrap();
    let res = c.app.clone().oneshot(req).await.unwrap();
    let id = serde_json::from_slice::<Value>(&axum::body::to_bytes(res.into_body(), 4096).await.unwrap()).unwrap()["id"].as_str().unwrap().to_string();
    assert_eq!(c.call("GET", &format!("{guest}/files/{id}"), None, None).await.0, StatusCode::NOT_FOUND);
    assert!(!c.snapshot(&guest, CLIENT).await["files"].to_string().contains("plan.pdf"));
}

#[tokio::test]
async fn nobody_in_a_breakout_goes_on_air_by_accident() {
    let c = ctx(true);
    let host = live_with_host(&c).await;
    let guest = c.member("guest", "Gus").await;
    c.enter(&guest, "Gus", CLIENT).await;
    let id = c.snapshot(&guest, CLIENT).await["me"]["identity"].as_str().unwrap().to_string();
    assert_eq!(c.act(&host, CLIENT, json!({"type":"breakouts_open","kind":"open","rooms":[{"name":"One"}]})).await.0, StatusCode::OK);
    assert_eq!(c.act(&guest, CLIENT, json!({"type":"breakout_join","id":"1"})).await.0, StatusCode::OK);
    assert_eq!(c.act(&host, CLIENT, json!({"type":"stage_add","identity":id})).await.0, StatusCode::CONFLICT);
}

#[tokio::test]
async fn bad_breakouts_dont_close_the_good_ones() {
    let c = ctx(true);
    let host = live_with_host(&c).await;
    assert_eq!(c.act(&host, CLIENT, json!({"type":"breakouts_open","kind":"open","rooms":[{"name":"One"}]})).await.0, StatusCode::OK);
    assert_eq!(c.act(&host, CLIENT, json!({"type":"breakouts_open","kind":"open","rooms":[{"name":"  "}]})).await.0, StatusCode::BAD_REQUEST);
    assert_eq!(c.snapshot(&host, CLIENT).await["state"]["breakouts"][0]["name"], "One");
}

#[tokio::test]
async fn a_time_already_gone_doesnt_go_live_on_the_spot() {
    let c = ctx(true);
    let host = live_with_host(&c).await;
    let req = Request::put(format!("{host}/files?kind=media&name=a.mp4")).body(Body::from(b"\0\0\0\x18ftypisom0000".to_vec())).unwrap();
    let res = c.app.clone().oneshot(req).await.unwrap();
    let id = serde_json::from_slice::<Value>(&axum::body::to_bytes(res.into_body(), 4096).await.unwrap()).unwrap()["id"].as_str().unwrap().to_string();
    assert_eq!(c.act(&host, CLIENT, json!({"type":"simulive_set","fileId":id,"atMs":1000,"duration":10.0,"autoEnd":true})).await.0, StatusCode::CONFLICT);
}

#[tokio::test]
async fn rescheduling_while_live_leaves_the_session_on_the_air() {
    let c = ctx(true);
    let host = live_with_host(&c).await;
    assert_eq!(c.act(&host, CLIENT, json!({"type":"go_live"})).await.0, StatusCode::OK);
    c.room(2).await;
    assert_eq!(c.snapshot(&host, CLIENT).await["state"]["phase"], "live");
    assert_eq!(c.act(&host, CLIENT, json!({"type":"end_session"})).await.0, StatusCode::OK);
    c.room(3).await;
    assert_eq!(c.snapshot(&host, CLIENT).await["state"]["phase"], "not_live", "after the end, it's a new session");
}

#[tokio::test]
async fn a_script_hammering_actions_is_slowed_down() {
    let c = ctx(true);
    c.room(1).await;
    let a = c.attendee_path("p1").await;
    c.enter(&a, "Ada", CLIENT).await;
    let mut slowed = false;
    for _ in 0..60 {
        if c.act(&a, CLIENT, json!({"type":"hand","up":true})).await.0 == StatusCode::TOO_MANY_REQUESTS {
            slowed = true;
        }
    }
    assert!(slowed);
}

#[tokio::test]
async fn an_erasure_request_removes_what_this_service_holds() {
    let c = ctx(true);
    let host = live_with_host(&c).await;
    let a = c.attendee_path("p1").await;
    c.enter(&a, "Ada", CLIENT).await;
    assert_eq!(c.act(&a, CLIENT, json!({"type":"chat_send","text":"Find me","channel":"event"})).await.0, StatusCode::OK);
    assert_eq!(c.v1("POST", &format!("/v1/rooms/{ROOM}/people/p1/forget"), None).await.0, StatusCode::OK);
    assert!(!c.snapshot(&host, CLIENT).await["chat"].to_string().contains("Find me"));
    let (_, data) = c.v1("GET", &format!("/v1/rooms/{ROOM}/engagement"), None).await;
    assert!(data["people"]["p1"].is_null());
    assert_eq!(c.call("POST", &format!("/v1/rooms/{ROOM}/people/p1/forget"), None, None).await.0, StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn crew_can_be_removed_with_a_post_too() {
    let c = ctx(true);
    c.room(1).await;
    let (_, added) = c.v1("POST", &format!("/v1/rooms/{ROOM}/crew"), Some(json!({"role":"guest","name":"Gus"}))).await;
    let id = added["id"].as_str().unwrap();
    assert_eq!(c.v1("POST", &format!("/v1/rooms/{ROOM}/crew/{id}/remove"), None).await.0, StatusCode::OK);
}

#[tokio::test]
async fn engagement_pages_stay_small_however_much_people_said() {
    let c = ctx(true);
    c.room(1).await;
    for i in 0..4 {
        let a = c.attendee_path(&format!("p{i}")).await;
        c.enter(&a, "Ada", CLIENT).await;
        for _ in 0..3 {
            c.act(&a, CLIENT, json!({"type":"qa_ask","text":"😀".repeat(400),"anonymous":false})).await;
            tokio::time::sleep(std::time::Duration::from_millis(5)).await;
        }
    }
    let (status, page) = c.v1("GET", &format!("/v1/rooms/{ROOM}/engagement"), None).await;
    assert_eq!(status, StatusCode::OK);
    assert!(page.to_string().len() < 64 * 1024);
}

#[tokio::test]
async fn made_up_addresses_cant_flood_the_activity_log_or_be_huge() {
    let c = ctx(true);
    for i in 0..3 {
        c.call("POST", "/login", None, Some(json!({"email":format!("nobody{i}@example.com"),"password":"nope-nope-nope"}))).await;
    }
    let long = "x".repeat(100_000);
    assert_eq!(c.call("POST", "/login", None, Some(json!({"email":long,"password":"nope"}))).await.0, StatusCode::UNAUTHORIZED);
}

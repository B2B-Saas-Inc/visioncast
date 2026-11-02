//! `/login` and `/admin`: the live server's own settings. Everyone has their own account, there's no
//! sign-up, and what each person did is on record. An owner adds people, who set their own password
//! from a one-time link. A VisionCast Administrator gets signed straight in from the studio.
use argon2::{
    password_hash::{rand_core::OsRng, PasswordHash, PasswordHasher, PasswordVerifier, SaltString},
    Argon2,
};
use axum::{
    extract::{Path, Request, State},
    http::{header, HeaderMap, HeaderValue, StatusCode},
    middleware::Next,
    response::{Html, IntoResponse, Redirect, Response},
    Extension, Json,
};
use rand::Rng;
use serde::Deserialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::sync::Arc;

use crate::app::{iso, now, ApiError, ApiResult, AppState};
use crate::store::Account;

const SESSION_SECONDS: i64 = 8 * 3600;
const STUDIO_LINK_SECONDS: i64 = 60;
const SET_PASSWORD_SECONDS: i64 = 72 * 3600;
const COOKIE: &str = "vc_admin";
const MIN_PASSWORD: usize = 12;

/// Who did something: goes on the record.
#[derive(Clone, Debug)]
pub struct Actor {
    pub account: Account,
}

pub fn record(app: &AppState, actor: Option<&Actor>, action: &str, detail: &str) {
    let who = actor.map_or("Someone", |a| a.account.email.as_str());
    let detail: String = detail.chars().take(300).collect();
    if let Err(err) = app.store.audit(now(), who, action, &detail) {
        tracing::error!(error = %err, "could not write the activity log");
    }
}

fn digest(token: &str) -> String {
    Sha256::digest(token.as_bytes()).iter().map(|b| format!("{b:02x}")).collect()
}

fn new_token() -> String {
    let mut rng = rand::rng();
    (0..32).map(|_| format!("{:02x}", rng.random::<u8>())).collect()
}

fn hash_password(password: &str) -> Result<String, ApiError> {
    let salt = SaltString::generate(&mut OsRng);
    Argon2::default().hash_password(password.as_bytes(), &salt).map(|h| h.to_string()).map_err(|_| ApiError(StatusCode::INTERNAL_SERVER_ERROR, "internal_error"))
}

fn password_matches(password: &str, hash: &str) -> bool {
    PasswordHash::new(hash).is_ok_and(|parsed| Argon2::default().verify_password(password.as_bytes(), &parsed).is_ok())
}

fn cookie_header(app: &AppState, value: &str, max_age: i64) -> HeaderValue {
    let secure = if app.config.public_url.starts_with("https://") { "; Secure" } else { "" };
    HeaderValue::from_str(&format!("{COOKIE}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}{secure}")).expect("valid header text")
}

fn start_session(app: &AppState, account: &Account) -> ApiResult<HeaderValue> {
    let token = new_token();
    app.store.put_session(&digest(&token), &account.id, now() + SESSION_SECONDS)?;
    let mut updated = account.clone();
    updated.last_login_at = now();
    app.store.put_account(&updated)?;
    Ok(cookie_header(app, &token, SESSION_SECONDS))
}

fn cookie_token(headers: &HeaderMap) -> Option<String> {
    headers
        .get(header::COOKIE)?
        .to_str()
        .ok()?
        .split(';')
        .filter_map(|c| c.trim().strip_prefix(&format!("{COOKIE}=")))
        .next()
        .map(str::to_string)
}

/// Lets the request through for someone with a good session, and says who they are.
pub async fn require_admin(State(app): State<Arc<AppState>>, headers: HeaderMap, mut request: Request, next: Next) -> Response {
    let account = cookie_token(&headers).and_then(|t| app.store.session_account(&digest(&t), now()).ok().flatten());
    let Some(account) = account else {
        return ApiError(StatusCode::UNAUTHORIZED, "unauthorized").into_response();
    };
    request.extensions_mut().insert(Actor { account });
    next.run(request).await
}

// ---- pages. Empty without a session: what's in them comes from the API, which checks.

pub async fn login_page() -> Response {
    ([(header::CACHE_CONTROL, "no-store")], Html(include_str!("../assets/login.html"))).into_response()
}

pub async fn set_password_page(Path(_token): Path<String>) -> Response {
    ([(header::CACHE_CONTROL, "no-store"), (header::REFERRER_POLICY, "no-referrer")], Html(include_str!("../assets/login.html"))).into_response()
}

pub async fn admin_page() -> Response {
    ([(header::CACHE_CONTROL, "no-store")], Html(include_str!("../assets/admin.html"))).into_response()
}

// ---- signing in

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LoginBody {
    email: String,
    password: String,
}

/// Tries at signing in. One person guessing from one place is stopped quickly. Someone trying an
/// address from everywhere is stopped later, so that a stranger can't easily lock its owner out.
#[derive(Default)]
pub struct Failures(HashMap<String, (u32, i64)>);

const TRIES_FROM_ONE_PLACE: u32 = 8;
const TRIES_FROM_ANYWHERE: u32 = 60;
const TRIES_WINDOW: i64 = 900;
const REMEMBERED_ADDRESSES: usize = 50_000;

impl Failures {
    fn count(&mut self, key: String, at: i64, limit: u32) -> bool {
        let entry = self.0.entry(key).or_insert((0, at));
        if at - entry.1 > TRIES_WINDOW {
            *entry = (0, at);
        }
        entry.0 += 1;
        entry.0 <= limit
    }

    /// Counts a try. False if it's one too many.
    fn attempt(&mut self, email: &str, from: &str, at: i64) -> bool {
        if self.0.len() > REMEMBERED_ADDRESSES {
            self.0.retain(|_, (_, first)| at - *first <= TRIES_WINDOW);
            if self.0.len() > REMEMBERED_ADDRESSES {
                self.0.clear();
            }
        }
        let here = self.count(format!("{email}|{from}"), at, TRIES_FROM_ONE_PLACE);
        let anywhere = self.count(email.to_string(), at, TRIES_FROM_ANYWHERE);
        here && anywhere
    }

    fn clear(&mut self, email: &str, from: &str) {
        self.0.remove(&format!("{email}|{from}"));
    }
}

/// Who's asking, from the last hop Traefik added. Only when Traefik is known to be in front.
fn client_address(app: &AppState, headers: &axum::http::HeaderMap) -> String {
    if !app.config.trust_forwarded_for {
        return String::new();
    }
    headers.get("x-forwarded-for").and_then(|v| v.to_str().ok()).and_then(|v| v.rsplit(',').next()).map_or_else(String::new, |v| v.trim().chars().take(64).collect())
}

/// Used when the address isn't known, so a wrong address takes as long as a wrong password.
const DUMMY_HASH: &str = "$argon2id$v=19$m=19456,t=2,p=1$c29tZXNhbHRzb21lc2FsdA$CTFhFdXPJO1aFaMaO6Mm5c8y7cJHAph8ArZWb2GDtk0";

pub async fn login(State(app): State<Arc<AppState>>, headers: axum::http::HeaderMap, Json(body): Json<LoginBody>) -> ApiResult<Response> {
    let email = body.email.trim().to_lowercase();
    if email.len() > 254 || body.password.len() > 1024 {
        return Err(ApiError(StatusCode::UNAUTHORIZED, "wrong_password"));
    }
    let at = now();
    let from = client_address(&app, &headers);
    // A try counts as soon as it starts, so a burst at once gets no more guesses than one at a time.
    if !app.login_failures.lock().unwrap_or_else(|e| e.into_inner()).attempt(&email, &from, at) {
        return Err(ApiError(StatusCode::TOO_MANY_REQUESTS, "slow_down"));
    }
    // Hashing is slow and takes a lot of memory, on the machine running the rooms. A few at a time.
    let Ok(_permit) = app.login_slots.clone().try_acquire_owned() else {
        return Err(ApiError(StatusCode::TOO_MANY_REQUESTS, "slow_down"));
    };
    let account = app.store.account_by_email(&email)?;
    let hash = account.as_ref().map(|a| a.password_hash.clone()).filter(|h| !h.is_empty()).unwrap_or_else(|| DUMMY_HASH.to_string());
    let password = body.password;
    // Password hashing is deliberately slow: it mustn't hold up the rooms.
    let good = tokio::task::spawn_blocking(move || password_matches(&password, &hash)).await.unwrap_or(false);
    let account = account.filter(|a| good && !a.password_hash.is_empty());
    let Some(account) = account else {
        // Logged only for accounts that exist, so made-up addresses can't push real events out.
        if app.store.account_by_email(&email)?.is_some() {
            record(&app, None, "login.failed", &email);
        }
        return Err(ApiError(StatusCode::UNAUTHORIZED, "wrong_password"));
    };
    app.login_failures.lock().unwrap_or_else(|e| e.into_inner()).clear(&email, &from);
    let cookie = start_session(&app, &account)?;
    record(&app, Some(&Actor { account }), "login", "with a password");
    let mut response = Json(json!({ "ok": true })).into_response();
    response.headers_mut().insert(header::SET_COOKIE, cookie);
    Ok(response)
}

/// Ends this session on the server, so a copy of the cookie stops working too.
pub async fn logout(State(app): State<Arc<AppState>>, headers: HeaderMap) -> ApiResult<Response> {
    if let Some(token) = cookie_token(&headers) {
        app.store.delete_session(&digest(&token))?;
    }
    let mut response = Json(json!({ "ok": true })).into_response();
    response.headers_mut().insert(header::SET_COOKIE, cookie_header(&app, "", 0));
    Ok(response)
}

// ---- one-time links

fn one_time_link(app: &AppState, account: &Account, purpose: &str, room: &str, seconds: i64, path: &str) -> ApiResult<String> {
    let token = new_token();
    app.store.put_one_time_link(&digest(&token), &account.id, purpose, room, now() + seconds)?;
    Ok(format!("{}{path}/{token}", app.config.public_url))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SetPassword {
    password: String,
}

/// Sets a password from a one-time link, and signs them in.
pub async fn set_password(State(app): State<Arc<AppState>>, Path(token): Path<String>, Json(body): Json<SetPassword>) -> ApiResult<Response> {
    if body.password.len() < MIN_PASSWORD {
        return Err(ApiError(StatusCode::BAD_REQUEST, "password_too_short"));
    }
    let hash = digest(&token);
    // Checked before hashing, then used only once the hash is ready.
    app.store.peek_one_time_link(&hash, "set_password", now())?.ok_or(ApiError(StatusCode::GONE, "link_used"))?;
    let password = body.password;
    let new_hash = tokio::task::spawn_blocking(move || hash_password(&password)).await.map_err(|_| ApiError(StatusCode::INTERNAL_SERVER_ERROR, "internal_error"))??;
    let (account_id, _) = app.store.take_one_time_link(&hash, "set_password", now())?.ok_or(ApiError(StatusCode::GONE, "link_used"))?;
    let mut account = app.store.account(&account_id)?.ok_or(ApiError(StatusCode::GONE, "link_used"))?;
    account.password_hash = new_hash;
    app.store.put_account(&account)?;
    app.store.delete_sessions_of(&account.id)?;
    let cookie = start_session(&app, &account)?;
    record(&app, Some(&Actor { account }), "password.set", "from a one-time link");
    let mut response = Json(json!({ "ok": true })).into_response();
    response.headers_mut().insert(header::SET_COOKIE, cookie);
    Ok(response)
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LinkBody {
    email: String,
    #[serde(default)]
    name: String,
    #[serde(default)]
    room: String,
}

/// From VisionCast, over the service secret: signs one of its Administrators in, once, within a
/// minute. They get an account here the first time, so what they do is on record as them.
pub async fn create_link(State(app): State<Arc<AppState>>, Json(body): Json<LinkBody>) -> ApiResult<Json<Value>> {
    let email = body.email.trim().to_lowercase();
    if !email.contains('@') || email.len() > 254 || (!body.room.is_empty() && !crate::ticket::valid_id(&body.room)) {
        return Err(ApiError(StatusCode::BAD_REQUEST, "invalid_input"));
    }
    let account = match app.store.account_by_email(&email)? {
        Some(account) => account,
        None => {
            let first = app.store.accounts()?.is_empty();
            let account = Account { id: new_token()[..24].to_string(), email: email.clone(), name: crate::app::clean_name(&body.name, ""), role: if first { "owner" } else { "admin" }.to_string(), password_hash: String::new(), created_at: now(), last_login_at: 0 };
            app.store.put_account(&account)?;
            record(&app, None, "account.added", &format!("{email}, from the studio"));
            account
        }
    };
    let url = one_time_link(&app, &account, "studio", &body.room, STUDIO_LINK_SECONDS, "/login/link")?;
    Ok(Json(json!({ "url": url })))
}

pub async fn use_link(State(app): State<Arc<AppState>>, Path(token): Path<String>) -> Response {
    let refused = || Redirect::to("/login").into_response();
    let Ok(Some((account_id, room))) = app.store.take_one_time_link(&digest(&token), "studio", now()) else { return refused() };
    let Ok(Some(account)) = app.store.account(&account_id) else { return refused() };
    let Ok(cookie) = start_session(&app, &account) else { return refused() };
    record(&app, Some(&Actor { account }), "login", "from the studio");
    let target = if room.is_empty() { "/admin".to_string() } else { format!("/admin#{room}") };
    let mut response = Redirect::to(&target).into_response();
    response.headers_mut().insert(header::SET_COOKIE, cookie);
    response
}

/// Creates the first owner, from the environment, when there's nobody yet.
pub fn seed_first_admin(app: &AppState) -> Result<(), String> {
    if !app.store.accounts().map_err(|e| e.to_string())?.is_empty() {
        return Ok(());
    }
    let Some((email, password)) = app.config.first_admin.clone() else {
        tracing::warn!("nobody can sign in at /admin yet: LIVE_ADMIN_EMAIL needs an @ and LIVE_ADMIN_PASSWORD at least 12 characters. Fix them and restart, or run reset-admin");
        return Ok(());
    };
    let hash = hash_password(&password).map_err(|_| "could not hash the password".to_string())?;
    let account = Account { id: new_token()[..24].to_string(), email: email.clone(), name: String::new(), role: "owner".into(), password_hash: hash, created_at: now(), last_login_at: 0 };
    app.store.put_account(&account).map_err(|e| e.to_string())?;
    record(app, None, "account.added", &format!("{email}, the first owner"));
    Ok(())
}

/// For someone locked out, run on the server itself: a link to set a new password.
pub fn reset_link_for(app: &AppState, email: &str) -> Result<String, String> {
    let email = email.trim().to_lowercase();
    let account = match app.store.account_by_email(&email).map_err(|e| e.to_string())? {
        Some(account) => account,
        None => {
            // With no owner left, this is how one is made, on the server itself.
            let first = !app.store.accounts().map_err(|e| e.to_string())?.iter().any(|a| a.role == "owner");
            if !first {
                return Err(format!("There's no account for {email}."));
            }
            let account = Account { id: new_token()[..24].to_string(), email: email.clone(), name: String::new(), role: "owner".into(), password_hash: String::new(), created_at: now(), last_login_at: 0 };
            app.store.put_account(&account).map_err(|e| e.to_string())?;
            account
        }
    };
    let link = one_time_link(app, &account, "set_password", "", SET_PASSWORD_SECONDS, "/login/set").map_err(|e| e.1.to_string())?;
    record(app, None, "password.reset_link", &format!("{email}, from the server's command line"));
    Ok(link)
}

// ---- my account

pub async fn me(Extension(actor): Extension<Actor>) -> Json<Value> {
    Json(json!({ "id": actor.account.id, "email": actor.account.email, "name": actor.account.name, "role": actor.account.role, "hasPassword": !actor.account.password_hash.is_empty() }))
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ChangePassword {
    #[serde(default)]
    current: String,
    new_password: String,
}

/// Changing a password signs out everywhere else.
pub async fn change_password(State(app): State<Arc<AppState>>, Extension(actor): Extension<Actor>, Json(body): Json<ChangePassword>) -> ApiResult<Response> {
    if body.new_password.len() < MIN_PASSWORD {
        return Err(ApiError(StatusCode::BAD_REQUEST, "password_too_short"));
    }
    let mut account = actor.account.clone();
    if !account.password_hash.is_empty() {
        let (given, stored) = (body.current, account.password_hash.clone());
        let good = tokio::task::spawn_blocking(move || password_matches(&given, &stored)).await.unwrap_or(false);
        if !good {
            return Err(ApiError(StatusCode::FORBIDDEN, "wrong_password"));
        }
    }
    let new = body.new_password;
    account.password_hash = tokio::task::spawn_blocking(move || hash_password(&new)).await.map_err(|_| ApiError(StatusCode::INTERNAL_SERVER_ERROR, "internal_error"))??;
    app.store.put_account(&account)?;
    app.store.delete_sessions_of(&account.id)?;
    let cookie = start_session(&app, &account)?;
    record(&app, Some(&actor), "password.changed", "");
    let mut response = Json(json!({ "ok": true })).into_response();
    response.headers_mut().insert(header::SET_COOKIE, cookie);
    Ok(response)
}

// ---- other people's accounts: owners only

fn require_owner(actor: &Actor) -> ApiResult<()> {
    if actor.account.role == "owner" { Ok(()) } else { Err(ApiError(StatusCode::FORBIDDEN, "owners_only")) }
}

fn account_json(a: &Account) -> Value {
    json!({ "id": a.id, "email": a.email, "name": a.name, "role": a.role, "hasPassword": !a.password_hash.is_empty(),
            "lastLoginAt": if a.last_login_at > 0 { json!(iso(a.last_login_at)) } else { Value::Null } })
}

pub async fn list_accounts(State(app): State<Arc<AppState>>, Extension(actor): Extension<Actor>) -> ApiResult<Json<Value>> {
    require_owner(&actor)?;
    // Everyone, in one place: hosts, producers and guests join by link, so they're listed with their session.
    let titles: std::collections::HashMap<String, String> = app.store.rooms()?.into_iter().map(|r| (r.id, r.title)).collect();
    let crew: Vec<Value> = app.store.all_crew()?.iter().map(|m| {
        let mut row = crate::crew::member_json(&app, m);
        row["room"] = json!(m.room);
        row["roomTitle"] = json!(titles.get(&m.room).cloned().unwrap_or_default());
        row
    }).collect();
    Ok(Json(json!({ "accounts": app.store.accounts()?.iter().map(account_json).collect::<Vec<_>>(), "crew": crew })))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NewAccount {
    email: String,
    #[serde(default)]
    name: String,
    #[serde(default)]
    role: String,
}

/// There's no sign-up: an owner adds someone, and hands them a link to set their own password.
pub async fn add_account(State(app): State<Arc<AppState>>, Extension(actor): Extension<Actor>, Json(body): Json<NewAccount>) -> ApiResult<Json<Value>> {
    require_owner(&actor)?;
    let email = body.email.trim().to_lowercase();
    let role = if body.role.is_empty() { "admin" } else { body.role.as_str() };
    if !email.contains('@') || email.contains(char::is_whitespace) || email.len() > 254 || !["owner", "admin"].contains(&role) {
        return Err(ApiError(StatusCode::BAD_REQUEST, "invalid_input"));
    }
    if app.store.account_by_email(&email)?.is_some() {
        return Err(ApiError(StatusCode::CONFLICT, "account_exists"));
    }
    let account = Account { id: new_token()[..24].to_string(), email: email.clone(), name: crate::app::clean_name(&body.name, ""), role: role.to_string(), password_hash: String::new(), created_at: now(), last_login_at: 0 };
    app.store.put_account(&account)?;
    let link = one_time_link(&app, &account, "set_password", "", SET_PASSWORD_SECONDS, "/login/set")?;
    record(&app, Some(&actor), "account.added", &format!("{email} ({role})"));
    Ok(Json(json!({ "account": account_json(&account), "setPasswordUrl": link })))
}

fn other_account(app: &AppState, id: &str) -> ApiResult<Account> {
    app.store.account(id)?.ok_or(ApiError(StatusCode::NOT_FOUND, "not_found"))
}

/// A forgotten password: signs them out everywhere, and gives a link to set a new one.
pub async fn reset_account(State(app): State<Arc<AppState>>, Extension(actor): Extension<Actor>, Path(id): Path<String>) -> ApiResult<Json<Value>> {
    require_owner(&actor)?;
    let mut account = other_account(&app, &id)?;
    account.password_hash.clear();
    app.store.put_account(&account)?;
    app.store.delete_sessions_of(&account.id)?;
    let link = one_time_link(&app, &account, "set_password", "", SET_PASSWORD_SECONDS, "/login/set")?;
    record(&app, Some(&actor), "account.reset", &account.email);
    Ok(Json(json!({ "setPasswordUrl": link })))
}

pub async fn delete_account(State(app): State<Arc<AppState>>, Extension(actor): Extension<Actor>, Path(id): Path<String>) -> ApiResult<Json<Value>> {
    require_owner(&actor)?;
    let account = other_account(&app, &id)?;
    // One at a time, so two owners removing each other can't leave nobody.
    let _guard = app.account_lock.lock().await;
    if account.role == "owner" && app.store.accounts()?.iter().filter(|a| a.role == "owner").count() <= 1 {
        return Err(ApiError(StatusCode::CONFLICT, "last_owner"));
    }
    app.store.delete_sessions_of(&account.id)?;
    app.store.delete_account(&account.id)?;
    record(&app, Some(&actor), "account.removed", &account.email);
    Ok(Json(json!({ "ok": true })))
}

pub async fn activity(State(app): State<Arc<AppState>>) -> ApiResult<Json<Value>> {
    let rows: Vec<Value> = app.store.audit_rows(300)?.into_iter().map(|(at, actor, action, detail)| json!({ "at": iso(at), "actor": actor, "action": action, "detail": detail })).collect();
    Ok(Json(json!({ "activity": rows })))
}

// ---- the sessions

pub async fn list_rooms(State(app): State<Arc<AppState>>) -> ApiResult<Json<Value>> {
    let mut rooms = app.store.rooms()?;
    rooms.sort_by_key(|r| std::cmp::Reverse(r.starts_at));
    let mut out = vec![];
    for room in rooms {
        // From what's saved: listing sessions shouldn't wake every room up.
        let phase = crate::studio::saved_phase(&app, &room.id)?;
        out.push(json!({ "id": room.id, "slug": room.slug, "title": room.title, "startsAt": iso(room.starts_at), "phase": phase }));
    }
    Ok(Json(json!({ "rooms": out, "publicUrl": app.config.public_url })))
}

pub async fn room_detail(State(app): State<Arc<AppState>>, Path(room): Path<String>) -> ApiResult<Json<Value>> {
    let found = app.store.room(&room)?.ok_or(ApiError(StatusCode::NOT_FOUND, "not_found"))?;
    Ok(Json(json!({
        "id": found.id, "slug": found.slug, "title": found.title, "startsAt": iso(found.starts_at),
        "phase": crate::studio::saved_phase(&app, &room)?,
        "crew": crate::crew::list_json(&app, &room)?,
        "destinations": crate::broadcast::admin_rows(&app, &room)?,
        "recordings": crate::app::recordings_json(&app, &room)?,
        "captionsAvailable": crate::captions::available(&app),
    })))
}

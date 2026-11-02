//! A session's crew: hosts, producers and guests, each with their own link. A link can be replaced
//! or taken away without touching anyone else's, and everything in the room is tied to the person.
use axum::{
    extract::{Path, State},
    http::StatusCode,
    Extension, Json,
};
use rand::Rng;
use serde::Deserialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::sync::Arc;

use crate::admin::Actor;
use crate::app::{clean_name, now, ApiError, ApiResult, AppState};
use crate::store::CrewMember;
use crate::studio::{self, Audience};
use crate::ticket::{self, Role};

const ROLES: [&str; 3] = ["host", "producer", "guest"];

fn role_of(name: &str) -> Option<Role> {
    match name {
        "host" => Some(Role::Host),
        "producer" => Some(Role::Producer),
        "guest" => Some(Role::Guest),
        _ => None,
    }
}

fn random_hex(bytes: usize) -> String {
    let mut rng = rand::rng();
    (0..bytes).map(|_| format!("{:02x}", rng.random::<u8>())).collect()
}

/// A member's link. Signed over their ID and nonce, so a new nonce kills the old link.
pub fn link_of(app: &AppState, m: &CrewMember) -> String {
    let role = role_of(&m.role).unwrap_or(Role::Guest);
    let signed = ticket::sign(&app.config.ticket_key, role, &m.room, &format!("{}_{}", m.id, m.nonce));
    format!("{}/join/{signed}", app.config.public_url)
}

/// The person a crew link belongs to, if that link is still good.
pub fn member_for(app: &AppState, role: Role, room: &str, identity: &str) -> rusqlite::Result<Option<CrewMember>> {
    let Some((id, nonce)) = identity.rsplit_once('_') else { return Ok(None) };
    let Some(member) = app.store.crew_member(id)? else { return Ok(None) };
    let good = member.room == room && member.nonce == nonce && role_of(&member.role) == Some(role);
    Ok(good.then_some(member))
}

pub fn member_json(app: &AppState, m: &CrewMember) -> Value {
    json!({ "id": m.id, "role": m.role, "name": m.name, "title": m.title, "email": m.email, "url": link_of(app, m) })
}

pub async fn list(State(app): State<Arc<AppState>>, Path(room): Path<String>) -> ApiResult<Json<Value>> {
    app.store.room(&room)?.ok_or(ApiError(StatusCode::NOT_FOUND, "not_found"))?;
    Ok(Json(json!({ "crew": list_json(&app, &room)? })))
}

pub fn list_json(app: &AppState, room: &str) -> ApiResult<Value> {
    Ok(json!(app.store.crew_of(room)?.iter().map(|m| member_json(app, m)).collect::<Vec<_>>()))
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct NewMember {
    role: String,
    name: String,
    #[serde(default)]
    title: String,
    #[serde(default)]
    email: String,
    /// Asking again with the same key returns the same person: how VisionCast gives each of its
    /// users one host link per session.
    #[serde(default)]
    key: String,
}

pub async fn add(State(app): State<Arc<AppState>>, Path(room): Path<String>, actor: Option<Extension<Actor>>, Json(body): Json<NewMember>) -> ApiResult<Json<Value>> {
    app.store.room(&room)?.ok_or(ApiError(StatusCode::NOT_FOUND, "not_found"))?;
    if !ROLES.contains(&body.role.as_str()) || body.name.trim().is_empty() || body.email.len() > 254 || body.key.len() > 200 {
        return Err(ApiError(StatusCode::BAD_REQUEST, "invalid_input"));
    }
    let id = if body.key.is_empty() {
        random_hex(12)
    } else {
        let digest = Sha256::digest(format!("{room}|{}", body.key).as_bytes());
        digest.iter().take(12).map(|b| format!("{b:02x}")).collect()
    };
    let existing = app.store.crew_member(&id)?;
    let member = CrewMember {
        id,
        room: room.clone(),
        role: body.role,
        name: clean_name(&body.name, "Crew"),
        title: clean_name(&body.title, ""),
        email: body.email.trim().to_lowercase(),
        // The same person asking again keeps their link.
        nonce: existing.as_ref().map_or_else(|| random_hex(8), |e| e.nonce.clone()),
        created_at: existing.as_ref().map_or_else(now, |e| e.created_at),
    };
    app.store.put_crew(&member)?;
    // Added back: whatever got them removed before is over.
    let identity = studio::crew_identity(&member.role, &member.id);
    let flags = app.store.person_flags(&room, &identity)?;
    if flags.2 {
        app.store.set_person_flags(&room, &identity, (flags.0, flags.1, false))?;
    }
    if existing.is_none() {
        crate::admin::record(&app, actor.as_ref().map(|a| &a.0), "crew.added", &format!("{room}: {} ({})", member.name, member.role));
    }
    Ok(Json(member_json(&app, &member)))
}

fn found(app: &AppState, room: &str, id: &str) -> ApiResult<CrewMember> {
    app.store.crew_member(id)?.filter(|m| m.room == room).ok_or(ApiError(StatusCode::NOT_FOUND, "not_found"))
}

/// Replaces someone's link: the old one stops working and they're put out of the room until they
/// use the new one. For a link that went astray.
pub async fn new_link(State(app): State<Arc<AppState>>, Path((room, id)): Path<(String, String)>, actor: Option<Extension<Actor>>) -> ApiResult<Json<Value>> {
    let mut member = found(&app, &room, &id)?;
    member.nonce = random_hex(8);
    app.store.put_crew(&member)?;
    put_out(&app, &member).await?;
    let identity = studio::crew_identity(&member.role, &member.id);
    let flags = app.store.person_flags(&room, &identity)?;
    if flags.2 {
        app.store.set_person_flags(&room, &identity, (flags.0, flags.1, false))?;
    }
    crate::admin::record(&app, actor.as_ref().map(|a| &a.0), "crew.new_link", &format!("{room}: {}", member.name));
    Ok(Json(member_json(&app, &member)))
}

pub async fn remove(State(app): State<Arc<AppState>>, Path((room, id)): Path<(String, String)>, actor: Option<Extension<Actor>>) -> ApiResult<Json<Value>> {
    let member = found(&app, &room, &id)?;
    app.store.delete_crew(&id)?;
    put_out(&app, &member).await?;
    crate::admin::record(&app, actor.as_ref().map(|a| &a.0), "crew.removed", &format!("{room}: {} ({})", member.name, member.role));
    Ok(Json(json!({ "ok": true })))
}

/// Takes someone out of the live rooms straight away, whatever their browser thinks.
async fn put_out(app: &Arc<AppState>, member: &CrewMember) -> ApiResult<()> {
    let identity = studio::crew_identity(&member.role, &member.id);
    let hub = app.hub(&member.room)?;
    studio::take_off_stage(app, &hub, &member.room, &identity).await?;
    let _ = app.backend.remove_participant(&member.room, &identity).await;
    let _ = app.backend.remove_participant(&studio::green_room(&member.room), &identity).await;
    hub.send(Audience::Crew, "removed", json!({ "identity": identity }));
    hub.disconnect(&identity);
    hub.forget_person(&identity);
    Ok(())
}

//! Join links. A ticket names a room, a person and a role, and is signed so nobody can forge
//! or alter one. Tickets carry no times: the room's schedule decides when they work, so a
//! reschedule never invalidates a link already handed out. They carry no personal data either:
//! the identity is an opaque registration ID.
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
use hmac::{Hmac, Mac};
use sha2::Sha256;
use subtle::ConstantTimeEq;

type HmacSha256 = Hmac<Sha256>;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Role {
    Host,
    Producer,
    Guest,
    Attendee,
    /// The recorder's page: sees what the audience sees and can do nothing.
    Recorder,
    /// The recorder sending one speaker's microphone in for captions, and nothing else.
    CaptionSource,
}

impl Role {
    fn code(self) -> &'static str {
        match self {
            Role::Host => "h",
            Role::Producer => "p",
            Role::Guest => "g",
            Role::Attendee => "a",
            Role::Recorder => "r",
            Role::CaptionSource => "c",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Ticket {
    pub role: Role,
    pub room: String,
    pub identity: String,
}

/// IDs are restricted to characters that are safe in URLs and need no escaping.
pub fn valid_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

fn mac(key: &str, role: &str, room: &str, identity: &str) -> HmacSha256 {
    let mut mac = HmacSha256::new_from_slice(key.as_bytes()).expect("HMAC accepts any key length");
    mac.update(format!("visioncast-live-ticket-v1|{role}|{room}|{identity}").as_bytes());
    mac
}

pub fn sign(key: &str, role: Role, room: &str, identity: &str) -> String {
    let tag = mac(key, role.code(), room, identity).finalize().into_bytes();
    format!("{}.{room}.{identity}.{}", role.code(), URL_SAFE_NO_PAD.encode(tag))
}

pub fn verify(key: &str, ticket: &str) -> Option<Ticket> {
    let mut parts = ticket.split('.');
    let (role, room, identity, tag) = (parts.next()?, parts.next()?, parts.next()?, parts.next()?);
    if parts.next().is_some() || !valid_id(room) || !valid_id(identity) {
        return None;
    }
    let role = match role {
        "h" => Role::Host,
        "p" => Role::Producer,
        "g" => Role::Guest,
        "a" => Role::Attendee,
        "r" => Role::Recorder,
        "c" => Role::CaptionSource,
        _ => return None,
    };
    let given = URL_SAFE_NO_PAD.decode(tag).ok()?;
    let expected = mac(key, role.code(), room, identity).finalize().into_bytes();
    if expected.as_slice().ct_eq(&given).into() {
        Some(Ticket { role, room: room.to_string(), identity: identity.to_string() })
    } else {
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const KEY: &str = "unit-test-key-unit-test-key-unit-test";

    #[test]
    fn round_trips_both_roles() {
        for role in [Role::Host, Role::Producer, Role::Guest, Role::Attendee, Role::Recorder] {
            let t = sign(KEY, role, "vc-room", "person-1");
            assert_eq!(verify(KEY, &t), Some(Ticket { role, room: "vc-room".into(), identity: "person-1".into() }));
        }
    }

    #[test]
    fn an_attendee_cannot_become_a_host_or_change_identity_or_room() {
        let t = sign(KEY, Role::Attendee, "room", "me");
        assert_eq!(verify(KEY, &t.replacen("a.", "h.", 1)), None);
        assert_eq!(verify(KEY, &t.replace(".me.", ".you.")), None);
        assert_eq!(verify(KEY, &t.replace("a.room.", "a.other.")), None);
    }

    #[test]
    fn rejects_other_keys_and_malformed_input() {
        let t = sign(KEY, Role::Host, "room", "host");
        assert_eq!(verify("another-key-another-key-another-key-1", &t), None);
        for bad in ["", "a", "a.b.c", "a.b.c.d.e", "x.room.me.AAAA", "a.ro/om.me.AAAA", "a.room.me.not base64"] {
            assert_eq!(verify(KEY, bad), None, "{bad}");
        }
    }

    #[test]
    fn ids_are_url_safe_and_bounded() {
        assert!(valid_id("vc-3f2a1b6c_x"));
        assert!(!valid_id(""));
        assert!(!valid_id("a.b"));
        assert!(!valid_id("a/b"));
        assert!(!valid_id(&"x".repeat(129)));
    }
}

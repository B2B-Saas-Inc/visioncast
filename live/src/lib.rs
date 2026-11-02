//! visioncast-live: self-hosted webinar rooms for VisionCast, built on LiveKit.
//!
//! VisionCast treats this service like any other hosting provider: it creates rooms and
//! per-attendee join links here, and pulls attendance back. LiveKit's own webhooks tell this
//! service who joined, left and what was recorded. LiveKit itself (Go) carries the media.
pub mod admin;
pub mod app;
pub mod backend;
pub mod broadcast;
pub mod captions;
pub mod config;
pub mod crew;
pub mod store;
pub mod studio;
pub mod ticket;

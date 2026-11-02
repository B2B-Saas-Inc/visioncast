//! Durable state: the rooms VisionCast scheduled, who attended, and what was recorded.
//! No names or email addresses: attendance rows hold the opaque registration ID VisionCast
//! issued, and VisionCast joins it back to a person. Everything is removed after the
//! retention period.
use rusqlite::{params, Connection, OptionalExtension};
use std::collections::HashMap;
use serde::Serialize;
use std::sync::Mutex;

/// Fits VisionCast's 64 KB response limit even with the longest identities.
pub const ATTENDANCE_PAGE: usize = 200;

#[derive(Clone, Debug, PartialEq)]
pub struct Room {
    pub id: String,
    pub title: String,
    pub starts_at: i64,
    pub ends_at: i64,
    pub auto_record: bool,
    /// The address: `/{slug}`. Unique, and the room's ID when nothing else was given.
    pub slug: String,
    /// Where someone without a personal link goes to get in, such as the waiting room.
    pub entry_url: String,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AttendanceRow {
    pub external_id: String,
    pub joined_at: i64,
    pub left_at: Option<i64>,
    pub duration_seconds: i64,
}

#[derive(Clone, Debug, Default, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Recording {
    pub egress_id: String,
    pub status: String,
    pub started_at: i64,
    pub ended_at: i64,
    pub duration_seconds: i64,
    pub size_bytes: i64,
    pub filename: String,
    pub location: String,
    pub error: String,
}

/// One person on a session's crew. The nonce is part of their link: a new one kills the old link.
#[derive(Clone, Debug, PartialEq)]
pub struct CrewMember {
    pub id: String,
    pub room: String,
    pub role: String,
    pub name: String,
    pub title: String,
    pub email: String,
    pub nonce: String,
    pub created_at: i64,
}

fn crew_row(r: &rusqlite::Row) -> rusqlite::Result<CrewMember> {
    Ok(CrewMember { id: r.get(0)?, room: r.get(1)?, role: r.get(2)?, name: r.get(3)?, title: r.get(4)?, email: r.get(5)?, nonce: r.get(6)?, created_at: r.get(7)? })
}

/// One person who can sign in to `/admin`. `role` is owner (also manages accounts) or admin.
#[derive(Clone, Debug, PartialEq)]
pub struct Account {
    pub id: String,
    pub email: String,
    pub name: String,
    pub role: String,
    pub password_hash: String,
    pub created_at: i64,
    pub last_login_at: i64,
}

fn account_row(r: &rusqlite::Row) -> rusqlite::Result<Account> {
    Ok(Account { id: r.get(0)?, email: r.get(1)?, name: r.get(2)?, role: r.get(3)?, password_hash: r.get(4)?, created_at: r.get(5)?, last_login_at: r.get(6)? })
}

pub struct Store {
    conn: Mutex<Connection>,
}

const ACTIVE: &str = "('starting','active','ending')";

impl Store {
    pub fn open(path: &str) -> rusqlite::Result<Self> {
        let conn = Connection::open(path)?;
        conn.execute_batch(
            "PRAGMA journal_mode=WAL;
             PRAGMA foreign_keys=ON;
             PRAGMA busy_timeout=5000;
             CREATE TABLE IF NOT EXISTS rooms (
               id TEXT PRIMARY KEY,
               title TEXT NOT NULL,
               starts_at INTEGER NOT NULL,
               ends_at INTEGER NOT NULL,
               auto_record INTEGER NOT NULL DEFAULT 0,
               slug TEXT NOT NULL UNIQUE,
               entry_url TEXT NOT NULL DEFAULT '',
               updated_at INTEGER NOT NULL
             );
             CREATE TABLE IF NOT EXISTS attendance (
               room TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
               identity TEXT NOT NULL,
               sid TEXT NOT NULL,
               joined_at INTEGER NOT NULL,
               left_at INTEGER,
               PRIMARY KEY (room, sid)
             );
             CREATE TABLE IF NOT EXISTS recordings (
               egress_id TEXT PRIMARY KEY,
               room TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
               status TEXT NOT NULL,
               started_at INTEGER NOT NULL DEFAULT 0,
               ended_at INTEGER NOT NULL DEFAULT 0,
               duration_seconds INTEGER NOT NULL DEFAULT 0,
               size_bytes INTEGER NOT NULL DEFAULT 0,
               filename TEXT NOT NULL DEFAULT '',
               location TEXT NOT NULL DEFAULT '',
               error TEXT NOT NULL DEFAULT ''
             );
             CREATE TABLE IF NOT EXISTS webhook_events (
               id TEXT PRIMARY KEY,
               received_at INTEGER NOT NULL
             );
             CREATE TABLE IF NOT EXISTS crew (
               id TEXT PRIMARY KEY,
               room TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
               role TEXT NOT NULL,
               name TEXT NOT NULL,
               title TEXT NOT NULL DEFAULT '',
               email TEXT NOT NULL DEFAULT '',
               nonce TEXT NOT NULL,
               created_at INTEGER NOT NULL
             );
             CREATE TABLE IF NOT EXISTS accounts (
               id TEXT PRIMARY KEY,
               email TEXT NOT NULL UNIQUE,
               name TEXT NOT NULL DEFAULT '',
               role TEXT NOT NULL,
               password_hash TEXT NOT NULL DEFAULT '',
               created_at INTEGER NOT NULL,
               last_login_at INTEGER NOT NULL DEFAULT 0
             );
             CREATE TABLE IF NOT EXISTS admin_sessions (
               token_hash TEXT PRIMARY KEY,
               account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
               expires_at INTEGER NOT NULL
             );
             CREATE TABLE IF NOT EXISTS one_time_links (
               token_hash TEXT PRIMARY KEY,
               account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
               purpose TEXT NOT NULL,
               room TEXT NOT NULL DEFAULT '',
               expires_at INTEGER NOT NULL
             );
             CREATE TABLE IF NOT EXISTS audit_log (
               seq INTEGER PRIMARY KEY AUTOINCREMENT,
               at INTEGER NOT NULL,
               actor TEXT NOT NULL,
               action TEXT NOT NULL,
               detail TEXT NOT NULL DEFAULT ''
             );
             CREATE TABLE IF NOT EXISTS service_settings (
               key TEXT PRIMARY KEY,
               json TEXT NOT NULL
             );
             CREATE TABLE IF NOT EXISTS studio_state (
               room TEXT PRIMARY KEY REFERENCES rooms(id) ON DELETE CASCADE,
               json TEXT NOT NULL
             );
             CREATE TABLE IF NOT EXISTS studio_docs (
               seq INTEGER PRIMARY KEY AUTOINCREMENT,
               room TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
               kind TEXT NOT NULL,
               id TEXT NOT NULL,
               json TEXT NOT NULL,
               UNIQUE (room, kind, id)
             );
             CREATE TABLE IF NOT EXISTS studio_votes (
               room TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
               target TEXT NOT NULL,
               identity TEXT NOT NULL,
               choice INTEGER NOT NULL,
               PRIMARY KEY (room, target, identity)
             );
             CREATE TABLE IF NOT EXISTS studio_people (
               room TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
               identity TEXT NOT NULL,
               promoted INTEGER NOT NULL DEFAULT 0,
               blocked INTEGER NOT NULL DEFAULT 0,
               removed INTEGER NOT NULL DEFAULT 0,
               PRIMARY KEY (room, identity)
             );",
        )?;
        Ok(Self { conn: Mutex::new(conn) })
    }

    fn db(&self) -> std::sync::MutexGuard<'_, Connection> {
        // A panic while holding the lock leaves SQLite itself consistent, so keep serving.
        self.conn.lock().unwrap_or_else(|e| e.into_inner())
    }

    pub fn upsert_room(&self, room: &Room, now: i64) -> rusqlite::Result<()> {
        self.db().execute(
            "INSERT INTO rooms (id, title, starts_at, ends_at, auto_record, slug, entry_url, updated_at) VALUES (?1,?2,?3,?4,?5,?6,?7,?8)
             ON CONFLICT(id) DO UPDATE SET title=excluded.title, starts_at=excluded.starts_at, ends_at=excluded.ends_at,
               auto_record=excluded.auto_record, slug=excluded.slug, entry_url=excluded.entry_url, updated_at=excluded.updated_at",
            params![room.id, room.title, room.starts_at, room.ends_at, room.auto_record, room.slug, room.entry_url, now],
        )?;
        Ok(())
    }

    fn room_where(&self, clause: &str, value: &str) -> rusqlite::Result<Option<Room>> {
        self.db()
            .query_row(
                &format!("SELECT id, title, starts_at, ends_at, auto_record, slug, entry_url FROM rooms WHERE {clause}=?1"),
                [value],
                |r| Ok(Room { id: r.get(0)?, title: r.get(1)?, starts_at: r.get(2)?, ends_at: r.get(3)?, auto_record: r.get(4)?, slug: r.get(5)?, entry_url: r.get(6)? }),
            )
            .optional()
    }

    pub fn room(&self, id: &str) -> rusqlite::Result<Option<Room>> {
        self.room_where("id", id)
    }

    pub fn room_by_slug(&self, slug: &str) -> rusqlite::Result<Option<Room>> {
        self.room_where("slug", slug)
    }

    pub fn rooms(&self) -> rusqlite::Result<Vec<Room>> {
        let ids = self.room_ids()?;
        let mut out = vec![];
        for id in ids {
            if let Some(room) = self.room(&id)? {
                out.push(room);
            }
        }
        Ok(out)
    }

    pub fn first_delivery(&self, event_id: &str, now: i64) -> rusqlite::Result<bool> {
        Ok(self
            .db()
            .execute("INSERT OR IGNORE INTO webhook_events (id, received_at) VALUES (?1,?2)", params![event_id, now])?
            == 1)
    }

    pub fn participant_joined(&self, room: &str, identity: &str, sid: &str, at: i64) -> rusqlite::Result<()> {
        self.db().execute(
            "INSERT INTO attendance (room, identity, sid, joined_at) VALUES (?1,?2,?3,?4)
             ON CONFLICT(room, sid) DO UPDATE SET joined_at = MIN(joined_at, excluded.joined_at)",
            params![room, identity, sid, at],
        )?;
        Ok(())
    }

    pub fn participant_left(&self, room: &str, identity: &str, sid: &str, joined_at: i64, at: i64) -> rusqlite::Result<()> {
        // Inserts when the join event was missed, so the visit still counts.
        self.db().execute(
            "INSERT INTO attendance (room, identity, sid, joined_at, left_at) VALUES (?1,?2,?3,?4,?5)
             ON CONFLICT(room, sid) DO UPDATE SET left_at = excluded.left_at",
            params![room, identity, sid, joined_at, at],
        )?;
        Ok(())
    }

    /// The room closed: anyone still marked present left when it did.
    pub fn close_open_visits(&self, room: &str, at: i64) -> rusqlite::Result<()> {
        self.db().execute("UPDATE attendance SET left_at=?2 WHERE room=?1 AND left_at IS NULL", params![room, at])?;
        Ok(())
    }

    /// One page of one row per person, after the identity `after`, plus whether more follow.
    /// `complete` is false while anyone is still marked present, so VisionCast can say the
    /// list may be partial.
    pub fn attendance(&self, room: &str, after: &str) -> rusqlite::Result<(Vec<AttendanceRow>, bool, bool)> {
        let db = self.db();
        let mut stmt = db.prepare(
            "SELECT identity, MIN(joined_at),
                    CASE WHEN SUM(left_at IS NULL) > 0 THEN NULL ELSE MAX(left_at) END,
                    SUM(COALESCE(left_at, joined_at) - joined_at)
             FROM attendance WHERE room=?1 AND identity>?2 GROUP BY identity ORDER BY identity LIMIT ?3",
        )?;
        let mut rows = stmt
            .query_map(params![room, after, ATTENDANCE_PAGE + 1], |r| {
                Ok(AttendanceRow { external_id: r.get(0)?, joined_at: r.get(1)?, left_at: r.get(2)?, duration_seconds: r.get::<_, i64>(3)?.max(0) })
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        let more = rows.len() > ATTENDANCE_PAGE;
        rows.truncate(ATTENDANCE_PAGE);
        let open: bool = db.query_row(
            "SELECT EXISTS(SELECT 1 FROM attendance WHERE room=?1 AND left_at IS NULL)",
            [room],
            |r| r.get(0),
        )?;
        Ok((rows, !open, more))
    }

    pub fn upsert_recording(&self, room: &str, rec: &Recording) -> rusqlite::Result<()> {
        self.db().execute(
            "INSERT INTO recordings (egress_id, room, status, started_at, ended_at, duration_seconds, size_bytes, filename, location, error)
             VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10)
             ON CONFLICT(egress_id) DO UPDATE SET
               status=CASE WHEN status IN ('complete','failed') THEN status ELSE excluded.status END,
               started_at=MAX(started_at, excluded.started_at), ended_at=MAX(ended_at, excluded.ended_at),
               duration_seconds=MAX(duration_seconds, excluded.duration_seconds), size_bytes=MAX(size_bytes, excluded.size_bytes),
               filename=CASE WHEN excluded.filename='' THEN filename ELSE excluded.filename END,
               location=CASE WHEN excluded.location='' THEN location ELSE excluded.location END,
               error=CASE WHEN excluded.error='' THEN error ELSE excluded.error END",
            params![rec.egress_id, room, rec.status, rec.started_at, rec.ended_at, rec.duration_seconds, rec.size_bytes, rec.filename, rec.location, rec.error],
        )?;
        Ok(())
    }

    pub fn recordings(&self, room: &str) -> rusqlite::Result<Vec<Recording>> {
        let db = self.db();
        let mut stmt = db.prepare(
            "SELECT egress_id, status, started_at, ended_at, duration_seconds, size_bytes, filename, location, error
             FROM recordings WHERE room=?1 ORDER BY started_at DESC, egress_id LIMIT 50",
        )?;
        let rows = stmt.query_map([room], |r| {
            Ok(Recording {
                egress_id: r.get(0)?,
                status: r.get(1)?,
                started_at: r.get(2)?,
                ended_at: r.get(3)?,
                duration_seconds: r.get(4)?,
                size_bytes: r.get(5)?,
                filename: r.get(6)?,
                location: r.get(7)?,
                error: r.get(8)?,
            })
        })?;
        rows.collect()
    }

    pub fn active_recording(&self, room: &str) -> rusqlite::Result<Option<String>> {
        self.db()
            .query_row(
                &format!("SELECT egress_id FROM recordings WHERE room=?1 AND status IN {ACTIVE} LIMIT 1"),
                [room],
                |r| r.get(0),
            )
            .optional()
    }

    /// Removes rooms that ended before `cutoff`, with their attendance and recording records
    /// (not the recording files), and webhook IDs too old to be redelivered.
    // ---- the studio: chat, questions, polls and files are JSON documents per room

    // ---- the crew: one row per person, each with their own link

    pub fn put_crew(&self, m: &CrewMember) -> rusqlite::Result<()> {
        self.db().execute(
            "INSERT INTO crew (id, room, role, name, title, email, nonce, created_at) VALUES (?1,?2,?3,?4,?5,?6,?7,?8)
             ON CONFLICT(id) DO UPDATE SET role=excluded.role, name=excluded.name, title=excluded.title, email=excluded.email, nonce=excluded.nonce",
            params![m.id, m.room, m.role, m.name, m.title, m.email, m.nonce, m.created_at],
        )?;
        Ok(())
    }

    pub fn crew_member(&self, id: &str) -> rusqlite::Result<Option<CrewMember>> {
        self.db()
            .query_row("SELECT id, room, role, name, title, email, nonce, created_at FROM crew WHERE id=?1", [id], crew_row)
            .optional()
    }

    pub fn all_crew(&self) -> rusqlite::Result<Vec<CrewMember>> {
        let db = self.db();
        let mut stmt = db.prepare("SELECT id, room, role, name, title, email, nonce, created_at FROM crew ORDER BY name COLLATE NOCASE, created_at")?;
        let rows = stmt.query_map([], crew_row)?;
        rows.collect()
    }

    pub fn crew_of(&self, room: &str) -> rusqlite::Result<Vec<CrewMember>> {
        let db = self.db();
        let mut stmt = db.prepare("SELECT id, room, role, name, title, email, nonce, created_at FROM crew WHERE room=?1 ORDER BY created_at, id")?;
        let rows = stmt.query_map([room], crew_row)?;
        rows.collect()
    }

    pub fn delete_crew(&self, id: &str) -> rusqlite::Result<bool> {
        Ok(self.db().execute("DELETE FROM crew WHERE id=?1", [id])? > 0)
    }

    // ---- people who sign in to /admin

    pub fn put_account(&self, a: &Account) -> rusqlite::Result<()> {
        self.db().execute(
            "INSERT INTO accounts (id, email, name, role, password_hash, created_at, last_login_at) VALUES (?1,?2,?3,?4,?5,?6,?7)
             ON CONFLICT(id) DO UPDATE SET email=excluded.email, name=excluded.name, role=excluded.role,
               password_hash=excluded.password_hash, last_login_at=excluded.last_login_at",
            params![a.id, a.email, a.name, a.role, a.password_hash, a.created_at, a.last_login_at],
        )?;
        Ok(())
    }

    fn account_where(&self, clause: &str, value: &str) -> rusqlite::Result<Option<Account>> {
        self.db()
            .query_row(&format!("SELECT id, email, name, role, password_hash, created_at, last_login_at FROM accounts WHERE {clause}=?1"), [value], account_row)
            .optional()
    }

    pub fn account(&self, id: &str) -> rusqlite::Result<Option<Account>> {
        self.account_where("id", id)
    }

    pub fn account_by_email(&self, email: &str) -> rusqlite::Result<Option<Account>> {
        self.account_where("email", email)
    }

    pub fn accounts(&self) -> rusqlite::Result<Vec<Account>> {
        let db = self.db();
        let mut stmt = db.prepare("SELECT id, email, name, role, password_hash, created_at, last_login_at FROM accounts ORDER BY email")?;
        let rows = stmt.query_map([], account_row)?;
        rows.collect()
    }

    pub fn delete_account(&self, id: &str) -> rusqlite::Result<bool> {
        Ok(self.db().execute("DELETE FROM accounts WHERE id=?1", [id])? > 0)
    }

    pub fn put_session(&self, token_hash: &str, account_id: &str, expires_at: i64) -> rusqlite::Result<()> {
        self.db().execute("INSERT INTO admin_sessions (token_hash, account_id, expires_at) VALUES (?1,?2,?3)", params![token_hash, account_id, expires_at])?;
        Ok(())
    }

    /// The account a session belongs to, while it's good.
    pub fn session_account(&self, token_hash: &str, now: i64) -> rusqlite::Result<Option<Account>> {
        self.db()
            .query_row(
                "SELECT a.id, a.email, a.name, a.role, a.password_hash, a.created_at, a.last_login_at FROM admin_sessions s JOIN accounts a ON a.id=s.account_id WHERE s.token_hash=?1 AND s.expires_at>?2",
                params![token_hash, now],
                account_row,
            )
            .optional()
    }

    pub fn delete_session(&self, token_hash: &str) -> rusqlite::Result<()> {
        self.db().execute("DELETE FROM admin_sessions WHERE token_hash=?1", [token_hash])?;
        Ok(())
    }

    /// Signs someone out everywhere, for a changed password or a removed account.
    pub fn delete_sessions_of(&self, account_id: &str) -> rusqlite::Result<()> {
        self.db().execute("DELETE FROM admin_sessions WHERE account_id=?1", [account_id])?;
        self.db().execute("DELETE FROM one_time_links WHERE account_id=?1", [account_id])?;
        Ok(())
    }

    pub fn put_one_time_link(&self, token_hash: &str, account_id: &str, purpose: &str, room: &str, expires_at: i64) -> rusqlite::Result<()> {
        self.db().execute(
            "INSERT INTO one_time_links (token_hash, account_id, purpose, room, expires_at) VALUES (?1,?2,?3,?4,?5)",
            params![token_hash, account_id, purpose, room, expires_at],
        )?;
        Ok(())
    }

    /// Reads a one-time link without using it: for showing the set-password page.
    pub fn peek_one_time_link(&self, token_hash: &str, purpose: &str, now: i64) -> rusqlite::Result<Option<String>> {
        self.db()
            .query_row(
                "SELECT account_id FROM one_time_links WHERE token_hash=?1 AND purpose=?2 AND expires_at>?3",
                params![token_hash, purpose, now],
                |r| r.get(0),
            )
            .optional()
    }

    /// Uses a one-time link: it can't be used again, even by a request racing this one.
    pub fn take_one_time_link(&self, token_hash: &str, purpose: &str, now: i64) -> rusqlite::Result<Option<(String, String)>> {
        let db = self.db();
        let found = db
            .query_row(
                "DELETE FROM one_time_links WHERE token_hash=?1 AND purpose=?2 AND expires_at>?3 RETURNING account_id, room",
                params![token_hash, purpose, now],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()?;
        Ok(found)
    }

    pub fn audit(&self, at: i64, actor: &str, action: &str, detail: &str) -> rusqlite::Result<()> {
        self.db().execute("INSERT INTO audit_log (at, actor, action, detail) VALUES (?1,?2,?3,?4)", params![at, actor, action, detail])?;
        Ok(())
    }

    pub fn audit_rows(&self, limit: i64) -> rusqlite::Result<Vec<(i64, String, String, String)>> {
        let db = self.db();
        let mut stmt = db.prepare("SELECT at, actor, action, detail FROM audit_log ORDER BY seq DESC LIMIT ?1")?;
        let rows = stmt.query_map([limit], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)))?;
        rows.collect()
    }

    pub fn setting(&self, key: &str) -> rusqlite::Result<Option<String>> {
        self.db().query_row("SELECT json FROM service_settings WHERE key=?1", [key], |r| r.get(0)).optional()
    }

    pub fn save_setting(&self, key: &str, json: &str) -> rusqlite::Result<()> {
        self.db().execute(
            "INSERT INTO service_settings (key, json) VALUES (?1,?2) ON CONFLICT(key) DO UPDATE SET json=excluded.json",
            params![key, json],
        )?;
        Ok(())
    }

    pub fn studio_state(&self, room: &str) -> rusqlite::Result<Option<String>> {
        self.db().query_row("SELECT json FROM studio_state WHERE room=?1", [room], |r| r.get(0)).optional()
    }

    pub fn save_studio_state(&self, room: &str, json: &str) -> rusqlite::Result<()> {
        self.db().execute(
            "INSERT INTO studio_state (room, json) VALUES (?1,?2) ON CONFLICT(room) DO UPDATE SET json=excluded.json",
            params![room, json],
        )?;
        Ok(())
    }

    pub fn put_doc(&self, room: &str, kind: &str, id: &str, json: &str) -> rusqlite::Result<()> {
        self.db().execute(
            "INSERT INTO studio_docs (room, kind, id, json) VALUES (?1,?2,?3,?4)
             ON CONFLICT(room, kind, id) DO UPDATE SET json=excluded.json",
            params![room, kind, id, json],
        )?;
        Ok(())
    }

    pub fn get_doc(&self, room: &str, kind: &str, id: &str) -> rusqlite::Result<Option<String>> {
        self.db()
            .query_row("SELECT json FROM studio_docs WHERE room=?1 AND kind=?2 AND id=?3", [room, kind, id], |r| r.get(0))
            .optional()
    }

    pub fn del_doc(&self, room: &str, kind: &str, id: &str) -> rusqlite::Result<()> {
        self.db().execute("DELETE FROM studio_docs WHERE room=?1 AND kind=?2 AND id=?3", [room, kind, id])?;
        Ok(())
    }

    /// The newest `limit` documents, oldest first.
    pub fn docs(&self, room: &str, kind: &str, limit: i64) -> rusqlite::Result<Vec<String>> {
        let db = self.db();
        let mut stmt = db.prepare("SELECT json FROM studio_docs WHERE room=?1 AND kind=?2 ORDER BY seq DESC LIMIT ?3")?;
        let mut rows = stmt.query_map(params![room, kind, limit], |r| r.get(0))?.collect::<rusqlite::Result<Vec<String>>>()?;
        rows.reverse();
        Ok(rows)
    }

    /// Records a vote. False when this person already voted, unless `replace`.
    pub fn vote(&self, room: &str, target: &str, identity: &str, choice: i64) -> rusqlite::Result<bool> {
        let changed = self.db().execute(
            "INSERT OR IGNORE INTO studio_votes (room, target, identity, choice) VALUES (?1,?2,?3,?4)",
            params![room, target, identity, choice],
        )?;
        Ok(changed > 0)
    }

    pub fn unvote(&self, room: &str, target: &str, identity: &str) -> rusqlite::Result<bool> {
        Ok(self.db().execute("DELETE FROM studio_votes WHERE room=?1 AND target=?2 AND identity=?3", [room, target, identity])? > 0)
    }

    pub fn tally(&self, room: &str, target: &str) -> rusqlite::Result<Vec<(i64, i64)>> {
        let db = self.db();
        let mut stmt = db.prepare("SELECT choice, COUNT(*) FROM studio_votes WHERE room=?1 AND target=?2 GROUP BY choice")?;
        let rows = stmt.query_map([room, target], |r| Ok((r.get(0)?, r.get(1)?)))?;
        rows.collect()
    }

    pub fn my_vote(&self, room: &str, target: &str, identity: &str) -> rusqlite::Result<Option<i64>> {
        self.db().query_row("SELECT choice FROM studio_votes WHERE room=?1 AND target=?2 AND identity=?3", [room, target, identity], |r| r.get(0)).optional()
    }

    pub fn voters(&self, room: &str, target: &str) -> rusqlite::Result<Vec<(String, i64)>> {
        let db = self.db();
        let mut stmt = db.prepare("SELECT identity, choice FROM studio_votes WHERE room=?1 AND target=?2")?;
        let rows = stmt.query_map([room, target], |r| Ok((r.get(0)?, r.get(1)?)))?;
        rows.collect()
    }

    pub fn tallies_with_prefix(&self, room: &str, prefix: &str) -> rusqlite::Result<Vec<(String, i64, i64)>> {
        let db = self.db();
        let mut stmt = db.prepare("SELECT target, choice, COUNT(*) FROM studio_votes WHERE room=?1 AND target >= ?2 AND target < ?3 GROUP BY target, choice")?;
        let end = format!("{prefix}\u{10ffff}");
        let rows = stmt.query_map([room, prefix, end.as_str()], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?;
        rows.collect()
    }

    pub fn my_votes(&self, room: &str, identity: &str) -> rusqlite::Result<Vec<(String, i64)>> {
        let db = self.db();
        let mut stmt = db.prepare("SELECT target, choice FROM studio_votes WHERE room=?1 AND identity=?2")?;
        let rows = stmt.query_map([room, identity], |r| Ok((r.get(0)?, r.get(1)?)))?;
        rows.collect()
    }

    /// `(promoted, blocked, removed)` for one person.
    pub fn person_flags(&self, room: &str, identity: &str) -> rusqlite::Result<(bool, bool, bool)> {
        Ok(self
            .db()
            .query_row("SELECT promoted, blocked, removed FROM studio_people WHERE room=?1 AND identity=?2", [room, identity], |r| {
                Ok((r.get(0)?, r.get(1)?, r.get(2)?))
            })
            .optional()?
            .unwrap_or((false, false, false)))
    }

    pub fn set_person_flags(&self, room: &str, identity: &str, flags: (bool, bool, bool)) -> rusqlite::Result<()> {
        self.db().execute(
            "INSERT INTO studio_people (room, identity, promoted, blocked, removed) VALUES (?1,?2,?3,?4,?5)
             ON CONFLICT(room, identity) DO UPDATE SET promoted=excluded.promoted, blocked=excluded.blocked, removed=excluded.removed",
            params![room, identity, flags.0, flags.1, flags.2],
        )?;
        Ok(())
    }

    pub fn room_ids(&self) -> rusqlite::Result<Vec<String>> {
        let db = self.db();
        let mut stmt = db.prepare("SELECT id FROM rooms")?;
        let rows = stmt.query_map([], |r| r.get(0))?;
        rows.collect()
    }

    /// Everyone's flags in a room, in one query rather than one each.
    pub fn people_flags(&self, room: &str) -> rusqlite::Result<HashMap<String, (bool, bool, bool)>> {
        let db = self.db();
        let mut stmt = db.prepare("SELECT identity, promoted, blocked, removed FROM studio_people WHERE room=?1")?;
        let rows = stmt.query_map([room], |r| Ok((r.get(0)?, (r.get(1)?, r.get(2)?, r.get(3)?))))?;
        rows.collect()
    }

    /// Every vote someone cast in a room, for an erasure request.
    pub fn delete_votes_of(&self, room: &str, identity: &str) -> rusqlite::Result<()> {
        self.db().execute("DELETE FROM studio_votes WHERE room=?1 AND identity=?2", [room, identity])?;
        Ok(())
    }

    pub fn forget_attendance(&self, room: &str, identity: &str) -> rusqlite::Result<()> {
        self.db().execute("DELETE FROM attendance WHERE room=?1 AND identity=?2", [room, identity])?;
        Ok(())
    }

    pub fn purge(&self, cutoff: i64) -> rusqlite::Result<usize> {
        let db = self.db();
        db.execute("DELETE FROM webhook_events WHERE received_at < ?1", [cutoff])?;
        // The activity log is for looking back over, not forever, and failed sign-ins can't fill the disk.
        db.execute("DELETE FROM audit_log WHERE seq NOT IN (SELECT seq FROM audit_log ORDER BY seq DESC LIMIT 20000)", [])?;
        db.execute("DELETE FROM rooms WHERE ends_at < ?1", [cutoff])
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn store_with_room() -> Store {
        let s = Store::open(":memory:").unwrap();
        s.upsert_room(&Room { id: "r".into(), title: "T".into(), starts_at: 100, ends_at: 200, auto_record: false, slug: "r".into(), entry_url: String::new() }, 1).unwrap();
        s
    }

    #[test]
    fn attendance_comes_in_pages() {
        let s = store_with_room();
        for i in 0..(ATTENDANCE_PAGE + 3) {
            let identity = format!("p{i:04}");
            s.participant_joined("r", &identity, &format!("PA_{i}"), 100).unwrap();
            s.participant_left("r", &identity, &format!("PA_{i}"), 100, 160).unwrap();
        }
        let (first, complete, more) = s.attendance("r", "").unwrap();
        assert_eq!((first.len(), complete, more), (ATTENDANCE_PAGE, true, true));
        let (second, _, more) = s.attendance("r", &first.last().unwrap().external_id).unwrap();
        assert_eq!((second.len(), more), (3, false));
        assert_eq!(second[0].external_id, format!("p{ATTENDANCE_PAGE:04}"));
    }

    #[test]
    fn rejoining_adds_up_to_one_row_per_person() {
        let s = store_with_room();
        s.participant_joined("r", "p1", "s1", 100).unwrap();
        s.participant_left("r", "p1", "s1", 100, 160).unwrap();
        s.participant_joined("r", "p1", "s2", 170).unwrap();
        s.participant_left("r", "p1", "s2", 170, 190).unwrap();
        let (rows, complete, _) = s.attendance("r", "").unwrap();
        assert!(complete);
        assert_eq!(rows, vec![AttendanceRow { external_id: "p1".into(), joined_at: 100, left_at: Some(190), duration_seconds: 80 }]);
    }

    #[test]
    fn someone_still_present_marks_the_list_incomplete() {
        let s = store_with_room();
        s.participant_joined("r", "p1", "s1", 100).unwrap();
        assert!(!s.attendance("r", "").unwrap().1);
        s.close_open_visits("r", 150).unwrap();
        let (rows, complete, _) = s.attendance("r", "").unwrap();
        assert!(complete);
        assert_eq!(rows[0].duration_seconds, 50);
    }

    #[test]
    fn a_missed_join_event_still_counts_the_visit() {
        let s = store_with_room();
        s.participant_left("r", "p1", "s1", 120, 150).unwrap();
        assert_eq!(s.attendance("r", "").unwrap().0[0].duration_seconds, 30);
    }

    #[test]
    fn webhook_delivery_is_deduplicated() {
        let s = store_with_room();
        assert!(s.first_delivery("e1", 1).unwrap());
        assert!(!s.first_delivery("e1", 2).unwrap());
    }

    #[test]
    fn rescheduling_keeps_the_room_and_recordings_progress_never_loses_results() {
        let s = store_with_room();
        s.upsert_room(&Room { id: "r".into(), title: "New".into(), starts_at: 500, ends_at: 600, auto_record: true, slug: "r".into(), entry_url: String::new() }, 2).unwrap();
        assert_eq!(s.room("r").unwrap().unwrap().starts_at, 500);
        let rec = Recording { egress_id: "e".into(), status: "active".into(), started_at: 500, ..Default::default() };
        s.upsert_recording("r", &rec).unwrap();
        assert_eq!(s.active_recording("r").unwrap().as_deref(), Some("e"));
        let done = Recording { egress_id: "e".into(), status: "complete".into(), ended_at: 600, duration_seconds: 100, size_bytes: 9, filename: "f.mp4".into(), location: "s3://f.mp4".into(), ..Default::default() };
        s.upsert_recording("r", &done).unwrap();
        // A late "active" update must not wipe what the finished recording reported.
        s.upsert_recording("r", &Recording { egress_id: "e".into(), status: "complete".into(), ..Default::default() }).unwrap();
        let got = &s.recordings("r").unwrap()[0];
        assert_eq!((got.started_at, got.filename.as_str(), got.size_bytes), (500, "f.mp4", 9));
        assert_eq!(s.active_recording("r").unwrap(), None);
    }

    #[test]
    fn purge_removes_only_rooms_past_retention_and_their_records() {
        let s = store_with_room();
        s.participant_joined("r", "p1", "s1", 100).unwrap();
        s.upsert_room(&Room { id: "keep".into(), title: "K".into(), starts_at: 900, ends_at: 1000, auto_record: false, slug: "keep".into(), entry_url: String::new() }, 1).unwrap();
        assert_eq!(s.purge(500).unwrap(), 1);
        assert!(s.room("r").unwrap().is_none());
        assert!(s.room("keep").unwrap().is_some());
        assert!(s.attendance("r", "").unwrap().0.is_empty());
    }
}

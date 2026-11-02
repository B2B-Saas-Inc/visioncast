//! Configuration, read once from the environment at start-up.
use std::env;

#[derive(Clone, Debug)]
pub struct RecordingStorage {
    pub bucket: String,
    pub region: String,
    pub endpoint: String,
    pub access_key: String,
    pub secret_key: String,
    pub force_path_style: bool,
    /// Public base URL of the bucket, when recordings can be linked to directly.
    pub public_base_url: Option<String>,
}

#[derive(Clone, Debug)]
pub struct Config {
    pub bind: String,
    /// Public origin of this service: join links are built from it.
    pub public_url: String,
    /// Where this service reaches the LiveKit server API.
    pub livekit_url: String,
    /// Where browsers reach the LiveKit server (wss://).
    pub livekit_public_url: String,
    pub livekit_api_key: String,
    pub livekit_api_secret: String,
    /// Where the recorder's browser reaches this service. Defaults to the public URL.
    pub internal_url: String,
    /// Where files uploaded in the studio are kept.
    pub upload_dir: String,
    pub max_upload_bytes: u64,
    /// Whisper, in its own container. Without it there are no captions.
    pub captions_url: Option<String>,
    /// Where this service reaches the bucket, when that differs from where the recorder does.
    pub recording_internal_endpoint: Option<String>,
    /// The first owner of `/admin`, created at start-up when there are no accounts yet.
    pub first_admin: Option<(String, String)>,
    /// Languages captions can be translated into, with a LibreTranslate server to do it.
    /// Traefik is in front, so the last X-Forwarded-For hop is who's really asking.
    pub trust_forwarded_for: bool,
    pub translate_url: Option<String>,
    /// What the speakers speak, and so what's translated from.
    pub captions_language: String,
    pub translate_languages: Vec<String>,
    /// The folder shared with the slides converter. Without it, decks aren't offered.
    pub slides_dir: Option<String>,
    /// The address encoders send RTMP to. Without it, RTMP input isn't offered.
    pub ingress_url: Option<String>,
    /// Below this many CPUs, the crew are warned before going live. 0 turns the warning off.
    pub min_cpus: usize,
    /// Lets a broadcast go to private addresses. Local testing only.
    pub rtmp_allow_private: bool,
    /// Bearer secret VisionCast sends on `/v1/*`.
    pub service_secret: String,
    /// Signs join links.
    pub ticket_key: String,
    pub db_path: String,
    pub retention_days: i64,
    pub recording: Option<RecordingStorage>,
}

fn required(name: &str) -> Result<String, String> {
    match env::var(name) {
        Ok(v) if !v.trim().is_empty() => Ok(v.trim().to_string()),
        _ => Err(format!("{name} must be set")),
    }
}

fn long_secret(name: &str) -> Result<String, String> {
    let value = required(name)?;
    if value.len() < 32 {
        return Err(format!("{name} must be at least 32 characters"));
    }
    Ok(value)
}

fn origin(name: &str, schemes: &[&str]) -> Result<String, String> {
    let value = required(name)?;
    let value = value.trim_end_matches('/').to_string();
    if !schemes.iter().any(|s| value.starts_with(&format!("{s}://"))) {
        return Err(format!("{name} must start with one of: {}", schemes.join(", ")));
    }
    Ok(value)
}

impl Config {
    pub fn from_env() -> Result<Self, String> {
        let recording = match env::var("RECORDING_S3_BUCKET") {
            Ok(bucket) if !bucket.trim().is_empty() => Some(RecordingStorage {
                bucket: bucket.trim().to_string(),
                region: env::var("RECORDING_S3_REGION").unwrap_or_default(),
                endpoint: env::var("RECORDING_S3_ENDPOINT").unwrap_or_default(),
                access_key: required("RECORDING_S3_ACCESS_KEY")?,
                secret_key: required("RECORDING_S3_SECRET_KEY")?,
                force_path_style: env::var("RECORDING_S3_FORCE_PATH_STYLE").is_ok_and(|v| v == "1" || v == "true"),
                public_base_url: env::var("RECORDING_PUBLIC_BASE_URL")
                    .ok()
                    .map(|v| v.trim().trim_end_matches('/').to_string())
                    .filter(|v| !v.is_empty()),
            }),
            _ => None,
        };
        let retention_days = match env::var("LIVE_RETENTION_DAYS") {
            Ok(v) => v.trim().parse::<i64>().map_err(|_| "LIVE_RETENTION_DAYS must be a whole number".to_string())?,
            Err(_) => 90,
        };
        if retention_days < 1 {
            return Err("LIVE_RETENTION_DAYS must be at least 1".to_string());
        }
        let public_url = origin("LIVE_PUBLIC_URL", &["https", "http"])?;
        Ok(Self {
            bind: env::var("LIVE_BIND").unwrap_or_else(|_| "0.0.0.0:8080".to_string()),
            public_url: public_url.clone(),
            livekit_url: origin("LIVEKIT_URL", &["http", "https"])?,
            livekit_public_url: origin("LIVEKIT_PUBLIC_URL", &["wss", "ws"])?,
            livekit_api_key: required("LIVEKIT_API_KEY")?,
            livekit_api_secret: long_secret("LIVEKIT_API_SECRET")?,
            service_secret: long_secret("LIVE_SERVICE_SECRET")?,
            ticket_key: long_secret("LIVE_TICKET_KEY")?,
            internal_url: env::var("LIVE_INTERNAL_URL").map(|v| v.trim_end_matches('/').to_string()).unwrap_or_else(|_| public_url.clone()),
            captions_url: env::var("LIVE_CAPTIONS_URL").ok().map(|v| v.trim_end_matches('/').to_string()).filter(|v| !v.is_empty()),
            recording_internal_endpoint: env::var("RECORDING_S3_INTERNAL_ENDPOINT").ok().filter(|v| !v.is_empty()),
            first_admin: match (env::var("LIVE_ADMIN_EMAIL"), env::var("LIVE_ADMIN_PASSWORD")) {
                (Ok(email), Ok(password)) if email.contains('@') && password.len() >= 12 => Some((email.trim().to_lowercase(), password)),
                _ => None,
            },
            captions_language: env::var("WHISPER_LANGUAGE").ok().filter(|v| !v.is_empty() && v != "auto").unwrap_or_else(|| "en".to_string()),
            trust_forwarded_for: env::var("LIVE_TRUST_FORWARDED_FOR").is_ok_and(|v| v == "1"),
            translate_url: env::var("LIVE_TRANSLATE_URL").ok().map(|v| v.trim_end_matches('/').to_string()).filter(|v| !v.is_empty()),
            translate_languages: env::var("LIVE_TRANSLATE_LANGUAGES").unwrap_or_default().split(',').map(|v| v.trim().to_lowercase()).filter(|v| v.len() >= 2 && v.len() <= 8 && v.bytes().all(|b| b.is_ascii_alphabetic() || b == b'-')).collect(),
            slides_dir: env::var("LIVE_SLIDES_DIR").ok().filter(|v| !v.is_empty()),
            ingress_url: env::var("LIVE_INGRESS_URL").ok().filter(|v| !v.is_empty()),
            min_cpus: env::var("LIVE_MIN_CPUS").ok().and_then(|v| v.parse().ok()).unwrap_or(8),
            rtmp_allow_private: env::var("LIVE_RTMP_ALLOW_PRIVATE").is_ok_and(|v| v == "1"),
            upload_dir: env::var("LIVE_UPLOAD_DIR").unwrap_or_else(|_| "/data/uploads".to_string()),
            max_upload_bytes: match env::var("LIVE_MAX_UPLOAD_MB") {
                Ok(v) => v.trim().parse::<u64>().map_err(|_| "LIVE_MAX_UPLOAD_MB must be a whole number".to_string())? * 1024 * 1024,
                Err(_) => 500 * 1024 * 1024,
            },
            db_path: env::var("LIVE_DB_PATH").unwrap_or_else(|_| "/data/live.sqlite3".to_string()),
            retention_days,
            recording,
        })
    }
}

//! Everything that talks to the LiveKit server, behind a trait so the HTTP layer can be
//! tested without one. The real server is exercised by `tests/e2e/`.
use async_trait::async_trait;
use livekit_api::access_token::{AccessToken, VideoGrants};
use livekit_api::services::egress::{EgressClient, EgressOutput, RoomCompositeOptions, TrackEgressOutput};
use livekit_api::services::ingress::{CreateIngressOptions, IngressClient};
use livekit_api::services::room::{CreateRoomOptions, RoomClient, UpdateParticipantOptions};
use livekit_protocol as proto;
use std::time::Duration;

use crate::config::{Config, RecordingStorage};

#[derive(Debug)]
pub struct BackendError(pub String);

#[async_trait]
pub trait Backend: Send + Sync {
    async fn ensure_room(&self, room: &str) -> Result<(), BackendError>;
    /// Starts recording the room to `filepath`, by opening `page` in the recorder's browser, so
    /// the file has the same layout, frames and overlays the audience sees. Returns its ID.
    async fn start_recording(&self, room: &str, filepath: &str, page: &str) -> Result<String, BackendError>;
    /// Sends the stage to RTMP destinations, drawn by `page` like a recording. Returns its ID.
    async fn start_broadcast(&self, room: &str, urls: Vec<String>, page: &str) -> Result<String, BackendError>;
    /// Adds and removes destinations on a broadcast that's running.
    async fn update_broadcast(&self, egress_id: &str, add: Vec<String>, remove: Vec<String>) -> Result<(), BackendError>;
    /// Stops a recording or a broadcast.
    async fn stop_egress(&self, egress_id: &str) -> Result<(), BackendError>;
    /// Gives or takes away the right to publish cameras and microphones in a room. LiveKit
    /// itself unpublishes what someone already has on the way out.
    async fn set_can_publish(&self, room: &str, identity: &str, can_publish: bool) -> Result<(), BackendError>;
    async fn remove_participant(&self, room: &str, identity: &str) -> Result<(), BackendError>;
    /// Who is publishing a microphone in a room: (identity, track ID). Screens don't count.
    async fn stage_mics(&self, room: &str) -> Result<Vec<(String, String)>, BackendError>;
    /// Streams one audio track to a WebSocket as raw PCM. Returns its ID.
    async fn start_track_stream(&self, room: &str, track: &str, url: &str) -> Result<String, BackendError>;
    /// Closes a room and disconnects everyone in it.
    async fn close_room(&self, room: &str) -> Result<(), BackendError>;
    /// Makes an RTMP input that publishes to a room as `identity`. Returns its ID and stream key.
    async fn create_ingress(&self, room: &str, identity: &str, name: &str) -> Result<(String, String), BackendError>;
    async fn delete_ingress(&self, id: &str) -> Result<(), BackendError>;
    /// Plays the video at `url` into a room as `identity`, once, through LiveKit's ingress, so
    /// everyone gets it the way they get cameras. Returns the ingress ID.
    async fn create_video_ingress(&self, room: &str, identity: &str, name: &str, url: &str) -> Result<String, BackendError>;
}

/// A room stays open this long with nobody in it, so a host can drop and rejoin.
const EMPTY_TIMEOUT_SECONDS: u32 = 30 * 60;

pub struct LiveKit {
    rooms: RoomClient,
    egress: EgressClient,
    ingress: IngressClient,
    storage: Option<RecordingStorage>,
    video_kbps: i32,
}

/// How the recording is encoded: full HD at a bitrate that holds up for editing and for YouTube,
/// with AAC, which editors and every platform take. LiveKit's own default is 4.5 Mbps with Opus.
pub fn recording_encoding(video_kbps: i32) -> livekit_api::services::egress::encoding::EncodingOptions {
    livekit_api::services::egress::encoding::EncodingOptions {
        width: 1920,
        height: 1080,
        depth: 24,
        framerate: 30,
        audio_codec: proto::AudioCodec::Aac,
        audio_bitrate: 256,
        audio_frequency: 48000,
        video_codec: proto::VideoCodec::H264High,
        video_bitrate: video_kbps,
        keyframe_interval: 2.0,
        audio_quality: 0,
        video_quality: 0,
    }
}

impl LiveKit {
    pub fn new(config: &Config) -> Self {
        let (url, key, secret) = (&config.livekit_url, &config.livekit_api_key, &config.livekit_api_secret);
        Self {
            rooms: RoomClient::with_api_key(url, key, secret).with_request_timeout(Duration::from_secs(10)),
            egress: EgressClient::with_api_key(url, key, secret).with_request_timeout(Duration::from_secs(30)),
            ingress: IngressClient::with_api_key(url, key, secret),
            storage: config.recording.clone(),
            video_kbps: config.recording_video_kbps,
        }
    }
}

#[async_trait]
impl Backend for LiveKit {
    async fn ensure_room(&self, room: &str) -> Result<(), BackendError> {
        let options = CreateRoomOptions { empty_timeout: EMPTY_TIMEOUT_SECONDS, ..Default::default() };
        self.rooms.create_room(room, options).await.map(|_| ()).map_err(|e| BackendError(e.to_string()))
    }

    async fn start_recording(&self, room: &str, filepath: &str, page: &str) -> Result<String, BackendError> {
        let storage = self.storage.as_ref().ok_or_else(|| BackendError("recording storage is not configured".into()))?;
        let output = proto::EncodedFileOutput {
            file_type: proto::EncodedFileType::Mp4 as i32,
            filepath: filepath.to_string(),
            disable_manifest: true,
            output: Some(proto::encoded_file_output::Output::S3(proto::S3Upload {
                access_key: storage.access_key.clone(),
                secret: storage.secret_key.clone(),
                region: storage.region.clone(),
                endpoint: storage.endpoint.clone(),
                bucket: storage.bucket.clone(),
                force_path_style: storage.force_path_style,
                ..Default::default()
            })),
        };
        let options = RoomCompositeOptions {
            layout: "program".to_string(),
            custom_base_url: page.to_string(),
            encoding: recording_encoding(self.video_kbps),
            ..Default::default()
        };
        let info = self
            .egress
            .start_room_composite_egress(room, vec![EgressOutput::File(output)], options)
            .await
            .map_err(|e| BackendError(e.to_string()))?;
        Ok(info.egress_id)
    }

    async fn start_broadcast(&self, room: &str, urls: Vec<String>, page: &str) -> Result<String, BackendError> {
        let output = proto::StreamOutput { protocol: proto::StreamProtocol::Rtmp as i32, urls };
        let options = RoomCompositeOptions { layout: "program".to_string(), custom_base_url: page.to_string(), ..Default::default() };
        let info = self
            .egress
            .start_room_composite_egress(room, vec![EgressOutput::Stream(output)], options)
            .await
            .map_err(|e| BackendError(e.to_string()))?;
        Ok(info.egress_id)
    }

    async fn update_broadcast(&self, egress_id: &str, add: Vec<String>, remove: Vec<String>) -> Result<(), BackendError> {
        self.egress.update_stream(egress_id, add, remove).await.map(|_| ()).map_err(|e| BackendError(e.to_string()))
    }

    async fn stop_egress(&self, egress_id: &str) -> Result<(), BackendError> {
        self.egress.stop_egress(egress_id).await.map(|_| ()).map_err(|e| BackendError(e.to_string()))
    }

    async fn set_can_publish(&self, room: &str, identity: &str, can_publish: bool) -> Result<(), BackendError> {
        let permission = proto::ParticipantPermission {
            can_subscribe: true,
            can_publish,
            can_publish_sources: if can_publish { CAMERA_AND_MIC.iter().map(|s| *s as i32).collect() } else { vec![] },
            ..Default::default()
        };
        let options = UpdateParticipantOptions { permission: Some(permission), ..Default::default() };
        self.rooms.update_participant(room, identity, options).await.map(|_| ()).map_err(|e| BackendError(e.to_string()))
    }

    async fn remove_participant(&self, room: &str, identity: &str) -> Result<(), BackendError> {
        self.rooms.remove_participant(room, identity).await.map_err(|e| BackendError(e.to_string()))
    }

    async fn stage_mics(&self, room: &str) -> Result<Vec<(String, String)>, BackendError> {
        let participants = self.rooms.list_participants(room).await.map_err(|e| BackendError(e.to_string()))?;
        let mut out = vec![];
        for p in participants {
            for track in p.tracks {
                if track.source == proto::TrackSource::Microphone as i32 {
                    out.push((p.identity.clone(), track.sid));
                }
            }
        }
        Ok(out)
    }

    async fn start_track_stream(&self, room: &str, track: &str, url: &str) -> Result<String, BackendError> {
        let info = self
            .egress
            .start_track_egress(room, TrackEgressOutput::WebSocket(url.to_string()), track)
            .await
            .map_err(|e| BackendError(e.to_string()))?;
        Ok(info.egress_id)
    }

    async fn close_room(&self, room: &str) -> Result<(), BackendError> {
        self.rooms.delete_room(room).await.map_err(|e| BackendError(e.to_string()))
    }

    async fn create_ingress(&self, room: &str, identity: &str, name: &str) -> Result<(String, String), BackendError> {
        let options = CreateIngressOptions { name: name.to_string(), room_name: room.to_string(), participant_identity: identity.to_string(), participant_name: name.to_string(), ..Default::default() };
        let info = self.ingress.create_ingress(proto::IngressInput::RtmpInput, options).await.map_err(|e| BackendError(e.to_string()))?;
        Ok((info.ingress_id, info.stream_key))
    }

    async fn delete_ingress(&self, id: &str) -> Result<(), BackendError> {
        self.ingress.delete_ingress(id).await.map(|_| ()).map_err(|e| BackendError(e.to_string()))
    }

    async fn create_video_ingress(&self, room: &str, identity: &str, name: &str, url: &str) -> Result<String, BackendError> {
        let (video, audio) = video_ingress_encoding();
        let options = CreateIngressOptions {
            name: name.to_string(),
            room_name: room.to_string(),
            participant_identity: identity.to_string(),
            participant_name: name.to_string(),
            url: url.to_string(),
            video,
            audio,
            ..Default::default()
        };
        let info = self.ingress.create_ingress(proto::IngressInput::UrlInput, options).await.map_err(|e| BackendError(e.to_string()))?;
        Ok(info.ingress_id)
    }
}

/// How a video sent through the media server is encoded: 1080p30 H.264 at 6 Mbps, with 720p and
/// 360p layers for slower connections, and 128 kbps stereo Opus without silence suppression, so
/// music and quiet passages come through.
pub fn video_ingress_encoding() -> (proto::IngressVideoOptions, proto::IngressAudioOptions) {
    let layer = |quality: proto::VideoQuality, width: u32, height: u32, bitrate: u32| proto::VideoLayer { quality: quality as i32, width, height, bitrate, ..Default::default() };
    let video = proto::IngressVideoOptions {
        name: "video".into(),
        source: proto::TrackSource::ScreenShare as i32,
        encoding_options: Some(proto::ingress_video_options::EncodingOptions::Options(proto::IngressVideoEncodingOptions {
            video_codec: proto::VideoCodec::H264Baseline as i32,
            frame_rate: 30.0,
            layers: vec![
                layer(proto::VideoQuality::High, 1920, 1080, 6_000_000),
                layer(proto::VideoQuality::Medium, 1280, 720, 2_500_000),
                layer(proto::VideoQuality::Low, 640, 360, 600_000),
            ],
        })),
    };
    let audio = proto::IngressAudioOptions {
        name: "audio".into(),
        source: proto::TrackSource::ScreenShareAudio as i32,
        encoding_options: Some(proto::ingress_audio_options::EncodingOptions::Options(proto::IngressAudioEncodingOptions {
            audio_codec: proto::AudioCodec::Opus as i32,
            bitrate: 128_000,
            disable_dtx: true,
            channels: 2,
        })),
    };
    (video, audio)
}

const CAMERA_AND_MIC: [proto::TrackSource; 2] = [proto::TrackSource::Camera, proto::TrackSource::Microphone];

/// What one connection to a room may do. There is deliberately no way to publish data: chat,
/// questions and polls go through this service, where they can be moderated.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Access {
    /// Watches and listens. `hidden` keeps them out of everyone's participant list.
    Watch { hidden: bool },
    /// Camera and microphone.
    Present,
    /// A screen and its sound, and nothing else. A producer's screen goes through this, so
    /// LiveKit refuses their camera and microphone on the stage whatever their browser tries.
    Share,
}

/// The short-lived credential a browser connects with.
const TOKEN_MINUTES: u64 = 15;

pub fn access_token(config: &Config, access: Access, room: &str, identity: &str, name: &str) -> Result<String, BackendError> {
    let grants = VideoGrants {
        room_join: true,
        room: room.to_string(),
        can_publish: Some(matches!(access, Access::Present | Access::Share)),
        can_subscribe: Some(!matches!(access, Access::Share)),
        can_publish_data: Some(false),
        can_publish_sources: match access {
            Access::Watch { .. } => vec![],
            Access::Present => vec!["camera".to_string(), "microphone".to_string()],
            Access::Share => vec!["screen_share".to_string(), "screen_share_audio".to_string()],
        },
        hidden: matches!(access, Access::Watch { hidden: true }),
        ..Default::default()
    };
    AccessToken::with_api_key(&config.livekit_api_key, &config.livekit_api_secret)
        .with_identity(identity)
        .with_name(name)
        // Short, because taking away a credential means waiting for it to run out. LiveKit keeps a
        // live connection's credential fresh by itself, and the webhook checks catch a stale one.
        .with_ttl(Duration::from_secs(TOKEN_MINUTES * 60))
        .with_grants(grants)
        .to_jwt()
        .map_err(|e| BackendError(e.to_string()))
}

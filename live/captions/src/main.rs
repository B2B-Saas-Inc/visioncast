//! Whisper as a small HTTP service. It's separate from visioncast-live so a crash or a memory
//! spike here can't touch a live room: the worst case is a caption that doesn't appear.
//!
//! `POST /transcribe?prompt=...` takes 16 kHz mono signed 16-bit PCM and answers `{"text": ...}`.
//! It listens only where compose puts it, on the server's loopback, so there's no sign-in.
use axum::{
    body::Bytes,
    extract::{DefaultBodyLimit, Query, State},
    http::StatusCode,
    routing::{get, post},
    Json, Router,
};
use serde::Deserialize;
use serde_json::{json, Value};
use std::sync::Arc;
use tokio::sync::Semaphore;
use whisper_rs::{FullParams, SamplingStrategy, WhisperContext, WhisperContextParameters};

struct Engine {
    context: WhisperContext,
    /// Each transcription is a few CPU threads for a moment: this keeps them from piling up.
    permits: Semaphore,
    threads: i32,
    language: String,
}

#[derive(Deserialize)]
struct Params {
    #[serde(default)]
    prompt: String,
}

fn number(name: &str, default: usize) -> usize {
    std::env::var(name).ok().and_then(|v| v.parse().ok()).unwrap_or(default)
}

fn run(engine: &Engine, audio: &[f32], prompt: &str) -> Result<String, String> {
    let mut state = engine.context.create_state().map_err(|e| e.to_string())?;
    let mut params = FullParams::new(SamplingStrategy::Greedy { best_of: 1 });
    params.set_n_threads(engine.threads);
    params.set_language(Some(&engine.language));
    params.set_translate(false);
    params.set_print_progress(false);
    params.set_print_realtime(false);
    params.set_print_special(false);
    params.set_print_timestamps(false);
    params.set_suppress_blank(true);
    params.set_no_context(true);
    if !prompt.is_empty() {
        params.set_initial_prompt(prompt);
    }
    state.full(params, audio).map_err(|e| e.to_string())?;
    let mut text = String::new();
    for segment in state.as_iter() {
        // Whisper's own guess that this stretch wasn't speech.
        if segment.no_speech_probability() > 0.8 {
            continue;
        }
        text.push_str(segment.to_str_lossy().map_err(|e| e.to_string())?.as_ref());
    }
    Ok(text.trim().to_string())
}

async fn transcribe(State(engine): State<Arc<Engine>>, Query(q): Query<Params>, body: Bytes) -> Result<Json<Value>, StatusCode> {
    let audio: Vec<f32> = body.chunks_exact(2).map(|p| i16::from_le_bytes([p[0], p[1]]) as f32 / 32768.0).collect();
    let _permit = engine.permits.acquire().await.map_err(|_| StatusCode::SERVICE_UNAVAILABLE)?;
    let worker = engine.clone();
    let started = std::time::Instant::now();
    let seconds = audio.len() as f32 / 16_000.0;
    let result = tokio::task::spawn_blocking(move || run(&worker, &audio, &q.prompt)).await;
    match result {
        Ok(Ok(text)) => {
            let ms = started.elapsed().as_millis() as u64;
            println!("{{\"audioSeconds\":{seconds:.1},\"tookMs\":{ms}}}");
            Ok(Json(json!({ "text": text, "ms": ms })))
        }
        Ok(Err(err)) => {
            eprintln!("transcription failed: {err}");
            Err(StatusCode::INTERNAL_SERVER_ERROR)
        }
        Err(_) => Err(StatusCode::INTERNAL_SERVER_ERROR),
    }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let model = std::env::var("WHISPER_MODEL").unwrap_or_else(|_| "/models/ggml-base.en.bin".to_string());
    let bind = std::env::var("CAPTIONS_BIND").unwrap_or_else(|_| "127.0.0.1:8090".to_string());
    whisper_rs::install_logging_hooks();
    let context = WhisperContext::new_with_params(&model, WhisperContextParameters::default())?;
    let engine = Arc::new(Engine {
        context,
        permits: Semaphore::new(number("CAPTIONS_CONCURRENCY", 2)),
        threads: number("CAPTIONS_THREADS", 2) as i32,
        language: std::env::var("WHISPER_LANGUAGE").unwrap_or_else(|_| "en".to_string()),
    });
    let app = Router::new()
        .route("/transcribe", post(transcribe))
        .route("/healthz", get(|| async { Json(json!({ "status": "ok" })) }))
        .layer(DefaultBodyLimit::max(4 * 1024 * 1024))
        .with_state(engine);
    println!("listening on {bind}, model {model}");
    let listener = tokio::net::TcpListener::bind(&bind).await?;
    axum::serve(listener, app).with_graceful_shutdown(async { let _ = tokio::signal::ctrl_c().await; }).await?;
    Ok(())
}

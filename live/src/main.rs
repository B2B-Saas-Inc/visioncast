use std::sync::Arc;
use std::time::Duration;
use tracing_subscriber::EnvFilter;
use visioncast_live::{
    app::{router, AppState},
    backend::LiveKit,
    config::Config,
    store::Store,
};

const DAY_SECONDS: i64 = 24 * 3600;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt()
        .json()
        .with_env_filter(EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")))
        .init();
    let config = Config::from_env()?;
    let store = Store::open(&config.db_path)?;
    let backend = Arc::new(LiveKit::new(&config));
    let bind = config.bind.clone();
    let retention = config.retention_days;
    let recording = config.recording.is_some();
    let state = AppState::new(config, store, backend);
    visioncast_live::admin::seed_first_admin(&state)?;

    // On the server itself, for someone locked out: `visioncast-live reset-admin you@example.com`
    let args: Vec<String> = std::env::args().collect();
    if args.get(1).map(String::as_str) == Some("reset-admin") {
        let email = args.get(2).ok_or("Usage: visioncast-live reset-admin <email>")?;
        println!("{}", visioncast_live::admin::reset_link_for(&state, email)?);
        return Ok(());
    }

    visioncast_live::studio::revive_schedules(&state);
    let purger = state.clone();
    tokio::spawn(async move {
        let mut tick = tokio::time::interval(Duration::from_secs(3600));
        loop {
            tick.tick().await;
            let cutoff = chrono::Utc::now().timestamp() - retention * DAY_SECONDS;
            if let Ok(rooms) = purger.store.rooms() {
                for room in rooms.iter().filter(|r| r.ends_at < cutoff) {
                    visioncast_live::studio::delete_inputs(&purger, &room.id).await;
                }
            }
            match purger.store.purge(cutoff) {
                Ok(0) => {}
                Ok(rooms) => {
                    tracing::info!(rooms, "removed rooms past retention");
                    visioncast_live::studio::purge_uploads(&purger).await;
                }
                Err(err) => tracing::error!(error = %err, "retention purge failed"),
            }
        }
    });

    let listener = tokio::net::TcpListener::bind(&bind).await?;
    tracing::info!(%bind, recording, version = env!("CARGO_PKG_VERSION"), "visioncast-live listening");
    axum::serve(listener, router(state))
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await?;
    Ok(())
}

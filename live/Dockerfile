# visioncast-live: the service in front of a LiveKit server. Base images are pinned by digest.
ARG RUST_IMAGE=rust:1.99-slim-bookworm@sha256:24707e70a4f1e23d9a7b25202ec43126d28a52294b96aad5c670df1fd1f1d400
ARG RUNTIME_IMAGE=debian:bookworm-slim@sha256:99c0b25c46d0214d5a2091092e7908907dcaecd6a962cde0ac700acee222b6f3

FROM ${RUST_IMAGE} AS build
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY src ./src
COPY assets ./assets
RUN cargo build --release --locked --bin visioncast-live

FROM ${RUNTIME_IMAGE} AS production
RUN useradd --system --uid 10001 --no-create-home live && mkdir /data /spool && chown live /data /spool
COPY --from=build /src/target/release/visioncast-live /usr/local/bin/visioncast-live
USER live
VOLUME /data
EXPOSE 8080
ENTRYPOINT ["visioncast-live"]

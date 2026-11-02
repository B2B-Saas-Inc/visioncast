# Whisper for visioncast-live. The model isn't in the image: compose mounts it from models/.
ARG RUST_IMAGE=rust:1.99-slim-bookworm@sha256:24707e70a4f1e23d9a7b25202ec43126d28a52294b96aad5c670df1fd1f1d400
ARG RUNTIME_IMAGE=debian:bookworm-slim@sha256:99c0b25c46d0214d5a2091092e7908907dcaecd6a962cde0ac700acee222b6f3

FROM ${RUST_IMAGE} AS build
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends cmake clang libclang-dev g++ make > /dev/null
WORKDIR /src
COPY Cargo.toml ./
COPY Cargo.lock ./
COPY src ./src
# GCC 12 can't compile Whisper's ARM half-precision code. Clang can. x86 builds don't care.
RUN if [ "$(uname -m)" = aarch64 ]; then export CC=clang CXX=clang++; fi; cargo build --locked --release --bin visioncast-captions

FROM ${RUNTIME_IMAGE} AS production
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends libgomp1 > /dev/null; rm -rf /var/lib/apt/lists/*; useradd --system --uid 10002 --no-create-home captions
COPY --from=build /src/target/release/visioncast-captions /usr/local/bin/visioncast-captions
USER captions
EXPOSE 8090
ENTRYPOINT ["visioncast-captions"]

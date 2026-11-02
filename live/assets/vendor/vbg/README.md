# Virtual backgrounds

Served from this service so a room never reaches out to a CDN.

- `vbg.js`: `@livekit/track-processors` 0.8.2 and `@mediapipe/tasks-vision` 0.10.14 (the version it requires), bundled with esbuild.
  Both are Apache-2.0.
- `vision_wasm_internal.js` and `.wasm`: from `@mediapipe/tasks-vision` 0.10.14.
- `selfie_segmenter.tflite`: MediaPipe's selfie segmenter, float16, from
  storage.googleapis.com/mediapipe-models. Apache-2.0.

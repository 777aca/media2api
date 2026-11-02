# Grok gateway simulation context

This simulation stands in for the HTTP API of a separately configured grok2api service. Its handlers intentionally cover only transport and representative response shapes; they do not simulate real accounts, models, media generation, billing or quotas. An owner-published mock was not located because Postman workspace search requires login.

The relevant upstream contract is the [grok2api v3.1.8 README](https://github.com/chenyme/grok2api/blob/v3.1.8/README.zh-CN.md) and [HTTP handler](https://github.com/chenyme/grok2api/blob/v3.1.8/backend/internal/transport/http/inference/handler.go). The local mock uses a synthetic client key and explicitly returns 501 for endpoints it does not model. WebSocket tests use a separate local echo server, not the Postman mock.

Run locally:

```powershell
postman simulation run postman/simulations/grok-gateway.sim.yaml --port 40103 --no-history --output ndjson
.venv\Scripts\python.exe scripts\run_grok_simulation_tests.py http://127.0.0.1:40103/grok
```

On 2026-10-30, with Postman CLI 1.77.0 and the local working tree based on `0ae7a40` plus uncommitted changes, the test passed. The simulation log recorded model listing, Chat Completions, Responses create/get/delete, Messages, image generation/edit, video create/read, synthetic ticket upload, speech and transcription. Missing authentication returned 401; a two-image request was rejected locally and never reached the stand-in. A loopback WebSocket echo verified text and binary forwarding through both realtime and STT paths. A request through a real local Uvicorn server confirmed that its access log redacts the synthetic upload ticket and query.

These observations establish forwarding behavior against the local stand-ins only. A real grok2api container, its model-specific behavior, upstream account authorization, actual media, and production network configuration remain unverified.

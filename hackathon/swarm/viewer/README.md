# ViewPoint Swarm — Static Replay Exporter & Cloud Run Viewer (`swarm/viewer/`)

## Honest Scope Statement

Read-only `REPLAY` viewer (`live=false`); displays recorded `events.jsonl` + SHA-256 verified still frames + optional composed `.mp4`.

- Zero third-party Python dependencies (Python 3.12 standard library only).
- Performs **zero** live Blender, X11/Wayland, or broker actuation (`mode: "REPLAY"`, `live: false`).
- Fail-closed startup validation via `ReplayStore(..., require_pin=True)` (`viewpoint.replay/v1`, byte-identical copy of `gateway/replay_store.py`) and `validate_swarm_bundle(..., require_pin=True)` (`viewpoint.swarm_timeline/v1`).
- External SHA-256 pinning (`R4` + `H5_r2`): pins live outside mutable `replays/` and `static/data/` directories in `/app/PINS.manifest` (`0444`), every `replays/<id>.json` cryptographically binds its paired `static/data/<id>.swarm.json` via `swarm_sha256=<64-hex>` in `provenance.note`, and when `REPLAY_PINS` (`"id1=sha256,id2=sha256"`) + `REPLAY_REQUIRE_PIN=1` are set in the environment, `REPLAY_PINS` takes strict precedence over any local manifest file.
- Honest verdict & budget accounting (`R8`, `R7`): `VERIFIED_WITH_SPILL` maps exclusively to `status="observed"` with amber/warning UI styling (`verdict-spill` / `verdict-amber`, never `"verified"` or green), and every bundle discloses exported vs. source step/frame counts plus raw `events.jsonl` SHA-256.
- HTTP server permits `GET` and `HEAD` only (`405` on mutating verbs), serves `GET /healthz` (`{"ok": true}`), exposes optional validated `GATEWAY_URL` (`#gateway-link` in UI topbar), enforces a strict `Content-Security-Policy` (`script-src 'self'`, `style-src 'self'`, `object-src 'none'`, `frame-ancestors 'none'`), rejects symlinks and `..` path traversal, and renders all dynamic strings via `textContent`.

## Pre-Seeded Verified Live Replays

1. **`take2-w2-4agents`** *(default flagship selection)* — StarForge Sentinel — 4-Agent Live Rehearsal (Wave 2) (`modeler-agent`, `props-agent`, `look-agent`, `camera-agent`; `10` verified PNG frames from `16` source frames, `34` steps from `184` records, `60` turns).
2. **`take3-w2-guards`** — Deterministic Guard Beats — `CLAIM_REFUSED` & `STALE_FRAME` (`3` verified PNG frames, `15` steps from `69` records, `24` turns).
3. **`take1-w1-2agents`** — StarForge Sentinel — 2-Agent Live Gate (Wave 1) (`9` verified PNG frames, `20` steps from `100` records, `32` turns).

Exact external `REPLAY_PINS` environment string for all three pre-seeded replays:

```bash
export REPLAY_REQUIRE_PIN=1
export REPLAY_PINS="take2-w2-4agents=6c83360080dd4d82d76f4135034c6f9427953e471f4e36da7c1ac167580c1e8c,take3-w2-guards=1c376a25fd99e2349717156bc3bf5e0570e8e65620fb1970dc096ecad4166b6e,take1-w1-2agents=2023da5127580be7c77522e312f455733c2fbbff56a7992512e112b5b3d6f335"
```

## Export Verified Replay Bundles

Run from repository root (`.`):

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B -m swarm.viewer.export_replay \
  --events swarm/run/take2_w2_4agents/evidence/events.jsonl \
  --frames swarm/run/take2_w2_4agents/evidence/frames \
  --sidecar <VIDEO_DIR>/swarm_take2_w2_4agents_140051.mp4.meta.json \
  --max-frames 10 \
  --title "StarForge Sentinel — 4-Agent Live Rehearsal (Wave 2)" \
  --out-dir ./swarm/viewer \
  --replay-id take2-w2-4agents

PYTHONDONTWRITEBYTECODE=1 python3 -B -m swarm.viewer.export_replay \
  --events swarm/run/take3_w2_guards/evidence/events.jsonl \
  --frames swarm/run/take3_w2_guards/evidence/frames \
  --sidecar <VIDEO_DIR>/swarm_take3_w2_guards_141533.mp4.meta.json \
  --title "Deterministic Guard Beats — CLAIM_REFUSED & STALE_FRAME" \
  --out-dir ./swarm/viewer \
  --replay-id take3-w2-guards

PYTHONDONTWRITEBYTECODE=1 python3 -B -m swarm.viewer.export_replay \
  --events swarm/run/take1_w1_2agents/evidence/events.jsonl \
  --frames swarm/run/take1_w1_2agents/evidence/frames \
  --sidecar <VIDEO_DIR>/swarm_take1_w1_2agents_131916.mp4.meta.json \
  --title "StarForge Sentinel — 2-Agent Live Gate (Wave 1)" \
  --out-dir ./swarm/viewer \
  --replay-id take1-w1-2agents
```

## Run Locally

```bash
REPLAY_REQUIRE_PIN=1 \
REPLAY_PINS="take2-w2-4agents=6c83360080dd4d82d76f4135034c6f9427953e471f4e36da7c1ac167580c1e8c,take3-w2-guards=1c376a25fd99e2349717156bc3bf5e0570e8e65620fb1970dc096ecad4166b6e,take1-w1-2agents=2023da5127580be7c77522e312f455733c2fbbff56a7992512e112b5b3d6f335" \
PYTHONDONTWRITEBYTECODE=1 python3 -B -m swarm.viewer.server --host 127.0.0.1 --port 8080
```

Open `http://127.0.0.1:8080` (`GET http://127.0.0.1:8080/healthz` returns `{"ok":true}`).

## Run Unit & Must-Fail Verification Suite

```bash
PYTHONDONTWRITEBYTECODE=1 systemd-run --user --scope -q \
  -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 \
  python3 -B -m unittest discover -s swarm/viewer -p 'test_*.py' -v
```

## Cloud Run Operator Deployment

> **Operator-only step**: Cloud Run deployment is executed exclusively by the human operator using their authenticated `gcloud` environment (automated agents never run `gcloud run deploy`).

```bash
gcloud run deploy viewpoint-swarm-viewer \
  --source ./swarm/viewer \
  --region europe-west1 \
  --platform managed \
  --allow-unauthenticated \
  --port 8080 \
  --memory 512Mi \
  --cpu 1 \
  --max-instances 3 \
  --set-env-vars "^@^REPLAY_REQUIRE_PIN=1@REPLAY_PINS=take2-w2-4agents=6c83360080dd4d82d76f4135034c6f9427953e471f4e36da7c1ac167580c1e8c,take3-w2-guards=1c376a25fd99e2349717156bc3bf5e0570e8e65620fb1970dc096ecad4166b6e,take1-w1-2agents=2023da5127580be7c77522e312f455733c2fbbff56a7992512e112b5b3d6f335"
```

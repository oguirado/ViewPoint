# TASK — judge-facing explainer block (deadline 18:55 CET, be fast)
Write a compact, high-clarity "What you're looking at" explainer for a hackathon judge who has 60 seconds, to be inserted in static/index.html immediately after </header> and before <section class="error-banner" ...>.
Must contain, in plain English (max ~170 words of visible text in total):
1. One-sentence headline: what ViewPoint is (from README section 1/2) and what this page is (a recorded, verified replay of AI agents sharing one Blender, not live).
2. "How to read it" as 3-4 short numbered steps: pick a take, press Play/Next, left = what the viewport looked like after each turn, right = which agent acted + what it promised + whether the core verified it.
3. A tiny legend for the verdict pills: STATE_VERIFIED, VERIFIED_WITH_SPILL, JOB_ERROR, PASS, CLAIM_REFUSED/STALE_FRAME (take3).
4. "Try this": take3-w2-guards to watch the guards refuse bad actions.
5. One honest-scope line + link to https://github.com/oguirado/ViewPoint (rel="noopener noreferrer", target="_blank").
Use a <section class="explainer" aria-labelledby="explainer-title"> and, for the legend, <details> so it can stay compact. Reuse existing CSS variables from style.css; include a mobile rule (<= 720px).
OUTPUT EXACTLY two fenced blocks and nothing else: ```html (the section only) and ```css (rules to APPEND to style.css). No style= attributes, no scripts.
# CONTEXT PACK — ViewPoint hosted replay viewer (Track H, Gemini-authored code)

## 1. Repo README, hackathon build section (verbatim)
# ViewPoint

ViewPoint lets an AI run a browser capsule the operator owns, watches, and can take over — and no agent action counts until the core has admitted it and verified it against a fresh frame.
## Hackathon build (swarm-1114)

The `swarm-1114` build introduces multi-agent collaborative 3D asset generation on **one shared Blender 4.0.2 instance** [MEASURED] (`vpswarm` on private Xvfb `:2` [MEASURED]), governed by an explicit non-interactive turn queue (`THINK -> ENQUEUE -> TURN -> RETURN`). Headless agents (`modeler-agent`, `props-agent`, `look-agent`, `camera-agent`) inspect frozen frames concurrently, while scene mutations and viewport camera adjustments are serialized one turn at a time through disjoint prefix write-set claims (`col:CHAR_`, `obj:CHAR_`, `col:PROPS_`, `obj:PROP_`, `mat:`, `light:`, `world:`, `cam:`, `render:`) declared per agent and checked by the broker; out-of-claim effects are detected and logged as spill.

### Two-Track Declaration & Cross-Family Reviews

1. **Track H (`swarm/`) — Hackathon-Claimed Code**:
   All claimed code under `swarm/` was written by Gemini sessions (`gemini-4-hydrogen-high`, `gemini-4-carbon-high`, `gemini-3.8-flash-high`) via headless `agy --output-format stream-json` after `08:59 CET` [CITED] on Saturday 14 Nov 2026. Cryptographic SHA-256 receipts for all `25` Track H `.jsonl` proof logs are indexed in [swarm/docs/PROOF.md](swarm/docs/PROOF.md).
2. **Track P (`product/`) — Declared Non-Gemini Product & Adversarial Work**:
   Authored by Anthropic models (`claude-opus-5-5`, `claude-fable-5-2`) via `claude -p --verbose` prior to `13:59 CET` [CITED] (`19` `.jsonl` proof logs including `product/proof/P4f_1343.jsonl`) as declared product explorations (`product/admission/`, `product/fanout/`, `product/mpx/REPORT.md`) and findings-only adversarial reviews (`product/reviews/`, `product/repair_specs/REPAIR_SPECS.md`, `product/comparison/H_VS_REF_REPORT.md`). 0 files of Track P code written under `swarm/` [CITED: `PROVENANCE.md`]; one early Track P housekeeping commit (`f667745`) updated `swarm/.gitignore` and removed compiled `.pyc` files. Track P code is never imported into `swarm/` or claimed as Gemini-generated.
3. **Post-`13:59 CET` Cross-Family Reviews (`Sol 6.1` via `swarm`)**:
   Findings-only verification on `gpt-6.1-sol` (`--sin-herramientas`) produced [product/reviews/Sol_H5_r1.md](product/reviews/Sol_H5_r1.md) (`REJECTED`, `4` `LIVE_PATH_MAJOR` findings fixed in `H5_r2`), [product/reviews/Sol_H5_r2.md](product/reviews/Sol_H5_r2.md) (`APPROVED`, `0` blockers, `0` live-path defects), [product/reviews/Sol_H6_r2.md](product/reviews/Sol_H6_r2.md) (`REJECTED`, `1` `LIVE_PATH_MAJOR` review-status wording defect fixed in `H6_r3`), and [product/reviews/Sol_H6_r3.md](product/reviews/Sol_H6_r3.md) (`APPROVED`, `0` blockers, `0` live-path defects).

### Honest Process Boundary & Projection Guarantee

> "Browser capsules (measured 13 Nov) run as containers with their own display. Today's Blender 4.0.2 swarm capsule runs as host processes inside `systemd-run` memory-capped scopes on a private Xvfb display (`:2`) under one shared UID: per-run bridge nonces and prefix claims catch accidental cross-writes, stale frames, and buggy agents, and are not an OS security boundary against a hostile same-UID process. Per-agent UIDs/containers are roadmap work."

Projection to OpenSwitchboard (`viewpoint-capsule`) and TaskLight (`context-wall`) is best-effort; `swarm/run/events.jsonl` is the authoritative local record of truth, audited at end-of-run by `projector.py --finalize` (via `reconciler.py`).

### Measured Results & Live Takes

- **Unit Test Suites**:
  - `H1` broker + bridge: `19/19` tests passing [MEASURED]
  - `H2` agents + runner: `19/19` tests passing [MEASURED]
  - `H3` overlay compositor: `15/15` tests passing [MEASURED]
  - `H4` projection + reconciler: `29/29` tests passing [MEASURED]
  - `H5` standalone replay viewer (`swarm/viewer/`): `17/17` tests passing [MEASURED] (`3` pre-seeded real replays)
  - `H6` docs & honesty gates (`swarm/docs/test_docs.py`): passing [MEASURED]
- **Wave 2 Flagship 4-Agent Rehearsal (`take2_w2_4agents`)**:
  - `4` `gemini-4-hydrogen-high` agents (`modeler-agent`, `props-agent`, `look-agent`, `camera-agent`) exited `rc=0` [MEASURED]; `184` broker events [MEASURED]; `13` verified turns (`11` `STATE_VERIFIED` + `2` `VERIFIED_WITH_SPILL`), `2` failed scripts typed `JOB_ERROR` and retried (`m1`, `l3`), `0` accepted cross-claim jobs, `0` open turns [MEASURED]

## 2. Viewer README (verbatim, first 30 lines)
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


## 3. Review of the live page by the operator's reviewer (18:30 CET, 14 Nov 2026)
Live URL: https://viewpoint-swarm-viewer-dtrnknr6bq-ew.a.run.app (Cloud Run, europe-west1, REPLAY only).
A first-time hackathon judge does NOT understand what they are looking at:
- No sentence says what ViewPoint is or what this page proves. It opens with "REPLAY · NOT LIVE" and "schema=viewpoint.swarm_timeline/v1".
- Jargon never explained: scene_epoch, view_epoch, lease, write_prefixes, write set, spill, PASS x45, SHA-256 receipts.
- Viewer starts on frame 0 = almost empty Blender default scene; judge does not know to press Play/Next or pick another take.
- Verdict pills shown: STATE_VERIFIED (green), VERIFIED_WITH_SPILL (amber = an agent touched something outside its claim; logged, not counted as clean), JOB_ERROR (red), PASS (empty turn: agent had nothing to do). take3 shows guard refusals CLAIM_REFUSED and STALE_FRAME.
- Layout: left card "Viewport Evidence" = the Blender viewport frame after each turn with agent HUD; right "Active Turn Inspector" = who acted, what it declared it would do (Declarative Expect), its write set, verdict; "Turn-by-turn timeline"; "Disjoint agent ring & claims"; "Gateway replay steps"; "SHA-256 cryptographic receipt".
- Three takes: take2-w2-4agents (flagship: 4 agents build the StarForge Sentinel robot scene), take3-w2-guards (guards refusing bad actions), take1-w1-2agents (first 2-agent gate).
- Gemini planner gateway is a separate service and is NOT deployed now (gateway link stays hidden). Do not claim live Gemini planning on this page.

## 4. Current static/index.html (verbatim)
```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ViewPoint Swarm — Static Replay Viewer</title>
  <link rel="stylesheet" href="/static/style.css">
</head>
<body>
  <header class="topbar">
    <div class="brand-group">
      <span class="replay-pill" id="mode-badge">REPLAY · NOT LIVE</span>
      <div>
        <h1 class="brand-title" id="replay-title">ViewPoint Swarm Evidence Viewer</h1>
        <p class="brand-subtitle" id="replay-subtitle">Read-only recorded bundle (mode=REPLAY, live=false) · zero live capsule actuation</p>
      </div>
    </div>
    <div class="selector-group">
      <a id="gateway-link" class="gateway-link" hidden rel="noopener noreferrer" target="_blank">Live Gemini plan (separate service)</a>
      <label class="field-label" for="replay-select">Recorded Bundle</label>
      <select id="replay-select" class="select-input" aria-label="Select recorded replay bundle"></select>
    </div>
  </header>

  <section class="error-banner" id="error-banner" role="alert" hidden></section>

  <main class="workspace">
    <section class="stage-column">
      <div class="card viewport-card">
        <div class="card-header">
          <div class="header-row">
            <span class="section-tag">VIEWPORT EVIDENCE</span>
            <span class="mono-meta" id="active-frame-meta">Frame —</span>
          </div>
          <div class="view-mode-controls">
            <button type="button" class="btn btn-small is-active" id="btn-mode-frames">Verified Frames + HUD</button>
            <button type="button" class="btn btn-small" id="btn-mode-video" hidden>Composed MP4</button>
          </div>
        </div>

        <div class="video-container" id="video-slot" hidden>
          <video id="replay-video" class="viewport-video" controls preload="metadata"></video>
        </div>

        <div class="frame-stage" id="frame-slot">
          <div class="frame-canvas-wrap">
            <img id="frame-image" class="viewport-img" alt="Verified Blender capsule viewport frame">
            <canvas id="cursor-canvas" class="viewport-canvas" width="1920" height="1080" aria-label="Off-display multi-agent cursor overlay"></canvas>
          </div>
          <p class="video-fallback-note" id="video-fallback-note">
            Offline MP4 not mounted in static bundle — scrubbing SHA-256 verified PNG frames with off-display cursor overlay.
          </p>
        </div>

        <p class="frame-caption" id="frame-caption">—</p>

        <div class="scrubber-bar">
          <div class="scrubber-buttons">
            <button type="button" class="btn" id="btn-prev" aria-label="Previous turn or frame">Prev</button>
            <button type="button" class="btn btn-primary" id="btn-play" aria-label="Play or pause frame scrubber">Play</button>
            <button type="button" class="btn" id="btn-next" aria-label="Next turn or frame">Next</button>
          </div>
          <div class="scrubber-slider-wrap">
            <input type="range" id="timeline-slider" class="range-slider" min="0" max="0" value="0" step="1" aria-label="Scrub verified replay turns">
            <div class="scrubber-meta">
              <span class="mono-meta" id="scrubber-pos-label">Step 0 / 0</span>
              <span class="mono-meta" id="scrubber-time-label">t = 0 ms</span>
            </div>
          </div>
        </div>
      </div>

      <div class="card agents-card">
        <div class="card-header">
          <span class="section-tag">DISJOINT AGENT RING &amp; CLAIMS</span>
          <span class="mono-meta">Round-Robin: modeler → props → look → camera</span>
        </div>
        <div class="agent-grid" id="agent-roster"></div>
      </div>

      <div class="card steps-card">
        <div class="card-header">
          <span class="section-tag">GATEWAY REPLAY STEPS (viewpoint.replay/v1)</span>
          <span class="mono-meta" id="gateway-steps-count">0 steps</span>
        </div>
        <div class="gateway-step-list" id="gateway-step-list"></div>
      </div>
    </section>

    <aside class="inspector-column">
      <div class="card turn-inspector-card">
        <div class="card-header">
          <span class="section-tag">ACTIVE TURN INSPECTOR</span>
          <span class="verdict-pill verdict-observed" id="detail-verdict">—</span>
        </div>
        <dl class="detail-grid">
          <div class="detail-item">
            <dt>Agent / Turn</dt>
            <dd id="detail-agent">—</dd>
          </div>
          <div class="detail-item">
            <dt>Event ID / Lane</dt>
            <dd id="detail-event">—</dd>
          </div>
          <div class="detail-item">
            <dt>Frame Transition</dt>
            <dd id="detail-frames">—</dd>
          </div>
          <div class="detail-item">
            <dt>Write Set</dt>
            <dd id="detail-writeset">—</dd>
          </div>
          <div class="detail-item detail-wide">
            <dt>Declarative Expect</dt>
            <dd id="detail-expect">—</dd>
          </div>
          <div class="detail-item detail-wide">
            <dt>Reason / Receipt Note</dt>
            <dd class="mono-block" id="detail-reason">—</dd>
          </div>
        </dl>
      </div>

      <div class="card timeline-card">
        <div class="card-header">
          <span class="section-tag">TURN-BY-TURN TIMELINE</span>
          <label class="toggle-label">
            <input type="checkbox" id="toggle-hide-pass" checked>
            <span>Hide empty PASS</span>
          </label>
        </div>
        <div class="verdict-summary-row" id="verdict-summary"></div>
        <div class="turn-list" id="turn-list" role="listbox" aria-label="Recorded broker turns"></div>
      </div>

      <div class="card receipt-card">
        <div class="card-header">
          <span class="section-tag">SHA-256 CRYPTOGRAPHIC RECEIPT</span>
          <span class="attestation-badge" id="receipt-attestation">pinned</span>
        </div>
        <dl class="receipt-list">
          <div class="receipt-row">
            <dt>Replay ID / Recorded At</dt>
            <dd class="mono-hash" id="receipt-id">—</dd>
          </div>
          <div class="receipt-row">
            <dt>Source events.jsonl SHA-256</dt>
            <dd class="mono-hash" id="receipt-events-sha">—</dd>
          </div>
          <div class="receipt-row">
            <dt>viewpoint.replay/v1 SHA-256</dt>
            <dd class="mono-hash" id="receipt-replay-sha">—</dd>
          </div>
          <div class="receipt-row">
            <dt>viewpoint.swarm_timeline/v1 SHA-256</dt>
            <dd class="mono-hash" id="receipt-swarm-sha">—</dd>
          </div>
          <div class="receipt-row">
            <dt>Active Frame PNG SHA-256</dt>
            <dd class="mono-hash" id="receipt-frame-sha">—</dd>
          </div>
        </dl>
        <div class="limitations-box">
          <h2 class="sub-heading">Scope &amp; Honest Limitations</h2>
          <p class="task-summary" id="replay-task">—</p>
          <ul class="limitations-list" id="limitations-list"></ul>
        </div>
      </div>
    </aside>
  </main>

  <script src="/static/app.js" defer></script>
</body>
</html>
```

## 5. Current static/style.css (verbatim)
```css
:root {
  --bg-root: #0b0f17;
  --bg-surface: #121826;
  --bg-elevated: #1a2234;
  --bg-hover: #222d44;
  --border-subtle: #28354f;
  --border-strong: #3a4c70;
  --text-primary: #f1f5f9;
  --text-secondary: #94a3b8;
  --text-muted: #64748b;

  --agent-modeler: #ff6b6b;
  --agent-props: #4ecdc4;
  --agent-look: #ffe66d;
  --agent-camera: #38bdf8;
  --agent-system: #cbd5e1;

  --status-green-bg: rgba(34, 197, 94, 0.16);
  --status-green-fg: #4ade80;
  --status-green-bd: rgba(74, 222, 128, 0.45);

  --status-cyan-bg: rgba(56, 189, 248, 0.16);
  --status-cyan-fg: #38bdf8;
  --status-cyan-bd: rgba(56, 189, 248, 0.45);

  --status-amber-bg: rgba(245, 158, 11, 0.16);
  --status-amber-fg: #fbbf24;
  --status-amber-bd: rgba(251, 191, 36, 0.45);

  --status-red-bg: rgba(239, 68, 68, 0.16);
  --status-red-fg: #f87171;
  --status-red-bd: rgba(248, 113, 113, 0.45);

  --status-neutral-bg: rgba(148, 163, 184, 0.14);
  --status-neutral-fg: #cbd5e1;
  --status-neutral-bd: rgba(148, 163, 184, 0.35);

  --font-sans: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  --font-mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
}

*,
*::before,
*::after {
  box-sizing: border-box;
}

[hidden] {
  display: none !important;
}

body {
  margin: 0;
  min-height: 100vh;
  background-color: var(--bg-root);
  color: var(--text-primary);
  font-family: var(--font-sans);
  font-size: 14px;
  line-height: 1.45;
}

.topbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 14px 22px;
  background: var(--bg-surface);
  border-bottom: 1px solid var(--border-subtle);
}

.brand-group {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
}

.replay-pill {
  display: inline-flex;
  align-items: center;
  padding: 5px 11px;
  border-radius: 999px;
  font-family: var(--font-mono);
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.06em;
  background: var(--status-amber-bg);
  color: var(--status-amber-fg);
  border: 1px solid var(--status-amber-bd);
  white-space: nowrap;
}

.brand-title {
  margin: 0;
  font-size: 17px;
  font-weight: 700;
  letter-spacing: -0.01em;
}

.brand-subtitle {
  margin: 2px 0 0;
  font-size: 12px;
  color: var(--text-secondary);
}

.selector-group {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.gateway-link {
  display: inline-flex;
  align-items: center;
  padding: 5px 11px;
  border-radius: 6px;
  font-family: var(--font-mono);
  font-size: 12px;
  font-weight: 600;
  text-decoration: none;
  background: var(--status-cyan-bg);
  color: var(--status-cyan-fg);
  border: 1px solid var(--status-cyan-bd);
  white-space: nowrap;
}

.gateway-link:hover {
  background: rgba(56, 189, 248, 0.26);
  color: #e0f2fe;
}

.field-label {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
}

.select-input {
  background: var(--bg-elevated);
  color: var(--text-primary);
  border: 1px solid var(--border-strong);
  border-radius: 6px;
  padding: 6px 12px;
  font-size: 13px;
  font-family: var(--font-mono);
}

.error-banner {
  margin: 12px 22px 0;
  padding: 10px 14px;
  border-radius: 8px;
  background: var(--status-red-bg);
  color: var(--status-red-fg);
  border: 1px solid var(--status-red-bd);
  font-family: var(--font-mono);
  font-size: 13px;
}

.workspace {
  display: grid;
  grid-template-columns: minmax(0, 1.45fr) minmax(360px, 1fr);
  gap: 18px;
  padding: 18px 22px 28px;
  max-width: 1760px;
  margin: 0 auto;
}

@media (max-width: 1120px) {
  .workspace {
    grid-template-columns: 1fr;
  }
}

.stage-column,
.inspector-column {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-width: 0;
}

.card {
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: 10px;
  padding: 14px 16px;
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.header-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.section-tag {
  font-family: var(--font-mono);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.08em;
  color: var(--text-secondary);
}

.mono-meta {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-secondary);
}

.view-mode-controls {
  display: flex;
  gap: 6px;
}

.btn {
  background: var(--bg-elevated);
  color: var(--text-primary);
  border: 1px solid var(--border-strong);
  border-radius: 6px;
  padding: 6px 12px;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
}

.btn:hover {
  background: var(--bg-hover);
}

.btn-small {
  padding: 4px 10px;
  font-size: 11px;
}

.btn-primary,
.btn.is-active {
  background: rgba(56, 189, 248, 0.2);
  border-color: var(--agent-camera);
  color: #e0f2fe;
}

.frame-canvas-wrap,
.video-container {
  position: relative;
  width: 100%;
  aspect-ratio: 16 / 9;
  background: #05070b;
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  overflow: hidden;
}

.viewport-img,
.viewport-video {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: contain;
}

.viewport-canvas {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
}

.video-fallback-note {
  margin: 8px 0 0;
  font-size: 12px;
  color: var(--text-muted);
}

.frame-caption {
  margin: 10px 0 12px;
  padding: 8px 10px;
  background: var(--bg-elevated);
  border-radius: 6px;
  font-size: 12.5px;
  color: var(--text-primary);
  border-left: 3px solid var(--agent-camera);
}

.scrubber-bar {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
}

.scrubber-buttons {
  display: flex;
  gap: 6px;
}

.scrubber-slider-wrap {
  flex: 1 1 240px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.range-slider {
  width: 100%;
  accent-color: var(--agent-camera);
  cursor: pointer;
}

.scrubber-meta {
  display: flex;
  justify-content: space-between;
}

.agent-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 10px;
}

.agent-card {
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  padding: 10px 12px;
  border-top-width: 3px;
}

.agent-card.is-head {
  background: var(--bg-hover);
  box-shadow: 0 0 0 1px var(--border-strong);
}

.agent-badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 2px 8px;
  border-radius: 999px;
  font-family: var(--font-mono);
  font-size: 11.5px;
  font-weight: 700;
  border: 1px solid transparent;
}

.agent-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
}

.agent-modeler {
  border-top-color: var(--agent-modeler);
}
.badge-modeler {
  color: var(--agent-modeler);
  background: rgba(255, 107, 107, 0.14);
  border-color: rgba(255, 107, 107, 0.4);
}
.dot-modeler {
  background: var(--agent-modeler);
}

.agent-props {
  border-top-color: var(--agent-props);
}
.badge-props {
  color: var(--agent-props);
  background: rgba(78, 205, 196, 0.14);
  border-color: rgba(78, 205, 196, 0.4);
}
.dot-props {
  background: var(--agent-props);
}

.agent-look {
  border-top-color: var(--agent-look);
}
.badge-look {
  color: var(--agent-look);
  background: rgba(255, 230, 109, 0.14);
  border-color: rgba(255, 230, 109, 0.4);
}
.dot-look {
  background: var(--agent-look);
}

.agent-camera {
  border-top-color: var(--agent-camera);
}
.badge-camera {
  color: var(--agent-camera);
  background: rgba(56, 189, 248, 0.14);
  border-color: rgba(56, 189, 248, 0.4);
}
.dot-camera {
  background: var(--agent-camera);
}

.badge-system {
  color: var(--agent-system);
  background: var(--status-neutral-bg);
  border-color: var(--status-neutral-bd);
}
.dot-system {
  background: var(--agent-system);
}

.agent-meta-line {
  margin: 6px 0 0;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-secondary);
  word-break: break-word;
}

.verdict-pill {
  display: inline-flex;
  align-items: center;
  padding: 2px 8px;
  border-radius: 5px;
  font-family: var(--font-mono);
  font-size: 11px;
  font-weight: 700;
  border: 1px solid transparent;
  white-space: nowrap;
}

.verdict-green {
  background: var(--status-green-bg);
  color: var(--status-green-fg);
  border-color: var(--status-green-bd);
}

.verdict-cyan {
  background: var(--status-cyan-bg);
  color: var(--status-cyan-fg);
  border-color: var(--status-cyan-bd);
}

.verdict-spill {
  background: rgba(245, 158, 11, 0.22);
  color: #fcd34d;
  border-color: rgba(251, 191, 36, 0.65);
}

.verdict-amber {
  background: var(--status-amber-bg);
  color: var(--status-amber-fg);
  border-color: var(--status-amber-bd);
}

.verdict-red {
  background: var(--status-red-bg);
  color: var(--status-red-fg);
  border-color: var(--status-red-bd);
}

.verdict-observed {
  background: var(--status-neutral-bg);
  color: var(--status-neutral-fg);
  border-color: var(--status-neutral-bd);
}

.detail-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
  margin: 0;
}

.detail-item {
  background: var(--bg-elevated);
  border-radius: 6px;
  padding: 8px 10px;
  min-width: 0;
}

.detail-wide {
  grid-column: 1 / -1;
}

.detail-item dt {
  font-size: 11px;
  color: var(--text-muted);
  margin-bottom: 3px;
}

.detail-item dd {
  margin: 0;
  font-size: 12.5px;
  color: var(--text-primary);
  word-break: break-word;
}

.mono-block {
  font-family: var(--font-mono);
  font-size: 11.5px;
  white-space: pre-wrap;
  max-height: 110px;
  overflow-y: auto;
}

.toggle-label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-secondary);
  cursor: pointer;
}

.verdict-summary-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 10px;
}

.turn-list,
.gateway-step-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 330px;
  overflow-y: auto;
  padding-right: 4px;
}

.turn-row,
.step-row {
  display: flex;
  flex-direction: column;
  gap: 4px;
  text-align: left;
  width: 100%;
  background: var(--bg-elevated);
  color: var(--text-primary);
  border: 1px solid var(--border-subtle);
  border-radius: 7px;
  padding: 8px 10px;
  cursor: pointer;
  font-family: inherit;
}

.turn-row:hover,
.step-row:hover {
  background: var(--bg-hover);
}

.turn-row.is-selected {
  border-color: var(--agent-camera);
  background: rgba(56, 189, 248, 0.1);
}

.turn-row-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
}

.turn-row-desc {
  font-size: 12px;
  color: var(--text-secondary);
  word-break: break-word;
}

.attestation-badge {
  padding: 2px 8px;
  border-radius: 999px;
  font-family: var(--font-mono);
  font-size: 11px;
  font-weight: 700;
  background: var(--status-green-bg);
  color: var(--status-green-fg);
  border: 1px solid var(--status-green-bd);
}

.receipt-list {
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.receipt-row {
  background: var(--bg-elevated);
  border-radius: 6px;
  padding: 7px 10px;
}

.receipt-row dt {
  font-size: 11px;
  color: var(--text-muted);
  margin-bottom: 2px;
}

.receipt-row dd {
  margin: 0;
}

.mono-hash {
  font-family: var(--font-mono);
  font-size: 11.5px;
  color: #e2e8f0;
  word-break: break-all;
}

.limitations-box {
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px solid var(--border-subtle);
}

.sub-heading {
  margin: 0 0 6px;
  font-size: 12px;
  font-family: var(--font-mono);
  color: var(--text-secondary);
}

.task-summary {
  margin: 0 0 8px;
  font-size: 12px;
  color: var(--text-secondary);
}

.limitations-list {
  margin: 0;
  padding-left: 18px;
  color: var(--text-secondary);
  font-size: 12px;
}

.limitations-list li {
  margin-bottom: 4px;
}
```

## 6. Hard constraints (tests + CSP)
- CSP: script-src 'self'; style-src 'self'. NO inline <script>, NO style= attributes anywhere (a test asserts "style=" not in index.html), NO innerHTML/outerHTML/insertAdjacentHTML/document.write/eval( strings anywhere in index.html.
- Keep every existing element and id unchanged; keep this exact line verbatim: <a id="gateway-link" class="gateway-link" hidden rel="noopener noreferrer" target="_blank">Live Gemini plan (separate service)</a>
- Static HTML + CSS only. No JS changes. No external fonts/images/CDNs. English.
- Honesty: every claim must be supported by sections 1-3. No numbers that are not in sections 1-3. Gemini-written code (Track H) — say so plainly; do not claim live Gemini planning here.

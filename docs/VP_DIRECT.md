# ViewPoint vp-direct — control plane

`vp-direct` is the single control surface that exposes ViewPoint to a browser agent. One server (`viewpoint-agent`), one capability registry, three equivalent lanes — MCP, CLI (`mvctl`) and JSON mailbox — and the wire contract is the frozen `vp.control.req/v1` / `vp.control.reply/v1` envelope. Everything below is read from the committed registry and frozen schemas; the strategy (STRATEGY_FABLE3) wins where documents diverge.

Registry digest for this edition: `0d22e8f21b317e6b83e762694702f4c4265dd7da064ffa4284704d3daf69cd21` [CITED: `components/vp-direct/expose/mcp/registration_manifest.json`, `components/vp-direct/expose/cli/registration_manifest.json`]. Rebuild checks use `--catalog-only --check` on both generators; a changed digest means the registry moved and the docs are stale.

This guide contains no home paths and no credentials. Session files, sockets and evidence paths are shown with placeholders (`<repo>/`, `<session.json>`, `<mailbox>/`).

## 1. Architecture

- One server: `viewpoint-agent` (version `0.2.0`) serving stdio MCP to Claude Desktop, Codex and Antigravity 2. The executable is `/usr/bin/python3 -I <repo>/hackathon/mcp/vp_mcp/vp_mcp.py` with `VP_MCP_SESSION` set to an absolute session file path. Startup refuses on missing, relative, oversized or invalid JSON session input. The server speaks JSON-RPC 2.0 (`initialize`, `tools/list`, `tools/call`, `ping`) with protocol version `2024-11-05` and an inbound frame limit `MAX_LINE_BYTES = 262144`; frames over the limit are rejected with `-32700` and never dispatched [CITED: `components/vp-direct/expose/mcp/__init__.py`, `hackathon/mcp/vp_mcp/vp_mcp.py:13,39,1848`].
- One registry: the `vp.capability/v1` catalog built by `control/core/control.py:CapabilityRegistry`. It is the single source for `tools/list`, the `mvctl` verbs, the JSON-lane ops and the `vp_tool_route` index. At this edition the registry is the frozen adapter catalog alone (14 tools, 105 selectors). Every selector (`args.op` value) across the 14 tools is `supported:false` with `binding.kind:"none"` (0 supported); execution returns `HOLD` until a carbon-verified binding row in `schemas/adapter_ops.json` qualifies it. Selector validation is never admission [CITED: `components/vp-direct/schemas/adapter_ops.json`, `components/vp-direct/router/index-rebuild.json`].
- Three lanes, one digest. Every request is normalized by `control/core/control.py` to `vp.control.req/v1`; the reply carries `effective_request_sha256` over the canonical normalized bytes. The same normalized request yields the same digest through MCP, CLI and mailbox; no lane has its own defaults. `context.caller` and `context.agent_instance` are stamped from provisioned lane identity, never taken from client input. Dispatcher order is closed schema → `operator_only` → `request_id` store (`REQUEST_ID_CONFLICT` on mutated reuse) → pace/risk gate (S8) → lease/epoch → backend; unknown ops are refused and routing recommends while the backend admits.
- Frozen files. `agents/metaviewer_agents/mcp_server.py` and `contracts/v0.2/mv_tool_call.schema.json` keep their pinned sha256 and are never edited by exposing lanes [CITED: `expose/mcp/tests/test_mcp.py`].

### Envelope

```json
{"schema":"vp.control.req/v1","request_id":"<id>","op":"<MCP tool name>",
 "context":{"caller":"agent|operator","agent_instance":"<id>","session_id":"<id>",
            "capsule_id":"<id>","incarnation":"<id>","authority_epoch":0,"observation_id":"<id>"},
 "args":{},"dry_run":false}
```

The reply (`vp.control.reply/v1`) carries `request_id`, `op`, `ok`, `code`, `result` (empty when `ok` is false), `effective_request_sha256`, `evidence[]` and `not_verified[]`. MCP wire stays flat: per-tool `inputSchema` merges context fields and `request_id` / `dry_run` into `properties`, excludes `caller`, `agent_instance`, `request` and `context`, and omits `$id`/`$schema`/`$defs`/`$ref` for portability. JSON-RPC errors `-32601` (unknown method) and `-32602` (invalid params) are preserved; oversize frames are `-32700` [CITED: `expose/mcp/__init__.py`, `expose/mcp/tests/test_mcp.py`]. `--dry-run` is accepted on every op; `--json-arg` always names a file, never inline JSON.

Idempotency is durable: `RequestStore` (SQLite) claims `(request_id, digest)`. A replay with the same digest returns `UNCERTAIN_OUTCOME` if the reply is absent and the stored reply if present; a mutated digest returns `REQUEST_ID_CONFLICT`. Once the backend is entered, a failed or invalid result is `UNCERTAIN_OUTCOME` and never authorizes a resend.

### Risk and admission

Risk classes sort as `observar < leer < preparar < enviar < publicar < destructivo < irreversible` [CITED: `vp.capability/v1` schema]. Operator-only ops refuse agent callers (`OWNER`, `FORGE_APPROVAL_REQUIRED` for `vp_forge_admit`). `args.op` selectors that lack a frozen `tools.<selector>.args/v1` / `.result/v1` pair refuse with `ARGS_SCHEMA_ABSENT`. Refusals from Core (`OWNER`, `LAYOUT_INPUT_MODE_READ`) pass through verbatim. Pin, layout save/load and storyboard remain `supported:false` and are never dispatched. Pacing and `risk_class >= enviar` gates live in S8; lease and `authority_epoch` live in the broker (U3a).

## 2. Tools

The table projects the accepted digest. All tools are closed adapters; `args.op` is the JSON op and CLI verb mapping is `tool minus vp_ and _call, _→-`. `ops` is the projection of `op_table[].op`.

| MCP tool | CLI | Ops | Operator-only ops |
|---|---|---|---|
| `vp_capsule_call` | `capsule` | `add, stop, pause, resume, role_switch, pin, layout_save, layout_load` | `role_switch` |
| `vp_capture_call` | `capture` | `screenshot, frame_subscribe, frame_unsubscribe, ram_export, ram_list, ram_select` | — |
| `vp_data_call` | `data` | `resolve, put, lease_acquire, lease_release, status` | — |
| `vp_factory_call` | `factory` | `af.generate, af.matte, af.repair, af.lod, af.uv_bake, af.rig, af.motion_from_video, af.assemble, af.render, af.registry.resolve, af.registry.put, vqa3d.score, vqa3d.gate, af.publish, af.lipsync` | `af.publish` (parked) |
| `vp_guest_call` | `guest` | `pack_list, pack_validate, pack_import, desktop_create, desktop_stop, viewer_attach, viewer_detach, snapshot_mark, status` | `pack_import` |
| `vp_guest_command_call` | `guest-command` | `create, run, cancel, stop, output` | — |
| `vp_handoff_call` | `handoff` | `list, ack, network_open, network_allowlist` | `network_open, network_allowlist` |
| `vp_headless_call` | `headless` | `create, run, stop` | — |
| `vp_identity_call` | `identity` | `login, release` | — |
| `vp_perception_call` | `perception` | `sleep, wake, off, status` | — |
| `vp_saved_data_call` | `saved-data` | `save, overwrite, load, list, copy, delete` | `delete` |
| `vp_settings_call` | `settings` | `get, set, storage_get, storage_set` | `storage_get, storage_set` |
| `vp_terminal_call` | `terminal` | `screen_list, screen_open, screen_close, profile_list, profile_select, copy, paste, cmd` | `cmd` |
| `vp_workspace_call` | `workspace` | `window_list, app_list, attach, detach, program_open, layout_get, layout_fold, layout_hide, layout_move, layout_tab, layout_float, multislot_get, multislot_set, view_list, view_save, view_select, rename, preset_list, preset_apply, preset_save, demo_start, demo_stop, demo_state, demo_check_site, storyboard_list, storyboard_open` | — (`layout_*` except `layout_get` and `multislot_set` are agent read-refused via `LAYOUT_INPUT_MODE_READ`) |

Every one of the 105 selectors across the 14 tools is currently `supported:false` / `HOLD` (0 supported). The flags come from `schemas/adapter_ops.json` and are qualified only by a verified binding row; the manifest is authoritative when advanced bindings land. The factory manifest (when it arrives) is loaded with `origin:"factory"` and the loader rejects any op not in it. `af.publish` (`enviar`, parked, `supported:false`) and `af.lipsync` (`supported:false`, tool `AUSENTE`) stay parked; `storyboard_*`, `pin` and `layout_save`/`layout_load` stay `supported:false` and never dispatched. Per-op payloads are `<tool>.<op>.args/v1` and `.result/v1`; an op without a frozen pair refuses with `ARGS_SCHEMA_ABSENT`. Risk mapping follows Fable 3 §1.4: `resolve`/`score`/`gate` → `leer`/`observar`; `generate`/`bake`/`render`/`rig`/`put` → `preparar`; `publish` → `enviar`.

### Frozen sibling schemas (reference)

`vp.session/v1` (frozen enum `LAUNCHING|LOGIN_PENDING|AGENT|HUMAN|CLOSED`, no `SLEEPING`), `vp.perception_state/v1`, `vp.demand/v1`, `vp.surface_binding/v1`, `vp.admission/v1`, `vp.focus_trace/v1`, `vp.image_pack/v1`, `vp.guest/v1`, `vp.snapshot_receipt/v1`, `vp.transition_receipt/v1`, `vp.artifact_ref/v1`, `vp.model_lease/v1`, `vp.seat/v1`, `vp.forge_tool/v1`, `vp.forge_receipt/v1` and `vp.tool_route.index/v1` live under `components/vp-direct/schemas/`. W-B0′ expanded `op_table` and re-froze host adapter naming; a name without a binding is refused.

## 3. Lane recipes

### 3.1 MCP (host attachment)

The service owner provisions the `CapabilityRegistry` and `LaneIdentity`; the server only projects it. The manifest is the projection of `expose/mcp/tools.py:registration_manifest`; `tools/list` equals `registry.entries()` in registry order and every `inputSchema` is a closed object. Invoke the server directly (never by bare tool name) as:

```bash
VP_MCP_SESSION=<session.json> /usr/bin/python3 -I <repo>/hackathon/mcp/vp_mcp/vp_mcp.py
```

For a dry-run smoke over stdio (never a live dispatch), send `initialize` then `tools/list` then a `tools/call` with `"dry_run": true` and compare `effective_request_sha256` with the CLI and mailbox lanes for the same request. Host config examples are in `docs/vp-direct/hosts/`:

- [Claude Desktop](vp-direct/hosts/claude-desktop.md)
- [Codex](vp-direct/hosts/codex.md)
- [Antigravity 2](vp-direct/hosts/antigravity2.md)

Each recipe sets `VP_MCP_SESSION` to an absolute path, launches with `python3 -I`, and verifies the same request digest through the three lanes with `--dry-run`. No recipe opens browser profiles, cookies, tokens or auth stores, and no recipe changes the OS default browser.

### 3.2 CLI (`mvctl`)

`mvctl` projects the registry through `expose/cli/verbs.py:registration_manifest`. Every verb requires the seven transport flags, `--json-arg <file>` (a path; inline JSON is refused with `input_error`) and optionally `--dry-run`:

```bash
mvctl <verb> --socket <core.sock> --session-id <session> \
  --agent-instance <agent> --core-instance <core> \
  --timeout-ms 500 --max-line-bytes 2048 --max-reply-bytes 4096 \
  --json-arg <request.json> --dry-run
```

`<request.json>` is a `vp.control.req/v1` file with `caller`/`agent_instance` matching the transport binding; identity mismatch fails before dispatch. Online CLI exits `0` on success, `2` on invalid input and `3` on dispatch errors; offline exits `0/2/3` by the same codes. The flag set is frozen (`--socket`, `--session-id`, `--agent-instance`, `--core-instance`, `--timeout-ms`, `--max-line-bytes`, `--max-reply-bytes`, `--json-arg`, `--dry-run`) and required on every call [CITED: `expose/cli/verbs.py:TRANSPORT_FLAGS`, `expose/cli/tests/test_cli.py`]. Generate or check the CLI manifest with:

```bash
python3 -I <repo>/components/vp-direct/expose/cli/generate.py --catalog-only --check
```

### 3.3 JSON lane (OpenFresnel companion, `oflane/`)

File transport only, no sockets. One mailbox per lane identity, published atomically with `fsync` of file and directory, never by overwriting:

- `<mailbox>/<request_id>.req.json` — a `vp.control.req/v1`, carried 1:1
- `<mailbox>/<request_id>.reply.json` — a `vp.oflane.reply/v1`
- `<mailbox>/<request_id>.abort` — abort cancels the order and never kills a process
- `<!-- <request_id>_DONE -->` — terminal sentinel, written only by an independent verifier and never by the executor; self-written completion and `accepted`-as-done are refused

Rules: `request_id` digest idempotency (same digest → replay, mutated → `REQUEST_ID_CONFLICT`), resource locks keyed by `session_id` or `capsule_id`, `--dry-run` on every op, human gate at `risk_class >= enviar`, literal arguments with metacharacters refused (`METACHARACTERS_REFUSED`), and `accepted` never read as done [CITED: `components/vp-direct/oflane/__init__.py`, STRATEGY_FABLE3 §3.2 C4]. The worker and verifier share an attestation key that is never loaded from disk in the worker.

Dry-run through the mailbox (offline, synthetic mailbox):

```bash
python3 -I -B - <<'PY'
from pathlib import Path
import sys
sys.path.insert(0, "<repo>/components/vp-direct")
from control.core.control import LaneIdentity
from oflane import Mailbox
# `mb` is a provisioned mailbox; the request file is a frozen vp.control.req/v1
# mb.submit(request); mb.run_one(request_id, dry_run=True)
PY
```

## 4. Question router (`vp_tool_route`)

Read-only, no rebuild and no write. The same query and `index_sha256` give the same bytes. The reply (`vp.tool_route.reply/v1`) carries `verdict` `MATCH|ABSTAIN`, `code` `ROUTE_ABSTAIN`, `reason` `policy_denied|ambiguous|missing_arguments|stale_index|absent_capability`, `index_sha256`, `candidates[]` with `tool`, `mcp_call`, `cli`, `json`, `missing_args`, `margin`, `citations[]`, and optional `detail` (`model_unqualified|model_excluded|op_unbound`). `MATCH` requires `top1 >= score_min`, `margin >= margin_min`, `missing_args == []`, policy pass and `registry_sha256` equal to the live registry, otherwise `stale_index`. Only `absent_capability` may point to the forge; `missing_arguments` and `ambiguous` never do. The router recommends and never dispatches.

Calibration is recorded as **open**. The margin value stays `AUSENTE` until a held-out independent judgment set exists; `fixtures/golden` is not held-out. The index is published only in `calibrated` state; there is no `uncalibrated` escape. If no safe margin exists the build fails and no index is published [CITED: STRATEGY_FABLE3 §2.3, `components/vp-direct/router/router.py:256,322`]. Local models are routable only with `qualification.state=="runnable"`, a receipt and `territory_ok`; only `MEASURED` facts rank; `Hunyuan3D-2.1` is `excluded` and never routable (router abstains with `policy_denied` / `detail:model_excluded`, never `absent_capability`).

Dry-run a route:

```bash
python3 -I <repo>/components/vp-direct/router/router.py --help  # (inspect module surface; query via vp_tool_route)
```

## 5. Sessions, perception and limits

- Session binding is `{pid, xid, witness, cdp_target_id}` tied to instance, tab and capsule. One profile, one instance, one lease; a second open returns `PROFILE_IN_USE`. Default profile → `DEFAULT_PROFILE_REFUSED`; snap Chromium → `SNAP_CDP_UNAVAILABLE`; Firefox CDP → `BIDI_DEFERRED`. Agent `vp_session/v1.state` stays frozen; sleep/wake lives in a separate `vp.perception_state/v1` (`ACTIVE|ASLEEP|WAKING|OFF|QUARANTINED`). Agent `OFF` keeps the application and session open and resident [CITED: STRATEGY_FABLE3 §1.3, `vp.session_v1.schema.json:94`].
- Demand coordinator: producers stop only when the last compatible obligation ends; recording, display, delivery and outstanding borrows are retained. Work is published before notification and rechecked before sleep; notifications confer no authority. `MaxSurfaces=16` and `MAX_CAPSULES=16` are never raised to pass T1 [CITED: `core/src/record/mod.rs:129`, `subscriptions.go:80`, STRATEGY_FABLE3 §1.3].
- W-L0 backend table (receipt `vp.l0_receipt/v1`, 8/8 PASS with qualifications): Chrome 157 DEB → CDP (AT-SPI 269 nodes); Antigravity 2 (Electron 44.3, Chrome 152) → CDP only (AT-SPI shallow 114 nodes); Codex Desktop → CDP primary, AT-SPI usable. CDP targets are created only with `Target.createTarget {background:true}`; never `PUT /json/new` on `:0`. AT-SPI needs a persistent D-Bus connection; only the hidden Chrome tab `DoAction(0)` is qualified. The OAuth `BROWSER` callback is `NOT RUN` and stays open for S7 [CITED: `components/vp-direct/config/wl0.json`].
- Hard limits bind every lane: no camouflage and no AI captcha solving; never read cookies, profiles, tokens, `~/.codex` or `auth.json`; pace and `risk_class >= enviar` gates on every lane.

## 6. Verifying this edition

Run the local suite in a capped scope (no live hosts, no network beyond loopback):

```bash
systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -- sh <repo>/docs/vp-direct/tests/safe_run.sh
```

The suite checks that the registry digest matches the accepted value, that every documented tool/verb/ops entry appears in `tools/list` and the CLI manifest, that every documented command carries `--dry-run`, that no home path or credential is present, and that stale digests / unknown tools / missing `--dry-run` / undocumented divergence each fail. LIVE scripts under `tests/` (`LIVE_*.py`) are marked and are not executed by the suite; they inspect the published index and live registry when the operator supplies those inputs.

## 7. Open decisions

1. Commit W-B0 and authorize W-B0′ re-freeze. 2. Adapter op names stay proposals until a verified binding row lands. 3. T1–T3 as initial contracts; limits stay until T1 passes. 4. State retained while asleep stays minimal authorized context, with `OFF` meaning exclusive resources unloaded and the app left open. 5. Pool sharing stays same-trust-domain only. 6. `vm_viewer` stays operator-gated until admitted. 7. H4/H5 stay separate gated commits; H1 keeps its approved scope. 8. Graphical MicroVM backend and GPU stay deferred until G2 passes without GPU. 9. Snapshot coverage stays enumerated per pack. 10. Factory: one batch equals one attempt, `af.publish` stays parked. 11. Factory `preparar` risk mapping stays. 12. Seat file location and Sol 6.2 / Astra 6.2 ids stay `AUSENTE`. 13. F1–F6 operator parts stay as recommended in Fable 3 §4. 14. SF2 decisions 1–10 stay open.

Where a schema field is missing, see `components/vp-direct/schemas/schema_gaps.json`; report `SCHEMA_GAP` in the lane receipt rather than inventing a field. See also `STRATEGY_FABLE3.md` (plan of record) and `STRATEGY_FABLE2.md` §1–§2 for the envelope, registry, session, window, frame and router contracts.

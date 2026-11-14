# ViewPoint

ViewPoint lets an AI run a browser capsule the operator owns, watches, and can take over — and no agent action counts until the core has admitted it and verified it against a fresh frame.

## Problem

AI agents that drive a browser are hard to trust. They act without a named target, without visible ownership, and without a checkable outcome. When a human cannot see what the agent saw, who owns the keyboard, and whether the action actually worked, the agent is a liability, not a tool.

ViewPoint starts from one rule: **explicit admission between an agent's proposed input and its native executor.**

## What ViewPoint does

- Each agent gets an **isolated browser capsule** on a **private display**. The operator keeps the desktop keyboard.
- The operator **watches through a viewer** and can **take over any capsule** in human mode, type, and hand it back.
- Every agent action runs an **observe -> propose -> admit -> execute -> verify** loop:
  1. **Observe** the surface and capture a frame.
  2. **Propose** an action bound to that frame (`sha256` + `generation`).
  3. **Admit** — the core alone decides admission.
  4. **Execute** natively through the adapter.
  5. **Verify** against a fresh frame; the receipt is required before the next step.
- **Failures are typed, never silently counted as success**: `INPUT_UNCERTAIN`, `STALE_FRAME`, `VERIFICATION_FAILED`. Navigation stops on any refusal — there is no blind replay.

Demo identities (operator-chosen names): `arxiv`, `chatgpt`, `gemini`, `wikipedia`. Logins are manual by the operator; nothing reads credentials. Gemini login status is AUSENTE — if Google refuses the browser, there is no bypass.

## Where Gemini fits

Gemini is the **planner in the Cloud Run gateway** — it turns a recorded task into a short plan with a stated rationale, shown next to the recorded actions. This is **[TARGET] until a deploy receipt exists**: no real Gemini call receipt yet, the key is not configured, and the model is not configured. Today's verified takes use the bounded browser runner instead. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## What is in this repo vs private

In this repo:

| Path | What it is |
|---|---|
| `gateway/` | Cloud Run gateway: replay page + server-side Gemini planner, tests |
| `mcp/vp_mcp/` | viewpoint-agent MCP stdio server: navigate, bounded browser tasks, read page, identity tools |
| `launcher/` | Host-specific take scripts (`vp_demo_take.sh` and helpers) |
| `docs/IDEA.md`, `docs/TEAM.md` | One-pagers |

Private, deliberately **not** in this repo: the **vision engine**, the **wender-agent** planner, the **OpenSuperficies capsule binaries**, and the **ViewPoint app fork itself**. The public parts above call into them, but they are not here — that is why the launcher and MCP server need a prepared host (see [launcher/README.md](launcher/README.md)).

## Relationship to MetaViewer

ViewPoint is the hackathon build of [MetaViewer](https://github.com/oguirado/MetaViewer) (AGPL-3.0-only), the open base. The UI still shows MetaViewer identifiers. ViewPoint adds the hackathon demo layer on top: the gateway, the MCP server, and the take scripts, backed by private engines listed above.

## Quick start

### Gateway (local run + tests)

```bash
cd gateway
python3 -m unittest discover -s tests -v
python3 app.py --host 127.0.0.1 --port 8080
```

Open `http://127.0.0.1:8080/`. Without `GEMINI_API_KEY` the replay still works and the plan panel shows `GEMINI_KEY_MISSING`. The page never calls `/api/plan` on load — Gemini is called only when a visitor clicks "Generate plan" (no retries). Full deploy notes: [gateway/README.md](gateway/README.md).

### MCP server

The server is `mcp/vp_mcp/vp_mcp.py` (stdlib only, no pip deps). It needs one session file, given by env `VP_MCP_SESSION` (absolute path, validated against `mcp/vp_mcp/session.schema.json`):

```bash
VP_MCP_SESSION=/abs/path/to/session.json python3 -I mcp/vp_mcp/vp_mcp.py
```

Tools: `vp_list_capsules`, `vp_navigate`, `vp_browser_task`, `vp_read_page`, `vp_identity_list`, `vp_identity_create`, `vp_identity_set_label`, `vp_identity_delete`, `vp_show`, `vp_hide`.

Behaviour: `vp_navigate` is `https://`-only; URLs with a non-empty query or fragment are refused before any input because the capsule reports URLs without query by privacy design. `vp_browser_task` runs bounded `mw_run` tasks and is serialised (`agent busy` instead of running concurrently). Identity tools refuse active capsules and wrong confirms. Tests:

```bash
cd mcp/vp_mcp && python3 -B -m unittest discover -s tests -v
```

## Measured results

Only the numbers below are claimed (all [MEASURED] on Fri 13 Nov 2026 evening). See [docs/RESULTS.md](docs/RESULTS.md) for method and the two degradation cliffs.

| Result | Number |
|---|---|
| Navigations verified, 1 capsule, after fixes (CAND9) | **156 / 158** (2 isolated `STALE_FRAME`) |
| 4-capsule demo flow, 5 rounds: CAND9 verified searches | **12 / 15** (HN excluded) |
| 4-capsule demo flow, 5 rounds: CAND7 verified searches | **11 / 15**, session end at round 5 |
| RC1 integrated: arXiv | **5 / 5** |
| RC1 integrated: Wikipedia | **3 / 3** attempted |
| URL with `?` / `#` | refused in **30–54 ms**, zero input |
| Bulk "Pass all to Humans" | **4 / 4** views ready in **51 s** |
| Fresh take (`start`) | 4 named capsules, 2x2, per-capsule preflight with negative control (unknown identity refused) |
| Restart before each take | **~3 min**, state clean; logins persist |

Measured under host load1 15–27 from other projects. Private and link-local addresses are refused or timed out (`169.254.169.254` timed out). Reddit is a reCAPTCHA, not bypassed.

## Honest limits

- **Cloud Run deploy + real Gemini call [TARGET]** — no receipt yet; the gateway is local-only and the plan panel shows `GEMINI_KEY_MISSING`.
- **Final recording, H07/H08 captures, submission [TARGET]** — not done. The shipped replay is a synthetic SAMPLE, labelled as such.
- **Native desktop window [ON HOLD]** — agents work hidden on a private display; the operator watches through a viewer. `show` is disabled until it passes on a private test display. Do not claim it.
- **Human-to-agent return** — the round-trip input bug is fixed in the current candidate (agents -> humans -> agents verified with 4/4 navigations plus 1 task), but we still restart after manual logins as the cheap, known-clean path; seamless return is not claimed as the default flow.
- **Load sensitivity** — round-5 failures under load1 15–27 were slow verifications from external host load, not a product limit. Mitigation: pause other projects while recording, takes of at most 4 rounds, restart per take.
- **One-command take** needs the private components on a prepared GB10 arm64 host — it does not run from a fresh clone.

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
  - Exported `starforge_sentinel.glb`: `28` meshes (`15` `CHAR_*` + `12` `PROP_*` + `1` factory `Cube`), `3582` vertices, `4000` polygons, `6` materials [MEASURED], plus `3` 1080p Eevee renders (`hero.png`, `turntable.png`, `front.png`) [MEASURED]
  - Reconciliation (`projector.py --finalize`): `CLEAN` (`0` anomalies, `0` uncertain events) [MEASURED]
- **Wave 2 Deterministic Guard Take (`take3_w2_guards`)**:
  - `69` broker events [MEASURED]; exercised `CLAIM_REFUSED` (`tick=0` unchanged) and `STALE_FRAME` (`3` stale drops -> `parked_after_3_stale_drops` -> camera re-observe -> `VERIFIED_WITH_SPILL` with `rebased=false`); `projector.py --finalize` `CLEAN` (`0` anomalies, `0` uncertain events) [MEASURED]
- **Wave 1 2-Agent Live Gate (`take1_w1_2agents`, commit `04a1eb4` [CITED])**:
  - `100` broker events [MEASURED]; `7` verified turns (`5` `STATE_VERIFIED` + `2` `VERIFIED_WITH_SPILL`), `1` `JOB_ERROR` retried, `0` open turns [MEASURED]
  - Exported `starforge_sentinel.glb`: `16` meshes (`15` `CHAR_*` + preserved `Cube`), `1340` vertices, `2368` polygons [MEASURED]; `projector.py --finalize` `CLEAN` (`0` anomalies, `0` uncertain events) [MEASURED]

### Documentation & Verification Links

- [PROVENANCE.md](PROVENANCE.md) — Comprehensive two-track provenance ledger and witness hashes
- [swarm/CONTRACT.md](swarm/CONTRACT.md) — Frozen capsule interface and turn-queue contract
- [swarm/docs/PROOF.md](swarm/docs/PROOF.md) — Cryptographic proof index of all Track H, Track P, and `Sol 6.1` review logs
- [swarm/docs/ONE_PAGER_IDEA.md](swarm/docs/ONE_PAGER_IDEA.md) — Architecture and idea one-pager
- [swarm/docs/ONE_PAGER_TEAM.md](swarm/docs/ONE_PAGER_TEAM.md) — Multi-model factory and team one-pager
- [swarm/viewer/README.md](swarm/viewer/README.md) — Standalone Cloud Run replay viewer (`REPLAY` mode, `3` pre-seeded real takes)
- [swarm/docs/OPERATOR_CHECKLIST.md](swarm/docs/OPERATOR_CHECKLIST.md) — Operator release, two-service Cloud Run deploy, and submission runbook

## License

ViewPoint is licensed under the [PolyForm Strict License 1.0.0](LICENSE): you may read, run and evaluate it for noncommercial purposes; changes and redistribution are not licensed. The base [MetaViewer](https://github.com/oguirado/MetaViewer) keeps its own `AGPL-3.0-only` license. The private engines named above are not part of this repository and are not covered by either license.

## Licence update (14 Nov 2026)

From release v0.3.0-hackathon this repository is licensed under the GNU Affero General Public License v3.0 (AGPL-3.0-only); see LICENSE. Earlier commits were published under PolyForm Strict 1.0.0.

## Public redaction note

Proof logs (swarm/proof, product/proof) are published with personal data redacted (emails as `<redacted-email>`, home paths as `~/`). The unredacted originals and their sha256 values recorded in PROVENANCE.md are kept privately by the operator and can be shown to judges on request.

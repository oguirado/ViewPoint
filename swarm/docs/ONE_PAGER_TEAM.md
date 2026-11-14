# TEAM — ViewPoint Swarm: Multi-Model Software Factory (`ONE_PAGER_TEAM.md`)

## Team & Operational Substrate

- **Solo Human Operator**: `oguirado` (operator, architect, and reviewer).
- **Autonomous Multi-Model Software Factory**: Antigravity 2 orchestration substrate operating locally on an NVIDIA GB10 Linux workstation (`aarch64` kernel `7.0.0-1019-nvidia` [MEASURED], `20` CPU cores [MEASURED], `121 GiB` total system RAM [MEASURED]).
- **Containment & Execution Floor**: Agent runs and tests are launched in memory-capped `systemd-run` scopes (`MemoryHigh=6G`, `MemoryMax=8G`, `MemorySwapMax=0`) by rule [CITED: `PROVENANCE.md`].
- **Licence**: ViewPoint: PolyForm Strict 1.0.0 (source-available, not open source). Base MetaViewer v0.2.6: AGPL-3.0-only.

## Strict Two-Track Architecture

To keep model authorship separable and auditable, development is split into two non-overlapping tracks recorded in `PROVENANCE.md`:

### 1. Track H (`swarm/`) — Hackathon-Claimed Gemini Implementations

Every module, specification, and test inside `swarm/` (excluding orchestrator briefs in `swarm/briefs/` and runtime events in `swarm/run/`) was authored strictly after `08:59 CET` [CITED] on Saturday 14 Nov 2026 by autonomous Gemini models invoked via headless `agy --output-format stream-json`.
- **H0 Contract**: `gemini-4-hydrogen-high` authored `swarm/CONTRACT.md` (SHA-256 `b392f7dd...` [CITED]).
- **H1 Turn Broker & Bridge**: `gemini-4-hydrogen-high` authored `swarm/broker/` (`19/19` tests passing [MEASURED]).
- **H2 Agents & Intent Runner**: `gemini-4-hydrogen-high` authored `swarm/agents/` (`19/19` tests passing [MEASURED]).
- **H3 Compositor Overlay**: `gemini-4-carbon-high` (v0) and `gemini-4-hydrogen-high` (`r1`, `r2`) authored `swarm/overlay/` (`15/15` tests passing [MEASURED]).
- **H4 Projection & Reconciler**: `gemini-3.8-flash-high` (v0, `r1`) and `gemini-4-hydrogen-high` (`r2`) authored `swarm/projection/` (`29/29` tests passing [MEASURED]).
- **H5 Replay Viewer**: `gemini-4-carbon-high` (v0, `r1`, `r2`) authored `swarm/viewer/` (`17/17` tests passing [MEASURED], `3` pre-seeded real replays: `take2-w2-4agents`, `take3-w2-guards`, `take1-w1-2agents`).
- **H6 Documentation & Honesty Gates**: `gemini-3.8-flash-high` (v0) and `gemini-4-hydrogen-high` (`r1`, `r2`, `r3`) authored `swarm/docs/` and the honest verification suite (`swarm/docs/test_docs.py`).

Full execution streams and cryptographic SHA-256 receipts for all `25` Track H `.jsonl` generation/run proof logs [MEASURED] are preserved in `swarm/proof/` and indexed in `swarm/docs/PROOF.md`.

### 2. Track P (`product/`) — Declared Non-Gemini Reference & Cross-Family Reviews

All assets under `product/` (excluding post-`13:59 CET` `Sol 6.1` review reports) represent declared product explorations and cross-family reviews authored by Anthropic models (`claude-opus-5-5`, `claude-fable-5-2`) via `claude -p --verbose` prior to `13:59 CET` [CITED], with `19` `.jsonl` proof logs in `product/proof/` (including `product/proof/P4f_1343.jsonl`).
- **Standalone Reference Implementations**:
  - `product/admission/`: Reference admission controller with negative controls (`52/52` tests passing [MEASURED]).
  - `product/fanout/`: Single-producer immutable frame broadcaster and pixel attribution (`33/33` tests passing [MEASURED]).
  - `product/mpx/REPORT.md`: Empirical X11 MPX multi-pointer spike on Blender 4.0.2 (`VERDICT: MPX_ROADMAP_ONLY` [MEASURED]).
- **Cross-Family Adversarial Code Reviews**:
  - `product/reviews/`: In-depth code reviews (`P4a_H1.md`..`P4d_H4.md`, `P4f_H6.md`, and `H1.md`..`H4_r1.md`) identifying corner cases, display leaks, and timeout gaps.
  - **Post-`13:59 CET` Cross-Family `Sol 6.1` Reviews**: `gpt-6.1-sol` via the `swarm` skill (`--sin-herramientas`) authored `product/reviews/Sol_H5_r1.md` (`REJECTED`, `4` `LIVE_PATH_MAJOR` findings fixed in `H5_r2`), `product/reviews/Sol_H5_r2.md` (`APPROVED`, `0` blockers, `0` live-path defects), `product/reviews/Sol_H6_r2.md` (`REJECTED`, `1` `LIVE_PATH_MAJOR` review-status wording defect fixed in `H6_r3`), and `product/reviews/Sol_H6_r3.md` (`APPROVED`, `0` blockers, `0` live-path defects).
  - `product/repair_specs/REPAIR_SPECS.md`: Formal repair specifications that guided Track H Gemini repair runs (`H1_r2` through `H4_r2`).
  - `product/comparison/H_VS_REF_REPORT.md`: Seam comparison confirming the choice of the Track H integrated stack for the live gate.
- **Strict Boundary**: 0 files of Track P code written under `swarm/` [CITED: `PROVENANCE.md`]; one early Track P housekeeping commit (`f667745`) updated `swarm/.gitignore` and removed compiled `.pyc` files. Track P code is never imported into `swarm/` and never claimed as Gemini-generated.

## Verification & Quality Philosophy

Every unit ships with negative controls, and cross-family reviews (Claude Opus/Fable before `13:59 CET`; `Sol 6.1` via `swarm` after `13:59 CET`) hunt for the cases they miss.

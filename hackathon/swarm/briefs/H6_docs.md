# Brief H6 — Two-Track Documentation, One-Pagers, Proof Index & Honesty Tests (`swarm/docs/` & `README.md`)

## Ownership & Strict Boundaries
- **Unit**: `H6` (Track H, model `gemini-3.8-flash-high`)
- **Owned Paths**:
  - `~/Documentos/viewpoint-hackathon/swarm/docs/` (`ONE_PAGER_TEAM.md`, `ONE_PAGER_IDEA.md`, `PROOF.md`, `OPERATOR_CHECKLIST.md`, `test_docs.py`, `__init__.py`)
  - `~/Documentos/viewpoint-hackathon/README.md` (append/add section `## Hackathon build (swarm-1114)` cleanly before `## License`, preserving ALL existing sections and comments verbatim!)
- **Read-Only Reference Paths**:
  - `~/Documentos/viewpoint-hackathon/PROVENANCE.md`
  - `~/Documentos/viewpoint-hackathon/swarm/CONTRACT.md`
  - `~/Documentos/viewpoint-hackathon/docs/IDEA.md` and `docs/TEAM.md`
  - `~/Documentos/viewpoint-hackathon/product/mpx/REPORT.md`, `product/admission/README.md`, `product/fanout/README.md`, `product/comparison/H_VS_REF_REPORT.md`
- **STRICT FORBIDDEN PATHS**:
  - NEVER open, read, or mention `~/Documentos/ViewPoint/hackathon/playbooks/` or `prior_playbook_helpers`.
  - NEVER modify `PROVENANCE.md` (owned exclusively by `director-agent`) or any file under `product/`, `gateway/`, `swarm/broker/`, `swarm/agents/`, `swarm/overlay/`, `swarm/projection/`, or `swarm/viewer/`.
  - NEVER read any credentials, keys, tokens, or files under `~/.gemini`.
  - Run all tests inside `systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0` with `PYTHONDONTWRITEBYTECODE=1`.

## Mandatory Truth & Claim Rules (Strict Anti-Overclaim Contract)
1. **Trust & Process Boundary Wording**:
   - Include this exact honest boundary statement in `swarm/docs/ONE_PAGER_IDEA.md` and `README.md` (`## Hackathon build (swarm-1114)`):
     > "Browser capsules (measured 13 Nov) run as containers with their own display. Today's Blender 4.0.2 swarm capsule runs as host processes inside `systemd-run` memory-capped scopes on a private Xvfb display (`:2`) under one shared UID: per-run bridge nonces and prefix claims catch accidental cross-writes, stale frames, and buggy agents, and are not an OS security boundary against a hostile same-UID process. Per-agent UIDs/containers are roadmap work."
2. **Projection Guarantee**:
   - State clearly: "Projection to OpenSwitchboard (`viewpoint-capsule`) and TaskLight (`context-wall`) is best-effort; `swarm/run/events.jsonl` is the authoritative local record of truth, audited at end-of-run by `reconciler.py` (`--finalize`)."
3. **BANNED PHRASES (Zero Occurrences Allowed in `swarm/docs/*.md` and `README.md` `swarm-1114` section)**:
   - NEVER write any of these phrases (except inside `test_docs.py` as banned-phrase regex patterns!):
     - `secure against agents`
     - `sandboxed agents`
     - `tamper-proof`
     - `authenticated agents`
     - `sealed-bid`
     - `truthful auction`
     - `guaranteed delivery`
     - `exactly-once`
     - `at-least-once`
     - `rewinds everything`
     - `audit ok` (as standalone proof)
     - `"in parallel"` when describing Blender actuation (agents think/prepare concurrently, while turn execution on the shared `.blend` / viewport is **serialized one turn at a time** by the turn queue!).
4. **Committed-Only Citations**:
   - Every repository relative link or file path cited as committed evidence in `swarm/docs/*.md` and the new `README.md` section MUST exist in `git ls-tree -r HEAD` (or be one of the `swarm/docs/*` files created in this unit). Do NOT cite untracked worktree paths such as `product/narrative/*` or `product/cloudrun/*`; cite `PROVENANCE.md`, `swarm/CONTRACT.md`, `swarm/broker/`, `swarm/agents/`, `swarm/overlay/`, `swarm/projection/`, `product/admission/`, `product/fanout/`, `product/mpx/REPORT.md`, `product/reviews/`, `product/comparison/H_VS_REF_REPORT.md`, and `swarm/proof/`.
   - Quantitative values must carry `[MEASURED]` or `[TARGET]` tags.

## Required Deliverables

1. `swarm/docs/ONE_PAGER_IDEA.md` (Plain-text Markdown one-pager):
   - Problem & Idea: Multi-agent collaborative 3D asset building ("StarForge Sentinel") on **one shared Blender 4.0.2 instance** (`vpswarm` on private Xvfb `:2`) coordinated by an explicit non-interactive turn queue (`THINK -> ENQUEUE -> TURN -> RETURN`), prefix write-set claims (`col:CHAR_`, `obj:CHAR_`, `col:PROPS_`, `obj:PROP_`, `mat:`, `light:`, `world:`, `cam:`, `render:`), frame-bound GUI camera orbits (`STALE_FRAME`, `VERIFIED`, `VERIFIED_WITH_SPILL`, `NO_VISIBLE_CHANGE`), off-display composited coloured agent cursors + queue strip, and live OpenSwitchboard + TaskLight cards.
   - How Google Tools & Gemini Models Were Used:
     - **Google Antigravity 2 & `agy` CLI**: Orchestrator (`director-agent` on `gemini-4-carbon-high`) + headless `agy --output-format stream-json` sub-agents writing 100% of claimed Track H code under `swarm/` after `08:59 CET` Sat 14 Nov 2026 (`gemini-4-hydrogen-high` for `H0`, `H1`, `H2`, repairs `H1_r2..H4_r2`, and live Blender agents; `gemini-4-carbon-high` for `H3` v0 and `H5`; `gemini-3.8-flash-high` for `H4` v0/r1 and `H6`).
     - **Google Cloud Run**: Deployment target (`[TARGET]` until operator deploy receipt) for the read-only replay viewer (`gateway/` and `swarm/viewer/`).
   - Two-Track Transparency & Honest Boundaries (including the mandatory shared-UID scope note above and measured Wave 1 live gate `take1_w1_2agents` numbers from `PROVENANCE.md`: `100` broker events [MEASURED], `7` verified turns [MEASURED], `16`-mesh / `1340`-vertex / `2368`-polygon `.glb` [MEASURED], `projector --finalize` `CLEAN` [MEASURED]).

2. `swarm/docs/ONE_PAGER_TEAM.md` (Plain-text Markdown one-pager):
   - Team & Multi-Model Factory Workflow: Solo human operator (`oguirado`) + autonomous multi-agent software factory on an arm64 NVIDIA GB10 Linux workstation (`20` cores, `121 GiB` RAM [MEASURED]).
   - Strict Two-Track Architecture:
     - **Track H (`swarm/`)**: Hackathon-claimed code written exclusively by Gemini models via `agy` with `stream-json` logs and SHA-256 receipts in `swarm/proof/`.
     - **Track P (`product/`)**: Declared non-Gemini product & cross-family adversarial review work (`claude-opus-5-5`, `claude-fable-5-2` via `claude -p --verbose` before `13:59 CET`), producing standalone reference modules (`product/admission/`, `product/fanout/`, `product/mpx/REPORT.md`) and findings-only code reviews (`product/reviews/P4*.md`, `product/repair_specs/REPAIR_SPECS.md`, `product/comparison/H_VS_REF_REPORT.md`) with `0` files written under `swarm/`. Every piece ships with negative controls, and cross-family reviews hunt for edge cases they miss.

3. `swarm/docs/PROOF.md` (Cryptographic Proof Index):
   - Parse and index EVERY proof log listed in `PROVENANCE.md` (`swarm/proof/*.jsonl` for Track H and `product/proof/*.jsonl` for Track P), listing Unit, Track (`H` vs `P`), Model, Proof Log Path, exact 64-hex SHA-256 matching both disk and `PROVENANCE.md`, plus the ` Design Inputs Written by Claude` SHA-256 table from `PROVENANCE.md`.

4. `swarm/docs/OPERATOR_CHECKLIST.md`:
   - Step-by-step operator-only submission & release runbook (explicitly noting the AI agent NEVER pushes, tags remotely, deploys, or publishes):
     1. Inspect `git status`, `PROVENANCE.md`, and C5 secrets preflight report.
     2. Push branch `swarm-1114` and create/push release tag `v0.3.0-hackathon` (keeping `pre-hackathon-1114` local only — never push `pre-hackathon-1114`).
     3. Deploy read-only replay viewer (`swarm/viewer` or `gateway/`) to Google Cloud Run and save deploy receipt (`~/vp-deploy/deploy_receipt.txt`).
     4. Upload final composed `.mp4` (`~/Vídeos/ViewPoint_hackathon/`) to YouTube and submit hackathon form with links to `README.md`, `PROVENANCE.md`, `swarm/docs/ONE_PAGER_IDEA.md`, and `swarm/docs/ONE_PAGER_TEAM.md`.

5. `README.md` Section `## Hackathon build (swarm-1114)`:
   - Insert cleanly right before `## License` in `~/Documentos/viewpoint-hackathon/README.md` (do NOT alter lines 1-100 or `## License`!).
   - Summarize the `swarm-1114` multi-agent single-Blender-capsule turn-queue build, the strict Two-Track Declaration (`swarm/` = Track H Gemini via `agy`; `product/` = Track P declared Claude product/review work, never claimed as Gemini), the honest shared-UID scope boundary, measured unit/live gate results (`H1` `19/19`, `H2` `19/19`, `H3` `15/15`, `H4` `29/29`, `take1_w1_2agents` `100` events / `16` meshes [MEASURED]), and clickable relative links to [PROVENANCE.md](PROVENANCE.md), [swarm/CONTRACT.md](swarm/CONTRACT.md), [swarm/docs/PROOF.md](swarm/docs/PROOF.md), [swarm/docs/ONE_PAGER_IDEA.md](swarm/docs/ONE_PAGER_IDEA.md), and [swarm/docs/ONE_PAGER_TEAM.md](swarm/docs/ONE_PAGER_TEAM.md).

6. `swarm/docs/test_docs.py` (`swarm/docs/__init__.py` included) — **Automated Verification + 4 Mandatory Must-Fail Controls**:
   - Write clean helper validators in `swarm/docs/test_docs.py` (or `swarm/docs/verify_docs.py` imported by `test_docs.py`) and test BOTH that the real repo passes AND that planted mutants fail (must-fail controls!):
     - **D1 (Committed-Only Link Checker)**: Extract all Markdown links `[...](...)` and backticked repo-relative paths (`swarm/...`, `product/...`, `docs/...`, `gateway/...`, `PROVENANCE.md`, `README.md`) across `swarm/docs/*.md` and `README.md`, verifying every target exists in `git ls-tree -r HEAD` union `{swarm/docs/*}` (allowing `swarm/viewer` only if explicitly marked `[TARGET]` or Wave 2 pending if not yet committed). Plant a mutant string linking to `product/narrative/FAKE_UNTRACKED.md` and assert the checker returns FAIL!
     - **D2 (Proof Index SHA-256 Re-Hash Verification)**: Parse every `(path, sha256)` row in `swarm/docs/PROOF.md`, compute actual `hashlib.sha256` of the file on disk, and cross-check against `PROVENANCE.md`. Plant a 1-character hex mutation in a copy of `PROOF.md` text and assert the validator returns FAIL!
     - **D3 (Banned Overclaim Phrase & Qualifier Grep)**: Scan `swarm/docs/*.md` and the `swarm-1114` section of `README.md` for all banned phrases (`secure against agents`, `sandboxed agents`, `tamper-proof`, `authenticated agents`, `sealed-bid`, `truthful auction`, `guaranteed delivery`, `exactly-once`, `at-least-once`, `rewinds everything`, `audit ok`). Plant `"projector is at-least-once"` and `"sandboxed agents"` in a mutant doc string and assert the validator returns FAIL!
     - **D4 (Two-Track Split Enforcement)**: Verify `swarm/docs/PROOF.md` and `README.md` classify `swarm/*` units (`H0..H6`) strictly under Track H (Gemini) and `product/*` units (`P1..P6`) strictly under Track P (Claude). Plant a `product/admission/admission.py` path inside the Track H claimed list in a mutant string and assert the validator returns FAIL!

## Verification Before Finishing
Run:
`PYTHONDONTWRITEBYTECODE=1 systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 python3 -B -m unittest discover -s swarm/docs -p 'test_*.py' -v`
Confirm 100% pass, ensure zero `__pycache__` directories remain, and report the test count.

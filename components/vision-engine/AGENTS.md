# AGENTS.md - vision-engine 1.1.0

Integer-only visual change engine (S0-S6) plus visual memory (vmem) and engine adapters (vadapt).

## Ladder (read the cheapest rung that answers; stop early)
1. R0 llms.txt (<= 600 B): purpose and links.
2. R1 this file (<= 6,000 B): ladder, layers, ownership, edit rules.
3. R2 `python3 tools/route_code.py --q "<task>"`: top-3 anchors, answer <= 2,000 B; rc 3 = abstain.
4. R2b `catalogue/subjects.json` -> `catalogue/routes.tsv` when the router abstains.
5. R3 one row of `catalogue/symbols.jsonl`, `thresholds.tsv` or `failures.tsv` (<= 200 B).
6. R4 the `# @module {...}` header of the file (first <= 40 lines).
7. R5 the slice from `# @anchor:<id>` to the next anchor (<= 4,000 tok).
Budget: task -> anchor in <= 3 reads + 1 slice.

## Layers (a module may import only the same or a lower layer; gate g_layer)
| L | visioncore modules |
|---|---|
| 0 | ids params layout errors canon frame |
| 1 | decode grid desc |
| 2 | change pyramid regions |
| 3 | gop pack cite |
| 4 | pipeline cli `__init__` |
Packages: `visioncore` (numpy + stdlib, L0 stdlib only) never imports `vmem` or `vadapt`.
`vadapt` -> visioncore.{frame,ids,errors,canon,params}. `vmem` -> visioncore.{ids,layout,errors,canon,schemas} + stdlib.
`tools/` is stdlib only. Data: `catalogue/layers.json`.

## Ownership
CORE: grid desc change pyramid regions. TIME: gop pack cite. VMEM: vmem/. ARCH: ids params errors canon frame decode
pipeline cli vadapt tools catalogue. Shared registry (ids, enums, schemas, rc, params): SPEC_INT section R; it wins ties.

## Rules for edits
- Integers only in L0-L3 (no float literal, `float(`, `/`); ratios are ppm or num/den. Gate g_nofloat.
- Ids are formatted and parsed only in `visioncore/ids.py` (4 lower-case hex Morton digits).
- Every constant is a row of `visioncore/params.py` AND `catalogue/thresholds.src`. No untabled constants.
- No engine names or branches in `visioncore/`. Modules <= 800 lines. Canonical JSON only.
- Schema ids use the slash form `vqa.<name>/v<N>`; files `<name>.v<N>.schema.json`.
- Exit codes: 0 OK, 2 usage/schema, 3 reject, 4 stale, 5 gate, 6 budget. Never 1. One canonical JSON object on stdout.
- A refusal (rc != 0) leaves caller-owned outputs byte-identical (stage to `<out>.tmp`, then `os.replace`).
- Catalogue: edit only `catalogue/*.src`, then `python3 tools/build_catalogue.py`. Never hand-edit generated files.
- Route targets in `catalogue/routes.src` must exist as code anchors, or be listed in `layers.src` `pending_anchors` (unbuilt stage). Queries that must route: `catalogue/route_queries.tsv` (gate g_route_budget).
- Gate `g_catalogue` fails on a stale catalogue: run `python3 tools/build_catalogue.py` last.
- Anchor grammar: `# @anchor:<pkg>.<unit>.<symbol>`, pkg in vc|vm|va|tools; one anchor per public symbol and per PARAMS key.
- Do not commit `build/`, `*.npy`, or vmem stores.

## Commands
- Gates: `python3 tools/gates.py --all` (rc 0 pass, 5 fail).
- Tests: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -t .`
- Catalogue: `python3 tools/build_catalogue.py` (`--check` verifies freshness, rc 4 if stale).

## External web agents (ChatGPT / Gemini / Jules)
- Work only on a branch `web/<tool>/<task-id>`; open a PR; never push to `main`.
- Read first: AGENTS.md, QUICKSTART.md, llms.txt, then the HyperBook in `hyperbooks/` for the area you touch.
- Run before the PR: `python3 tools/gates.py --all`, `python3 tools/build_catalogue.py --check`, `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -t .`.
- Timings or throughput from your VM are NOT evidence: tag them [VM], never [MEASURED].
- Do not edit sealed trees: `visioncore/` and `vmem/` (gate `g_sealed_untouched`, tag `ve-sealed-1`), `hyperbooks/` incl. `out/` receipts, `eval/results/`, `*PREREG*`, `SEAL_HISTORY.jsonl`, `*.sha256`, `hbtools_v2/PROVENANCE.json`, `rust/visioncore_native/Cargo.lock`, pinned wheels (`tools/build_wheel.py` output).
- Frames never on disk (STORAGE LAW): RAM only (`vadapt/ramws.py`); gate `g_disk_frames`.
- PR body states: task id; files touched; commands run with rc; open questions.

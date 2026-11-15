# P4f — Cross-Family Adversarial Review of `H6` (`swarm/docs/` + `README.md`)

- **Reviewer**: `P4f` (`claude-fable-5-2`, Track P, findings-only; 0 files written under `swarm/` or to `README.md`)
- **Target**: `~/Documentos/viewpoint-hackathon` working tree, branch `swarm-1114`, `HEAD = 9c2e4d3` [MEASURED]. `H6` output is uncommitted (`swarm/docs/` untracked, `README.md` modified).
- **Probe window**: 2026-11-14 13:43–13:48 CET [MEASURED]
- **Probe copy**: `git clone` of `HEAD` into `/tmp/p4f_probe/repo` plus the working-tree `README.md` and `swarm/docs/*` (byte-identical, aggregate digest `c8c19786…` on both sides [MEASURED]). Probe scripts in `/tmp/p4f_probe/scripts/` (`audit.py`, `d4.py`, `mutants.py`).
- **Scope wrapper for every probe**: `PYTHONDONTWRITEBYTECODE=1 systemd-run --user --scope -q -p MemoryHigh=4G -p MemoryMax=6G -p MemorySwapMax=0 …`

## SHA-256 of Files Reviewed [MEASURED]

| File | SHA-256 |
|---|---|
| `README.md` (working tree) | `e4ba6457b52f3ef08320031150d2b15db78400e76481928fd4f81ec43bc3464d` |
| `swarm/docs/__init__.py` | `445eeda6aaf355ea66cdd732945011b69a9c87ac97dbdfd20985ee0d2c2efb6f` |
| `swarm/docs/ONE_PAGER_IDEA.md` | `ec6b6bdd7bed4fa9737f8a40c36f7cb07f56d10acc4f4aabc8c6f440ee4b7c29` |
| `swarm/docs/ONE_PAGER_TEAM.md` | `eb553907b9222b9c9a4503a0e5e16033c51c8e4ed31e7a668792a0fbe595b066` |
| `swarm/docs/OPERATOR_CHECKLIST.md` | `5d9d2b9f13971298d1cebfc7825a688d8f7925dd85f26946069652b35804be55` |
| `swarm/docs/PROOF.md` | `d8d86aaf204a386d72db168bb52ae552651c3c1f7b77eabc31a8d41a7925a2ea` |
| `swarm/docs/test_docs.py` | `bbbfa5f8194acde8c7c4e2fb7af253aeb1ef922901d9d96708c11515a6d54b2e` |
| `PROVENANCE.md` (reference, `HEAD`) | `5ba82cf0392d0e368996156745591c0ac7fc2b9824c21e9f6933a85681c3b4eb` |

## VERDICT: REJECTED

`0` BLOCKER, `2` LIVE_PATH, `9` BACKLOG [MEASURED]. All four gates D1–D4 and the unit suite pass; the rejection rests only on two verifiably false statements in submission-facing text (`LP-1`, `LP-2`). Both are single-line edits; a narrow `H6_r1` touching 3 lines clears them.

| Gate | Result | Evidence |
|---|---|---|
| Unit suite | PASS | `10/10` OK, `0.024–0.027 s` over 3 runs, `rc=0` [MEASURED] |
| D1 links & citations | PASS | `11/11` Markdown links resolve to `HEAD ∪ swarm/docs/*`; `0` untracked worktree citations [MEASURED] |
| D2 proof re-hash | PASS | `33/33` proof rows match disk; `0` missing rows vs `PROVENANCE.md`; `10/10` design inputs match [MEASURED] |
| D3 anti-overclaim | PASS (wording) / 2 LIVE_PATH (facts) | `0` banned-phrase hits; boundary + projection statements verbatim in both required files [MEASURED] |
| D4 two-track & README preservation | PASS | `git diff --numstat README.md` = `38` insertions, `0` deletions [MEASURED] |

## LIVE_PATH Findings (must fix before commit/tag)

### LP-1 — False `[MEASURED]` kernel version
- `swarm/docs/ONE_PAGER_TEAM.md:6`: "`aarch64` kernel `6.17.0-1018-nvidia` [MEASURED]".
- Host reports `7.0.0-1019-nvidia` [MEASURED]. Repro: `uname -r`.
- The value is not in `swarm/briefs/H6_docs.md` (the brief supplies only `20` cores and `121 GiB`), and `grep -c uname swarm/proof/H6_1333.jsonl` returns `0` [MEASURED], so the stream shows no measurement behind the tag.
- Fix: replace with the `uname -r` value, or drop the kernel string.

### LP-2 — Cited audit command does not exist
- `swarm/docs/ONE_PAGER_IDEA.md:50`: "Reconciliation audit (`python3 -m swarm.projection.reconciler --finalize`): `CLEAN` [MEASURED]".
- `README.md:129`: "End-of-run reconciliation audit (`reconciler.py --finalize`): `CLEAN` [MEASURED]".
- Repro (probe copy): `python3 -m swarm.projection.reconciler --finalize` → `reconciler.py: error: unrecognized arguments: --finalize`, `rc=2` [MEASURED]. The flag lives at `swarm/projection/projector.py:1279`; `swarm/projection/reconciler.py:315-337` defines only `--events`, `--cw-home`, `--cw-room`, `--state-file`.
- `PROVENANCE.md:64` records the real provenance of the number: "`projector --finalize` `CLEAN`".
- Fix: cite `python3 -m swarm.projection.projector --finalize` on both lines.
- Not counted against `H6`: the mandated projection sentence ("audited at end-of-run by `reconciler.py` (`--finalize`)", `README.md:116`, `ONE_PAGER_IDEA.md:42`) is director-authored wording from the brief and pinned verbatim by `test_docs.py:29-32`. It is loose rather than false (the reconciler audit runs inside `projector --finalize`), but the director should consider rewording brief + test + docs together.

## Gate Detail

### 1. Unit Suite
```
cd /tmp/p4f_probe/repo && PYTHONDONTWRITEBYTECODE=1 systemd-run --user --scope -q -p MemoryHigh=4G -p MemoryMax=6G -p MemorySwapMax=0 python3 -m unittest swarm.docs.test_docs -v
```
`Ran 10 tests in 0.025s / 0.024s / 0.027s — OK` [MEASURED]. 5 pass-tests, 4 must-fail controls, 1 extra verbatim check (`test_docs.py:260-429`).

### 2. D1 — Committed-Only Link & Citation Audit
Independent extractor (`audit.py`): every `](target)` and every backticked path-like token in `swarm/docs/*.md` and `README.md:101-137`, checked against `git ls-tree -r HEAD` (`301` files [MEASURED]) ∪ `swarm/docs/*`. `11` links, `124` backticked path tokens scanned [MEASURED].

- Links: `11/11` OK — `README.md:108,133-137`; `OPERATOR_CHECKLIST.md:15,69-72`.
- `0` citations of `product/narrative/*`, `product/cloudrun/*`, or any other untracked worktree file.
- Not in `HEAD`, each accounted for:

| Citation | Where | Status |
|---|---|---|
| `swarm/viewer/` | `ONE_PAGER_IDEA.md:28,30`; `ONE_PAGER_TEAM.md:21`; `OPERATOR_CHECKLIST.md:44` | Untracked `H5` output; all 4 lines carry `[TARGET]` — allowed by the gate |
| `swarm/run/events.jsonl`, `swarm/run/` | `README.md:116`; `ONE_PAGER_IDEA.md:42`; `ONE_PAGER_TEAM.md:15` | Gitignored runtime (`swarm/.gitignore:1`), declared as runtime, not as committed evidence |
| `~/Documentos/ViewPoint/hackathon/antigravity2/0{0..5}_*.md` | `PROOF.md:58-63` | External absolute paths copied from `PROVENANCE.md:21-26`; present on disk, hashes match |
| `waves/w180-multicursor-strategy/r1/R1_0{1..4}_*.md` | `PROOF.md:64-67` | See `BL-7` |
| `~/.gemini`, `~/Vídeos/ViewPoint_hackathon/` | `OPERATOR_CHECKLIST.md:17,66` | Operator-host paths, not evidence |

`tools/check_public.sh` and `gateway/Dockerfile` (`OPERATOR_CHECKLIST.md:19,47`) are tracked. Commit `04a1eb4` (`ONE_PAGER_IDEA.md:46`) exists.

### 3. D2 — Independent Proof SHA-256 Re-Hash
`hashlib.sha256` over each file in the main checkout, compared to `PROOF.md` and to the hash following the filename in `PROVENANCE.md`:

| Set | Rows | Disk match | `PROVENANCE.md` match | Tracked in `HEAD` |
|---|---|---|---|---|
| Track H (`PROOF.md:11-25`) | `15` | `15/15` | `15/15` full 64-hex | `15/15` |
| Track P (`PROOF.md:33-50`) | `18` | `18/18` | `16` full + `2` 8-hex prefix (`P4_H1_1202`, `P4_H4_1202`, truncated at `PROVENANCE.md:59`) | `18/18` |
| Design inputs (`PROOF.md:58-67`) | `10` | `10/10` | `10/10` | external |

All counts [MEASURED]. Set equality holds three ways: `.jsonl` names in `PROVENANCE.md` = `PROOF.md` rows = tracked `{swarm,product}/proof/*.jsonl` (`33` each, `0` extra, `0` missing). On-disk proof logs without a row: `H5_1333.jsonl`, `H6_1333.jsonl`, `P4f_1343.jsonl` — untracked, absent from `PROVENANCE.md`; see `BL-1`.

Extra check: the `"model"` field inside every log equals the model declared in `PROOF.md` (`33/33` [MEASURED]).

### 4. D3 — Anti-Overclaim & Banned Phrases
- Case-insensitive grep of the 4 docs + `README.md:101-137`, with hyphen/space variants, for all 11 listed phrases plus `in parallel`, `isolated`, `isolation`, `sandbox`, `tamper`, `secure`, `parallel`: `0` hits [MEASURED]. `isolated` does not occur at all.
- Blender actuation is described as serialized everywhere it appears (`README.md:103`, `ONE_PAGER_IDEA.md:11`).
- Shared-UID boundary statement verbatim at `README.md:114` and `ONE_PAGER_IDEA.md:38`; best-effort projection statement verbatim at `README.md:116` and `ONE_PAGER_IDEA.md:42`.
- `CLEAN` is never offered alone: it sits beside event, turn and mesh counts.
- Softer hits, not banned: "Real-time" (`ONE_PAGER_IDEA.md:19`, see `BL-6`), "To guarantee total provenance transparency" (`ONE_PAGER_TEAM.md:11`), "Cryptographic" for plain SHA-256 digests (`PROOF.md:1`, `README.md:108,135`).

Live-gate numbers re-derived from gitignored runtime evidence in the main checkout:

| Claim | Source checked | Result |
|---|---|---|
| `100` broker events | `wc -l swarm/run/events.jsonl`; sha `33a7d67c…` equals `PROVENANCE.md:64` | Confirmed [MEASURED] |
| `7` verified turns | `close` records: camera `2 STATE_VERIFIED + 2 VERIFIED_WITH_SPILL`, modeler `3 STATE_VERIFIED` | Confirmed; plus `1` modeler `JOB_ERROR` not mentioned (`BL-4`) [MEASURED] |
| `16` meshes / `1340` verts / `2368` polys | `swarm/run/take1_w1_2agents/glb_audit.txt` | Confirmed; the 16th mesh is `Cube` (`BL-4`) [MEASURED] |
| `CLEAN`, `0` mismatches, `0` dropped | `finalize_report.json`: `anomalies: []`; sum of `drops` fields = `0` | Confirmed; `refusals_count: 1` [MEASURED] |
| Blender `4.0.2`, Xvfb `:2` | not re-probed | AUSENTE — no inventar |
| `19/19`, `19/19`, `15/15`, `29/29`, `52/52`, `33/33` | not re-run here; equal to `PROVENANCE.md:52-57` | [CITED] |

### 5. D4 — Two-Track Integrity & README Preservation
- `git -C ~/Documentos/viewpoint-hackathon diff --numstat README.md` → `38  0` [MEASURED]. One hunk, inserted between line 99 and `## License`; lines 1–100 and the License section are byte-preserved.
- `H6` footprint in the main checkout: `README.md` + `swarm/docs/` only; no edits to `PROVENANCE.md`, `product/`, `gateway/` tracked files, or other `swarm/` units.
- Unit attribution vs `PROVENANCE.md:51-55` and log `"model"` fields:

| Unit | Docs claim (`ONE_PAGER_TEAM.md:16-22`, `ONE_PAGER_IDEA.md:27-29`) | Check |
|---|---|---|
| `H0`, `H1`, `H2` | `gemini-4-hydrogen-high` | Match |
| `H3` | carbon (v0), hydrogen (`r1`, `r2`) | Match |
| `H4` | flash (v0, `r1`), hydrogen (`r2`) | Match |
| `H5` | `gemini-4-carbon-high`, `[TARGET]` | Untracked `swarm/proof/H5_1333.jsonl` reports that model |
| `H6` | `gemini-3.8-flash-high` | Untracked `swarm/proof/H6_1333.jsonl` reports that model |
| `P1..P6`, `P4*` | `claude-opus-5-5` / `claude-fable-5-2` | `18/18` logs match |
| Pre-wave specs | "Written by Claude" (`PROOF.md:52-67`) | Table equals `PROVENANCE.md:19-30` |

- "Exactly 0 files written under `swarm/`" by Track P (`README.md:110`, `ONE_PAGER_TEAM.md:37`, `PROOF.md:29`): `d4.py` walked every `tool_use` in the `18` tracked `product/proof/*.jsonl` logs — `33` `Write`/`Edit` calls, `0` targeting `swarm/` or `README.md` [MEASURED]. Writes made through `Bash` are not covered by this probe.
- `SHA-256 b392f7dd...` for `swarm/CONTRACT.md` (`ONE_PAGER_TEAM.md:16`) matches disk [MEASURED].

## BACKLOG Findings

| ID | Where | Finding | Suggested action |
|---|---|---|---|
| `BL-1` | `README.md:108`, `ONE_PAGER_TEAM.md:24`, `PROOF.md:7` | "all 15" Track H logs is true for `HEAD` but the same docs claim `H5` and `H6` as Gemini-authored with no receipt row. `H6` cannot hash its own running log. | Director adds `H5_1333` / `H6_1333` (and repair logs) to `PROVENANCE.md` and `PROOF.md` at commit time and updates the count; `test_docs.py` requires every `PROOF.md` filename to appear in `PROVENANCE.md`, so both must change together. |
| `BL-2` | `README.md:110`, `ONE_PAGER_TEAM.md:28`, `PROOF.md:29` | "prior to 13:59 CET" for all Track P work. `P4f` ran 13:39–13:48 CET (`product/proof/P4f_1339.*`, `P4f_1343.*`); any later Track P round (e.g. a re-review of `H6_r1`) makes the sentence false. Wording inherited from `PROVENANCE.md:13`. | Director decides the cutoff wording once Track P is closed. |
| `BL-3` | `ONE_PAGER_TEAM.md:7` | "All agent runs, test executions, and background builds" under `MemoryHigh=6G`/`MemoryMax=8G` `[MEASURED]`. `2` of `43` brief occurrences use `4G`/`6G` (including this review) [MEASURED]. | "memory-capped `systemd-run` scopes (6G/8G for Track H units)". |
| `BL-4` | `README.md:126-128`, `ONE_PAGER_IDEA.md:47-49` | Accurate but incomplete: `8` turns enqueued, `7` verified, `1` modeler `JOB_ERROR`, `1` refusal; the `16` meshes are `15` `CHAR_*` plus Blender's default `Cube`. | Add "7 of 8 enqueued turns verified; 1 `JOB_ERROR`" and "15 `CHAR_*` meshes + default `Cube`". |
| `BL-5` | `README.md:103` | Lists four agents reasoning concurrently in the present tense; the measured gate enqueued `2` (`agents_enqueued` in `finalize_report.json`). `ONE_PAGER_IDEA.md:27` states it correctly. | Qualify: "Wave 1 live gate exercised `modeler-agent` and `camera-agent`". |
| `BL-6` | `ONE_PAGER_IDEA.md:19` | "Real-time" projection sits 23 lines above the best-effort statement. | "Live, best-effort". |
| `BL-7` | `PROOF.md:64-67` | `waves/w180-multicursor-strategy/r1/…` reads as repo-relative but resolves only under `~/Documentos/ViewPoint/` (hashes match there). Inherited from `PROVENANCE.md:27-30`. | Director prefixes the absolute root in `PROVENANCE.md`; `PROOF.md` follows. |
| `BL-8` | `swarm/docs/test_docs.py` | Validator gaps, each reproduced with `mutants.py` (`7` planted defects escape, `0` affect today's docs) — table below. | Tighten in `H6_r1` if time allows. |
| `BL-9` | `ONE_PAGER_IDEA.md:9`; `OPERATOR_CHECKLIST.md:57` | `$$…$$` LaTeX in a plain-text one-pager; placeholder URL `viewpoint-viewer-xxxx` in a copy-paste command. | Cosmetic. |

`BL-8` mutation results [MEASURED]:

| Mutant | `test_docs.py` | Outcome |
|---|---|---|
| Backticked fake `product/proof/P4_FAKE.jsonl` / `swarm/proof/run_fake.jsonl` | `:131` prefix skip | ESCAPED |
| Backticked `waves/none/FAKE.md` | `:127-129` root allow-list | ESCAPED |
| Backticked `product/narrative/{A.md,B.md}` | `:125` regex excludes braces | ESCAPED |
| 3 proof rows deleted (33 → 30) | `:169` threshold instead of set equality with `PROVENANCE.md` | ESCAPED |
| `PROVENANCE.md` hash altered, disk + `PROOF.md` agree | `:186-188` filename-only check | ESCAPED |
| "fully isolated", "tamper proof", "exactly once" | `:35-47` no `isolated`, hyphen-only patterns | ESCAPED |
| README section "Measured results" deleted | `:236-247` no preservation check | ESCAPED |
| Controls: untracked backticked path, "in parallel" near Blender, `audit ok`, Claude model in an `H` row | — | DETECTED |

## Repair Scope for `H6_r1`

1. `swarm/docs/ONE_PAGER_TEAM.md:6` — correct or remove the kernel string (`LP-1`).
2. `swarm/docs/ONE_PAGER_IDEA.md:50` and `README.md:129` — cite `projector --finalize` (`LP-2`).
3. Optional in the same pass: `BL-3`..`BL-6` wording, `BL-8` validator tightening.
4. Director-owned, outside `H6`: `BL-1`, `BL-2`, `BL-7`.

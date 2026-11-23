# Blender visual QA navigation and contracts

Read llms.txt, then this file, then route the task:
`python3 tools/route_code.py --root catalogue/router --q "task"`.
Wider index (every symbol, section, schema and test, Blender-coupling axis, abstention): `python3 -B route/query.py any "task"`; see route/README.md.
Fallback: catalogue/subjects.json -> routes.tsv -> one symbols.jsonl row -> anchor slice.
Subjects: drive, truth, oracles, judge, report, vision, repo, mutants, bpy.
Read only the selected module header and anchor or HyperBook section. Router abstention is rc 3 [CITED: BSPEC_BINT].

Host package: blender_visual_qa. Blender code: bpy_side. Host code must not import bpy, bmesh, mathutils or gpu;
bpy_side must not import blender_visual_qa. Only pure stdlib bpy_side.q and bpy_side.canon may cross the boundary.
Numpy is allowed in truth/, oracles/pixel.py and vision_stage.py. Never copy engine algorithms into this repository.
Depend on the committed engine pin in config/vision.json; vendored tooling provenance is tools/VENDORED.json.
Sources are read-only. Render a scratch copy; use a declared empty environment, owned process groups and bounded timeouts.
Use CPU Cycles. Never read operator.token, launch.token, *.key, auth.json or .beckett/token.

Verdict commands run/report/judge use ERROR > FAIL > INCOMPLETE > PASS.
Stage codes [CITED: BSPEC_BINT R-B3]:
| rc | meaning |
|---|---|
| 0 | OK |
| 2 | usage, schema, IO, drive or profile error |
| 3 | incomplete or cannot see |
| 4 | stale input/pin or drift |
| 5 | gate, determinism or mutant failure |
| 6 | budget exceeded |
Stage rc 1 is forbidden [CITED: BSPEC_BINT]. Schema ids use bvqa.name/vN for Blender and inherited vqa.name/vN for engine contracts.
JSON is canonical, sorted, integer-only for Blender stages. Frame identity hashes decoded RGB bytes.
Every refusal preserves caller output bytes; publish only after complete validation using staged writes and os.replace.

B0 checks: python3 tools/gates.py --all. Positive/negative host tests:
`PYTHONDONTWRITEBYTECODE=1 timeout 300 python3 -m unittest discover -s tests -p 'test_b0*.py'`.
Do not run make, commit, push, rewrite history, launch unbounded processes, or mutate the truth bench.
Thresholds marked DESIGN_CHOICE remain uncalibrated until their named calibration step passes.
The mandate is limited to the provided Blender, Godot baseline and pinned vision-engine corpus.

## External web agents
- Work only on branch `web/<tool>/<task-id>`; open a PR; never push to main.
- Read AGENTS.md, QUICKSTART.md, llms.txt and the relevant HyperBook (hyperbooks/HB-BVQA-*.md) first.
- Run: `PYTHONDONTWRITEBYTECODE=1 timeout 300 python3 -m unittest discover -s tests -p 'test_b0*.py'` and `python3 tools/gates.py --all`.
- Performance numbers from your VM are not evidence: tag them [VM], never [MEASURED].
- Do not touch sealed trees (tests/golden/, catalogue/, hyperbooks/, copy_manifest.tsv, migration/) or pinned tools/wheels/, tools/VENDORED.json, tools/*.json receipts.
- Frames never on disk (STORAGE LAW): every directory and file written by workers must lie on the verified RAM root device (`st_dev == root_dev`) and not be a symlink, enforced pre-write on exact final paths (`require_ram_out`, `ram_child`) and post-write via `os.lstat` walk (`verify_written`).
- Non-goal residual (STORAGE LAW): concurrent local actor TOCTOU races between guard/write/verify are not closed.
- PR body: task id, files touched, commands run with rc, open questions.

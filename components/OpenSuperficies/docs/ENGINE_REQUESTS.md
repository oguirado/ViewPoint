# ENGINE_REQUESTS — OpenSuperficies → vision-engine (append-only)

> Release note (PUB): `vcaps/`, `vadapt/`, `visioncore/`, `vmem/`, `eval/x10_compaction.py`, `ramws.py` and `w1/PEER_ANSWER_W1.md` live in the vision-engine repo or the conductor campaign dir, not in this repo; `capsule.json`, `frames.raw` and `owner.json` are runtime artifacts.


Owner of this file: os-cu-0 conductor. The visual-qa-0 conductor turns entries into cards; OpenSuperficies never edits the engine.
Pin in use: tag `ve-sealed-1-law4` → commit `2612b3acd67c49234adfaa77ab2b3803d5a81cb5`, wheel sha256 `5e2395d56709274f21123bb5ef714adf52f0cf58f78d4f4938da736c52a7a4a5` (W2 re-pin, lane R4; law3 `666d0198…` / `da54f304…bbee`, law2 `15714b62…62d8e` / `ebcf3854…6e89` and law1 `65a8469…` / `41e798d6…` are refused by `vision/pin.py`).

Format: `## ER-<nnn> <title>` / status / what / why / evidence.

## ER-001 vcaps capsule contract alignment
status: OPEN
what: confirm OS `docker/cu/entrypoint-cu.sh` flags + `capsule.json` satisfy `vcaps/lifecycle.py` expectations, or publish the frozen `vqa.capsule/v1` JSON Schema.
why: OS builds the capsule variant now (CARDS_W0 §3 ruling); the engine owns the schema.
evidence: `docker/ng/desktop/entrypoint.sh:43-45` lacks `-fbdir`; OS variant adds `Xvfb :100 -fbdir /run/vqa/fb`.

## ER-002 va.gui.frames accept storage rgb24_raw
status: DELIVERED@ve-sealed-1-law2 (commit 15714b62; verified by `vision/tests/test_engine_path.py`, storage `rgb24_raw`)
what: `GuiAdapter.frames()` reads `frames.raw` + `raw_path` without PNG decode (`storage: "rgb24_raw"`).
why: STORAGE LAW raw-RGB preference; PNG decode costs 18-66 ms/frame.
evidence: `vadapt/gui/from_screens.py:132-133` @65a8469 hard-codes `storage: "png_dir"`, `raw_path: None`.

## ER-003 publish CU JSON Schemas in a sealed wheel
status: OPEN (sub-item "reproducible wheel build": DELIVERED@ve-sealed-1-law2; OS rebuild reproduces `ebcf3854…6e89`, `vision/build_wheel.py --check` = `equal:ebcf3854`; schemas in the wheel still OPEN)
what: `vqa.action/v1`, `vqa.act-step/v1`, `vqa.act-plan/v1`, `vqa.mcp-result/v1`, `vqa.capsule/v1` as files in the wheel. Sub-item: build the wheel reproducibly (`SOURCE_DATE_EPOCH` = tag commit time, restored file modes) so its sha256 can be pinned by a rebuild; OS rebuilt `41e798d6208681f029c9ad5136a47b4e56dcb22f4884279df5d60b6b8a12a22b` (70/70 members except RECORD equal to peer wheel `de745d6e...cb94`).
why: OS types are validated only against DESIGN_CU prose; `pip wheel` without `SOURCE_DATE_EPOCH` is not byte-reproducible.
evidence: `src/cubook/action/types.go`, `vision/build_wheel.py`, `vision/VENDORED.json`.

## ER-004 actuator/takeover protocol
status: OPEN
what: freeze/thaw + `release_all` command protocol of `vqa-mano` and the xpra UDS takeover path.
why: OS ships `src/cubook/exec` (Actuator interface, mandatory ReleaseAll) and `XPRA_MODE=off`; the wire protocol must match the engine's.
evidence: `src/cubook/exec/types.go`, `docker/xpra-layer.sh:4,12` (xpra binds 0.0.0.0).

## ER-005 vadapt/mcp dispatcher interface + registry
status: OPEN
what: registry format, CAS layout, verification hooks (receipt/visual/readback).
why: OS `src/cubook/mcp` builds its own registry (`testdata/cu/mcp/registry.json`) and CAS (`cas:sha256:<64hex>`); the engine dispatcher must accept the same.
evidence: `src/cubook/mcp/registry.go`, `src/cubook/mcp/cas.go`.

## ER-006 register tool "opensuperficies" in STORAGE LAW L11 scope
status: DELIVERED@ve-sealed-1-law2 (commit 15714b62; `vadapt/ramws.py` FRAME_NAMES ⊇ `frames.raw`, `Xvfb_screen0`; `.rgb24` / `frames.rgb24` not yet in the engine list, OS shim adds them: mutant `os.disk.raw.08`)
what: ramws tool id `opensuperficies`, campaign `os-cu-0`; extend `vadapt/ramws.py` frame detection with `.raw`, `.xwd`, `frames.raw`, `Xvfb_screen0`; confirm `check_disk(since=)` semantics used by OS `scripts/cu/g_disk_frames.py`.
why: `ramws.py:23-24` FRAME_EXT/FRAME_NAMES omit them, so `check_disk(build/)` misses raw frames; OS closes the gap in `vision/ramws_shim.py` meanwhile (mutant `os.disk.raw.08`).
evidence: `git show ve-sealed-1-law1:vadapt/ramws.py | sed -n 23,24p`.

## ER-007 vcore/vmem stable CLI for stored rgb24_raw runs
status: OPEN
what: a documented CLI (or stable function) that turns a stored `rgb24_raw` run (`raw.json` + `frames.linear.raw`) into a `vmem.spool/v1` file; today `vcore analyze <manifest>` and `vmem ingest --store S --spool F` exist (`visioncore/cli.py:52-65`, `vmem/cli.py:26-50`) but nothing emits the spool, so `vision/ingest.py` ports `build_spool` from `eval/x10_compaction.py`.
why: OS must not copy engine eval code; the spool format is the contract between analysis and memory.
evidence: `vision/ingest.py` `build_spool`; REPORT_E (W1).

## ER-008 raw.json accepts ring + render_index order
status: OPEN
what: `GuiAdapter` raw input accepting the OS ring file plus the `render_index` order directly (ring slots wrap), so OS need not write a linearised copy `frames.linear.raw`.
why: halves the RAM held per capture (STORAGE LAW); the linearisation is `src/cubook/capture/rawjson.go` `WriteRawJSON`.
evidence: law2 `vadapt/gui/from_screens.py:89-99,110-128,164-165`.

## ER-009 rtring producer note + tag ve-rt-ring-1
status: OPEN (W2, PEER §3)
what: publish the rtring producer contract and tag `ve-rt-ring-1` so OS can replace ramws file captures with the ring in W2.
why: W1 captures are ramws files only; the 4K 60 fps WL segments (lane L) are RAM-bound without a ring.
evidence: `w1/PEER_ANSWER_W1.md` §3 (campaign dir).

### REQUEST to meta-orchestrator (not VE): editor operations believed missing from MCP
Per AMEND_W0 A1 [H], to confirm against live tool lists: (1) Blender transform-gizmo DRAG (GUI fallback, verified by `get_transform` readback + region check); (2) Godot Inspector property edit / Add Child Node; (3) Godot run/stop (F5/F8) and Output-panel read. Godot items run only against a scratch editor inside our own capsule (never :8770 / OZ tree).

## ER-010 ramws run dir shareable with the capsule uid
status: DELIVERED@ve-sealed-1-law4 (first delivered at law3 commit 666d0198, wheel da54f304…bbee; carried unchanged by law4 commit 2612b3ac, wheel 5e2395d5…a4a5; `open_run(..., share_with_uid=, share_group=)`, E_RAM_SHARE rc 5 otherwise; verified by `vision/tests/test_repin_law3.py` and `vision/tests/test_ramws_share.py`; ACL visibility through a docker bind mount is unmeasured until S-LIVE v2)
what: `vadapt.ramws.open_run(..., share_with_uid: int | None = None)`; when set, POSIX ACLs u:<uid>:rwx on the run dir and u:<uid>:x on
the parents, 0o700 for everyone else; recorded in `receipt()`; janitor keeps ownership of cleanup. Fallback without tmpfs ACLs: a
dedicated group with 0o2770 on the run dir only.
why: the capsule image runs as uid 10001 (Xvfb writes Xvfb_screen0, srf-cu-snap writes frames.raw), while the session dir is created
0o700 by the host user (ramws.py:317,322,323 at ve-sealed-1-law2). Running the capsule as the host uid failed (tmpfs ownership, dbus).
evidence: os-cu-0 campaign w1/evidence/SLIVE/slive_try1.txt, slive.txt. Rule: no manual chmod of ramws dirs.

## ER-011 shared run dir: `.lock` and `owner.json` readable by the shared uid
status: DELIVERED@ve-sealed-1-law4 (commit 2612b3ac, wheel 5e2395d5…a4a5; `.lock` and `owner.json` get `u:<uid>:r--` after creation, group fallback 0o640, `receipt().share.files_read == [".lock","owner.json"]`; read ACL failure = E_RAM_SHARE; verified by `vision/tests/test_repin_law4.py`; guard probe is LOCK_SH|LOCK_NB, `src/cubook/capture/guard_probe_test.go`)
what: with `share_with_uid`, also grant `u:<uid>:r--` on `.lock` and `owner.json` (law3 creates both 0600 for the host owner BEFORE sharing, so they carry no ACL).
why: the capsule checker `src/cubook/capture/guard.go` `CheckRunDir` opens `.lock` (O_RDONLY, flock) as uid 10001 and gets EACCES on a shared run dir. OS cannot chmod ramws files (rule: only the ramws API shares).
evidence: os-cu-0 w2/evidence/R/getfacl.txt, REPORT_R F3. Delivered by the peer (PEER_LAW4); bind-mount ACL visibility still to confirm at S-LIVE v2.

## ER-012 stable per-chunk analysis entry point for stored rgb24_raw chunks
status: OPEN (drafted by W3 lane E; not requested for a law-N patch)
what: a pinned, versioned function or CLI (ER-007 extension) that classifies tiles for N consecutive rgb24_raw frames of one chunk (`frames`, `W`, `H`, grid derived as `Grid(W,H).s`) and returns the class counts `{UNCHANGED,JITTER,CHANGED,MOVED,FLICKER,FROZEN,DRIFT}` as integers, in memory, without GuiAdapter/VisionRun disk output.
why: `vision/chunk_analyze.py` consumes `visioncore` internals (`decode.rgb24_raw`, `desc.describe_padded`, `change.ChangeState`/`classify_tiles`, `grid.Grid`) at ve-sealed-1-law4; a rename inside the engine would break the 4K rows-only path (KL-20) with no signal. Law4 pin-first and the wheel witness (`equal:5e2395d5`) guard the version, not the API.
evidence: os-cu-0 w3 REPORT_E (techniques table), `vision/chunk_analyze.py`, `vision/tests/test_chunk_analyze.py`.

## ER-013 FRAME_NAMES += chunk_*/frames.rgb24 (WITHDRAWN)
status: WITHDRAWN (number kept so the list stays contiguous; nothing requested)
what: the W3 card proposed it; `vadapt/ramws.py` already matches the basename `frames.rgb24` (ER-006, advocate AW3-12), so chunk files under `chunk_<k>/` are already covered.

## ER-014 long-period FLICKER (caret ~500 ms)
status: ACCEPTED by visual-qa-0 as vision-v2 track B (SYNC_VQA 2026-10-18 05:50); not a law-N patch, not delivered
what: a period-P toggle detector on the tile hash history (or S5 CYCLE beyond P_MAX=8 with a bounded hash ring) so periodic flicker up to ~1 s at 60 fps is FLICKER. Law4 S2 FLICKER is period-2 (matches `prev2`) and S5 CYCLE is P<=8 (~133 ms): a 15-on/15-off caret over 60 frames gives `{STATIC: 58, CHANGED: 2, FLICKER: 0}` (advocate AW3-04, measured at law4).
why: WL-2b (focused input, caret blink period pre-registered 500 ms in `bench/wl/prereg_wl_v2.json`) keeps `expected_kind FLICKER` as ground truth; the scene is never adapted to the engine. Measured on WL-2b and Blender flicker benches, held-out, no X4 false-positive regression.
evidence: SYNC_VQA 2026-10-18 05:50, os-cu-0 w3 ADVOCATE_CARDS_W3 AW3-04, `bench/wl/labels_v2.py` (`flicker_period_ms`).

FX2 / INT3 (2026-10-18): no new engine request. The numpy-free capsule decision keeps all engine use on the host through `vision/pin.py` (pinned wheel `ve-sealed-1-law4`, `equal:5e2395d5`); ER-012 stays OPEN, ER-014 stays ACCEPTED.

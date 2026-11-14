# Changelog

## ER-011 engine request from os-cu-0 (unreleased)
- ER-011 `vadapt.ramws.open_run(share_with_uid=...)`: after `.lock` and `owner.json` exist (and the run dir is shared), grant ACL `u:<uid>:r--` with mask `r--` on both files (read only, never write) so the capsule guard can open `.lock` O_RDONLY, probe `flock(LOCK_SH|LOCK_NB)` and read `owner.json`; `share_group` fallback: chgrp + 0o640 on both files. `receipt().share` gains `files_read: [".lock", "owner.json"]` (only when a share is used; default path byte-identical). A file ACL that cannot be applied is rolled back (`setfacl -b`, 0o600) and fails closed `E_RAM_SHARE` unless the group fallback applies. Integrity stays host-owned.
- Janitor vs a foreign `LOCK_SH`: liveness is decided by the host's own `LOCK_EX`. `_try_lock` takes `LOCK_EX|LOCK_NB`; if refused it probes `LOCK_SH|LOCK_NB`, which succeeds only when nobody holds `LOCK_EX`, so a dead run pinned by a reader's shared lock is still swept, and a live host (EX held) still refuses both. A held `LOCK_SH` cannot block the host (it already holds EX; `close()` is rmtree + fd close) and cannot block a sweep of a dead run; it can only make a second `LOCK_EX` attempt fail, which the probe handles. Tests: tests/test_ramws_share.py (8 new er11). visioncore/ and vmem/ unchanged.

## ER-10 engine request from os-cu-0 (unreleased)
- ER-10 `vadapt.ramws.open_run(..., share_with_uid=None, share_group=None)`: share a run dir with one container uid. POSIX ACL `u:<uid>:rwx` (+ default ACL) on the run dir and `u:<uid>:--x` on its parents up to root via `setfacl` (stdlib subprocess); fallback `share_group`: chgrp + 0o2770 on the run dir, 0o710 parents. Base mode stays 0o700; neither method applicable -> `E_RAM_SHARE` (rc 5, run dir removed). `receipt()` gains `share` {uid|gid, method} only when used; absent argument = byte-identical behaviour. Tests: tests/test_ramws_share.py. visioncore/ and vmem/ unchanged.
- FXER10 (ADV_ER10 F-01..F-05): default ACL also names the host owner uid (container-written files stay readable, accountable and deletable); `.lock` (O_NOFOLLOW) + flock + `owner.json` (0o600) are created before any share, run dir sticky (ACL 1770, group 3770), flock failure rolls the run dir back, the janitor ignores an `owner.json` that is a symlink or not host-owned; tool/campaign dirs created 0o700 per level, group fallback never chgrp's pre-existing ancestors (only o+x when needed), ACL skips already-traversable ancestors, `sweep()` removes an empty tool dir; ACL parents first then one setfacl on the run dir, `setfacl -b` before the group fallback (rollback failure -> `E_RAM_SHARE`), `ws.path()` makes new subdirs 0o2770 in group mode; `open_run` resolves `root` with realpath, `setfacl ... -- <path>`. Tests: tests/test_ramws_share.py (13 new).

## ER-1/2/3 engine requests from os-cu-0 (unreleased)
- ER-1 `vadapt.ramws`: `FRAME_EXT` gains `.raw`, `.xwd`; `FRAME_NAMES` gains `frames.raw`, `Xvfb_screen0` (check_disk sees raw X11 captures).
- ER-2 `vadapt/gui/from_screens.py`: raw RGB24 input (`raw.json` {width,height,stride?,file|files} + `frames.raw` or one file per frame), mmap + `np.frombuffer` zero-copy; manifest storage `rgb24_raw` (the sealed `visioncore` schema value; `adapt()` writes dense `frames.rgb24`). Frame sha256 and analysis outputs identical to the PNG path (tests/test_e16_raw_capture.py).
- ER-3 `tools/build_wheel.py [rev] [--out dir]`: wheel from `git archive <rev>`, SOURCE_DATE_EPOCH = commit time, prints sha256; two builds identical (tests/test_e16_build_wheel.py). visioncore/ and vmem/ unchanged.

## STORAGE LAW v1 - V1 (unreleased, ve-law1)
- Add `vadapt/ramws.py` (stdlib only): RAM workspace on tmpfs/ramfs (`open_run`, `RamWorkspace.path/reserve/account/release/receipt/close`), quotas (run 2 GiB default / 8 GiB max, host 16 GiB, statvfs free >= 8 GiB), flock janitor, `keep_frames`/`sweep_kept` (L7), `check_disk`, CLI `python3 -m vadapt.ramws {sweep,sweep-kept,status,check-disk}`.
- Gates: `g_sealed_untouched` (visioncore/ and vmem/ identical to tag ve-sealed-1), `g_disk_frames` (engine smoke leaves no frame file outside RAM), `g_layer` rule (ramws imports no visioncore/vmem/third party).
- Tests: tests/test_ramws.py, tests/test_e15_law_gates.py, tests/test_e15_ram_mutants.py (7 ram.* mutants, rc 5). KL-6 records L9 (swap). visioncore/ and vmem/ unchanged.

## INT-E integration (unreleased)
- X10 (G-E8) storage verdict now measures CLEAN 200-frame runs (clean_static, gpu_jitter) before/after clean compaction (500,000 / 50,000 B); the heavy flicker run is an advisory row. Add KNOWN_LIMITATIONS.md.
- Register S6 call/run/retry token caps and refresh generated catalogue anchors.
- Align legacy pack fixtures with mandatory-frame refusal while preserving track/class coverage.
- Share the run token allowance in judge-cache fixtures; cache hits remain free.
- Use one witness GOP for X8 citation checks while retaining complete source evidence.

## 1.1.0 - W0 (unreleased)
- Skeleton: Tier-1 gateway (llms.txt, AGENTS.md), pyproject, .gitignore.
- Registries: ids, params, errors, layout, canon, schemas (S1 lane).
- Gates: tools/gates.py with planted mutants (S2 lane).
- Catalogue sources (`catalogue/*.src`), `tools/build_catalogue.py`, `tools/route_code.py`.

# CU W0 — operator page (srf-cu-* CLIs and gates)

Pin: vision-engine tag `ve-sealed-1-law1` = `65a8469266c7fceef599689fbf899691cd1c88b9`, wheel sha256 `41e798d6208681f029c9ad5136a47b4e56dcb22f4884279df5d60b6b8a12a22b` (`config/vision.json`, `vision/pin.py`; mismatch = rc 5 `E_PIN`, never re-pin).
Exit codes (SPEC_INT R4): 0 ok, 2 usage/schema, 3 anchor/STOP file/io, 4 stale frame, 5 gate/denied, 6 budget/too large, 7 human needed. Never 1. Table: `src/cubook/rc/rc.go`.
STORAGE LAW: frames only under `/dev/shm/vqa-ramws/opensuperficies/os-cu-0/<run>/`; disk keeps manifests/receipts. Run every gate under `taskset -c 0,1`.

## CLIs (build: `go build ./cmd/srf-cu-snap ./cmd/srf-cu-exec ./cmd/srf-cu-mcp`)
| CLI | usage |
|---|---|
| `srf-cu-snap` | `--fake-fb \| --fb Xvfb_screen0 --display :100 --in-capsule --iso HEX32`; `--frames 1..64`; `--ramws-dir D` (session dir from the shim, holds `frames.raw`); `--out E` (manifest + `capture_receipt.json`, never under `/dev/shm`); `--ramws-root`, `--app`, `--scene`. Host display or ISO mismatch = rc 5. |
| `srf-cu-exec` | `validate --plan P` · `replay --plan P --steps S --frames F [--out O]` · `run --plan P --frames F --out S [--deny D] [--stop-file X] [--mock]`. STOP file = rc 3; irrev = rc 7; deny-list/match-teeth/key/shell/stuck = rc 5. |
| `srf-cu-mcp` | stdio JSON-RPC: `--registry R --cas-dir D [--fake F]... [--scratch-dir S] [--confirm-destructive]`. Endpoints other than `127.0.0.1:19876..19883` (and 9876/9877/8770) = rc 5 `operator_session`; content > 256 KiB = rc 6. |
| `vision/ramws_shim.py` | `sweep` · `sweep-kept --build B` · `status` · `check-disk DIR [--since NS]` · `session --run-id ID [--quota-bytes N]` (stdin JSON lines `path\|reserve\|account\|release\|receipt\|close`). |
| `vision/build_wheel.py --check` | rebuilds the wheel with `SOURCE_DATE_EPOCH` and compares sha + member equivalence. |

## Gates (`scripts/cu/`)
| script | what |
|---|---|
| `gates.sh` | bundle: first `vision/build_wheel.py` (builds the pinned wheel into `build/wheels/` from the sealed tag, so a fresh checkout needs no manual step; sha mismatch = rc 5 `E_PIN`) then `build_wheel.py --check` (read-only re-verify), then `bash -n`, suite plan, `tests/test_gates.py`, `g_disk_frames.py`, `g_disk_size.py`, `run_mutants.py`. rc 0/2/5. |
| `g_disk_frames.py` | smoke (`srf-cu-snap --fake-fb` x8 + `srf-cu-exec` replay) into `build/gate_disk/<pid>/`; fails rc 5 on any png/raw/xwd/`frames.raw`/`Xvfb_screen0` newer than the stamp, any leftover in `/dev/shm/vqa-ramws/opensuperficies`, `ram_peak_bytes > quota`. |
| `g_disk_size.py --out D` | total <= 64 MB; each `fail_evidence/<ep>` <= 100 KB and <= 6 files; `kept_frames/*` need a live `KEEP.json`. |
| `run_suite.sh [--plan]` | janitor (`ramws_shim.py sweep`) first, then `go test -p 2 -count=1 -timeout 30s` minus `pbx` (needs live OpenSwitchboard) and `grabador` (host Xvfb, >74 s) with `manager` at `-p 1`, plus cmd/srf-cu-*, `vision/tests`, `scripts/cu/tests`. Failure rc 5. |
| `run_mutants.py [--only ID]` | applies `mutants.json` (22 mutants: 10 INT `os.*.01..10`, FX2 `os.exec.xtest.M19b/M20b`, `os.mcp.x3.MX1..MX10`) in a temp copy under `build/gate_disk/`, each must be killed by its oracle. |

Engine needs: `docs/ENGINE_REQUESTS.md` (ER-001..ER-006). Live capsule smoke (`--memory 4g`) is a separate gated step (S-LIVE), not part of these gates.

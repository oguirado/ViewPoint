# CU W1 — operator page (campaign os-cu-0, wave 1)

Delta over `docs/CU_W0.md`. Every command runs under `PYTHONDONTWRITEBYTECODE=1 taskset -c 0,1 timeout N` (Go `-p 2 -count=1`, Python `-B`). Exit codes never 1 (0,2..7).

Pin: vision-engine tag `ve-sealed-1-law2` = commit `15714b62225caec064235ff341335a378d462d8e`, wheel sha256 `ebcf3854d14ab77e1ae7b6d65fd88ed07e417cef920e3c2b7f6b4149e7416e89` (`config/vision.json`, `vision/pin.py`, `vision/VENDORED.json`). law1 is refused (rc 5 `E_PIN`). `vision/build_wheel.py` builds from `git archive <tag>` (never the VE worktree, `PIP_NO_INDEX=1`); `--check` must print `equal:ebcf3854`.

## New / changed CLIs and flags
| CLI | usage |
|---|---|
| `srf-cu-exec run` (prod) | `--plan P --out S --fb F --ramws-dir D --iso HEX32`; refuses (rc 5, 0 events) unless in the capsule, `DISPLAY=:100` and the root-window `_OPENNEWTON_ISOLATION_ID` == `VQA_ISO`. Fake path: `OS_CU_TEST=1 ... --frames F --mock` only. |
| `srf-cu-mcp` | prod GUI executor needs `--fb --ramws-dir --deny-list`; on gate failure it stays `Unwired`. `--fake`, `--mock-gui`, `--l2-fake` all require `OS_CU_TEST=1`. |
| `capsule/astcheck/astcheck.py` | `[--out-dir D] < script` -> JSON verdict; rc 0 allow, 5 `ast_denied`, 2 usage. Closed allowlist (`allowlist.json`); stdlib only. Go side: `src/cubook/mcp/astgate.go` (`ASTGate`), live script tools run only inside the capsule. |
| `vision/ingest.py` | `[--run-id ID] [--store DIR] [--frames N] [--receipt-out FILE]`; synthetic capture in RAM -> `GuiAdapter` (`rgb24_raw`) -> analyze -> `vmem ingest`; receipt `os.cu.engine-receipt/v1`; ramws released before return. |
| `bench/cu/harness.py` | `--dry-run bench/cu/prereg_w1.json` prints `prereg_sha256` (`e72dae2b…9851`). `--run --split cal\|test --resource-ok F --expect-sha SHA` refused (rc 5) on missing file, sha drift, task-file tamper, or `--split test` on a dirty tree. No measurement exists in W1. |
| `bench/wl/recorder.py` | `validate\|order <manifest>` · `plan\|labels <manifest> <WL-N> <w> <h> [default\|qtile]`; frames only under the ramws root, else rc 5. Manifest `bench/wl/prereg_wl.json`. |

## Gates (`scripts/cu/`)
`gates.sh` order: `wheel`, `wheel-check`, `sweep`, `bash-n`, `suite-plan`, `unit`, `ast`, `engine`, `bench-tests`, `bench-dry`, `wl-tests`, `wl-validate`, `disk-frames`, `disk-size`, `mutants`. Each gate runs under `taskset -c 0,1`. `g_disk_frames.py` sets `OS_CU_TEST=1` for the fake `srf-cu-exec run --frames` smoke and also scans `testdata/cu` for `.raw/.xwd/.rgb24/frames.rgb24/Xvfb_screen0`. `run_mutants.py` COPY now includes `capsule` and `bench`. `mutants.json`: 36 mutants incl. RD-02 `os.mcp.fx3.RDm1/RDm2` and one per W1 lane (`os.pin.law1.11`, `os.prod.gate.12`, `os.prod.fake.13`, `os.ast.nonascii.14`, `os.ast.dunder.15`, `os.ast.bypass.16`, `os.engine.order.17`, `os.bench.seeds.18`, `os.bench.prereg.19`, `os.ingest.sha.20`, `os.wl.label.21`, `os.wl.order.22`). `run_suite.sh` adds `py-astcheck`, `py-bench-cu`, `py-bench-wl`.

## S-LIVE prerequisites (conductor, gated; nothing here is run by the gates)
1. `RESOURCE os-cu-0 W1 S-LIVE` approval from the meta-orchestrator (1 capsule `opensuperficies-desktop-arm64:1.0.0`, `--memory 4g`, `--cpuset-cpus 0,1`, `--network none`, wall <= 15 min, ramws <= 2 GiB); no `docker run` before `OK`.
2. `python3 vision/ramws_shim.py sweep` rc 0; session `run_id=slive-w1`.
3. Launch with the `capture.RunArgs` vector; mount binaries ro, the canonical ramws session rw and a disk `--out` for receipts only. Run `srf-cu-snap` / `srf-cu-exec` INSIDE the capsule via `docker exec` (no `--in-capsule` flag exists).
4. Assert `docker inspect -f '{{.HostConfig.NetworkMode}}'` == `none`; two static snaps equal `det_sha256`; teardown `docker rm -f`; `/dev/shm/vqa-ramws/opensuperficies/` empty; `ramws_shim.py check-disk build/ --since NS` rc 0.
5. WL captures: WL-6 first, `wm_profile` `default` (qtile waits for an operator-approved image build, W2). 4K at 60 fps for 5 s needs ~7.5 GB RAM: cap or ring it against the 2 GiB quota.

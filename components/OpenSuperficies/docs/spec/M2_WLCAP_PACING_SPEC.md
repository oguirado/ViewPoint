# Milestone M2 — Chunked 60 FPS Capture Driver & Pacing Specification (`srf-cu-wlcap`)

## 1. Source Files and Verification Hashes
The following source files and contracts from branch `bridge/integration` define the capture driver implementation:

- `cmd/srf-cu-wlcap/main.go`: `92d283a0567629b6b4a3a832a1d6c62c711ea1d7c99b40f5acdc1d57ab85e6b9`
- `cmd/srf-cu-wlcap/main_test.go`: `2972471638f87743bb40161821517eaff45f7b01371e0b64851dd170df95a92a`
- `src/cubook/capture/livedriver.go`: `a5893f3c5efedb49573555b7d0f7809b44315b442227ab36dce316ade2573018`
- `src/cubook/capture/livedriver_test.go`: `78aba987aa039d1968c0d691629a63af1acbbac7104aaeb4b7d9bf8d119695c1`
- `src/cubook/capture/analyzer.go`: `734c3948e9deb7adb45da2863d62bc4494ace29adfc75a53e2d05adb67391197`
- `src/cubook/capture/ring.go`: `27115ce11a135bc010fd720785e9533c9b212231d1d4ff6b27c9cc778d3657a3`
- `contracts/cubook/schemas/os.cu.wlcap-start-v1.schema.json`: `2ac3b9be6a036449264b090ebb9383ea8849b48aca654862daa87141fca8f0c5`
- `contracts/cubook/schemas/os.cu.wlcap-receipt-v1.schema.json`: `72a5cf1d50de20a89c65d5094c84e8c7b672d9df82169100cb9538160e86434d`
- `contracts/cubook/schemas/os.cu.wlcap-bench-v1.schema.json`: `1e19787bc1eba4a28bbae0393baf8f09335532e1288ecda5541d09d81a8e1e8f`
- `docs/CU_KNOWN_LIMITATIONS.md`: `f417f47be86590b1cec82a1aa5db6604a657a292cf8bf5612115e197b714a816`

## 2. Driver Operational Modes, Flag Rules, and Refusals
The capture driver supports four operational modes (`src/cubook/capture/livedriver.go:25-28`):
- `wl`: Workload capture mode. Requires `--det on` (`src/cubook/capture/livedriver.go:250-252`). Default `--require-continuous` is enabled (`cmd/srf-cu-wlcap/main.go:81`, `src/cubook/capture/livedriver.go:784-789`).
- `live`: Live capsule streaming mode. Defaults to `--det off` (`cmd/srf-cu-wlcap/main.go:66`). Explicit `--det on` is refused (`src/cubook/capture/livedriver.go:245-247`). For 4K resolution (W*H > 1920*1080), `--analyze` is strictly required (`src/cubook/capture/livedriver.go:247-249`).
- `handover-1s`: Handover single-second burst mode. Requires resolution exactly 3840x2160 (`src/cubook/capture/livedriver.go:253-255`), frame count exactly 60 (`src/cubook/capture/livedriver.go:256-257`), `--keep true` implicitly (`src/cubook/capture/livedriver.go:257`), and quota <= 2 GiB (`src/cubook/capture/livedriver.go:258-259`).
- `bench`: Pacing measurement mode. Nothing is retained, analyzed, handed over, or started (`src/cubook/capture/livedriver.go:260-266`). Setting `--keep` (`src/cubook/capture/livedriver.go:260-262`) or `--analyze` (`src/cubook/capture/livedriver.go:263-265`) is refused. Writes an `os.cu.wlcap-bench/v1` receipt (`src/cubook/capture/livedriver.go:840-866`).

Flag parsing rules and default parameters (`cmd/srf-cu-wlcap/main.go:57-82`):
- `--mode`: string, default `"live"` (`cmd/srf-cu-wlcap/main.go:65`).
- `--det`: string `"on"|"off"`, default `"off"` (`cmd/srf-cu-wlcap/main.go:66`). Invalid values return exit code 2 (`cmd/srf-cu-wlcap/main.go:107-109`).
- `--analyze`: string `"off"|"engine"|"host"`, default `"off"` (`cmd/srf-cu-wlcap/main.go:77`). Invalid values return exit code 2 (`cmd/srf-cu-wlcap/main.go:110-112`).
- `--analyze-wait-ms`: int64 in range [1, 600000], default 30000 ms (`cmd/srf-cu-wlcap/main.go:78`). Out of range values return exit code 2 (`cmd/srf-cu-wlcap/main.go:113-115`).
- `--src`: string `"readat"|"mmap"|"xshm"`, default `"readat"` (`cmd/srf-cu-wlcap/main.go:76`). Invalid values return exit code 2 (`cmd/srf-cu-wlcap/main.go:116-118`).
- `--require-continuous`: boolean flag, defaults to `true` for `wl` mode, `false` otherwise (`src/cubook/capture/livedriver.go:784-789`).
- `--keep`: boolean flag, default `false`. When enabled, chunks are kept resident up to `--ttl-seconds` (`src/cubook/capture/livedriver.go:267-273`).
- `--quota-bytes`: int64, default 2 GiB (`DefaultQuota = 2 << 30`, `src/cubook/capture/livedriver.go:20`). Maximum quota is 8 GiB (`MaxQuota = 8 << 30`, `src/cubook/capture/livedriver.go:23`). Exceeding quota produces exit code 5 (`src/cubook/capture/livedriver.go:270-272`).
- `--ttl-seconds`: int64, default 7200 seconds when kept (`src/cubook/capture/livedriver.go:22,268-270`). Maximum allowed TTL is 7200 seconds (`src/cubook/capture/livedriver.go:271-273`).
- `--fake-fb`: boolean flag. Synthetic framebuffer source requiring `OS_CU_TEST=1` in the environment (`cmd/srf-cu-wlcap/main.go:59,137-140`).

## 3. Pacer Mechanics, Frame Deadlines, Overruns, and Gaps
The driver paces capture at 60 fps using a fixed step interval:
- Constant frame interval `FrameNs = 16_666_667` ns (`src/cubook/capture/livedriver.go:18`).
- Monotonic clock source `monoNs()` retrieves nanoseconds via `unix.ClockGettime(unix.CLOCK_MONOTONIC, &ts)` (`cmd/srf-cu-wlcap/main.go:226-232`).
- `Pacer` maintains state (`delta`, `last`, `acc`, `cur`, `started`) (`src/cubook/capture/livedriver.go:34-39`).
- Step interval must satisfy `0 < delta <= 2^40` nanoseconds (`src/cubook/capture/livedriver.go:46-48`).
- `Pacer.Tick(nowNs)` computes elapsed nanoseconds `f = nowNs - p.last` (`src/cubook/capture/livedriver.go:53-60`), total accumulation `t = acc + f`, step count `k = t / delta`, and remainder `acc = t % delta` (`src/cubook/capture/livedriver.go:61-62`).
- Render slot index advances by `cur += k` (`src/cubook/capture/livedriver.go:66`).
- Overrun count: `missing = k - 1` steps skipped before the current frame (`src/cubook/capture/livedriver.go:67`).
- Gap logging: When render slot index `idx > lastIdx + 1`, a gap record `Gap{After: lastIdx, Missing: idx - lastIdx - 1}` is added to `res.Gaps` (`src/cubook/capture/livedriver.go:512-517`).
- Tail overruns: When the loop finishes before reaching the target frame count, missing tail slots are recorded as a gap (`src/cubook/capture/livedriver.go:497-504`).
- Conservation Law: `Published == Frames + Overrun + Dropped` (`src/cubook/capture/livedriver.go:585`). If conservation fails, `Run` aborts with exit code 5 (`src/cubook/capture/livedriver.go:585-588`).

## 4. RAM Workspace Directory Layout, Chunk Directories, File Modes, Release, and TTL Reaper
All raw frame storage is isolated to the RAM workspace directory:
- Base run directory provided via `--ramws-dir` (`cmd/srf-cu-wlcap/main.go:61`).
- Storage law: Raw frames reside strictly under `/dev/shm` (ramws); no frame file ever touches persistent disk (`src/cubook/capture/ring.go:104`, `docs/CU_KNOWN_LIMITATIONS.md:12`).
- Process umask: `syscall.Umask(0o027)` set at program entry (`cmd/srf-cu-wlcap/main.go:19`).
- Chunk directory naming: Subdirectories created as `<ramws-dir>/chunk_<k>` with directory mode `0o770` (`src/cubook/capture/livedriver.go:461`).
- Raw frame file: Created as `<ramws-dir>/chunk_<k>/frames.rgb24` with file mode `0o660` (`src/cubook/capture/livedriver.go:465`).
- Ring buffer file mode: `FileMode = 0o640` (`src/cubook/capture/ring.go:12`).
- Marker file mode: `0o640` (`src/cubook/capture/analyzer.go:351`, `src/cubook/capture/livedriver.go:682`).
- Chunk Release Policy:
  - Default releaser function performs `os.RemoveAll(chunkDir)` (`src/cubook/capture/livedriver.go:361-364`).
  - Concurrent residency limit: Without `--keep`, at most 2 chunks remain resident concurrently (`src/cubook/capture/livedriver.go:283-288,453-460`).
  - Sequential release: Analysis worker releases chunk `k-2` upon completion (`src/cubook/capture/livedriver.go:403-413`).
  - Abort release: On run error, `releaseAll()` frees all chunks, `handover.json`, and `START` (`src/cubook/capture/livedriver.go:472-485`).
- Handover TTL and Reaper:
  - Kept runs write `<ramws-dir>/handover.json` containing `{"ttl_until": now + TTLSeconds}` with file mode `0o660` (`src/cubook/capture/livedriver.go:589-599`).
  - `--reap` flag executes `ReapExpired(runDir, nowSec)` (`cmd/srf-cu-wlcap/main.go:93-106`, `src/cubook/capture/livedriver.go:751-780`).
  - When `nowSec >= ttl_until`, `ReapExpired` deletes all `chunk_*` subdirectories and `handover.json` (`src/cubook/capture/livedriver.go:767-778`).

## 5. START Marker Contract
The driver writes a synchronization marker for the actuator:
- Marker file path: `<ramws-dir>/START` (`src/cubook/capture/analyzer.go:207`, `src/cubook/capture/livedriver.go:670`).
- Written on the first due tick (render slot index 0) if `WriteStart` is enabled and mode is not `bench` (`src/cubook/capture/livedriver.go:505-510,666-697`).
- Atomic creation: Written via `<ramws-dir>/START.tmp` and renamed (`src/cubook/capture/livedriver.go:678-687`). File mode `0o640` (`src/cubook/capture/livedriver.go:682`).
- Size cap: 4096 bytes (`docs/CU_KNOWN_LIMITATIONS.md:32`).
- Schema ID: `os.cu.wlcap-start/v1` (`contracts/cubook/schemas/os.cu.wlcap-start-v1.schema.json:1-49`).
- Schema keys (`src/cubook/capture/livedriver.go:675-677`):
  - `schema`: `"os.cu.wlcap-start/v1"`
  - `clock`: `"CLOCK_MONOTONIC"`
  - `t0_ns`: int64 nanosecond timestamp of render slot 0
  - `delta_ns`: int64 frame step (16,666,667 ns)
  - `frames`: int64 requested frame count
  - `w`: int width
  - `h`: int height
  - `chunk_frames`: int frames per chunk
  - `mode`: string mode name
  - `run_nonce`: 128-bit hex string (32 hex characters)
- Run Nonce generation: Generated via `crypto/rand` (`src/cubook/capture/livedriver.go:811-817`). Entropy failure yields an empty string and produces exit code 5 (`cmd/srf-cu-wlcap/main.go:131-133`, `src/cubook/capture/livedriver.go:365-368`).

## 6. Host Analyzer Handshake (READY and ANALYZED)
When running with `--analyze host`, the driver interacts with a host analyzer via file markers:
- Protocol markers (`src/cubook/capture/analyzer.go:207-210`):
  - Driver writes `<chunkDir>/READY`.
  - Host writes `<chunkDir>/ANALYZED` or `<chunkDir>/ANALYZE_FAILED`.
- Atomic marker writing: Written via `<name>.tmp` and renamed with file mode `0o640` (`src/cubook/capture/analyzer.go:349-366`).
- Secure file descriptors: `openMarker` opens files using `O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC` (`src/cubook/capture/analyzer.go:332-336`).
- File validation: `syscall.Fstat` verifies regular file (`S_IFREG`), single hard link (`st_nlink == 1`), and non-zero size within bounds (`src/cubook/capture/analyzer.go:338-341`).
- Size caps (`src/cubook/capture/analyzer.go:21,214,293,299`):
  - `READY`: 64 KiB (`analyzeOutMax = 64 << 10`).
  - `ANALYZED`: 64 KiB (`analyzeOutMax = 64 << 10`).
  - `ANALYZE_FAILED`: 4096 bytes (`hostMarkerMax = 4096`).
- Anti-replay and Stale Protection:
  - Driver checks for pre-existing `ANALYZED` or `ANALYZE_FAILED` markers before publishing `READY`; if present, returns exit code 5 `marker_preexisting` (`src/cubook/capture/analyzer.go:261-265`).
  - Marker modification time must satisfy `mtime >= readyNs`; stale markers are rejected (`src/cubook/capture/analyzer.go:300-302,324`).
  - Nonce matching: Marker JSON must echo `run_nonce`, or driver returns exit code 5 `marker_replay` / `analyze_failed` (`src/cubook/capture/analyzer.go:187-192,324-327`).
- Bounded Wait and Timeout:
  - Bounded wait per chunk: `--analyze-wait-ms` in range [1, 600000] ms, default 30000 ms (`cmd/srf-cu-wlcap/main.go:78,113-115`, `src/cubook/capture/analyzer.go:212,274-279`).
  - Poll loop: Period defaults to 2 ms (`src/cubook/capture/analyzer.go:213,280`).
  - Timeout outcome: Missing marker after deadline records `AnalyzeRC = 4` and `AnalyzeTimeout = 1` (`src/cubook/capture/analyzer.go:310-313`). The pacer loop continues uninterrupted (`src/cubook/capture/analyzer.go:204-206`).

## 7. Receipt Schemas and Fields
At run completion, evidence receipts are written to `--out`:
- Output receipt file: `<OutDir>/receipt.json` (`src/cubook/capture/livedriver.go:668,855`).
- Standard Receipt Schema: `os.cu.wlcap-receipt/v1` (`contracts/cubook/schemas/os.cu.wlcap-receipt-v1.schema.json:1-134`). Fields: `analyze`, `analyze_timeouts`, `capture_us_p50`, `capture_us_p95`, `chunk_frames`, `chunks`, `continuous_required`, `delta_ns`, `det`, `dropped`, `frames`, `gaps`, `height`, `keep`, `longest_run_ms`, `mode`, `overrun`, `peak_resident_bytes`, `published`, `quota_bytes`, `requested`, `schema`, `segment_ms`, `span_ms`, `src`, `status`, `width`, `write_us_per_frame`.
- Bench Receipt Schema: `os.cu.wlcap-bench/v1` (`contracts/cubook/schemas/os.cu.wlcap-bench-v1.schema.json:1-89`). Fields: `capture_us_p50`, `capture_us_p95`, `det`, `dropped`, `frames`, `gaps`, `height`, `longest_run_ms`, `overrun`, `peak_resident_bytes`, `published`, `requested`, `resident_end_bytes`, `schema`, `span_ms`, `src`, `status`, `width`, `write_us_per_frame`.
- Schema difference: `resident_end_bytes` is present only in `os.cu.wlcap-bench/v1` (`contracts/cubook/schemas/os.cu.wlcap-bench-v1.schema.json:49`).
- Associated evidence files in `--out`:
  - `rows.jsonl`: per-chunk metadata records (`src/cubook/capture/livedriver.go:672-678`).
  - `gaps.jsonl`: overrun/gap records (`src/cubook/capture/livedriver.go:679-685`).
  - Bench mode writes ONLY `receipt.json` (`src/cubook/capture/livedriver.go:840-866`).

## 8. Non-Retention Policy
The capture subsystem enforces strict data non-retention:
- Bench Mode (`ModeBench`): Pacing measurement only; zero frames retained, analyzed, handed over, or started (`src/cubook/capture/livedriver.go:260-266`, `src/cubook/capture/livedriver_bench_test.go:35-54`).
- Unkept Runs (`Keep == false`): Chunks are released immediately after analysis/hashing completes (`src/cubook/capture/livedriver.go:403-413`), keeping at most 2 concurrent resident chunks (`src/cubook/capture/livedriver.go:283-288,453-460`).
- Aborted/Failed Runs: `releaseAll()` removes all chunk directories, `handover.json`, and `START` (`src/cubook/capture/livedriver.go:472-485`).
- RAM Workspace Law: All frame buffer storage resides in `/dev/shm` (ramws); zero frame files are permitted on persistent disk (`src/cubook/capture/ring.go:104`, `docs/CU_KNOWN_LIMITATIONS.md:12`).

## 9. Replacement Implementation Invariants Table
A non-Go implementation of M2 `srf-cu-wlcap` must preserve all of the following invariants:

| Invariant Description | Covering Test (or AUSENTE) | Code Reference |
|---|---|---|
| CLI exit codes never return exit code 1 | `TestNoRc1` | `cmd/srf-cu-wlcap/main_test.go:20-44` |
| Driver clock monoNs matches CLOCK_MONOTONIC | `TestMonoNsIsClockMonotonic` | `cmd/srf-cu-wlcap/main_test.go:91-99` |
| Flag validation returns exit code 2 for usage errors | `TestWlcapSrcFlagNoRc1` | `cmd/srf-cu-wlcap/main_test.go:46-67` |
| Host analyzer wait flag limits enforced (1..600000ms) | `TestWlcapAnalyzeHostFlags` | `cmd/srf-cu-wlcap/main_test.go:69-89` |
| Pacer accumulates steps without back-filling lost slots | `TestPacerOverrunMakesRenderIndexGap` | `src/cubook/capture/livedriver_test.go:104-114` |
| Frame conservation law Published == Frames + Overrun + Dropped | `TestConservationPublishedEqualsFramesPlusOverrun` | `src/cubook/capture/livedriver_test.go:163-194` |
| Unkept runs release chunks sequentially keeping max 2 resident | `TestChunkReleasedBeforeNextBeyondTwo` | `src/cubook/capture/livedriver_test.go:128-161` |
| RequireContinuous failure produces status segment_gappy and rc 5 | `TestSegmentGappyRc5WhenContinuousRequired` | `src/cubook/capture/livedriver_w3_test.go:65-84` |
| Kept run writes handover.json and retains chunks up to quota | `TestKeptRunAlwaysWritesTTLMarker` | `src/cubook/capture/livedriver_test.go:388-408` |
| Handover TTL clock overflow triggers failure | `TestTTLCapAndOverflowSafeDeadline` | `src/cubook/capture/livedriver_test.go:560-583` |
| Handover TTL exceeding 7200s is refused | `TestTTLCapAndOverflowSafeDeadline` | `src/cubook/capture/livedriver_test.go:560-583` |
| ReapExpired releases expired chunks when nowSec >= ttl_until | `TestTTLExpiryReleasesRun` | `src/cubook/capture/livedriver_test.go:370-386` |
| Bench mode retains nothing, writes bench receipt, 0 resident | `TestBenchNeverKeeps` | `src/cubook/capture/livedriver_bench_test.go:35-54` |
| Run writing START creates os.cu.wlcap-start/v1 file | `TestStartFileT0BeforeFrame0` | `src/cubook/capture/livedriver_w3_test.go:365-385` |
| Host analyzer writes READY, consumes ANALYZED, validates schema | `TestHostHandshakeReadyAnalyzedThenRelease` | `src/cubook/capture/analyzer_test.go:205-246` |
| Host analyzer timeout produces analyze_rc 4 and analyze_timeout 1 | `TestHostNoAnswerBoundedWaitNoStall` | `src/cubook/capture/analyzer_test.go:270-300` |
| Pre-existing ANALYZED or ANALYZED_FAILED returns rc 5 marker_preexisting | `TestHostAnalyzeFailedReleasesRc5` | `src/cubook/capture/analyzer_test.go:248-268` |
| Mismatched or missing run_nonce in marker returns rc 5 | `TestAnalyzedWrongNonceRc5` | `src/cubook/capture/analyzer_m_test.go:133-169` |
| OpenMarker open flags reject symlinks, FIFOs, and oversized files | `TestMarkerFifoNoBlock` | `src/cubook/capture/analyzer_m_test.go:31-57` |
| Percentile calculation xkUs matches nearest rank order statistic | `TestBenchXkPercentiles` | `src/cubook/capture/livedriver_bench_test.go:102-116` |
| Ring buffer stores 4 raw RGB24 frames with 0o640 file mode | `TestRingNeverCreatesRunDir` | `src/cubook/capture/capture_test.go:492-502` |

## 10. Refusals and Error Exit Codes Table
The driver defines the following refusal exit codes and reasons:

| Refusal Condition / Reason | Exit Code (rc) | Code Reference |
|---|---|---|
| Invalid flag argument syntax / CLI parse failure | 2 | `cmd/srf-cu-wlcap/main.go:83-86` |
| Invalid `--det` value (not `on` or `off`) | 2 | `cmd/srf-cu-wlcap/main.go:107-109` |
| Invalid `--analyze` value (not `off`, `engine`, or `host`) | 2 | `cmd/srf-cu-wlcap/main.go:110-112` |
| `--analyze-wait-ms` out of range (< 1 ms or > 600,000 ms) | 2 | `cmd/srf-cu-wlcap/main.go:113-115` |
| Invalid `--src` value (not `readat`, `mmap`, or `xshm`) | 2 | `cmd/srf-cu-wlcap/main.go:116-118` |
| Invalid frame / seconds specification or missing required arguments | 2 | `cmd/srf-cu-wlcap/main.go:119-128` |
| Synthetic `--fake-fb` used without `OS_CU_TEST=1` environment variable | 2 | `cmd/srf-cu-wlcap/main.go:137-140` |
| Invalid mode string | 2 | `src/cubook/capture/livedriver.go:267` |
| Mode `live` requested with `--det on` | 2 | `src/cubook/capture/livedriver.go:245-247` |
| Mode `wl` requested with `--det off` | 2 | `src/cubook/capture/livedriver.go:250-252` |
| Mode `live` 4K resolution (W*H > 1920*1080) without `--analyze` | 2 | `src/cubook/capture/livedriver.go:247-249` |
| Mode `handover-1s` with resolution other than 3840x2160 | 2 | `src/cubook/capture/livedriver.go:253-255` |
| Mode `handover-1s` with frame count other than 60 | 2 | `src/cubook/capture/livedriver.go:256-257` |
| Mode `bench` requested with `--keep` | 2 | `src/cubook/capture/livedriver.go:260-262` |
| Mode `bench` requested with `--analyze` | 2 | `src/cubook/capture/livedriver.go:263-265` |
| `--analyze` enabled without an analysis hook function | 2 | `src/cubook/capture/livedriver.go:275` |
| Ramws run directory not under configured ramws root | 5 | `cmd/srf-cu-wlcap/main.go:97-99` |
| Run entropy failure / empty `run_nonce` | 5 | `cmd/srf-cu-wlcap/main.go:130-133` |
| Mode `handover-1s` quota exceeds 2 GiB | 5 | `src/cubook/capture/livedriver.go:258-259` |
| Quota exceeds maximum allowed 8 GiB | 5 | `src/cubook/capture/livedriver.go:270-272` |
| Kept run TTL exceeds maximum 7200 seconds | 5 | `src/cubook/capture/livedriver.go:271-273` |
| 4K resolution chunk size exceeds 30 frames | 5 | `src/cubook/capture/livedriver.go:273-275` |
| Retention (`--keep`) requested for non-handover-1s 4K run | 5 | `src/cubook/capture/livedriver.go:276-278` |
| Ramws quota exceeded by resident frame memory | 5 | `src/cubook/capture/livedriver.go:290-293` |
| Output directory filesystem check failure (not mounted/permitted) | 5 | `src/cubook/capture/livedriver.go:300-303` |
| Run directory path guard check failure | 5 | `src/cubook/capture/livedriver.go:304-307` |
| Frame analysis failure (`analyze_failed`) | 5 | `src/cubook/capture/analyzer.go:103-112` |
| Pre-existing host analysis marker found (`marker_preexisting`) | 5 | `src/cubook/capture/analyzer.go:261-265` |
| Marker `run_nonce` mismatch or replay (`marker_replay`) | 5 | `src/cubook/capture/analyzer.go:135-140` |
| Frame conservation law `Published == Frames + Overrun + Dropped` violated | 5 | `src/cubook/capture/livedriver.go:585-588` |
| Required continuous capture lost frame (`segment_gappy`) | 5 | `src/cubook/capture/livedriver.go:600-609` |
| Continuous capture segment below minimum length (`segment_short`) | 5 | `src/cubook/capture/livedriver.go:602-611` |

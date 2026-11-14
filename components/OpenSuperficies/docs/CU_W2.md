# Computer Use (cubook) — W2 operator page (campaign os-cu-0, wave 2)

Pin: `ve-sealed-1-law4` (commit `2612b3ac`, wheel `5e2395d5…a4a5`). Law3, law2 and law1 are refused (fail closed, rc 5 `E_PIN`).
Gates: `taskset -c 0,1 timeout 900 bash scripts/cu/gates.sh` (rc 0 = all gates + every mutant KILLED, 0 INVALID); `bash scripts/cu/run_suite.sh` (legacy exclusions KL-07).

## Capsule as uid 10001
The image user is 10001 (`docker/ng/desktop/Dockerfile`). `capture.RunArgs` carries NO `--user`, the tenant tmpfs is `/run/newton-tenant:rw,nosuid,nodev,mode=0700,uid=10001,gid=0`,
`RunConfig.RamDir` is bind-mounted at the SAME path rw, `ToolsDir` at `/opt/cu` ro, `OutDir` at `/out` rw (receipts only). `docker/cu/entrypoint-cu.sh` preflight exits 4
(`uid_mismatch`, `tenant_not_owned`, unwritable or missing fb) before dbus. The run dir is shared only through the engine:
`python3 vision/ramws_shim.py session --run-id R --share-uid 10001` → `open_run(share_with_uid=10001)`; a pinned ramws without the keyword fails rc 5
`E_RAM_SHARE_UNSUPPORTED`; the shim never chmods or setfacls. The shim receipt merges the engine share `{uid, method}` with `via`.
Not exercised live until S-LIVE v2 (KL-01, KL-10); `.lock`/`owner.json` readable by 10001 is ER-011 (REQUESTED).

## srf-cu-wlcap (60 fps chunked driver)
`srf-cu-wlcap --ramws-dir D --out O (--fb F | --fake-fb --w W --h H) --mode wl|live|handover-1s --det on|off --seconds S|--frames N --chunk 60 [--keep]
[--quota-bytes Q] [--min-segment-ms M] [--ttl-seconds T] | --reap`. Integer-ns pacer (Δ=16,666,667 ns): an overrun makes a `render_index` gap (`gaps.jsonl`, never back-filled);
P = F + O in `receipt.json`. At most 2 chunks resident, each released after its hash.
- `wl`: `--det on` (hashed per closed chunk in a worker, never in the tick), segment >= 5,000 ms else rc 5 `segment_short`.
- `live`: `--det off`.
- Kept runs (`--keep`, any mode) always get `handover.json` with a TTL (default 7200 s); failed kept runs release their chunks; `--reap` must be scheduled at capture+2 h (it is a call, not a timer, KL-12). A tail overrun is logged as a gap row and `receipt.json` carries `requested` and `published`.
- `handover-1s`: 4K, 60 frames (~1.39 GiB), retained with a hard TTL of 2 h (`handover.json`; `--reap` releases it); any other 4K retention is refused rc 5.
- Quotas (STORAGE LAW): default 2 GiB per run, max 8 GiB explicit, 16 GiB host-wide, `/dev/shm` floor 8 GiB; 1080p WL-6 uses an explicit 3 GiB quota; configs whose resident bytes exceed the quota fail rc 5 `E_RAM_QUOTA` before capture. 4K chunk <= 30 frames.
`bench/wl/driver.py --item WL-6 --wm-profile default|qtile --det on --res 1920x1080 --chunk 60 --seconds 6 --out O --ramws-dir D` runs items in prereg `capture_order` (WL-6 first),
writes `labels/<item>.jsonl` (enum `NOT_CHANGED`, prereg sha `505aac99…cd58` asserted) and `wl_handover.json {run_ids, label_files, prereg_sha256}`; non-ramws dirs are refused.

INT5 closure (RJ2-01..04, ADC2-W2-01..04): `--ttl-seconds` above 7200 is refused rc 5 `ttl_exceeded`; a `now + ttl` that would wrap is refused rc 5 `ttl_clock_range`; `--reap` surfaces non-ENOENT `handover.json` read errors (rc 5 `handover_read`); a gap after a closed chunk counts on the last chunk (`sum(chunk gaps) == len(gaps)`); a failed chunk release keeps its bytes resident (`Result.ResidentEnd`). `bench/wl/driver.py` refuses `--out` on any `tmpfs`/`ramfs` mount (read from `/proc/self/mountinfo`, fail closed) and the qtile attestation rejects blank strings. `gates.sh` fails `wheel-check` unless the witness starts `equal:5e2395d5`.

## qtile profile
Data only: `bench/wl/qtile/layout_1080p.json`, `layout_4k.json` (integer MonadTall 0.5 rects), `docker/cu/profiles/qtile.env` (`WM=qtile`). `--wm-profile qtile` is refused rc 5
`image_layer_missing` unless `--image-has-qtile FILE` is a JSON attestation with the closed keys `schema` (`os.cu.qtile-image-attestation/v1`), `image`, `digest` (`sha256:<64 hex>`), `qtile_version`, `approval_id` (operator-approved image build pending, KL-14). No docker build in W2.

## Checker (ADW3-01)
`capsule/astcheck/astcheck.py` denies stored callables (`stored_callable`), zero-arg path calls (`path_call_without_path`), `filepath_raw` strings and module-attribute assignment. It stays a lint inside the capsule (KL-04).

## Mutants
`scripts/cu/mutants.json` carries the W1 set plus W2 behaviour mutants (`os.pin.*.R*`, `os.uid.*.U*`, `os.live.*.D*`, `os.ast.*.K*`, `os.qtile.*.Q*`); each is precheck-compiled and must be KILLED by an assertion.

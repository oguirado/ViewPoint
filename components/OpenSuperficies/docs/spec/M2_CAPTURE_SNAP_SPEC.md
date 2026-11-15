# M2 Capture Snap Specification

## Source SHAs
- `cmd/srf-cu-snap/main.go`: `a17c9a80d65bd94ef1b596d448dcc5fff1872d2d13281774d1f4fec26560dca8`
- `cmd/srf-cu-snap/main_test.go`: `d705c0c319788df1e37c66057b4c9f99ffa74b9906e057497767c9c53366e9d8`
- `cmd/srf-cu-snap/main_w3_test.go`: `6bc4203b73985e5e93549c5df26b1f11cd24077d57253c5ad3e092e906cf5cb0`
- `src/cubook/capture/fbsrc.go`: `99a2bdd58b9d2a0969b33434b061c1c535481a84678aceaefd9891aecb290cea`
- `src/cubook/capture/fbsrc_test.go`: `1de182eac62abf32e1713b45161cc93706778a5e55e4effb1c6fdda9a784dbd4`
- `src/cubook/capture/xwd.go`: `7e3426378e4e15716b24edaea73fb69fd6ab317fb73d9f9de16ca9cd87727384`
- `src/cubook/capture/snap.go`: `4963c29dcd155fd14452ab1852ac7c1fe3ff43783921bd82563590c007f0d703`
- `src/cubook/capture/guard.go`: `eafd88af27d4577a55f05386c209b80f13bd10e89f39d1b47ea54184f0644317`
- `src/cubook/capture/runargs.go`: `9e84e413d23cab67d15c0c9f504cc11292d95ee9d5435a5c39d9f782505c1215`
- `contracts/cubook/schemas/os.cu.capture-receipt-v1.schema.json`: `bbd2b64645e24e92e10523008db59287a917eee545bff27dcb49df49223f7d34`
- `docs/CU_KNOWN_LIMITATIONS.md`: `f417f47be86590b1cec82a1aa5db6604a657a292cf8bf5612115e197b714a816`

## 1. CLI Flags, Exit Codes, and Refusals

The `srf-cu-snap` CLI utility captures raw RGB24 frames from the CU capsule framebuffer or synthetic fixture into a RAM workspace directory (`cmd/srf-cu-snap/main.go:1-20`).
Process execution returns standard package exit codes from `src/cubook/rc` (`rc.OK` = 0, `rc.Usage` = 2, `rc.Schema` = 2, `rc.IO` = 4, `rc.Gate` = 5, `rc.Stale` = 7), and process exit code 1 is never produced (`cmd/srf-cu-snap/main.go:22-31`).

The supported command-line flags parsed by `srf-cu-snap` are (`cmd/srf-cu-snap/main.go:45-53`):
- `--fake-fb`: boolean flag indicating whether to use a synthetic in-memory XWD framebuffer (`cmd/srf-cu-snap/main.go:45`).
- `--fb`: string path to `Xvfb_screen0` memory-mapped framebuffer file (`cmd/srf-cu-snap/main.go:46`).
- `--frames`: integer count of frames to capture, constrained between 1 and 64 (`cmd/srf-cu-snap/main.go:47`).
- `--ramws-dir`: string path to the RAM workspace run directory where frames and metadata are generated (`cmd/srf-cu-snap/main.go:48`).
- `--out`: string path to the disk evidence output directory where manifest and receipt are written (`cmd/srf-cu-snap/main.go:49`).
- `--display`: string X display identifier, requiring `:100` inside the capsule netns (`cmd/srf-cu-snap/main.go:50`).
- `--iso`: expected VQA isolation ID hex string (`cmd/srf-cu-snap/main.go:51`).
- `--app`: manifest scope application identifier, defaulting to `cockpit` (`cmd/srf-cu-snap/main.go:52`).
- `--scene`: manifest scope scene identifier, defaulting to `idle` (`cmd/srf-cu-snap/main.go:53`).

Flag parsing or flag combinations are validated with usage refusal `args` (`rc` 2) under any of the following conditions (`cmd/srf-cu-snap/main.go:63-65`):
- `--frames` is less than 1 or greater than 64 (`cmd/srf-cu-snap/main.go:63`).
- `--ramws-dir` is empty (`cmd/srf-cu-snap/main.go:63`).
- `--out` is empty (`cmd/srf-cu-snap/main.go:63`).
- Both `--fake-fb` and `--fb` are set, or neither is set (`cmd/srf-cu-snap/main.go:63`).

Additionally, passing `--fake-fb` without environment variable `OS_CU_TEST=1` causes usage refusal `fake_requires_test_env` (`rc` 2) (`cmd/srf-cu-snap/main.go:66-68`).

## 2. Xvfb Framebuffer File Access (mmap vs ReadAt)

The framebuffer source abstracts frame reading across memory-mapped files (`readat`/`mmap`) and synthetic buffers (`cmd/srf-cu-snap/main.go:76`, `src/cubook/capture/fbsrc.go:106-128`).
`NewMmapSource` opens the underlying framebuffer file descriptor and maps its bytes via `syscall.Mmap` with protection `PROT_READ` and flags `MAP_SHARED` (`src/cubook/capture/fbsrc.go:20-41`).
Memory mapping issues exactly 1 system call during initialization rather than per-frame file read operations (`src/cubook/capture/fbsrc.go:21-22`, `src/cubook/capture/fbsrc_test.go:68`).

Bound check helper `mmapInBounds(o, l, m uint64)` uses `InBounds(o, l, m)` to confirm that offset `o` plus length `l` does not exceed mapped size `m` without integer addition overflow (`src/cubook/capture/fbsrc.go:16`, `src/cubook/capture/fbsrc_test.go:46`).
If the file size is smaller than the required 100-byte XWD header or pixel array length, frame access fails with IO error `pixels_out_of_bounds` (`src/cubook/capture/fbsrc.go:34-36`, `src/cubook/capture/fbsrc.go:47-50`).

In non-mmap mode (`readat`), frame capture reads raw bytes at offset `h.PixelOffset` using `io.ReaderAt` (`src/cubook/capture/snap.go:81-99`).

## 3. XWD Header Parsing and Bounds Checking

XWD dump headers are parsed from the first 100 bytes of the file via `ParseXWDHeader` (`src/cubook/capture/xwd.go:16-49`).
All big-endian 32-bit fields are read and validated (`src/cubook/capture/xwd.go:28-49`):
- `HeaderSize`: must be at least 100 bytes (`xwdFixedBytes`) (`src/cubook/capture/xwd.go:30-32`).
- `PixmapFormat`: must be `2` (ZPixmap) (`src/cubook/capture/xwd.go:38-41`).
- `PixmapDepth`: must be `24` or `32` (`src/cubook/capture/xwd.go:38-41`).
- `BitsPerPixel`: must be `32` (`src/cubook/capture/xwd.go:38-41`).
- `VisualClass`: must be `7` (TrueColor / DirectColor) (`src/cubook/capture/xwd.go:38-41`).
- `RedMask`: must match `0x00FF0000` (`src/cubook/capture/xwd.go:38-41`).
- `GreenMask`: must match `0x0000FF00` (`src/cubook/capture/xwd.go:38-41`).
- `BlueMask`: must match `0x000000FF` (`src/cubook/capture/xwd.go:38-41`).

Geometry validation rejects headers where width/height are out of bounds (1..16384) or where bytes-per-line (`BytesPerLine`) is less than `width * 4` (`src/cubook/capture/xwd.go:42-45`).
Invalid fields return schema refusals `xwd_header_short`, `xwd_format_unsupported`, or `xwd_geometry` (`src/cubook/capture/xwd.go:31`, `src/cubook/capture/xwd.go:40`, `src/cubook/capture/xwd.go:44`).

During multi-frame capture, `Snapper.Snap()` verifies that framebuffer width and height remain identical across consecutive frames, returning stale refusal `geometry_changed` (`rc` 7) if dimensions change (`src/cubook/capture/snap.go:86-88`).

## 4. BGRX to RGB Conversion Loop and Stride Rules

XWD ZPixmap frame data stores pixels in 32-bit BGRX layout (`src/cubook/capture/xwd.go:51-61`).
`BGRXToRGB24(dst, src []byte, w, h, bpl int)` transforms raw BGRX source bytes into 24-bit RGB destination bytes (`src/cubook/capture/xwd.go:51-61`).

For each row `y` from 0 to `h-1`:
- Source row slice is indexed at `y * bpl` (`src/cubook/capture/xwd.go:53`).
- Destination row slice is indexed at `y * w * 3` (`src/cubook/capture/xwd.go:54`).
- For each pixel `x` from 0 to `w-1`:
  - Pixel source byte 0 (Blue) maps to `dst[x*3 + 2]` (`src/cubook/capture/xwd.go:57`).
  - Pixel source byte 1 (Green) maps to `dst[x*3 + 1]` (`src/cubook/capture/xwd.go:56`).
  - Pixel source byte 2 (Red) maps to `dst[x*3 + 0]` (`src/cubook/capture/xwd.go:55`).
  - Pixel source byte 3 (Pad / X) is skipped (`src/cubook/capture/xwd.go:51-61`).

Bounds checking is hoisted using full 3-element slice expressions `row[x*4 : x*4+3 : x*4+3]` and `out[x*3 : x*3+3 : x*3+3]` to eliminate inner-loop slicing checks (`src/cubook/capture/fbsrc.go:71-75`).

## 5. Deterministic Hash Computation

Deterministic SHA-256 computation (`det_sha256`) generates a hash over captured pixel frame data (`src/cubook/capture/snap.go:39-78`, `cmd/srf-cu-snap/main_test.go:59-90`).
The hash calculation covers raw frame dimensions and converted RGB24 pixel byte sequences (`cmd/srf-cu-snap/main_test.go:59-90`).

Per STORAGE LAW L10, `det_sha256` covers frame geometry and pixel bytes across captured frames, explicitly omitting file paths, host identifiers, run nonces, or timestamps (`contracts/cubook/schemas/os.cu.capture-receipt-v1.schema.json:1-72`).

## 6. Receipt Fields and Storage Law

Capture output produces `capture_receipt.json` adhering to JSON schema `os.cu.capture-receipt-v1.schema.json` (`contracts/cubook/schemas/os.cu.capture-receipt-v1.schema.json:1-72`).
The receipt contains:
- `schema`: `"os.cu.capture-receipt/v1"` (`contracts/cubook/schemas/os.cu.capture-receipt-v1.schema.json:30`).
- `app`: string application name (`contracts/cubook/schemas/os.cu.capture-receipt-v1.schema.json:34-37`).
- `scene`: string scene name (`contracts/cubook/schemas/os.cu.capture-receipt-v1.schema.json:38-41`).
- `det_sha256`: 64-character hex SHA-256 digest (`contracts/cubook/schemas/os.cu.capture-receipt-v1.schema.json:42-46`).
- `frame_count`: integer count of captured frames (`contracts/cubook/schemas/os.cu.capture-receipt-v1.schema.json:47-50`).
- `width` and `height`: integer frame dimensions (`contracts/cubook/schemas/os.cu.capture-receipt-v1.schema.json:51-58`).

STORAGE LAW L10: Raw frames and in-memory frame buffers MUST remain strictly in volatile memory (`/dev/shm` tmpfs or ramfs mounts) and MUST NEVER touch persistent disk storage (`cmd/srf-cu-snap/main.go:1-20`, `src/cubook/capture/guard.go:42-125`).
Conversely, the evidence output directory specified by `--out` MUST reside on persistent disk and MUST NOT be created under `/dev/shm` or any RAM-backed filesystem (`cmd/srf-cu-snap/main.go:49`, `src/cubook/capture/guard.go:105-125`).

## 7. xshm Source and KL-22 Limitation

The `xshm` frame source option relies on MIT-SHM shared memory access via an injected `ShmGetter` interface (`src/cubook/capture/fbsrc.go:13`, `src/cubook/capture/fbsrc.go:91-128`).
`ShmGetter` defines the method `GetImage(dst []byte) error` (`src/cubook/capture/fbsrc.go:13`).

Without an injected `ShmGetter` implementation, calling `OpenSource("xshm", ...)` or `XshmSource.Frame(...)` immediately returns gate refusal `xshm_unavailable` with exit code `rc.Gate` (5) (`src/cubook/capture/fbsrc.go:100-103`, `src/cubook/capture/fbsrc.go:124-126`).

Per `docs/CU_KNOWN_LIMITATIONS.md` KL-22:
- MIT-SHM is unmeasured: `--src xshm` returns `rc` 5 `xshm_unavailable` unless an in-process `ShmGetter` is injected (`docs/CU_KNOWN_LIMITATIONS.md:28`).

## 8. Umask and File Permissions

Process execution forces file mode creation mask `0o027` at entry via `syscall.Umask(0o027)` (`cmd/srf-cu-snap/main.go:42`).
Directory creation for output path `--out` uses explicit directory permissions `0o750` (`cmd/srf-cu-snap/main.go:107`).
JSON files are written with explicit file mode `0o640` (`cmd/srf-cu-snap/main.go:38`).
In-memory run workspace directories enforce `0o700` directory permissions and `0o600` file permissions (`cmd/srf-cu-snap/main_test.go:100-109`).

## 9. Capsule Run Arguments Constraining Capture

Capsule execution environment arguments enforced by `RunConfig.Validate()` and built by `RunArgs()` impose security and operational constraints (`src/cubook/capture/runargs.go:13-164`):
- Non-root user execution: Capsule runs under fixed user ID `CapsuleUID = 10001` (`src/cubook/capture/runargs.go:28`). User flags (`-u`, `--user`) in extra arguments are strictly forbidden and return refusal `user_flag` (`src/cubook/capture/runargs.go:31`, `src/cubook/capture/runargs.go:53-73`).
- CPU core pinning: `--cpuset-cpus` is restricted to allowlisted sets (`"0,1"` or `"10-14"`), defaulting to `"0,1"`; unauthorized core sets return refusal `cpuset_not_allowed` (`src/cubook/capture/runargs.go:34-37`, `src/cubook/capture/runargs.go:81-133`).
- Container security isolation: `RunArgs` enforces `--network none`, `--read-only`, `--cap-drop ALL`, `--security-opt no-new-privileges`, and `--pids-limit 256` (`src/cubook/capture/runargs.go:135-164`).
- Isolation ID check: `VQA_ISO` must be a valid 32-character hex string (`src/cubook/capture/runargs.go:81-133`).
- Ramws path confinement: `RamDir` must be a direct child of `DefaultRamRoot` (`/dev/shm/vqa-ramws/opensuperficies/os-cu-0`); violations return `ramdir_outside_ramws` (`src/cubook/capture/guard.go:14`, `src/cubook/capture/runargs.go:81-133`).
- Framebuffer dir confinement: `FBDir` must be a descendant of `RamDir` and `DefaultRamRoot`; violations return `fb_outside_ramdir` (`src/cubook/capture/runargs.go:81-133`).
- Evidence path separation: `OutDir` must not reside under `/dev/shm` or RAM-backed storage (`tmpfs`/`ramfs`); violations return `out_in_ram` (`src/cubook/capture/guard.go:105-125`, `src/cubook/capture/runargs.go:81-133`).
- Forbidden mounts: Paths containing forbidden host resources (`docker.sock`, `/tmp/.X11-unix`, `/run/user`, `/dev/dri`, `/dev/uinput`, `:`) are refused with `forbidden_mount` (`src/cubook/capture/runargs.go:81-133`).

## 10. Invariants Table

| Invariant Description | Implementation Citation | Existing Test Citation |
| :--- | :--- | :--- |
| Umask is set to `0o027` at process startup; output dirs `0o750`, JSON files `0o640` | `cmd/srf-cu-snap/main.go:42`, `cmd/srf-cu-snap/main.go:107`, `cmd/srf-cu-snap/main.go:38` | `cmd/srf-cu-snap/main_w3_test.go:11` |
| `--fake-fb` requires environment variable `OS_CU_TEST=1` | `cmd/srf-cu-snap/main.go:66-68` | `cmd/srf-cu-snap/main_test.go:198` |
| Real display capture allows only `:100` and requires running inside capsule netns | `src/cubook/capture/guard.go:180-185` | `cmd/srf-cu-snap/main_test.go:187` |
| Evidence output dir `--out` must not be on RAM-backed filesystem (`tmpfs`/`ramfs`) | `src/cubook/capture/guard.go:105-125` | `cmd/srf-cu-snap/main_test.go:45` |
| `--ramws-dir` must be a managed directory with valid `owner.json` and held `.lock` | `src/cubook/capture/guard.go:63-100` | `cmd/srf-cu-snap/main_test.go:140` |
| XWD Header validation requires `BitsPerPixel` == 32, `VisualClass` == 7, valid masks | `src/cubook/capture/xwd.go:38-41` | `AUSENTE` |
| Memory mapping `MmapSource` issues exactly 1 system call across lifetime | `src/cubook/capture/fbsrc.go:21-22`, `src/cubook/capture/fbsrc.go:41` | `src/cubook/capture/fbsrc_test.go:68` |
| `MmapSource` matches `XWDSource` (`ReadAt`) frame conversion byte-for-byte | `src/cubook/capture/fbsrc.go:59-79`, `src/cubook/capture/xwd.go:51-61` | `src/cubook/capture/fbsrc_test.go:22` |
| Range check `mmapInBounds` uses addition-free integer check `InBounds` | `src/cubook/capture/fbsrc.go:16` | `src/cubook/capture/fbsrc_test.go:46` |
| Frame capture freeze watchdog thaws server and returns `freeze_watchdog` on timeout | `src/cubook/capture/snap.go:39-78` | `AUSENTE` |
| `det_sha256` computes SHA-256 over frame geometry and RGB24 pixel bytes | `src/cubook/capture/snap.go:39-78` | `cmd/srf-cu-snap/main_test.go:59` |
| `xshm` source without injected `ShmGetter` returns `rc` 5 `xshm_unavailable` | `src/cubook/capture/fbsrc.go:100-103`, `src/cubook/capture/fbsrc.go:124-126` | `src/cubook/capture/fbsrc_test.go:105` |
| Capsule run args validate cpuset against allowlist (`"0,1"`, `"10-14"`) | `src/cubook/capture/runargs.go:34-37`, `src/cubook/capture/runargs.go:81-133` | `AUSENTE` |
| Capsule execution forbids custom user flags (`-u`, `--user`), enforcing UID 10001 | `src/cubook/capture/runargs.go:28`, `src/cubook/capture/runargs.go:53-73` | `AUSENTE` |

## 11. Refusals Table

| Refused Input Condition | Exit Code (`rc`) | Reason String | Implementation Citation |
| :--- | :--- | :--- | :--- |
| Missing required flags or invalid flag values (`--frames` out of range 1..64, empty `--ramws-dir`, empty `--out`, or conflicting `--fake-fb`/`--fb`) | 2 | `args` | `cmd/srf-cu-snap/main.go:63-65` |
| `--fake-fb` specified without `OS_CU_TEST=1` environment variable | 2 | `fake_requires_test_env` | `cmd/srf-cu-snap/main.go:66-68` |
| Display argument is not `:100` or process is running outside capsule netns | 5 | `host_display` | `src/cubook/capture/guard.go:180-185` |
| Isolation ID hex string does not match `_OPENNEWTON_ISOLATION_ID` | 5 | `iso_mismatch` | `src/cubook/capture/guard.go:188-192` |
| Ramws run directory is not under `DefaultRamRoot` or lacks tmpfs/ramfs mount | 5 | `E_RAM_NOT_TMPFS` | `src/cubook/capture/guard.go:42-60` |
| Ramws run directory is missing `owner.json` or live session `.lock` flock | 5 | `unmanaged_run_dir` | `src/cubook/capture/guard.go:63-100` |
| Evidence directory `--out` resolves under `/dev/shm` or onto RAM-backed filesystem | 2 | `out_in_ram` | `src/cubook/capture/guard.go:105-125` |
| Framebuffer file truncation or pixel data extending past file size mapping | 4 | `pixels_out_of_bounds` | `src/cubook/capture/fbsrc.go:34-36`, `src/cubook/capture/fbsrc.go:47-50` |
| Framebuffer header shorter than 100 bytes fixed size | 2 | `xwd_header_short` | `src/cubook/capture/xwd.go:30-32` |
| Unsupported XWD format (visual != 7, bpp != 32, depth != 24/32, or bad masks) | 2 | `xwd_format_unsupported` | `src/cubook/capture/xwd.go:38-41` |
| Invalid XWD geometry (header size, colors, width/height bounds, stride) | 2 | `xwd_geometry` | `src/cubook/capture/xwd.go:42-45` |
| Frame geometry dimensions change between consecutive captures | 7 | `geometry_changed` | `src/cubook/capture/snap.go:86-88` |
| Framebuffer freeze duration exceeds 50 ms watchdog budget | 5 | `freeze_watchdog` | `src/cubook/capture/snap.go:39-78` |
| `xshm` source selected without an injected `ShmGetter` implementation | 5 | `xshm_unavailable` | `src/cubook/capture/fbsrc.go:100-103`, `src/cubook/capture/fbsrc.go:124-126` |
| Capsule run configuration specifies slot outside range 0..7 | 2 | `slot_range` | `src/cubook/capture/runargs.go:81-133` |
| Capsule run configuration contains invalid isolation ID hex string | 2 | `iso_format` | `src/cubook/capture/runargs.go:81-133` |
| Capsule run configuration extra args contain custom user flag (`-u`/`--user`) | 5 | `user_flag` | `src/cubook/capture/runargs.go:53-73` |
| Capsule run configuration extra args contain non-allowlisted environment key or option | 5 | `forbidden_arg` | `src/cubook/capture/runargs.go:53-73` |
| Capsule run configuration cpuset is not in allowlist (`"0,1"`, `"10-14"`) | 5 | `cpuset_not_allowed` | `src/cubook/capture/runargs.go:81-133` |
| Capsule run configuration contains forbidden host path mount | 5 | `forbidden_mount` | `src/cubook/capture/runargs.go:81-133` |

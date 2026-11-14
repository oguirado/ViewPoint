# RAM Frame Ring and RAM Workspace Specification

Normative byte-level and protocol specification derived exclusively from `vadapt/rtring.py`, `vadapt/ramws.py`, and `AGENTS.md` at ref `bridge/integration`. Any behavior, field, or constraint not defined in those bound source files is marked `ABSENT`.

---

## 1. Repository Layering and Storage Law Context (`AGENTS.md`, `vadapt/ramws.py`, `vadapt/rtring.py`)

| Rule / Property | Specification | Source |
|---|---|---|
| Storage Law | Frames never touch persistent disk; all frame buffers reside in RAM (`tmpfs` or `ramfs`) via `vadapt/ramws.py`, enforced by gate `g_disk_frames`. | `AGENTS.md:56`, `vadapt/ramws.py:1`, `vadapt/rtring.py:1` |
| `vadapt/ramws.py` imports | Python standard library only (`atexit`, `fcntl`, `json`, `os`, `re`, `shutil`, `signal`, `stat`, `subprocess`, `sys`, `threading`, `time`, `pathlib.Path`). Imports neither `visioncore` nor `vmem` nor third-party packages (enforced by gate `g_layer`). | `vadapt/ramws.py:1-17`, `AGENTS.md:15-25` |
| `vadapt/rtring.py` imports | Python standard library (`logging`, `mmap`, `os`, `struct`, `zlib`, `pathlib.Path`), `numpy`, and `vadapt.ramws`. | `vadapt/rtring.py:24-33` |
| Exit code convention | `0` OK, `2` usage/schema, `3` reject, `4` stale, `5` gate/law failure, `6` budget; never `1`. CLI emits one canonical JSON object on `stdout`. | `AGENTS.md:37`, `vadapt/ramws.py:3`, `vadapt/ramws.py:594-605` |
| Catalogue anchors in `rtring.py` / `ramws.py` | `ABSENT` (neither `vadapt/rtring.py` nor `vadapt/ramws.py` contains `# @module` or `# @anchor:` comments). | `vadapt/rtring.py:1-323`, `vadapt/ramws.py:1-610`, `AGENTS.md:11-12`, `AGENTS.md:42` |

---

## 2. RAM Frame Ring Specification (`vadapt/rtring.py`)

### 2.1 Overview and Constants

`vadapt/rtring.py` implements a single-producer single-consumer (SPSC) ring of raw `rgb24` (`uint8[height, width, 3]`) frames stored in a single memory-mapped file inside a `RamWorkspace` (`vadapt/rtring.py:1`, `vadapt/rtring.py:133-134`, `vadapt/rtring.py:170-172`).

| Constant / Symbol | Type / Value | Meaning | Source |
|---|---|---|---|
| `LOG` | `logging.Logger` | Logger named `"vadapt.rtring"` | `vadapt/rtring.py:35` |
| `MAGIC` | `bytes` (`8 B`) | `b"VQARING1"` (`0x56 0x51 0x41 0x52 0x49 0x4e 0x47 0x31`) | `vadapt/rtring.py:4`, `vadapt/rtring.py:36` |
| `VERSION` | `int` (`u32`) | `2` | `vadapt/rtring.py:37` |
| `HDR` | `int` | `64` (global header size in bytes) | `vadapt/rtring.py:4`, `vadapt/rtring.py:38` |
| `SLOT_HDR` | `int` | `64` (per-slot header stride and alignment in bytes) | `vadapt/rtring.py:6`, `vadapt/rtring.py:39` |
| `ALIGN` | `int` | `4096` (byte alignment of `data_off`) | `vadapt/rtring.py:8`, `vadapt/rtring.py:40` |
| `MASK` | `int` (`u64`) | `(1 << 64) - 1` (`0xFFFFFFFFFFFFFFFF`, 64-bit wraparound mask for `seq`) | `vadapt/rtring.py:41` |
| `REL` | `str` | `"ring/frames.rgb24"` (default path relative to `ws.root`) | `vadapt/rtring.py:42`, `vadapt/rtring.py:84` |
| `_G` | `struct.Struct` | `struct.Struct("<8sIIIIQQQ")` (`48 B`, little-endian global header fields) | `vadapt/rtring.py:43` |
| `_Q` | `struct.Struct` | `struct.Struct("<Q")` (`8 B`, little-endian `u64`) | `vadapt/rtring.py:44` |
| `_QQ` | `struct.Struct` | `struct.Struct("<QQ")` (`16 B`, two little-endian `u64` values) | `vadapt/rtring.py:45` |
| `_I` | `struct.Struct` | `struct.Struct("<I")` (`4 B`, little-endian `u32`) | `vadapt/rtring.py:46` |
| `CRC_OFF` | `int` | `16` (byte offset of `crc32` inside each 64-byte slot header) | `vadapt/rtring.py:47` |
| `HEAD_OFF` | `int` | `40` (byte offset of `head` inside the 64-byte global header) | `vadapt/rtring.py:48` |

### 2.2 File Geometry and Size Formulas

All multi-byte integers in the file header and slot headers are encoded in **little-endian** byte order (`<`) (`vadapt/rtring.py:3`, `vadapt/rtring.py:43-46`).

1. **Slot Payload Size (`slot_bytes`)**:
   $$\text{slot\_bytes} = \text{width} \times \text{height} \times 3$$
   (`vadapt/rtring.py:56`, `vadapt/rtring.py:90`).
2. **Payload Region Offset (`data_off(slots)`)**:
   $$\text{data\_off}(\text{slots}) = \left\lfloor \frac{\text{HDR} + \text{slots} \times \text{SLOT\_HDR} + \text{ALIGN} - 1}{\text{ALIGN}} \right\rfloor \times \text{ALIGN} = \left\lfloor \frac{64 + 64 \times \text{slots} + 4095}{4096} \right\rfloor \times 4096$$
   (`vadapt/rtring.py:51-52`, `vadapt/rtring.py:91`).
3. **Total Ring File Size (`ring_bytes(slots, w, h)`)**:
   $$\text{ring\_bytes}(\text{slots}, w, h) = \text{data\_off}(\text{slots}) + \text{slots} \times w \times h \times 3$$
   (`vadapt/rtring.py:55-56`, `vadapt/rtring.py:92`).
4. **Slot Mapping for `render_index = r`**:
   $$k = r \bmod \text{slots}$$
   - Slot `k` header offset: $\text{HDR} + k \times \text{SLOT\_HDR} = 64 + 64k$ (`vadapt/rtring.py:6`, `vadapt/rtring.py:143-146`, `vadapt/rtring.py:244-245`).
   - Slot `k` payload offset: $\text{data\_off} + k \times \text{slot\_bytes}$ (`vadapt/rtring.py:8`, `vadapt/rtring.py:133-134`, `vadapt/rtring.py:256-257`).

### 2.3 Global Header Layout (`0..64`, `HDR = 64` Bytes)

Packed and unpacked at file offset `0` via `_G = struct.Struct("<8sIIIIQQQ")` (`vadapt/rtring.py:4-5`, `vadapt/rtring.py:43`, `vadapt/rtring.py:113`, `vadapt/rtring.py:227`):

| Byte Offset | Size (B) | Struct / Rust Type | Field Name | Initial Value / Semantics | Source |
|---|---|---|---|---|---|
| `0..8` | `8` | `8s` / `[u8; 8]` | `magic` | `b"VQARING1"` (`MAGIC`). Validated by reader on open. | `vadapt/rtring.py:4`, `vadapt/rtring.py:36`, `vadapt/rtring.py:113`, `vadapt/rtring.py:227-230` |
| `8..12` | `4` | `<I` / `u32` | `version` | `2` (`VERSION`). Validated by reader on open. | `vadapt/rtring.py:4`, `vadapt/rtring.py:37`, `vadapt/rtring.py:113`, `vadapt/rtring.py:227-230` |
| `12..16` | `4` | `<I` / `u32` | `slots` | Number of ring slots (`> 0`). | `vadapt/rtring.py:4`, `vadapt/rtring.py:85`, `vadapt/rtring.py:113`, `vadapt/rtring.py:227` |
| `16..20` | `4` | `<I` / `u32` | `width` | Frame width in pixels (`> 0`). | `vadapt/rtring.py:4`, `vadapt/rtring.py:85`, `vadapt/rtring.py:113`, `vadapt/rtring.py:227` |
| `20..24` | `4` | `<I` / `u32` | `height` | Frame height in pixels (`> 0`). | `vadapt/rtring.py:4`, `vadapt/rtring.py:85`, `vadapt/rtring.py:113`, `vadapt/rtring.py:227` |
| `24..32` | `8` | `<Q` / `u64` | `slot_bytes` | `width * height * 3` (bytes per frame payload). | `vadapt/rtring.py:5`, `vadapt/rtring.py:90`, `vadapt/rtring.py:113`, `vadapt/rtring.py:227` |
| `32..40` | `8` | `<Q` / `u64` | `data_off` | `data_off(slots)` (4096-aligned byte offset to slot `0` payload). | `vadapt/rtring.py:5`, `vadapt/rtring.py:51-52`, `vadapt/rtring.py:113`, `vadapt/rtring.py:227` |
| `40..48` | `8` | `<Q` / `u64` | `head` | At `HEAD_OFF = 40`: `0` when empty; otherwise `newest_committed_render_index + 1`. | `vadapt/rtring.py:5`, `vadapt/rtring.py:48`, `vadapt/rtring.py:113`, `vadapt/rtring.py:154`, `vadapt/rtring.py:237-239` |
| `48..64` | `16` | `[u8; 16]` | *(padding / unused)* | Zero-filled by `os.ftruncate(fd, total)`; explicit field definition is `ABSENT`. | `vadapt/rtring.py:38`, `vadapt/rtring.py:43`, `vadapt/rtring.py:105` |

### 2.4 Slot Header Layout (`64 + 64*k .. 64 + 64*(k+1)`, `SLOT_HDR = 64` Bytes)

For each slot $k \in \{0, \dots, \text{slots}-1\}$, the slot header starts at file byte offset $\text{off}_k = \text{HDR} + k \times \text{SLOT\_HDR} = 64 + 64k$ (`vadapt/rtring.py:6-7`, `vadapt/rtring.py:114-115`, `vadapt/rtring.py:244-245`):

| Slot Offset | File Offset | Size (B) | Struct / Rust Type | Field Name | Initial Value / Semantics | Source |
|---|---|---|---|---|---|---|
| `+0..+8` | `64 + 64*k` | `8` | `<Q` / `u64` | `seq` | Seqlock counter: even = stable/idle, odd = write in progress. Initialized to `_seq0` (default `0`). Wrapped modulo $2^{64}$ via `& MASK`. | `vadapt/rtring.py:6`, `vadapt/rtring.py:41`, `vadapt/rtring.py:84`, `vadapt/rtring.py:115`, `vadapt/rtring.py:145`, `vadapt/rtring.py:153` |
| `+8..+16` | `64 + 64*k + 8` | `8` | `<Q` / `u64` | `render_index` | Frame `render_index` stored in slot `k`. Initialized to `0`. | `vadapt/rtring.py:6`, `vadapt/rtring.py:115`, `vadapt/rtring.py:146`, `vadapt/rtring.py:250` |
| `+16..+20` | `64 + 64*k + 16` | `4` | `<I` / `u32` | `crc32` | At `CRC_OFF = 16`: unsigned 32-bit `zlib.crc32` of slot `k` payload (`slot_bytes` bytes), written before the even `seq`. Initialized to `0` by `os.ftruncate`. | `vadapt/rtring.py:7`, `vadapt/rtring.py:47`, `vadapt/rtring.py:105`, `vadapt/rtring.py:152`, `vadapt/rtring.py:251`, `vadapt/rtring.py:260` |
| `+20..+64` | `64 + 64*k + 20` | `44` | `[u8; 44]` | *(padding / unused)* | Zero-filled by `os.ftruncate(fd, total)`; explicit field definition is `ABSENT`. | `vadapt/rtring.py:6`, `vadapt/rtring.py:39`, `vadapt/rtring.py:105` |

### 2.5 Inter-Header Padding and Frame Payload Layout

- **Alignment Padding (`64 + 64*slots .. data_off`)**: `data_off(slots) - (64 + 64 * slots)` bytes (between `0` and `4032` bytes), zero-initialized by `os.ftruncate` (`vadapt/rtring.py:51-52`, `vadapt/rtring.py:105`).
- **Frame Payload (`data_off + k*slot_bytes .. data_off + (k+1)*slot_bytes`)**: C-contiguous row-major unsigned 8-bit array (`dtype=np.uint8`) with shape `(height, width, 3)` (`rgb24`) and byte length `slot_bytes = width * height * 3` (`vadapt/rtring.py:8`, `vadapt/rtring.py:90`, `vadapt/rtring.py:132-134`, `vadapt/rtring.py:256-257`).

### 2.6 Memory Mapping Helpers (`_map`, `_unmap`)

1. **`_map(fd, size, write)` (`vadapt/rtring.py:59-71`)**:
   - Sets `prot = mmap.PROT_READ | (mmap.PROT_WRITE if write else 0)` (`vadapt/rtring.py:60`).
   - Sets `flags = mmap.MAP_SHARED | getattr(mmap, "MAP_POPULATE", 0)` (`vadapt/rtring.py:61`).
   - Creates `mm = mmap.mmap(fd, size, flags=flags, prot=prot)` (`vadapt/rtring.py:62`).
   - Initializes `advice = "unavailable"`. If `hasattr(mmap, "MADV_HUGEPAGE")`, calls `mm.madvise(mmap.MADV_HUGEPAGE)` and sets `advice = "ok"`; if `madvise` raises `OSError as e`, sets `advice = "failed: %s" % e` and logs `LOG.warning("madvise(MADV_HUGEPAGE) failed: %s", e)` (`vadapt/rtring.py:63-70`).
   - Returns `(mm, advice)` (`vadapt/rtring.py:71`).
2. **`_unmap(mm)` (`vadapt/rtring.py:74-78`)**:
   - Calls `mm.close()`. If `BufferError` is raised (live numpy views still reference `mm`), catches `BufferError` and logs `LOG.warning("ring closed with live numpy views; unmap deferred to GC")` (`vadapt/rtring.py:75-78`).

### 2.7 Writer Protocol (`RingWriter`) Step by Step

#### 2.7.1 Initialization (`RingWriter.__init__(self, ws, slots, width, height, rel=REL, _seq0=0)`)

Defined at `vadapt/rtring.py:84-115`:

1. **Validate dimensions and slot count**: Checks `all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in (slots, width, height))`. If false, raises `ValueError("slots/width/height must be positive ints")` (`vadapt/rtring.py:85-86`).
2. **Validate initial sequence number**: Checks `if _seq0 & 1:`. If odd, raises `ValueError("seq0 must be even")` (`vadapt/rtring.py:87-88`).
3. **Compute sizes and resolve workspace path**:
   - `self.slot_bytes = width * height * 3` (`vadapt/rtring.py:90`).
   - `self.data_off = data_off(slots)` (`vadapt/rtring.py:91`).
   - `total = self.data_off + slots * self.slot_bytes` (`vadapt/rtring.py:92`).
   - `self.path = ws.path(rel)` — validates `rel` against workspace escape rules and creates parent directories inside `ws.root` (`vadapt/rtring.py:93`).
   - Initializes `self.size = total`, `self._rel = rel`, `self.index = -1`, `self._open_slot = None`, `self._closed = False`, `self._released = False` (`vadapt/rtring.py:94-95`).
4. **Create file exclusively before reserving quota**:
   - Calls `fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)` (`vadapt/rtring.py:97`).
   - If the file already exists at `self.path`, `FileExistsError` (`OSError`) is raised before `ws.reserve(total)` is called (`vadapt/rtring.py:96-97`).
5. **Reserve workspace quota**:
   - Calls `ws.reserve(total)` (`vadapt/rtring.py:100`).
   - On any `BaseException` (including `ramws.RamError("E_RAM_QUOTA", ...)`), calls `self._drop_file()` (`self.ws.release(self.path)`, suppressing `ramws.RamError` and `OSError`) and re-raises (`vadapt/rtring.py:101-103`, `vadapt/rtring.py:117-121`).
6. **Allocate file size and map into memory**:
   - Calls `os.ftruncate(fd, total)` and `self._mm, self.advice = _map(fd, total, True)` (`vadapt/rtring.py:105-106`).
   - On any `BaseException`, calls `self._drop_file()` and `self._resync()` (`self.ws.account()`, suppressing `ramws.RamError` and `OSError`, which resets `ws.used` to actual `_tree_bytes(ws.root)` since `release()` only subtracts `st_blocks * 512`), then re-raises (`vadapt/rtring.py:107-110`, `vadapt/rtring.py:123-127`).
7. **Close file descriptor**:
   - `os.close(fd)` always runs in the `finally` block (`vadapt/rtring.py:111-112`).
8. **Write initial global and slot headers**:
   - Writes global header at offset `0`: `_G.pack_into(self._mm, 0, MAGIC, VERSION, slots, width, height, self.slot_bytes, self.data_off, 0)` (`head = 0`) (`vadapt/rtring.py:113`).
   - For each `k` in `range(slots)`, writes `(seq, render_index) = (_seq0, 0)` at `HDR + k * SLOT_HDR`: `_QQ.pack_into(self._mm, HDR + k * SLOT_HDR, _seq0, 0)` (`vadapt/rtring.py:114-115`).

#### 2.7.2 Publishing Frames (`begin`, `commit`, `publish`, `_begin`, `_copy`, `_commit`)

Defined at `vadapt/rtring.py:129-176`:

1. **Beginning a slot write (`_begin(self, r)` / `begin(self, render_index=None)`)**:
   - If `render_index is None`, defaults to `r = self.index + 1` (`0` for the first frame) (`vadapt/rtring.py:162`, `vadapt/rtring.py:173`).
   - If `self._closed` is `True`, raises `ValueError("ring closed")` (`vadapt/rtring.py:137-138`).
   - If `self._open_slot is not None`, raises `ValueError("previous frame not committed")` (`vadapt/rtring.py:139-140`).
   - If `r <= self.index`, raises `ValueError("render_index must increase (%d <= %d)" % (r, self.index))` (`vadapt/rtring.py:141-142`).
   - Computes slot index `k = r % self.slots` (`vadapt/rtring.py:143`).
   - Reads current slot sequence `s = _Q.unpack_from(self._mm, HDR + k * SLOT_HDR)[0]` (`vadapt/rtring.py:129-130`, `vadapt/rtring.py:144`).
   - Stores **odd** sequence `(s + 1) & MASK` (`<Q`) at `HDR + k * SLOT_HDR` (`vadapt/rtring.py:145`).
   - Stores `r` (`<Q`) at `HDR + k * SLOT_HDR + 8` (`vadapt/rtring.py:146`).
   - Sets `self._open_slot = (k, s, r)` (`vadapt/rtring.py:147`).
   - `begin()` returns `self._slot_view(k)`: a writable `np.ndarray` (`dtype=np.uint8`, shape `(height, width, 3)`) backed by `self._mm` at `self.data_off + k * self.slot_bytes` (`vadapt/rtring.py:132-134`, `vadapt/rtring.py:160-163`).
2. **Filling the slot payload**:
   - In `begin()` / `commit()` mode: caller writes directly into the returned numpy view (`vadapt/rtring.py:161-163`).
   - In `publish(self, frame, render_index=None)` mode: converts `frame = np.asarray(frame)`, verifies `frame.dtype == np.uint8 and frame.shape == (self.height, self.width, 3)` (else raises `ValueError("frame must be uint8 (%d, %d, 3)" % (self.height, self.width))`), calls `k = self._begin(...)`, and copies bytes via `self._slot_view(k)[...] = frame` (`_copy`) (`vadapt/rtring.py:157-158`, `vadapt/rtring.py:169-175`).
3. **Committing a slot write (`_commit(self)` / `commit(self)`)**:
   - Unpacks `k, s, r = self._open_slot` (`vadapt/rtring.py:151`; if `_open_slot` is `None`, Python raises `TypeError`).
   - Computes `zlib.crc32(self._slot_view(k))` and packs it (`<I`, `u32`) at `HDR + k * SLOT_HDR + CRC_OFF` (`+16`) **before** storing the even sequence counter (`vadapt/rtring.py:152`).
   - Stores **even** sequence `(s + 2) & MASK` (`<Q`, `u64`) at `HDR + k * SLOT_HDR` (`+0`) (`vadapt/rtring.py:153`).
   - Stores new `head = r + 1` (`<Q`, `u64`) at `HEAD_OFF` (`40`) in the global header (`vadapt/rtring.py:154`).
   - Sets `self.index = r` and `self._open_slot = None`, and returns `self.index` (`vadapt/rtring.py:155`, `vadapt/rtring.py:166-167`, `vadapt/rtring.py:175-176`).

#### 2.7.3 Closing the Writer (`RingWriter.close`, Context Manager)

Defined at `vadapt/rtring.py:178-198`:

1. Sets `self._closed = True` (`vadapt/rtring.py:181`).
2. If `self._released` is `True`, returns immediately (`vadapt/rtring.py:182-183`).
3. Calls `self._mm.close()` (`vadapt/rtring.py:185`).
4. If `self._mm.close()` raises `BufferError` (live numpy views exist): logs `LOG.warning("ring closed with live numpy views; file and quota kept until they drop")` and returns **without** setting `self._released = True` and **without** releasing the file or quota reservation (`vadapt/rtring.py:21-22`, `vadapt/rtring.py:186-188`). Calling `close()` again after all views are dropped completes the unmap and release (`vadapt/rtring.py:179-180`).
5. When `self._mm.close()` succeeds: sets `self._released = True` and, if `self.path.exists()`, calls `self.ws.release(self.path)` (`vadapt/rtring.py:189-191`).
6. `__enter__` returns `self`; `__exit__` calls `self.close()` (`vadapt/rtring.py:193-197`).

### 2.8 Reader Protocol (`RingReader` and `Frame`) Step by Step

#### 2.8.1 Opening the Ring (`RingReader.__init__(self, path, root=ramws.ROOT, verify=True)`)

Defined at `vadapt/rtring.py:214-233`:

1. **Workspace containment check**: Resolves `real = os.path.realpath(path)` and `base = os.path.realpath(root)`. If `not real.startswith(base + os.sep)`, raises `ramws.RamError("E_RAM_ESCAPE", "ring outside ramws root: %s" % path)` (`vadapt/rtring.py:215-218`).
2. **Open and minimum size check**: Opens `fd = os.open(real, os.O_RDONLY)`. Inside `try ... finally: os.close(fd)`: checks `size = os.fstat(fd).st_size`; if `size < HDR` (`64`), raises `ValueError("ring file too small")`; maps read-only via `self._mm, self.advice = _map(fd, size, False)` (`vadapt/rtring.py:219-226`).
3. **Global header validation**: Unpacks `magic, ver, self.slots, self.width, self.height, self.slot_bytes, self.data_off, _` from offset `0` via `_G` (`vadapt/rtring.py:227`). If `magic != MAGIC or ver != VERSION`, calls `_unmap(self._mm)` and raises `ValueError("not a VQARING1 v%d file" % VERSION)` (`vadapt/rtring.py:228-230`). Validation in `__init__` that `size >= ring_bytes(slots, width, height)` is `ABSENT`.
4. **Initialize state and counters**: Sets `self.verify = verify`, `self.last = -1`, `self.gaps = []`, `self.torn = []`, `self.overrun = []` (`vadapt/rtring.py:231-232`).

#### 2.8.2 Querying Head (`RingReader.head(self)`)

- Returns `_Q.unpack_from(self._mm, HEAD_OFF)[0] - 1` (`vadapt/rtring.py:237-239`).
- When the ring is empty (`head == 0` in the global header), `head()` returns `-1`; otherwise it returns the newest committed `render_index`.

#### 2.8.3 Single-Slot Seqlock + CRC-32 Read (`RingReader._try(self, r, retries)`)

Defined at `vadapt/rtring.py:241-263`. For target `render_index = r`:

1. Computes `k = r % self.slots` and slot header offset `off = HDR + k * SLOT_HDR` (`vadapt/rtring.py:244-245`).
2. Repeats up to `retries` times (`for _ in range(retries):`, default `8`) (`vadapt/rtring.py:246`):
   - **Step A (`s1` pre-read)**: Reads `s1 = _Q.unpack_from(self._mm, off)[0]` (`vadapt/rtring.py:247`).
   - **Step B (odd check)**: If `s1 & 1 != 0` (writer in flight), continues to the next retry (`vadapt/rtring.py:248-249`).
   - **Step C (metadata read)**: Reads `ri = _Q.unpack_from(self._mm, off + 8)[0]` and `crc = _I.unpack_from(self._mm, off + CRC_OFF)[0]` (`vadapt/rtring.py:250-251`).
   - **Step D (`render_index` match check)**: If `ri != r` (`vadapt/rtring.py:252`):
     - Re-reads `seq` at `off`: if `_Q.unpack_from(self._mm, off)[0] != s1`, continues to the next retry (`vadapt/rtring.py:253-254`).
     - Otherwise `s1` is unchanged and `ri` is authoritative: returns `(None, "lapped")` if `ri > r`, or `(None, "absent")` if `ri < r` (`vadapt/rtring.py:255`).
   - **Step E (zero-copy view construction)**: Creates read-only numpy view `v = np.frombuffer(self._mm, dtype=np.uint8, count=self.slot_bytes, offset=self.data_off + k * self.slot_bytes).reshape(self.height, self.width, 3)` (`vadapt/rtring.py:256-257`).
   - **Step F (`seq` post-read)**: Re-reads `seq` at `off`: if `_Q.unpack_from(self._mm, off)[0] != s1`, continues to the next retry (`vadapt/rtring.py:258-259`).
   - **Step G (CRC-32 verification)**: If `self.verify` is truthy and `zlib.crc32(v) != crc`, continues to the next retry (`vadapt/rtring.py:260-261`).
   - **Step H (success)**: Returns `(Frame(v, r, self, k, s1), None)` (`vadapt/rtring.py:262`).
3. If all `retries` attempts fail, returns `(None, "torn")` (`vadapt/rtring.py:263`).

#### 2.8.4 Frame Validity Check (`Frame.valid(self)`)

- `Frame` holds `view`, `render_index`, `_reader`, `_k`, `_seq` (`vadapt/rtring.py:201-204`).
- `Frame.valid()` returns `self._reader._seq(self._k) == self._seq` (`vadapt/rtring.py:206-208`). A zero-copy `Frame.view` is only guaranteed uncorrupted by a concurrent producer overwrite while `Frame.valid()` remains `True` (`vadapt/rtring.py:12-13`, `vadapt/rtring.py:207-208`).

#### 2.8.5 Reader Acquisition Modes (`acquire`, `poll`, `read`) and Close

1. **`acquire(self, r, retries=8)` (`vadapt/rtring.py:273-283`)**:
   - Calls `f, status = self._try(r, retries)` (`vadapt/rtring.py:275`).
   - If `f is None`: calls `self._drop(r, status)` (`"torn"` -> `self.torn.append(r)`, `"lapped"` -> `self.overrun.append(r)`, `"absent"` -> no counter change) and returns `None` (`vadapt/rtring.py:265-278`).
   - If `f is not None`:
     - If `r > self.last + 1`, appends `(self.last + 1, r - 1)` to `self.gaps` and logs `LOG.info("render_index gap %d..%d", self.last + 1, r - 1)` (`vadapt/rtring.py:279-281`).
     - Sets `self.last = max(self.last, r)` and returns `f` (`vadapt/rtring.py:282-283`).
2. **`poll(self, retries=8, latest=False)` (`vadapt/rtring.py:285-307`)**:
   - Reads `h = self.head()`. If `h < 0 or h <= self.last`, returns `None` (`vadapt/rtring.py:288-290`).
   - If `latest` is truthy: returns `self.acquire(h, retries)` (`vadapt/rtring.py:291-292`).
   - If `latest` is falsy (in-order mode):
     - Computes `lo = max(self.last + 1, h - self.slots + 1)` (`vadapt/rtring.py:293`).
     - If `self.last >= 0 and lo > self.last + 1`: for each `r` in `range(self.last + 1, lo)`, calls `self._drop(r, "lapped")` (appending `r` to `self.overrun`), then appends `(self.last + 1, lo - 1)` to `self.gaps` (`vadapt/rtring.py:294-297`). (Note: when `self.last == -1` on the first read and `h >= self.slots`, `self.last >= 0` is false, so indices `0 .. lo - 1` are not added to `overrun` or `gaps`).
     - Iterates `for r in range(lo, h + 1):` (`vadapt/rtring.py:298-306`):
       - Calls `f, status = self._try(r, retries)` (`vadapt/rtring.py:299`).
       - If `f is not None`: sets `self.last = r` and returns `f` (`vadapt/rtring.py:300-302`).
       - If `f is None`: calls `self._drop(r, status)`; if `status == "absent"`, appends `(r, r)` to `self.gaps`; sets `self.last = r` and continues to `r + 1` (`vadapt/rtring.py:303-306`).
     - Returns `None` if no slot in `lo..h` yielded a frame (`vadapt/rtring.py:307`).
3. **`read(self, fn, retries=8)` (`vadapt/rtring.py:309-319`)**:
   - Loops `for _ in range(retries):`:
     - Calls `f = self.poll(retries, latest=True)`; if `f is None`, returns `None` (`vadapt/rtring.py:312-314`).
     - Executes `res = fn(f.view)` (`vadapt/rtring.py:315`).
     - Checks `if f.valid(): return res, f.render_index` (`vadapt/rtring.py:316-317`).
     - If `f.valid()` is `False`, appends `f.render_index` to `self.torn` and continues the loop (`vadapt/rtring.py:318`).
   - Returns `None` if all `retries` attempts fail (`vadapt/rtring.py:319`).
4. **`close(self)` (`vadapt/rtring.py:321-322`)**:
   - Calls `_unmap(self._mm)`. Context manager methods (`__enter__`, `__exit__`) on `RingReader` are `ABSENT`.

### 2.9 Seqlock, CRC-32, Overrun, Torn, and Gap Counters Summary

| Mechanism / Counter | Type | Rule and Semantics | Source |
|---|---|---|---|
| Seqlock (`seq`) | `u64` at `64 + 64*k` | Even when slot is stable (initial `_seq0 = 0`, committed `(s + 2) & MASK`); odd `(s + 1) & MASK` while writer is updating slot `k`. Reader requires `s1` even and post-read `seq == s1` (and `Frame.valid()` requires current `seq == s1`). | `vadapt/rtring.py:6`, `vadapt/rtring.py:144-154`, `vadapt/rtring.py:206-208`, `vadapt/rtring.py:247-259` |
| Payload `crc32` | `u32` at `64 + 64*k + 16` | Computed via `zlib.crc32(slot_view)` and stored by writer after copying frame bytes and before storing even `seq`. Checked by reader when `verify=True` to detect store/load reordering in Python (which cannot emit ARM `stlr`/`ldar` or `dmb ish` fences). | `vadapt/rtring.py:7`, `vadapt/rtring.py:16-20`, `vadapt/rtring.py:152`, `vadapt/rtring.py:260-261` |
| `reader.overrun` | `list[int]` | List of `render_index` values lapped by the producer before the reader read them (`status == "lapped"` when `ri > r` with stable `seq`, or pre-window `self.last + 1 .. lo - 1` when `self.last >= 0` in `poll(latest=False)`). Logged at `INFO`. Clean drop, not counted as torn. | `vadapt/rtring.py:14`, `vadapt/rtring.py:232`, `vadapt/rtring.py:255`, `vadapt/rtring.py:269-271`, `vadapt/rtring.py:294-296` |
| `reader.torn` | `list[int]` | List of `render_index` values where `_try(r, retries)` exhausted all `retries` seeing odd `seq`, changing `seq`, or CRC-32 mismatch (`status == "torn"`, logged at `WARNING`), or where `read(fn)` saw `f.valid() == False` after `fn(f.view)` returned. | `vadapt/rtring.py:15`, `vadapt/rtring.py:232`, `vadapt/rtring.py:263`, `vadapt/rtring.py:266-268`, `vadapt/rtring.py:316-318` |
| `reader.gaps` | `list[tuple[int, int]]` | List of inclusive skipped `(start_index, end_index)` pairs: `(self.last + 1, r - 1)` in `acquire(r)` when `r > self.last + 1` (logged at `INFO`); `(self.last + 1, lo - 1)` in `poll(latest=False)` when `self.last >= 0 and lo > self.last + 1`; `(r, r)` in `poll(latest=False)` when `_try(r)` returns `status == "absent"`. | `vadapt/rtring.py:15`, `vadapt/rtring.py:232`, `vadapt/rtring.py:279-281`, `vadapt/rtring.py:294-297`, `vadapt/rtring.py:304-305` |

### 2.10 Error Codes and Exceptions in `vadapt/rtring.py`

| Error Code / Exception | Raised By | Condition | Message / Detail | Source |
|---|---|---|---|---|
| `ramws.RamError("E_RAM_ESCAPE")` | `RingWriter.__init__` (via `ws.path(rel)`) | `rel` is empty, contains `\0`, is absolute, contains `..`, or escapes `ws.root` via symlink | `"bad rel ..."` or `"escapes root: ..."` | `vadapt/rtring.py:93`, `vadapt/ramws.py:139-146` |
| `ramws.RamError("E_RAM_ESCAPE")` | `RingReader.__init__` | `os.path.realpath(path)` does not start with `os.path.realpath(root) + os.sep` | `"ring outside ramws root: %s" % path` | `vadapt/rtring.py:215-218` |
| `ramws.RamError("E_RAM_QUOTA")` | `RingWriter.__init__` (via `ws.reserve(total)`) | `total` violates per-run quota, `HOST_MAX` (`16 GiB`), or `FREE_MIN` (`8 GiB`) | Quota error message from `ws.reserve`; ring file is removed before raising | `vadapt/rtring.py:100-103`, `vadapt/ramws.py:161-169` |
| `ramws.RamError("E_RAM_LOCKED")` | `RingWriter.__init__` (via `ws.path` / `ws.reserve`) | Workspace `ws` is already closed | `"workspace closed"` | `vadapt/rtring.py:93`, `vadapt/rtring.py:100`, `vadapt/ramws.py:131-133` |
| `ValueError` | `RingWriter.__init__` | Any of `slots`, `width`, `height` is not `int`, is `bool`, or `<= 0` | `"slots/width/height must be positive ints"` | `vadapt/rtring.py:85-86` |
| `ValueError` | `RingWriter.__init__` | `_seq0 & 1` is non-zero (odd initial sequence number) | `"seq0 must be even"` | `vadapt/rtring.py:87-88` |
| `FileExistsError` (`OSError`) | `RingWriter.__init__` | Ring file already exists when opened with `O_CREAT \| O_EXCL` | OS error (raised before `ws.reserve`) | `vadapt/rtring.py:96-97` |
| `ValueError` | `RingWriter._begin` (`begin`, `publish`) | `self._closed` is `True` | `"ring closed"` | `vadapt/rtring.py:137-138` |
| `ValueError` | `RingWriter._begin` (`begin`, `publish`) | `self._open_slot is not None` (prior `begin()` not committed) | `"previous frame not committed"` | `vadapt/rtring.py:139-140` |
| `ValueError` | `RingWriter._begin` (`begin`, `publish`) | `r <= self.index` (non-increasing `render_index`) | `"render_index must increase (%d <= %d)" % (r, self.index)` | `vadapt/rtring.py:141-142` |
| `ValueError` | `RingWriter.publish` | `frame.dtype != np.uint8` or `frame.shape != (self.height, self.width, 3)` | `"frame must be uint8 (%d, %d, 3)" % (self.height, self.width)` | `vadapt/rtring.py:170-172` |
| `ValueError` | `RingReader.__init__` | `os.fstat(fd).st_size < HDR` (`64` bytes) | `"ring file too small"` | `vadapt/rtring.py:221-223` |
| `ValueError` | `RingReader.__init__` | `magic != b"VQARING1"` or `ver != 2` | `"not a VQARING1 v%d file" % VERSION` | `vadapt/rtring.py:228-230` |

---

## 3. RAM Workspace Specification (`vadapt/ramws.py`)

### 3.1 Constants and Module State

| Constant / Symbol | Value | Meaning | Source |
|---|---|---|---|
| `ROOT` | `"/dev/shm/vqa-ramws"` | Default `tmpfs` workspace root directory | `vadapt/ramws.py:19` |
| `RAM_FS` | `("tmpfs", "ramfs")` | Permitted filesystem types in `/proc/self/mountinfo` | `vadapt/ramws.py:20`, `vadapt/ramws.py:57` |
| `GIB` | `1 << 30` (`1,073,741,824`) | 1 GiB in bytes | `vadapt/ramws.py:21` |
| `RUN_DEFAULT` | `2 * GIB` (`2,147,483,648` B) | Default per-run quota (2 GiB) | `vadapt/ramws.py:22`, `vadapt/ramws.py:442` |
| `RUN_MAX` | `8 * GIB` (`8,589,934,592` B) | Maximum allowed per-run quota (8 GiB) | `vadapt/ramws.py:22`, `vadapt/ramws.py:450` |
| `HOST_MAX` | `16 * GIB` (`17,179,869,184` B) | Maximum host-wide workspace usage across all runs under `root` (16 GiB) | `vadapt/ramws.py:22`, `vadapt/ramws.py:165` |
| `FREE_MIN` | `8 * GIB` (`8,589,934,592` B) | Minimum `statvfs` free bytes required after a reservation (8 GiB) | `vadapt/ramws.py:22`, `vadapt/ramws.py:168` |
| `SWAP_WARN` | `GIB` (`1,073,741,824` B) | Host swap growth threshold (`> 1 GiB`) that emits `"W_RAM_SWAP"` in `receipt()` | `vadapt/ramws.py:23`, `vadapt/ramws.py:204-205` |
| `KEEP_CAP_MB` | `256` | Maximum MiB cap for `keep_frames` (`256 MiB`) | `vadapt/ramws.py:24`, `vadapt/ramws.py:508` |
| `KEEP_TTL_H` | `24` | Default TTL in hours for `keep_frames` (`24 h`) | `vadapt/ramws.py:24`, `vadapt/ramws.py:506` |
| `KEEP_TTL_MAX_H` | `72` | Maximum TTL in hours for `keep_frames` (`72 h`) | `vadapt/ramws.py:24`, `vadapt/ramws.py:509` |
| `FRAME_EXT` | `(".png", ".exr", ".npy", ".npz", ".jpg", ".jpeg", ".bmp", ".tga", ".tif", ".tiff", ".webp", ".gif", ".mp4", ".blend", ".blend1", ".raw", ".xwd")` | Case-insensitive file extensions flagged by `check_disk` | `vadapt/ramws.py:25`, `vadapt/ramws.py:550` |
| `FRAME_NAMES` | `("tiles.bin", "gops.bin", "frames.raw", "Xvfb_screen0")` | Exact filenames flagged by `check_disk` | `vadapt/ramws.py:26`, `vadapt/ramws.py:550` |
| `NAME_RE` | `re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")` | Validation regex for `tool`, `campaign`, and `run_id` identifiers | `vadapt/ramws.py:27`, `vadapt/ramws.py:61` |
| `SHARED_FILES` | `(".lock", "owner.json")` | Run metadata files granted read-only access when sharing is enabled | `vadapt/ramws.py:354` |
| `_OPEN` | `dict` (`{RamWorkspace: 1}`) | Active workspaces tracked for `atexit` / `SIGTERM` / `SIGINT` cleanup | `vadapt/ramws.py:28`, `vadapt/ramws.py:220`, `vadapt/ramws.py:240`, `vadapt/ramws.py:501` |
| `_SIG_DONE` | `list` | Non-empty once signal and `atexit` handlers are installed on the main thread | `vadapt/ramws.py:29`, `vadapt/ramws.py:257-259` |
| `_SIG_DONE_PREV` | `dict` | Previous `SIGTERM` and `SIGINT` handlers saved by `_install_handlers` | `vadapt/ramws.py:253`, `vadapt/ramws.py:246`, `vadapt/ramws.py:262` |

### 3.2 Directory Layout Under the `tmpfs` Root

Given `root` (default `"/dev/shm/vqa-ramws"`, resolved via `os.path.realpath(root)` at `vadapt/ramws.py:452`), `tool`, `campaign`, and `run_id`:

```text
<root>/                                      # mode 0o700 (or pre-existing; +u:<uid>:--x or o+x when shared if needed)
└── <tool>/                                  # mode 0o700 default; +u:<uid>:--x (ACL) or chgrp+0o710 (group if created here)
    └── <campaign>/                          # mode 0o700 default; +u:<uid>:--x (ACL) or chgrp+0o710 (group if created here)
        └── <run_id>/                        # ws.root: mode 0o700 default; 0o1700+ACL (mask rwx -> 0o1770) or chgrp+0o3770 (group)
            ├── .lock                        # empty flock file; 0o600 default; +u:<uid>:r--,m::r-- (ACL) or chgrp+0o640 (group)
            ├── owner.json                   # UTF-8 JSON; 0o600 default; +u:<uid>:r--,m::r-- (ACL) or chgrp+0o640 (group)
            └── <rel>                        # files/subdirs created via ws.path(rel), e.g., ring/frames.rgb24
```

- **Identifier validation (`_name(v, what)`, `vadapt/ramws.py:60-63`)**:
  - Applied to `tool`, `campaign`, and `run_id` (`vadapt/ramws.py:307`, `vadapt/ramws.py:448`, `vadapt/ramws.py:510`).
  - Must satisfy `isinstance(v, str) and NAME_RE.match(v) and v not in (".", "..")`.
  - Any failure raises `RamError("E_RAM_ESCAPE", "bad %s %r" % (what, v))` (`vadapt/ramws.py:61-62`).

### 3.3 Storage-Law Filesystem Verification (`is_ram_fs`, `_unesc`)

1. **`_unesc(s)` (`vadapt/ramws.py:40-41`)**: Replaces octal escape sequences `r"\\([0-7]{3})"` in mount point fields from `/proc/self/mountinfo` with their corresponding characters (`chr(int(m.group(1), 8))`).
2. **`is_ram_fs(p, mountinfo="/proc/self/mountinfo")` (`vadapt/ramws.py:44-57`)**:
   - Computes `real = os.path.realpath(str(p))` (`vadapt/ramws.py:46`).
   - Parses each line of `mountinfo` by splitting on `" - "` into `head, sep, tail`. Lines without `" - "` or with fewer than 5 whitespace-separated fields in `head` are skipped (`vadapt/ramws.py:48-53`).
   - Extracts mount point `mp = _unesc(parts[4])` and filesystem type `tail.split()[0]` (`vadapt/ramws.py:54-56`).
   - Selects the longest matching mount-point prefix where `(real == mp or real.startswith(mp.rstrip("/") + "/")) and len(mp) >= len(best)` (`vadapt/ramws.py:55-56`).
   - Returns `fst` if `fst in ("tmpfs", "ramfs")`, otherwise `None` (`vadapt/ramws.py:57`).
3. **Pre-creation check in `open_run` (`vadapt/ramws.py:452-458`)**:
   - Resolves `root = os.path.realpath(root)`, walks `anc = Path(root)` upward (`anc = anc.parent`) until `anc.exists()`, and verifies `is_ram_fs(anc)` is not `None`. If `None`, raises `RamError("E_RAM_NOT_TMPFS", "%s is not tmpfs/ramfs" % root)` before creating any directory.

### 3.4 Run Lifecycle (`open_run`, `RamWorkspace`, `close`, Signal Handlers)

#### 3.4.1 Opening a Run (`open_run`)

`open_run(campaign, run_id, quota_bytes=RUN_DEFAULT, *, tool, root=ROOT, statvfs=os.statvfs, share_with_uid=None, share_group=None)` (`vadapt/ramws.py:442-503`) executes in exact order:

1. **Validate names**: `_name(tool, "tool")`, `_name(campaign, "campaign")`, `_name(run_id, "run_id")` (`vadapt/ramws.py:448`).
2. **Validate share IDs**: `_int_id(share_with_uid, "share_with_uid")`, `_int_id(share_group, "share_group")` — each non-`None` value must be `int` (not `bool`) in `0 <= v < (1 << 32)`, else raises `RamError("E_RAM_SHARE", "bad %s %r" % (what, v))` (`vadapt/ramws.py:437-439`, `vadapt/ramws.py:449`).
3. **Validate quota**: `quota_bytes` must be `int` (not `bool`) with `0 < quota_bytes <= RUN_MAX` (`8 GiB`), else raises `RamError("E_RAM_QUOTA", "run quota must be in (0, %d]" % RUN_MAX)` (`vadapt/ramws.py:450-451`).
4. **Verify `tmpfs`/`ramfs`**: Resolves `root = os.path.realpath(root)`, finds existing ancestor `anc`, calls `fst = is_ram_fs(anc)`, and raises `RamError("E_RAM_NOT_TMPFS", ...)` if `fst is None` (`vadapt/ramws.py:452-458`).
5. **Create root and run janitor**: `Path(root).mkdir(parents=True, exist_ok=True, mode=0o700)`, then `sweep(tool, root)` (`vadapt/ramws.py:459-460`).
6. **Create `<tool>/<campaign>/<run_id>` hierarchy**:
   - Retries up to 3 times (`for _ in range(3):`) against concurrent `close()` removing empty parents (`FileNotFoundError`) (`vadapt/ramws.py:463-478`).
   - Creates `run.parent.parent` (`<tool>`) and `run.parent` (`<campaign>`) individually with `d.mkdir(mode=0o700)`, appending directories created in this call to `created` (`vadapt/ramws.py:465-470`).
   - Creates `run.mkdir(mode=0o700)`. If `run` already exists (`FileExistsError`), raises `RamError("E_RAM_LOCKED", "run dir exists (live owner): %s/%s" % (campaign, run_id))` (`vadapt/ramws.py:471-474`).
   - If all 3 loop iterations hit `FileNotFoundError`, raises `RamError("E_RAM_LOCKED", "cannot create run dir")` (`vadapt/ramws.py:477-478`).
7. **Create and lock `.lock`**:
   - Opens `fd = os.open(run / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)` (`vadapt/ramws.py:479`).
   - Calls `fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)` (`vadapt/ramws.py:481`).
   - If `flock` raises `OSError`: closes `fd`, calls `_rollback(run)` (`shutil.rmtree(run, ignore_errors=True)` + `rmdir()` on `run.parent` and `run.parent.parent`), and raises `RamError("E_RAM_LOCKED", "lock held")` (`vadapt/ramws.py:428-434`, `vadapt/ramws.py:482-485`).
8. **Write `owner.json`**:
   - Writes `json.dumps({"pid": os.getpid(), "starttime": _starttime(), "boot_id": _boot_id(), "tool": tool, "uid": os.getuid()}, sort_keys=True)` to `run / "owner.json"` (`vadapt/ramws.py:486-487`).
9. **Apply optional UID/group sharing**:
   - If `share_with_uid is not None or share_group is not None`: sets `os.chmod(run / "owner.json", 0o600)` and calls `share = _apply_share(run, Path(root), share_with_uid, share_group, created)` (`vadapt/ramws.py:489-492`).
   - On `(RamError, OSError)`: closes `fd`, calls `_rollback(run)`, and raises `RamError("E_RAM_SHARE", ...)` (`vadapt/ramws.py:493-498`).
10. **Construct and register workspace**:
    - Creates `ws = RamWorkspace(run, quota_bytes, tool, campaign, run_id, fd, statvfs, root, fst)`, sets `ws._share = share`, sets `_OPEN[ws] = 1`, calls `_install_handlers()`, and returns `ws` (`vadapt/ramws.py:499-503`).

#### 3.4.2 Path Resolution Inside a Run (`RamWorkspace.path(self, rel)`)

Defined at `vadapt/ramws.py:135-156`:

1. Calls `self._live()` (raises `RamError("E_RAM_LOCKED", "workspace closed")` if `self._closed`) (`vadapt/ramws.py:131-133`, `vadapt/ramws.py:137`).
2. Converts `s = os.fspath(rel)`. If `not s or "\0" in s or os.path.isabs(s) or ".." in Path(s).parts`, raises `RamError("E_RAM_ESCAPE", "bad rel %r" % s)` (`vadapt/ramws.py:138-140`).
3. Sets `cand = self.root / s` and runs 2 passes (`for _ in range(2):`) (`vadapt/ramws.py:141-155`):
   - Computes `real = os.path.realpath(cand)`. If `real != self._real and not real.startswith(self._real + os.sep)`, raises `RamError("E_RAM_ESCAPE", "escapes root: %r" % s)` (`vadapt/ramws.py:144-146`).
   - Determines target directory `tgt = cand if s.endswith("/") else cand.parent` (`vadapt/ramws.py:147`).
   - When `self._share` has `method == "group"`, collects all non-existent ancestor directories under `self.root` into `new`, calls `tgt.mkdir(parents=True, exist_ok=True)`, and `chmod`s each newly created host-owned directory in `reversed(new)` to `0o2770` (`vadapt/ramws.py:142`, `vadapt/ramws.py:147-155`).
   - When not in group share mode, calls `tgt.mkdir(parents=True, exist_ok=True)` (`vadapt/ramws.py:151`).
4. Returns `cand` (`Path`) (`vadapt/ramws.py:156`).

#### 3.4.3 Closing a Run (`RamWorkspace.close`, Signal and Exit Handlers)

1. **`RamWorkspace.close(self)` (`vadapt/ramws.py:210-231`)**:
   - Idempotent: returns immediately if `self._closed` is `True` (`vadapt/ramws.py:212-213`).
   - Updates `self.peak = max(self.peak, _tree_bytes(self.root))` if `self.root.is_dir()` (ignoring `OSError`) (`vadapt/ramws.py:214-218`).
   - Sets `self._closed = True` and removes `self` from `_OPEN` (`vadapt/ramws.py:219-220`).
   - Deletes `self.root` via `shutil.rmtree(self.root, ignore_errors=True)` (`vadapt/ramws.py:221`).
   - Attempts `d.rmdir()` on `self.root.parent` (`<campaign>`) and `self.root.parent.parent` (`<tool>`), ignoring `OSError` (`vadapt/ramws.py:222-226`).
   - Closes `self._fd` via `os.close(self._fd)`, ignoring `OSError` (`vadapt/ramws.py:227-230`).
2. **Process Exit and Signal Cleanup (`_install_handlers`, `_on_signal`, `_close_all`, `vadapt/ramws.py:239-263`)**:
   - `_install_handlers()` is a no-op if `_SIG_DONE` is non-empty or if called off the main thread (`threading.current_thread() is not threading.main_thread()`) (`vadapt/ramws.py:257-258`).
   - Registers `atexit.register(_close_all)` and wraps `signal.SIGTERM` and `signal.SIGINT` with `_on_signal`, storing previous handlers in `_SIG_DONE_PREV` (`vadapt/ramws.py:259-262`).
   - `_on_signal(sig, frame)` closes every open workspace in `list(_OPEN)`, then calls the previous handler if `callable(prev)`, or restores `signal.SIG_DFL` and sends `os.kill(os.getpid(), sig)` (`vadapt/ramws.py:239-250`).

### 3.5 Locks, Owner Files, and Crash Janitor (`_owner`, `_try_lock`, `sweep`)

1. **`owner.json` Schema and Reader (`_owner(run)`, `vadapt/ramws.py:83-92`, `265-282`, `486-487`)**:
   - Written at `open_run` with keys sorted (`sort_keys=True`):
     - `"boot_id"`: string from `/proc/sys/kernel/random/boot_id` (or `""` on `OSError`) (`vadapt/ramws.py:88-92`).
     - `"pid"`: `os.getpid()` (`int`) (`vadapt/ramws.py:486`).
     - `"starttime"`: `int(f.read().rsplit(")", 1)[1].split()[19])` from `/proc/self/stat` (process start time in clock ticks) (`vadapt/ramws.py:83-85`).
     - `"tool"`: `tool` (`str`) (`vadapt/ramws.py:486`).
     - `"uid"`: `os.getuid()` (`int`) (`vadapt/ramws.py:486`).
   - `_owner(run)` opens `run / "owner.json"` with `os.O_RDONLY | os.O_NOFOLLOW`, checks `stat.S_ISREG(st.st_mode)` and `st.st_uid == os.getuid()` via `os.fstat(fd)`, and parses JSON (`vadapt/ramws.py:267-276`). If it is a symlink, not a regular file, owned by a different UID, missing, or invalid JSON, `_owner(run)` returns `None` (`vadapt/ramws.py:268-278`).
2. **Lock Probe (`_try_lock(run)`, `vadapt/ramws.py:284-303`)**:
   - Opens `run / ".lock"` with `os.O_RDWR | os.O_NOFOLLOW`; returns `False` on `OSError` (`vadapt/ramws.py:286-289`).
   - Attempts `fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)`; if acquired, returns `fd` (`int`) (`vadapt/ramws.py:290-292`).
   - If `LOCK_EX` fails, attempts `fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)` (`vadapt/ramws.py:297-299`):
     - If `LOCK_SH` succeeds, no process holds `LOCK_EX` (only a foreign reader holds `LOCK_SH` on a dead run), so `_try_lock` returns `fd` (`int`) (`vadapt/ramws.py:295-299`).
     - If `LOCK_SH` also fails, the host process holds `LOCK_EX` (run is live); closes `fd` and returns `None` (`vadapt/ramws.py:300-302`).
3. **Janitor Sweep (`sweep(tool, root=ROOT)`, `vadapt/ramws.py:305-340`)**:
   - Iterates sorted `<campaign>/<run>` under `Path(root) / _name(tool, "tool")` (`vadapt/ramws.py:307-310`).
   - Skips `run` if `lstat` fails, `not run.is_dir()`, `run.is_symlink()`, or `st.st_uid != os.getuid()` (`vadapt/ramws.py:311-316`).
   - Reads `own = _owner(run)`. If `own is not None and (own.get("tool") != tool or own.get("uid") != os.getuid())`, skips `run` (`vadapt/ramws.py:317-319`).
   - Calls `fd = _try_lock(run)`. If `own is None and time.time() - st.st_mtime < 5`, sets `fd = None` (5-second grace period while run directory is being created) (`vadapt/ramws.py:320-322`).
   - Computes `stale_boot = own is not None and own.get("boot_id") not in (None, _boot_id())` (`vadapt/ramws.py:323`).
   - Keeps `run` if `(fd is None and not stale_boot)` or `(fd is False and not stale_boot)` (`vadapt/ramws.py:324-327`).
   - Otherwise removes `run` via `shutil.rmtree(run, ignore_errors=True)`, closes `fd` if `isinstance(fd, int)`, and appends `"%s/%s" % (camp.name, run.name)` to `gone` (`vadapt/ramws.py:328-331`).
   - Tries `camp.rmdir()` and `base.rmdir()` to prune empty `<campaign>` and `<tool>` directories, and returns `gone` (`vadapt/ramws.py:332-340`).

### 3.6 Sharing With Another UID or Group (`_apply_share`, `_share_acl`, `_share_group`)

When `open_run` is called with `share_with_uid` and/or `share_group` (`vadapt/ramws.py:343-426`, `489-498`):

- **Preconditions**:
  - `.lock` (`0o600`, `LOCK_EX` held) and `owner.json` (`0o600`) already exist in `run` before `_apply_share` is invoked (`vadapt/ramws.py:479-492`).
  - `chain` is the ordered list of ancestor directories from `Path(root)` down to `run.parent` (`[Path(root), Path(root)/tool, Path(root)/tool/campaign]`) (`vadapt/ramws.py:400-406`).

| Aspect | Method `"acl"` (`share_with_uid`) | Method `"group"` (`share_group` fallback or group-only) | Source |
|---|---|---|---|
| Trigger | `share_with_uid is not None` (attempted first) | `share_group is not None` (used if `share_with_uid is None` or after clean rollback of a failed ACL attempt) | `vadapt/ramws.py:408-425` |
| Ancestor directories (`chain`) | For each `d` in `chain`: if `os.stat(d).st_mode & 0o011 != 0o011` (`not _open_to_all`), runs `setfacl -m u:<uid>:--x -- <d>`. | For `d in created`: `os.chown(d, -1, gid)` then `os.chmod(d, 0o710)`. For pre-existing `d`: `gid` is never changed; if `not (st_mode & 0o001 or (st_gid == gid and st_mode & 0o010))`, adds `o+x` (`st_mode \| 0o001`). | `vadapt/ramws.py:350-351`, `vadapt/ramws.py:366-368`, `vadapt/ramws.py:385-392` |
| Run directory (`run`) | `os.chmod(run, 0o1700)` (sticky bit), then `setfacl -m u:<uid>:rwx,d:u::rwx,d:u:<host_uid>:rwx,d:u:<uid>:rwx,d:g::---,d:o::--- -- <run>`. | `os.chown(run, -1, gid)` then `os.chmod(run, 0o3770)` (`setgid` + `sticky` + user/group `rwx`). | `vadapt/ramws.py:369-371`, `vadapt/ramws.py:385-386`, `vadapt/ramws.py:393` |
| `SHARED_FILES` (`.lock`, `owner.json`) | For each `n` in `(".lock", "owner.json")`: `setfacl -m u:<uid>:r--,m::r-- -- <run/n>` (read-only). | For each `n` in `(".lock", "owner.json")`: `os.chown(run / n, -1, gid)` then `os.chmod(run / n, 0o640)` (group read-only). | `vadapt/ramws.py:354-360`, `vadapt/ramws.py:372`, `vadapt/ramws.py:385-386`, `vadapt/ramws.py:394-395` |
| Subdirectories created via `ws.path()` | Inherit default ACL (`d:u::rwx,d:u:<host_uid>:rwx,d:u:<uid>:rwx,d:g::---,d:o::---`). | `ws.path()` explicitly `chmod`s each newly created host-owned subdirectory to `0o2770`. | `vadapt/ramws.py:142`, `vadapt/ramws.py:147-155`, `vadapt/ramws.py:371` |
| Rollback / Fail-closed rule | On failure in `_share_acl`, runs `setfacl -b -- <run>`, `chmod 0o700`, and for each file in `SHARED_FILES` runs `setfacl -b` and `chmod 0o600`. If `run` still has mode bits `& 0o7077 != 0` or any shared file has mode bits `& 0o177 != 0`, raises `E_RAM_SHARE` without falling back to group. Any unhandled failure rolls back `run` and closes `fd`. | All `os.chown` calls run before any `os.chmod`. On `OSError`, `_apply_share` raises `E_RAM_SHARE` and `open_run` closes `fd` and calls `_rollback(run)`. | `vadapt/ramws.py:373-379`, `vadapt/ramws.py:385-386`, `vadapt/ramws.py:412-425`, `vadapt/ramws.py:493-498` |
| `receipt()["share"]` value | `{"uid": uid, "method": "acl", "files_read": [".lock", "owner.json"]}` | `{"gid": gid, "method": "group", "files_read": [".lock", "owner.json"]}` | `vadapt/ramws.py:206-207`, `vadapt/ramws.py:411`, `vadapt/ramws.py:422` |

### 3.7 Memory and Disk Accounting, Quotas, Kept Frames, and Storage-Law Checks

1. **Block Accounting (`_tree_bytes`, `_host_used`)**:
   - `_tree_bytes(p)` (`vadapt/ramws.py:66-80`): sums `os.lstat(x).st_blocks * 512` for `p` itself and (when `p` is a directory and not a symlink) every directory and file entry in `os.walk(p)`, without following symlinks. Returns `0` if `lstat(p)` fails.
   - `_host_used(root, skip=None)` (`vadapt/ramws.py:106-116`): walks `root` with `os.walk(root)`, pruning any directory equal to `str(skip)` (`dn[:] = [d for d in dn if os.path.join(dp, d) != str(skip)]`), and sums `os.lstat(os.path.join(dp, x)).st_blocks * 512` across all files `fn`.
2. **Swap Accounting (`_swap_used`, `vadapt/ramws.py:95-103`)**:
   - Reads `SwapTotal:` (`t` KiB) and `SwapFree:` (`f` KiB) from `/proc/meminfo` and returns `(t - f) * 1024` bytes.
   - `RamWorkspace.__init__` records `self._swap0 = _swap_used()` (`vadapt/ramws.py:128`); `receipt()` reports `"swap_used_delta": _swap_used() - self._swap0`, adding `"warnings": ["W_RAM_SWAP"]` when `swap_used_delta > SWAP_WARN` (`1 GiB`) (`vadapt/ramws.py:201-205`).
3. **Quota Enforcement (`reserve`, `account`, `release`)**:
   - `reserve(self, n)` (`vadapt/ramws.py:158-171`):
     - Requires `self._live()` and `isinstance(n, int) and not isinstance(n, bool) and n >= 0` (`vadapt/ramws.py:160-162`).
     - Enforces three inequalities before mutating `self.used`:
       1. `self.used + n <= self.quota` (`vadapt/ramws.py:163-164`).
       2. `_host_used(self._host, skip=self.root) + self.used + n <= HOST_MAX` (`16 GiB`) (`vadapt/ramws.py:165-166`).
       3. `sv.f_bavail * sv.f_frsize - n >= FREE_MIN` (`8 GiB`, where `sv = self._statvfs(str(self._host))`) (`vadapt/ramws.py:167-169`).
     - On success: `self.used += n` and `self.peak = max(self.peak, self.used)` (`vadapt/ramws.py:170-171`). On failure: raises `RamError("E_RAM_QUOTA", ...)` with `self.used` and `self.peak` unchanged.
   - `account(self)` (`vadapt/ramws.py:173-180`):
     - Requires `self._live()`. Sets `self.used = _tree_bytes(self.root)` and `self.peak = max(self.peak, self.used)`. If `self.used > self.quota`, raises `RamError("E_RAM_QUOTA", "used %d > quota %d" % (self.used, self.quota))`. Returns `self.used`.
   - `release(self, p)` (`vadapt/ramws.py:182-198`):
     - Requires `self._live()`. Resolves `cand = Path(s) if os.path.isabs(s) else self.root / s` and `tgt = Path(os.path.realpath(cand.parent)) / cand.name`.
     - If `cand.name in ("", ".", "..") or ".." in cand.parts or not str(tgt).startswith(self._real + os.sep)`, raises `RamError("E_RAM_ESCAPE", "release outside root: %r" % s)` (`vadapt/ramws.py:189-190`).
     - Computes `freed = _tree_bytes(tgt)`, unlinks `tgt` (if file/symlink) or `shutil.rmtree(tgt)` (if directory), updates `self.used = max(0, self.used - freed)` and `self._released += freed`, and returns `freed` (`vadapt/ramws.py:191-198`).
4. **Receipt Schema (`RamWorkspace.receipt(self)`, `vadapt/ramws.py:200-208`)**:
   - Always includes:
     - `"ram_peak_bytes"`: `self.peak` (`int`)
     - `"quota"`: `self.quota` (`int`)
     - `"released_bytes"`: `self._released` (`int`)
     - `"swap_used_delta"`: `_swap_used() - self._swap0` (`int`)
     - `"fstype"`: `self._fstype` (`"tmpfs"` or `"ramfs"`)
   - Conditionally includes:
     - `"warnings"`: `["W_RAM_SWAP"]` iff `swap_used_delta > SWAP_WARN` (`vadapt/ramws.py:204-205`).
     - `"share"`: `dict(self._share)` iff `self._share` is truthy (`vadapt/ramws.py:206-207`).
5. **Capped Kept Frames (`keep_frames`, `sweep_kept`, `vadapt/ramws.py:506-540`)**:
   - `keep_frames(src, build, run_id, cap_mb, *, now, reason, argv_sha, ttl_h=KEEP_TTL_H)`:
     - Caps `cap = min(int(cap_mb), KEEP_CAP_MB) * (1 << 20)` (`<= 256 MiB`) and `ttl = min(int(ttl_h), KEEP_TTL_MAX_H) * 3600` (`<= 72 h`) (`vadapt/ramws.py:508-509`).
     - Copies non-symlink files from `Path(src)` in sorted order into `Path(build) / "kept_frames" / _name(run_id, "run_id")`, stopping (`kept_truncated = True`) at the first file where `got + st_size > cap` (`vadapt/ramws.py:510-521`).
     - Writes `KEEP.json` (`{"argv_sha": argv_sha, "bytes": got, "cap_mb": cap // (1 << 20), "kept_truncated": trunc, "reason": reason, "ttl_until": int(now) + ttl}`) and returns that dictionary (`vadapt/ramws.py:522-525`).
   - `sweep_kept(build, now)`:
     - Scans sorted subdirectories `d` of `Path(build) / "kept_frames"`; removes `d` via `shutil.rmtree(d)` iff `d` is a non-symlink directory with a readable `KEEP.json` whose `"ttl_until"` is an `int` or `float` with `ttl_until < now`. Returns list of removed directory names (`vadapt/ramws.py:528-540`).
6. **Storage-Law Disk Check (`check_disk`, `vadapt/ramws.py:543-554`)**:
   - `check_disk(dir, allow=("kept_frames", "fail_evidence"), since=None)`:
     - Walks `Path(dir)`, pruning any directory whose name is in `allow` (`vadapt/ramws.py:547-548`).
     - Flags any file `f` where `f.lower().endswith(FRAME_EXT) or f in FRAME_NAMES` and (`since is None or os.lstat(p).st_mtime_ns > since`) (`vadapt/ramws.py:549-553`).
     - Returns sorted list of paths relative to `dir` (`vadapt/ramws.py:553-554`).

### 3.8 CLI Subcommands (`python3 -m vadapt.ramws`) and Error Codes

Defined at `vadapt/ramws.py:32-38`, `573-609`:

| Subcommand / Invocation | Arguments | Success Output (`rc = 0`) | Failure Output & Exit Code | Source |
|---|---|---|---|---|
| `sweep` | `--tool T [--root R]` | `{"code": "OK", "removed": [...]}` | `RamError` -> `{"code": e.code, "detail": str(e)}` (`rc = 5`) | `vadapt/ramws.py:584-585`, `vadapt/ramws.py:600-605` |
| `sweep-kept` | `--build B [--now S]` | `{"code": "OK", "removed": [...]}` | `RamError` -> `rc = 5` | `vadapt/ramws.py:586-587`, `vadapt/ramws.py:600-605` |
| `status` | `[--tool T] [--root R]` | `{"code": "OK", "free_bytes": ..., "fstype": ..., "host_bytes": ..., "runs": [...]}` | `RamError` -> `rc = 5` | `vadapt/ramws.py:557-570`, `vadapt/ramws.py:588-589`, `vadapt/ramws.py:600-605` |
| `check-disk` | `DIR [--allow a,b] [--since NS]` | `{"code": "OK", "offending": []}` | Non-empty `offending` -> `{"code": "E_DISK_FRAMES", "offending": [...]}` (`rc = 5`) | `vadapt/ramws.py:590-596`, `vadapt/ramws.py:603-605` |
| *(invalid args)* | Any other invocation | — | `{"code":"E_USAGE","detail":"ramws {sweep --tool T\|sweep-kept --build B [--now S]\|status [--tool T]\|check-disk DIR [--allow a,b] [--since NS]} [--root R]"}` (`rc = 2`) | `vadapt/ramws.py:597-599` |

#### All Error and Warning Codes in `vadapt/ramws.py`

| Code | Kind | CLI `rc` | Trigger Conditions | Source |
|---|---|---|---|---|
| `E_RAM_NOT_TMPFS` | `RamError.code` | `5` | `is_ram_fs(anc)` is `None` (`root` is not mounted on `tmpfs` or `ramfs`). | `vadapt/ramws.py:33`, `vadapt/ramws.py:456-458`, `vadapt/ramws.py:600-602` |
| `E_RAM_QUOTA` | `RamError.code` | `5` | `open_run` `quota_bytes` not an `int` (or is `bool`) or not in `(0, RUN_MAX]`; `reserve(n)` `n` not an `int` (or is `bool`) or `< 0`; `used + n > quota`; `_host_used + used + n > HOST_MAX`; `statvfs_free - n < FREE_MIN`; or `account()` finds `_tree_bytes(root) > quota`. | `vadapt/ramws.py:33`, `vadapt/ramws.py:161-169`, `vadapt/ramws.py:178-179`, `vadapt/ramws.py:450-451`, `vadapt/ramws.py:600-602` |
| `E_RAM_ESCAPE` | `RamError.code` | `5` | Invalid `tool`, `campaign`, or `run_id` in `_name()`; `ws.path(rel)` given empty string, `\0`, absolute path, `..`, or symlink escaping `ws.root`; `ws.release(p)` targeting `.`, `..`, `ws.root` itself, or a path outside `ws.root`. | `vadapt/ramws.py:33`, `vadapt/ramws.py:60-62`, `vadapt/ramws.py:139-146`, `vadapt/ramws.py:189-190`, `vadapt/ramws.py:600-602` |
| `E_RAM_LOCKED` | `RamError.code` | `5` | Operation on a closed `RamWorkspace` (`_live()`); `open_run` finds existing `run` directory (`FileExistsError`), fails 3 times to create `run` (`FileNotFoundError`), or fails `fcntl.flock(fd, LOCK_EX \| LOCK_NB)` on `.lock`. | `vadapt/ramws.py:33`, `vadapt/ramws.py:131-133`, `vadapt/ramws.py:473-485`, `vadapt/ramws.py:600-602` |
| `E_RAM_SHARE` | `RamError.code` | `5` | `share_with_uid` or `share_group` is not `int` (or is `bool`) or not in `[0, 2**32)`; ACL rollback fails (`st_mode & 0o7077 != 0` on `run` or `st_mode & 0o177 != 0` on `SHARED_FILES`); or neither ACL nor group sharing succeeds. | `vadapt/ramws.py:33`, `vadapt/ramws.py:415-425`, `vadapt/ramws.py:437-439`, `vadapt/ramws.py:493-498`, `vadapt/ramws.py:600-602` |
| `E_DISK_FRAMES` | CLI JSON `code` | `5` | `check-disk` CLI finds one or more offending frame files outside allow-listed directories. | `vadapt/ramws.py:593-596` |
| `E_USAGE` | CLI JSON `code` | `2` | `python3 -m vadapt.ramws` invoked with missing or unrecognized subcommand/arguments. | `vadapt/ramws.py:598-599` |
| `W_RAM_SWAP` | Receipt warning | — | Emitted in `ws.receipt()["warnings"]` when `_swap_used() - self._swap0 > SWAP_WARN` (`1 GiB`). | `vadapt/ramws.py:23`, `vadapt/ramws.py:204-205` |

---

## 4. Items Marked `ABSENT` in Bound Source Files

The following items are not defined in `vadapt/rtring.py`, `vadapt/ramws.py`, or `AGENTS.md` at ref `bridge/integration` and are therefore `ABSENT`:

1. **Global Header Bytes `48..64`**: No fields are packed into bytes `48..64` of the 64-byte global header (`_G` is `48` bytes; `vadapt/rtring.py:38`, `vadapt/rtring.py:43`).
2. **Slot Header Bytes `+20..+64`**: No fields are packed into bytes `+20..+64` of each 64-byte slot header (`seq` at `+0..+8`, `render_index` at `+8..+16`, `crc32` at `+16..+20`; `vadapt/rtring.py:39`, `vadapt/rtring.py:44-47`).
3. **Reader File-Size Full Payload Check on Open**: `RingReader.__init__` checks `size < HDR` (`64`), `magic == b"VQARING1"`, and `ver == 2` (`vadapt/rtring.py:222-230`), but a check in `__init__` that `size >= ring_bytes(slots, width, height)` is `ABSENT`.
4. **Reader Context Manager Protocol**: `RingWriter` defines `__enter__` and `__exit__` (`vadapt/rtring.py:193-197`), whereas `__enter__` and `__exit__` on `RingReader` are `ABSENT` (`vadapt/rtring.py:211-322`).
5. **Pre-Window Overrun/Gap Recording on First `poll(latest=False)`**: In `RingReader.poll(latest=False)`, the pre-window lapped loop `if self.last >= 0 and lo > self.last + 1:` is guarded by `self.last >= 0` (`vadapt/rtring.py:294-297`), so overrun/gap recording for `0 .. lo - 1` when `self.last == -1` (before the first frame is ever read) is `ABSENT`.
6. **Hardware Atomic Memory Barriers in Python**: CPU acquire/release fences (`stlr`/`ldar`, `dmb ish`, `AtomicU64`) are `ABSENT` in Python `vadapt/rtring.py`; only program-ordered `mmap` reads/writes plus per-slot `zlib.crc32` verification are implemented (`vadapt/rtring.py:16-20`).
7. **Multi-Producer or Multi-File Ring Support**: Multi-producer locking, non-`rgb24` pixel formats in the ring, and custom `visioncore.errors.VqaError` codes for ring errors are `ABSENT` (`vadapt/rtring.py` raises `ValueError`, `FileExistsError`, and `ramws.RamError`).

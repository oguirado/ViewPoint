# LANG-X Experiment X1 Fixtures

Shared conformance fixtures for LANG-X experiment X1 in `langx/x1/fixtures`, used by 15 independent language seeds in Rust, Go and Python that implement the same two kernels.

## Layout

- `gen_fixtures.py`: Python 3 script (stdlib only) that regenerates every fixture file byte-identically.
- `check_fixtures.py`: Verification script (stdlib only) that regenerates fixtures in memory and verifies every file byte for byte against independent reference implementations.
- `test_gen_fixtures.py`: `unittest` suite testing generation determinism, reference kernel worked check, and failure detection on corrupted data.
- `repack_cases.json`: Test cases metadata for Kernel K1.
- `repack_src.bin`: Concatenated source binary data for small repack cases.
- `repack_dst.bin`: Concatenated expected destination binary data for small repack cases.
- `damage_cases.json`: Test cases metadata and expected tile bitsets for Kernel K2.

## How a Seed Loads Cases

Language seeds loading these fixtures should look for the files in `langx/x1/fixtures`.
**Note for Seeds:** If the `langx/x1/fixtures` directory or fixture files are absent, seeds must skip execution with a clear, informative message (e.g., `Skipping: langx/x1/fixtures not found`).

When present:
1. `repack_cases.json`: Small cases specify `src_offset`, `src_buf_len`, `dst_offset`, and `dst_buf_len` into `repack_src.bin` and `repack_dst.bin`. Large cases specify a SplitMix64 `seed` and `expected_fnv64` FNV-1a 64-bit hash. Error cases specify expected error codes (`E_DIM`, `E_STRIDE`, `E_SHORT`).
2. `damage_cases.json`: Cases specify `width`, `height`, `s`, `rects` list, and either expected words (`expected_words` as 16-digit lowercase hex strings) and popcount (`expected_popcount`), or expected error code (`E_DIM`, `E_GRID`, `E_RECT`).

## Kernel Contracts

### Kernel K1: BGRX32 to RGB24 Repack with Stride
- **Inputs:** Width $W$ and height $H$, each $0$ to $16384$, a source byte buffer with `src_stride` bytes per row, `src_stride` at least $4 \times W$, and `dst_stride` at least $3 \times W$.
- **Source Layout:** Source pixel $x$ of row $y$ occupies bytes $y \times \text{src\_stride} + 4x + [0, 1, 2, 3]$ holding B, G, R, X. The X byte and source row padding are never copied.
- **Output Layout:** Output byte $y \times \text{dst\_stride} + 3x + [0, 1, 2]$ receives R, G, B. Pure byte permutation with no arithmetic and no rounding.
- **Destination Padding:** Destination row padding bytes, from $3W$ to `dst_stride`, are left untouched.
- **Buffer Lengths:** Source buffer must be at least $(H - 1) \times \text{src\_stride} + 4W$ bytes long; the last row may omit its padding. Destination buffer must be at least $(H - 1) \times \text{dst\_stride} + 3W$ bytes long.
- **Zero Dimensions:** $W = 0$ or $H = 0$ is valid and writes nothing.
- **Errors (checked before any write):**
  - `E_DIM`: $W$ or $H$ outside $0$ to $16384$.
  - `E_STRIDE`: `src_stride` $< 4W$ or `dst_stride` $< 3W$.
  - `E_SHORT`: Source or destination buffer too short.

### Kernel K2: Damage Rectangles to Tile Bitset for Grid $s$
- **Inputs:** Screen width $W$ and height $H$, each $1$ to $16384$, tile size $s$ a power of two from $1$ to $256$, and a list of rectangles $[x, y, w, h]$ as signed 32-bit integers.
- **Grid Dimensions:** $gw = \lceil W / s \rceil$, $gh = \lceil H / s \rceil$. The last column and last row may be partial edge tiles and are included normally.
- **Clipping (in 64-bit):**
  - $x_0 = \max(x, 0)$
  - $y_0 = \max(y, 0)$
  - $x_1 = \min(x + w, W)$
  - $y_1 = \min(y + h, H)$
- **Rectangle Skipping:**
  - A rectangle with $w < 0$ or $h < 0$ produces error `E_RECT`.
  - A rectangle whose clipped $x_1 \le x_0$ or $y_1 \le y_0$ is skipped and sets no bit (e.g. $x = 33, w = 0$ sets nothing).
- **Bitset Update:** Otherwise set every tile $tx$ from $\lfloor x_0 / s \rfloor$ up to but excluding $\lceil x_1 / s \rceil$, and $ty$ from $\lfloor y_0 / s \rfloor$ up to but excluding $\lceil y_1 / s \rceil$.
- **Tile Indexing & Bitset:** Tile index $t = ty \times gw + tx$. Bitset consists of $\lceil (gw \times gh) / 64 \rceil$ unsigned 64-bit words. Bit $t$ is bit $t \bmod 64$ of word $t \div 64$ counted from LSB. Unused high bits of the last word are $0$. Serialized bytes are the words in little-endian order. Returns popcount and words.
- **Errors:**
  - `E_DIM`: $W$ or $H$ outside $1$ to $16384$.
  - `E_GRID`: $s$ not a power of two in $1 \dots 256$.
  - `E_RECT`: $w < 0$ or $h < 0$.

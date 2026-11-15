"""CRC32C research prototype for problem P7.

Castagnoli polynomial reflected 0x82F63B78.
Includes:
- Bitwise reference implementation
- Pure Python slice-by-8 table implementation
- NumPy row-batched implementation for uint8 luma frames and buffers
"""

import numpy as np

CASTAGNOLI_POLY = 0x82F63B78
CHECK_VALUE_123456789 = 0xE3069283

# Build Slice-by-8 lookup tables for pure Python and NumPy
_TABLES_PY = [[0] * 256 for _ in range(8)]
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (_c >> 1) ^ CASTAGNOLI_POLY if (_c & 1) else (_c >> 1)
    _TABLES_PY[0][_i] = _c

for _k in range(1, 8):
    for _i in range(256):
        _TABLES_PY[_k][_i] = (_TABLES_PY[_k - 1][_i] >> 8) ^ _TABLES_PY[0][
            _TABLES_PY[_k - 1][_i] & 0xFF
        ]

_T0_PY, _T1_PY, _T2_PY, _T3_PY, _T4_PY, _T5_PY, _T6_PY, _T7_PY = _TABLES_PY
_T0_NP = np.array(_TABLES_PY[0], dtype=np.uint32)


def crc32c_bitwise(data, init: int = 0xFFFFFFFF) -> int:
    """Bitwise reference CRC32C computation.

    Args:
        data (bytes | bytearray | memoryview | np.ndarray): Input byte buffer.
        init (int): Initial CRC value, default 0xFFFFFFFF.

    Returns:
        int: 32-bit CRC32C check value.
    """
    if isinstance(data, (bytes, bytearray, memoryview)):
        buf = bytes(data)
    elif hasattr(data, "tobytes"):
        buf = data.tobytes()
    else:
        buf = bytes(data)

    crc = init & 0xFFFFFFFF
    for b in buf:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ CASTAGNOLI_POLY if (crc & 1) else (crc >> 1)
    return (crc ^ 0xFFFFFFFF) & 0xFFFFFFFF


def crc32c_slice8(data, init: int = 0xFFFFFFFF) -> int:
    """Pure Python slice-by-8 table-driven CRC32C computation.

    Args:
        data (bytes | bytearray | memoryview | np.ndarray): Input byte buffer.
        init (int): Initial CRC value, default 0xFFFFFFFF.

    Returns:
        int: 32-bit CRC32C check value.
    """
    if isinstance(data, (bytes, bytearray, memoryview)):
        buf = bytes(data)
    elif hasattr(data, "tobytes"):
        buf = data.tobytes()
    else:
        buf = bytes(data)

    crc = init & 0xFFFFFFFF
    n = len(buf)
    i = 0

    while n - i >= 8:
        b0 = buf[i] ^ (crc & 0xFF)
        b1 = buf[i + 1] ^ ((crc >> 8) & 0xFF)
        b2 = buf[i + 2] ^ ((crc >> 16) & 0xFF)
        b3 = buf[i + 3] ^ ((crc >> 24) & 0xFF)
        b4 = buf[i + 4]
        b5 = buf[i + 5]
        b6 = buf[i + 6]
        b7 = buf[i + 7]

        crc = (
            _T7_PY[b0]
            ^ _T6_PY[b1]
            ^ _T5_PY[b2]
            ^ _T4_PY[b3]
            ^ _T3_PY[b4]
            ^ _T2_PY[b5]
            ^ _T1_PY[b6]
            ^ _T0_PY[b7]
        )
        i += 8

    while i < n:
        crc = (crc >> 8) ^ _T0_PY[(crc ^ buf[i]) & 0xFF]
        i += 1

    return (crc ^ 0xFFFFFFFF) & 0xFFFFFFFF


def crc32c_numpy(data, init: int = 0xFFFFFFFF):
    """NumPy table-driven CRC32C computation.

    Supports 1D byte buffers (returns single uint32 int) or 2D batched row buffers
    of shape (M, N) (returns uint32 1D numpy array of shape (M,)).

    Args:
        data (bytes | bytearray | memoryview | np.ndarray): Input 1D or 2D buffer.
        init (int): Initial CRC value, default 0xFFFFFFFF.

    Returns:
        int | np.ndarray: 32-bit CRC32C check value(s).
    """
    if isinstance(data, (bytes, bytearray, memoryview)):
        arr = np.frombuffer(data, dtype=np.uint8)
        is_1d = True
    else:
        arr = np.asarray(data, dtype=np.uint8)
        is_1d = arr.ndim <= 1

    if is_1d:
        arr_2d = arr.reshape(1, -1)
    else:
        arr_2d = arr

    M, N = arr_2d.shape
    crc = np.full(M, init & 0xFFFFFFFF, dtype=np.uint32)
    arr_u32 = arr_2d.astype(np.uint32)

    for j in range(N):
        idx = (crc ^ arr_u32[:, j]) & np.uint32(0xFF)
        crc = (crc >> np.uint32(8)) ^ _T0_NP[idx]

    res = (crc ^ np.uint32(0xFFFFFFFF)) & np.uint32(0xFFFFFFFF)

    if is_1d:
        return int(res[0])
    return res


def crc32c_frame_tiles(frame: np.ndarray, s: int = 32) -> np.ndarray:
    """Compute per-tile CRCs for every s by s tile of a uint8 luma frame.

    Uses NumPy row-batched computation, processing one CRC per tile row block/tile.

    Args:
        frame (np.ndarray): 2D uint8 luma frame of shape (H, W).
        s (int): Tile dimension in pixels (default 32).

    Returns:
        np.ndarray: 1D uint32 array of CRCs for all (gh * gw) tiles in row-major tile order.
    """
    frame = np.asarray(frame, dtype=np.uint8)
    H, W = frame.shape
    gh = H // s
    gw = W // s

    # Extract non-overlapping s x s tiles in raster tile order
    tiles = (
        frame[: gh * s, : gw * s]
        .reshape(gh, s, gw, s)
        .swapaxes(1, 2)
        .reshape(gh * gw, s * s)
    )

    return crc32c_numpy(tiles, init=0xFFFFFFFF)

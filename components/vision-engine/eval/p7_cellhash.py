"""Research prototype for problem P7 fast tile hashing from screened paper."""
import numpy as np

MASK_64 = 0xFFFFFFFFFFFFFFFF
P1 = 11400714819323198485  # 2^64 / phi = 0x9E3779B97F4A7C15
P2 = 0x61C8864680B583EB  # 2^64 / phi^2 | 1
P3 = 0x3C6EF372FED4F879  # 2^64 / phi^3 | 1

C1 = 0xFF51AFD7ED558CCD
C2 = 0xC4CEB9FE1A85EC53
INV_C1 = 0x4F74430C22A54005
INV_C2 = 0x9CB4B2F8129337DB

FNV_OFFSET_64 = np.uint64(14695981039346656037)
FNV_PRIME_64 = np.uint64(1099511628211)


def _xorshift33(x: int) -> int:
    return (x ^ (x >> 33)) & MASK_64


def fmix64(k: int) -> int:
    """MurmurHash3 finaliser with xor-shift 33, multiply C1, xor-shift 33, multiply C2, xor-shift 33 (modulo 2^64)."""
    k = (int(k) & MASK_64)
    k = _xorshift33(k)
    k = (k * C1) & MASK_64
    k = _xorshift33(k)
    k = (k * C2) & MASK_64
    k = _xorshift33(k)
    return k


def inv_fmix64(k: int) -> int:
    """Explicit inverse of fmix64 that round trips."""
    k = (int(k) & MASK_64)
    k = _xorshift33(k)
    k = (k * INV_C2) & MASK_64
    k = _xorshift33(k)
    k = (k * INV_C1) & MASK_64
    k = _xorshift33(k)
    return k


def hash_cell2(seed: int, x: int, y: int) -> int:
    """hash_cell2 equal to fmix64 of (fmix64(seed) + P3 + x * P1) xor (y * P2) modulo 2^64."""
    s_mix = fmix64(seed)
    x_part = (int(x) * P1) & MASK_64
    y_part = (int(y) * P2) & MASK_64
    inner = ((s_mix + P3 + x_part) & MASK_64) ^ y_part
    return fmix64(inner)


def fnv1a64(val: int) -> int:
    """FNV-1a 64 fold of a 64-bit uint value by bytes taken with logical shifts so the result is endian-neutral."""
    h = 14695981039346656037
    prime = 1099511628211
    val = int(val) & MASK_64
    for i in range(8):
        b = (val >> (i * 8)) & 0xFF
        h = ((h ^ b) * prime) & MASK_64
    return h


def multiset_digest(entries) -> int:
    """Multiset digest equal to the sum of entry hashes modulo 2^64."""
    tot = sum(int(e) & MASK_64 for e in entries)
    return tot & MASK_64


def fibonacci_slot(h: int, b: int) -> int:
    """Fibonacci slot equal to h times 11400714819323198485 modulo 2^64 shifted right by 64 minus b."""
    m = (int(h) * P1) & MASK_64
    return m >> (64 - int(b))


def tile_digest(frame: np.ndarray, s: int = 16) -> np.ndarray:
    """Hashes each s by s uint8 luma tile of a frame vectorised in numpy uint64."""
    H, W = frame.shape
    gh = H // s
    gw = W // s
    num_tiles = gh * gw
    if num_tiles == 0:
        return np.zeros(0, dtype=np.uint64)

    tiles = frame[:gh * s, :gw * s].reshape(gh, s, gw, s).swapaxes(1, 2).reshape(num_tiles, s, s)
    words = np.ascontiguousarray(tiles).view("<u8").reshape(num_tiles, -1)

    h = np.full(num_tiles, FNV_OFFSET_64, dtype=np.uint64)
    num_words = words.shape[1]

    for w_idx in range(num_words):
        w = words[:, w_idx]
        for i in range(8):
            b = ((w >> np.uint64(i * 8)) & np.uint64(0xFF)).astype(np.uint64)
            h = (h ^ b) * FNV_PRIME_64

    return h

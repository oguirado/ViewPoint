"""Shared pure-stdlib canonical helpers (R-B1, R-B9). Host and Blender may both import this module."""
import json


# @anchor:bpy.canon.canonical_json
def canonical_json(value):
    """Sorted-key compact JSON with trailing newline; floats are refused (R-B8)."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"


# @anchor:bpy.canon.cycles_seed
def cycles_seed(bench_no, k, ri):
    """R-B9: SplitMix64 finalizer over an injective (bench_no, k, ri) pre-image, masked to int31."""
    for v, bits in ((bench_no, 24), (k, 20), (ri, 20)):
        if type(v) is not int or not 0 <= v < (1 << bits):
            raise ValueError("seed coordinate")
    mask = (1 << 64) - 1
    x = (((bench_no << 40) | (k << 20) | ri) * 0x9E3779B97F4A7C15) & mask
    x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9) & mask
    x = ((x ^ (x >> 27)) * 0x94D049BB133111EB) & mask
    return (x ^ (x >> 31)) & 0x7FFFFFFF

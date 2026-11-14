#!/usr/bin/env python3
"""LANG-X X1 host conformance + timing harness.

Runs every fixture case of langx/x1/fixtures through the kernels of every
seed (go_w*, py_w*, rust_w*) and compares bytes / words / popcount / error
codes exactly. Optional timing mode measures K1 and K2 at 1080p.

Seeds are never modified: Python seeds are imported by path under an isolated
module name; Go and Rust seeds are reached through thin adapters in
langx/x1/host/adapters/<seed>/ that are built into $TMPDIR and spoken to over
a binary stdin/stdout protocol (see README.md).

STORAGE LAW: frame/pixel buffers live only in RAM (generated in-process and
piped to adapters); nothing pixel-shaped is written to disk.

Usage: see README.md. stdlib + numpy only.
"""
import argparse
import importlib.util
import json
import os
import struct
import subprocess
import sys
import tempfile
import time

import numpy as np

HOST = os.path.dirname(os.path.abspath(__file__))
X1 = os.path.dirname(HOST)
FIX = os.path.join(X1, "fixtures")
ADAPTERS = os.path.join(HOST, "adapters")

GO_SEEDS = [f"go_w{i}" for i in range(1, 6)]
PY_SEEDS = [f"py_w{i}" for i in range(1, 6)]
RUST_SEEDS = [f"rust_w{i}" for i in range(1, 6)]
ALL_SEEDS = GO_SEEDS + PY_SEEDS + RUST_SEEDS

CODES = {0: None, 1: "E_DIM", 2: "E_STRIDE", 3: "E_SHORT", 4: "E_RECT", 5: "E_GRID"}
ST_PANIC, ST_OTHER, ST_NA = 97, 98, 99
NA = "N/A"  # input not representable in the seed's API types (e.g. negative into u32)

MASK64 = (1 << 64) - 1
PIN_CPUS = "2-4"
USE_COMMITTED_BINS = False


# --------------------------------------------------------------------------
# Reference helpers (independent of every seed)
# --------------------------------------------------------------------------
def splitmix_bytes(seed, n):
    """SplitMix64 stream as bytes (little-endian u64s), vectorised."""
    k = (n + 7) // 8
    with np.errstate(over="ignore"):
        idx = np.arange(1, k + 1, dtype=np.uint64)
        z = np.uint64(seed) + idx * np.uint64(0x9E3779B97F4A7C15)
        z = (z ^ (z >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
        z = (z ^ (z >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
        z = z ^ (z >> np.uint64(31))
    return z.astype("<u8").tobytes()[:n]


def fnv1a64(data):
    h = 0xCBF29CE484222325
    for b in data:
        h = ((h ^ b) * 0x100000001B3) & MASK64
    return h


def ref_k1(w, h, src, ss, dst, ds):
    out = bytearray(dst)
    if w == 0 or h == 0:
        return out
    a = np.frombuffer(src, dtype=np.uint8)
    o = np.frombuffer(out, dtype=np.uint8)
    sv = np.lib.stride_tricks.as_strided(a, shape=(h, w, 4), strides=(ss, 4, 1))
    dv = np.lib.stride_tricks.as_strided(o, shape=(h, w, 3), strides=(ds, 3, 1))
    dv[...] = sv[:, :, [2, 1, 0]]
    return out


def ref_k2(w, h, s, rects):
    gw, gh = -(-w // s), -(-h // s)
    words = [0] * (-(-(gw * gh) // 64))
    for x, y, rw, rh in rects:
        x0, y0, x1, y1 = max(x, 0), max(y, 0), min(x + rw, w), min(y + rh, h)
        if x1 <= x0 or y1 <= y0:
            continue
        for ty in range(y0 // s, -(-y1 // s)):
            for tx in range(x0 // s, -(-x1 // s)):
                t = ty * gw + tx
                words[t >> 6] |= 1 << (t & 63)
    return words, sum(bin(v).count("1") for v in words)


def bench_rects(w, h, n=64, seed=0x5EED):
    """64 random rects with a fixed SplitMix64 seed (deterministic)."""
    r = np.frombuffer(splitmix_bytes(seed, n * 4 * 8), dtype="<u8").astype(object)
    rects = []
    for i in range(n):
        a, b, c, d = (int(v) for v in r[4 * i:4 * i + 4])
        rects.append([a % (w + 64) - 32, b % (h + 64) - 32, c % 400, d % 300])
    return rects


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
def load_fixtures():
    def cases(name):
        doc = json.load(open(os.path.join(FIX, name)))
        return doc["cases"] if isinstance(doc, dict) else doc  # unwrap {"schema","cases"}
    rcases = cases("repack_cases.json")
    with open(os.path.join(FIX, "repack_src.bin"), "rb") as f:
        srcbin = f.read()
    with open(os.path.join(FIX, "repack_dst.bin"), "rb") as f:
        dstbin = f.read()
    small = [c for c in rcases if c["type"] == "small"]
    need_s = sum(c["src_buf_len"] for c in small)
    need_d = sum(c["dst_buf_len"] for c in small)
    info = {"committed_src_bin_len": len(srcbin), "committed_dst_bin_len": len(dstbin),
            "expected_src_bin_len": need_s, "expected_dst_bin_len": need_d}
    if (len(srcbin), len(dstbin)) != (need_s, need_d) and not USE_COMMITTED_BINS:
        # Committed .bin files are corrupt (escaped text, not raw bytes); rebuild the
        # small-case buffers in RAM from gen_fixtures.py's small_specs table.
        srcbin, dstbin = regen_small_bins(small)
        info["small_bins"] = "regenerated in RAM from gen_fixtures.py small_specs (committed bins corrupt)"
    else:
        info["small_bins"] = "committed repack_src.bin / repack_dst.bin"
    return rcases, cases("damage_cases.json"), srcbin, dstbin, info


def regen_small_bins(small):
    """Parse (never execute) gen_fixtures.py, take small_specs, regenerate buffers."""
    import ast
    tree = ast.parse(open(os.path.join(FIX, "gen_fixtures.py")).read())
    specs = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "small_specs" for t in node.targets):
            specs = ast.literal_eval(node.value)
    if specs is None:
        raise SystemExit("small_specs not found in gen_fixtures.py")
    src_all, dst_all = bytearray(), bytearray()
    byname = {c["name"]: c for c in small}
    for name, W, H, ss, ds, src_pad_last, dst_pad_last, seed in specs:
        if H == 0:
            sl = dl = 0
        else:
            sl = (H - 1) * ss + 4 * W + (ss - 4 * W if src_pad_last else 0)
            dl = (H - 1) * ds + 3 * W + (ds - 3 * W if dst_pad_last else 0)
        c = byname[name]
        assert (c["width"], c["height"], c["src_stride"], c["dst_stride"], c["src_buf_len"],
                c["dst_buf_len"], c["src_offset"], c["dst_offset"]) == (W, H, ss, ds, sl, dl, len(src_all), len(dst_all)), name
        src = splitmix_bytes(seed, sl)
        src_all += src
        dst_all += ref_k1(W, H, src, ss, b"\xEE" * dl, ds)
    return bytes(src_all), bytes(dst_all)


def k1_inputs(c, srcbin, dstbin, large_cache):
    """Returns (src, dst_init, expected_dst_or_None, expected_err_or_None)."""
    t = c["type"]
    if t == "small":
        src = srcbin[c["src_offset"]:c["src_offset"] + c["src_buf_len"]]
        exp = dstbin[c["dst_offset"]:c["dst_offset"] + c["dst_buf_len"]]
        return src, bytes(b"\xEE" * c["dst_buf_len"]), exp, None
    if t == "error":
        src = splitmix_bytes(7, max(c["src_buf_len"], 0))
        return src, bytes(b"\xEE" * max(c["dst_buf_len"], 0)), None, c["error"]
    # large: regenerate in RAM, reference output verified against expected_fnv64 once
    if c["name"] not in large_cache:
        src = splitmix_bytes(c["seed"], c["src_buf_len"])
        dst = bytes(b"\xEE" * c["dst_buf_len"])
        exp = bytes(ref_k1(c["width"], c["height"], src, c["src_stride"], dst, c["dst_stride"]))
        got = f"{fnv1a64(exp):016x}"
        if got != c["expected_fnv64"]:
            raise SystemExit(f"host reference disagrees with fixture fnv for {c['name']}: {got}")
        large_cache[c["name"]] = (src, dst, exp)
    return large_cache[c["name"]] + (None,)


# --------------------------------------------------------------------------
# Runners
# --------------------------------------------------------------------------
class PyRunner:
    ALT = {  # alternate (plain-loop) K2 entry points, reported separately
        "py_w1": lambda m: (lambda w, h, s, r: m.damage_tiles(w, h, s, r, use_numpy_path=False)),
        "py_w2": lambda m: (lambda w, h, s, r: m.damage_tiles(w, h, s, r, use_numpy=False)),
        "py_w3": lambda m: (lambda w, h, s, r: m.damage_tiles(w, h, s, r, use_numpy=False)),
        "py_w4": lambda m: m.damage_tiles_plain,
        "py_w5": lambda m: (lambda w, h, s, r: m.damage_tiles(w, h, s, r, use_plain_loop=True)),
    }

    def __init__(self, seed):
        self.seed = seed
        path = os.path.join(X1, seed, "x1.py")
        spec = importlib.util.spec_from_file_location(f"langx_x1_{seed}_x1", path)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.alt_k2 = self.ALT[seed](self.mod)

    def _err(self, e):
        if isinstance(e, self.mod.X1Error):
            code = getattr(e, "code", None) or (e.args[0] if e.args else None)
            return code if code in CODES.values() else f"OTHER:{code!r}"
        return f"EXC:{type(e).__name__}:{e}"

    def k1(self, w, h, ss, ds, src, dst):
        buf = bytearray(dst)
        try:
            self.mod.repack_bgrx_to_rgb(bytes(src), w, h, ss, buf, ds)
            return None, bytes(buf)
        except Exception as e:  # noqa: BLE001
            return self._err(e), bytes(buf)

    def k2(self, w, h, s, rects, fn=None):
        try:
            words, pc = (fn or self.mod.damage_tiles)(w, h, s, [list(r) for r in rects])
            return None, int(pc), [int(v) & MASK64 for v in words]
        except Exception as e:  # noqa: BLE001
            return self._err(e), None, None

    def bench_k1(self, w, h, ss, ds, src, dst, warm, iters):
        src, buf, f = bytes(src), bytearray(dst), self.mod.repack_bgrx_to_rgb
        for _ in range(warm):
            f(src, w, h, ss, buf, ds)
        out = []
        for _ in range(iters):
            t0 = time.perf_counter_ns()
            f(src, w, h, ss, buf, ds)
            out.append(time.perf_counter_ns() - t0)
        return out

    def bench_k2(self, w, h, s, rects, warm, iters):
        f, rects = self.mod.damage_tiles, [list(r) for r in rects]
        for _ in range(warm):
            f(w, h, s, rects)
        out = []
        for _ in range(iters):
            t0 = time.perf_counter_ns()
            f(w, h, s, rects)
            out.append(time.perf_counter_ns() - t0)
        return out

    def close(self):
        pass


class ProcRunner:
    def __init__(self, seed, exe, pin):
        self.seed = seed
        cmd = ([ "taskset", "-c", PIN_CPUS] if pin else []) + [exe]
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def _r(self, n):
        b = self.p.stdout.read(n)
        if len(b) != n:
            raise RuntimeError(f"{self.seed}: adapter died (rc={self.p.poll()})")
        return b

    def _status(self):
        st = struct.unpack("<i", self._r(4))[0]
        if st in CODES:
            return CODES[st]
        if st == ST_NA:
            return NA
        msg = self._r(struct.unpack("<I", self._r(4))[0]).decode("utf8", "replace")
        return ("PANIC:" if st == ST_PANIC else "OTHER:") + msg

    def _send(self, *parts):
        self.p.stdin.write(b"".join(parts))
        self.p.stdin.flush()

    def _k1msg(self, op, w, h, ss, ds, src, dst):
        return [bytes([op]), struct.pack("<qqqqQQ", w, h, ss, ds, len(src), len(dst)), src, dst]

    def _k2msg(self, op, w, h, s, rects):
        return [bytes([op]), struct.pack("<qqqQ", w, h, s, len(rects)),
                b"".join(struct.pack("<iiii", *r) for r in rects)]

    def k1(self, w, h, ss, ds, src, dst):
        self._send(*self._k1msg(1, w, h, ss, ds, src, dst))
        st = self._status()
        n = struct.unpack("<Q", self._r(8))[0]
        return st, self._r(n)

    def k2(self, w, h, s, rects, fn=None):
        self._send(*self._k2msg(2, w, h, s, rects))
        st = self._status()
        pc, n = struct.unpack("<QQ", self._r(16))
        words = list(struct.unpack(f"<{n}Q", self._r(8 * n)))
        return st, (pc if st is None else None), (words if st is None else None)

    def _times(self):
        n = struct.unpack("<I", self._r(4))[0]
        return list(struct.unpack(f"<{n}Q", self._r(8 * n)))

    def bench_k1(self, w, h, ss, ds, src, dst, warm, iters):
        self._send(*self._k1msg(3, w, h, ss, ds, src, dst), struct.pack("<II", warm, iters))
        return self._times()

    def bench_k2(self, w, h, s, rects, warm, iters):
        self._send(*self._k2msg(4, w, h, s, rects), struct.pack("<II", warm, iters))
        return self._times()

    def close(self):
        try:
            self._send(b"\x00")
            self.p.wait(timeout=10)
        except Exception:  # noqa: BLE001
            self.p.kill()


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------
def tmpdir():
    d = os.path.join(os.environ.get("TMPDIR") or tempfile.gettempdir(), "langx_x1_host")
    os.makedirs(d, exist_ok=True)
    return d


def build(seed, log):
    t = tmpdir()
    env = dict(os.environ)
    adir = os.path.join(ADAPTERS, seed)
    if seed.startswith("go_"):
        exe = os.path.join(t, "bin", seed)
        env.update(GOCACHE=os.path.join(t, "gocache"), GOPATH=os.path.join(t, "gopath"),
                   GOPROXY="off", GOFLAGS="-mod=mod", GOTOOLCHAIN="local", GOWORK="off")
        cmd = ["go", "build", "-trimpath", "-o", exe, "."]
    else:
        target = os.path.join(t, "cargo-target", seed)
        env.update(CARGO_TARGET_DIR=target)
        exe = os.path.join(target, "release", f"x1_host_adapter_{seed}")
        cmd = ["cargo", "build", "--release", "--offline", "-q"]
    r = subprocess.run(cmd, cwd=adir, env=env, capture_output=True, text=True)
    log[seed] = {"cmd": " ".join(cmd), "rc": r.returncode, "stderr": r.stderr[-2000:]}
    return exe if r.returncode == 0 else None


def runner_for(seed, pin, buildlog):
    if seed.startswith("py_"):
        return PyRunner(seed)
    exe = build(seed, buildlog)
    if exe is None:
        raise RuntimeError(f"build failed rc={buildlog[seed]['rc']}: {buildlog[seed]['stderr'][-400:]}")
    return ProcRunner(seed, exe, pin)


# --------------------------------------------------------------------------
# Conformance
# --------------------------------------------------------------------------
def first_diff(a, b):
    if len(a) != len(b):
        return f"length {len(a)} != expected {len(b)}"
    for i in range(len(a)):
        if a[i] != b[i]:
            return f"byte {i}: got 0x{a[i]:02x} expected 0x{b[i]:02x}"
    return None


def run_k1(r, rcases, srcbin, dstbin, large_cache):
    res = []
    for c in rcases:
        src, dst, exp, eerr = k1_inputs(c, srcbin, dstbin, large_cache)
        try:
            err, out = r.k1(c["width"], c["height"], c["src_stride"], c["dst_stride"], src, dst)
        except Exception as e:  # noqa: BLE001
            res.append({"name": c["name"], "ok": False, "why": f"harness: {e}"})
            break
        if err == NA:
            res.append({"name": c["name"], "ok": None, "why": "N/A: negative dim not representable in seed API (unsigned)"})
            continue
        why = None
        if eerr is not None:
            if err != eerr:
                why = f"error: got {err} expected {eerr}"
            elif out != dst:
                why = f"error {err} returned but dst modified ({first_diff(out, dst)})"
        elif err is not None:
            why = f"unexpected error {err}"
        else:
            d = None if out == exp else first_diff(out, exp)
            why = f"output mismatch: {d}" if d else None
        res.append({"name": c["name"], "ok": why is None, "why": why})
    return res


def run_k2(r, dcases, fn=None, valid_only=False):
    res = []
    for c in dcases:
        eerr = c.get("error")
        if valid_only and eerr is not None:
            continue
        try:
            err, pc, words = r.k2(c["width"], c["height"], c["s"], c["rects"], fn)
        except Exception as e:  # noqa: BLE001
            res.append({"name": c["name"], "ok": False, "why": f"harness: {e}"})
            break
        if err == NA:
            res.append({"name": c["name"], "ok": None, "why": "N/A: negative value not representable in seed API (unsigned)"})
            continue
        why = None
        if eerr is not None:
            if err != eerr:
                why = f"error: got {err} expected {eerr}"
        elif err is not None:
            why = f"unexpected error {err}"
        else:
            ew = [int(x, 16) for x in c["expected_words"]]
            if len(words) != len(ew):
                why = f"word count {len(words)} != expected {len(ew)}"
            else:
                for i, (g, e) in enumerate(zip(words, ew)):
                    if g != e:
                        why = f"word {i}: got {g:016x} expected {e:016x}"
                        break
            if why is None and pc != c["expected_popcount"]:
                why = f"popcount {pc} != expected {c['expected_popcount']}"
        res.append({"name": c["name"], "ok": why is None, "why": why})
    return res


def summarize(res):
    p = sum(1 for x in res if x["ok"] is True)
    na = sum(1 for x in res if x["ok"] is None)
    fails = [x for x in res if x["ok"] is False]
    return {"passed": p, "total": len(res) - na, "na": na,
            "first_mismatch": (f"{fails[0]['name']}: {fails[0]['why']}" if fails else None),
            "failed_cases": [f"{x['name']}: {x['why']}" for x in fails]}


# --------------------------------------------------------------------------
# Timing
# --------------------------------------------------------------------------
def timing(r, runs, warm):
    W, H, SS, DS = 1920, 1080, 7680, 5760
    src = splitmix_bytes(4242, (H - 1) * SS + 4 * W)  # RAM only
    dst = bytes(DS * H)
    t1 = r.bench_k1(W, H, SS, DS, src, dst, warm, runs)
    rects = bench_rects(W, H)
    t2 = r.bench_k2(W, H, 16, rects, warm, runs)
    # correctness of the timed K2 workload against the host reference
    err, pc, words = r.k2(W, H, 16, rects)
    ew, epc = ref_k2(W, H, 16, rects)
    k2_ok = err is None and words == ew and pc == epc
    med = lambda v: float(np.median(np.array(v, dtype=np.float64))) / 1e6  # noqa: E731
    return {"k1_ms_median": med(t1), "k1_ms_min": min(t1) / 1e6,
            "k2_ms_median": med(t2), "k2_ms_min": min(t2) / 1e6,
            "runs": runs, "warmup": warm, "k2_bench_output_matches_ref": k2_ok}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seeds", default=",".join(ALL_SEEDS))
    ap.add_argument("--time", action="store_true", help="also run the 1080p timing mode")
    ap.add_argument("--runs", type=int, default=101)
    ap.add_argument("--warmup", type=int, default=5)
    ap.add_argument("--no-pin", action="store_true", help="do not pin to cpus 2-4")
    ap.add_argument("--use-committed-bins", action="store_true",
                    help="use the committed repack_*.bin even if their length is wrong")
    ap.add_argument("--out", default=os.path.join(HOST, "results.json"))
    a = ap.parse_args()
    pin = not a.no_pin
    if pin:
        os.sched_setaffinity(0, {2, 3, 4})  # the driver + in-process Python seeds
    global USE_COMMITTED_BINS
    USE_COMMITTED_BINS = a.use_committed_bins
    rcases, dcases, srcbin, dstbin, fixinfo = load_fixtures()
    print(f"fixtures: {fixinfo}", flush=True)
    large_cache, buildlog, out = {}, {}, {}
    t0 = time.time()
    for seed in [s for s in a.seeds.split(",") if s]:
        entry = {"seed": seed}
        try:
            r = runner_for(seed, pin, buildlog)
        except Exception as e:  # noqa: BLE001
            entry["adapt_error"] = str(e)
            out[seed] = entry
            print(f"{seed}: NOT ADAPTED: {e}", flush=True)
            continue
        try:
            k1 = run_k1(r, rcases, srcbin, dstbin, large_cache)
            k2 = run_k2(r, dcases)
            entry["k1"], entry["k2"] = summarize(k1), summarize(k2)
            if isinstance(r, PyRunner):
                entry["k2_alt_plain_valid_cases"] = summarize(run_k2(r, dcases, fn=r.alt_k2, valid_only=True))
            if a.time:
                entry["timing"] = timing(r, a.runs, a.warmup)
        except Exception as e:  # noqa: BLE001
            entry["run_error"] = str(e)
        finally:
            r.close()
        out[seed] = entry
        k1s, k2s = entry.get("k1", {}), entry.get("k2", {})
        tm = entry.get("timing", {})
        print(f"{seed}: K1 {k1s.get('passed')}/{k1s.get('total')} (na {k1s.get('na')})  "
              f"K2 {k2s.get('passed')}/{k2s.get('total')} (na {k2s.get('na')})"
              + (f"  K1 {tm['k1_ms_median']:.3f} ms  K2 {tm['k2_ms_median']:.4f} ms" if tm else "")
              + (f"  RUN_ERROR {entry['run_error']}" if "run_error" in entry else ""), flush=True)
    meta = {"pinned_cpus": PIN_CPUS if pin else None, "wall_s": round(time.time() - t0, 1),
            "fixtures": fixinfo, "n_repack_cases": len(rcases), "n_damage_cases": len(dcases),
            "python": sys.version.split()[0], "numpy": np.__version__, "build": buildlog}
    with open(a.out, "w") as f:
        json.dump({"meta": meta, "seeds": out}, f, indent=1, sort_keys=True)
        f.write("\n")
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()

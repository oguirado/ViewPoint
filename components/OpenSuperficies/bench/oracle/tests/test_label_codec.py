"""W6a FX3 L5: compact label codec (vqa.oracle.label-compact/v1). Exact round-trip to canonical vqa.oracle.label/v1 JSONL, measured on the
live WL-1s inputs (SLIVE5 run_20261018T225012: 24 labels, 117690 B v1 > the 102400 B cap; fixtures/l5)."""
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zlib

from bench.oracle import label_codec as LC
from bench.oracle import labels_oracle as LO

HERE = os.path.dirname(os.path.abspath(__file__))
L5 = os.path.join(HERE, "fixtures", "l5")
L5B = os.path.join(HERE, "fixtures", "l5b")
WL4_SAMPLES = "/home/user/Música/TeamWork_Antigravity2/campaigns/os-cu-0/w6/evidence/X4L5/wl4_inputs/samples.json"
CLI = [sys.executable, "-B", os.path.join(os.path.dirname(HERE), "label_codec.py")]


def live():
    d = json.load(open(os.path.join(L5, "wl1s_live_samples.json")))
    e = json.load(open(os.path.join(L5, "wl1s_live_expect.json")))
    return LO.derive(d["samples"], d["ops"], e["w"], e["h"], e["run_id"]), e


def v1(labels):
    return "".join(json.dumps(l, sort_keys=True, separators=(",", ":")) + "\n" for l in labels).encode()


def lab(eid, tiles, run_id="r"):
    return {"schema": LO.SCHEMA, "run_id": run_id, "event_id": eid, "f0": 1, "f1": 2, "rects": [[0, 0, 32, 32]], "tiles": tiles, "ideal": 2,
            "mv": None, "period_ms": None, "flags": [], "source": "mutation", "source_ref": "7", "confidence": "exact"}


class Runs(unittest.TestCase):
    def test_runs_maximal_spans(self):
        self.assertEqual(LC.encode_runs(list(range(10)) + [20, 22, 23]), [0, 10, 20, 1, 22, 2])
        self.assertEqual(LC.encode_runs([]), [])
        self.assertEqual(LC.encode_runs([5]), [5, 1])
        self.assertEqual(LC.encode_runs([3, 2, 2]), [3, 1, 2, 1, 2, 1])  # any int list round-trips (order and duplicates kept)

    def test_runs_inverse(self):
        for t in ([], [0], list(range(2040)), [1, 2, 3, 7, 8, 100], [3, 2, 2, 9]):
            self.assertEqual(LC.decode_runs(LC.encode_runs(t)), t)

    def test_runs_non_canonical_refused(self):
        for bad in ([0, 2, 2, 1], [0], [0, 0], [-1, 2], [0, 1.0], [True, 1], "x"):
            with self.assertRaises(ValueError, msg=bad):
                LC.decode_runs(bad)


class RoundTrip(unittest.TestCase):
    def test_live_wl1s_v1_identity_and_size(self):
        labs, e = live()
        b1 = v1(labs)
        self.assertEqual((len(labs), len(b1), hashlib.sha256(b1).hexdigest()), (e["n_labels"], e["v1_bytes"], e["v1_sha256"]))
        c = LC.encode(labs)
        self.assertLessEqual(len(c), 24 * 1024)  # runner oracle dir budget 48 KB (rows <= 32 KB copied there)
        self.assertEqual(LC.canonical_v1(LC.decode(c)), b1)  # byte-for-byte
        self.assertEqual(LC.encode(LC.decode(c)), c)  # the compact form is unique

    def test_header_hoists_shared_fields(self):
        labs, e = live()
        lines = LC.encode(labs).decode().splitlines()
        hd = json.loads(lines[0])
        self.assertEqual(hd, {"codec": LC.CODEC, "n": e["n_labels"], "run_id": e["run_id"], "schema": LO.SCHEMA})
        self.assertEqual(len(lines), 1 + e["n_labels"])
        for ln in lines[1:]:
            r = json.loads(ln)
            self.assertFalse({"schema", "run_id", "event_id", "tiles"} & set(r))
            self.assertIn("t", r)
        self.assertEqual(json.loads(lines[1])["t"], [0, 2040])  # first WL-1s MOVED rect [0,0,1920,1076] covers all 60x34 tiles: one span

    def test_empty(self):
        c = LC.encode([])
        self.assertEqual(json.loads(c), {"codec": LC.CODEC, "n": 0, "run_id": None, "schema": LO.SCHEMA})
        self.assertEqual(LC.decode(c), [])

    def test_not_hoistable_refused(self):
        with self.assertRaises(ValueError):
            LC.encode([lab(0, [1]), lab(1, [2], run_id="other")])
        with self.assertRaises(ValueError):
            LC.encode([lab(1, [1])])  # event_id must equal the line index
        with self.assertRaises(ValueError):
            LC.encode([dict(lab(0, [1]), extra=1)])
        with self.assertRaises(ValueError):
            LC.encode([dict(lab(0, [1]), schema="x")])


class Strict(unittest.TestCase):
    def _c(self):
        return LC.encode([lab(0, [1, 2, 3]), lab(1, [9])]).decode().splitlines()

    def _bad(self, lines):
        with self.assertRaises(ValueError):
            LC.decode(("\n".join(lines) + "\n").encode())

    def test_decode_refuses_non_canonical(self):
        h, a, b = self._c()
        self._bad([h, a])  # n mismatch
        self._bad([h.replace(LC.CODEC, "x"), a, b])
        self._bad([h, a.replace('"t":[1,3]', '"t":[1,1,2,2]'), b])  # mergeable spans
        self._bad([h, a.replace('"t":[1,3]', '"tiles":[1,2,3]'), b])
        self._bad([h, a.replace('{', '{"event_id":0,', 1), b])
        for k, v in (("event_id", 0), ("run_id", "r"), ("zz", 1)):  # canonical line, extra key: refused by the key set, not by the sort order
            self._bad([h, json.dumps(dict(json.loads(a), **{k: v}), sort_keys=True, separators=(",", ":")), b])
        self._bad([h, a.replace(",", ", ", 1), b])  # not compact separators
        self._bad([h.replace('"run_id":"r"', '"run_id":null'), a, b])
        with self.assertRaises(ValueError):
            LC.decode(("\n".join([h, a, b])).encode())  # missing final newline


class Cli(unittest.TestCase):
    def test_cli_decode_encode_rc(self):
        labs, e = live()
        with tempfile.TemporaryDirectory() as td:
            p = lambda n: os.path.join(td, n)
            open(p("v1.jsonl"), "wb").write(v1(labs))
            r = subprocess.run(CLI + ["encode", p("v1.jsonl"), "--out", p("c.jsonl")], capture_output=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            r = subprocess.run(CLI + ["decode", p("c.jsonl")], capture_output=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(hashlib.sha256(r.stdout).hexdigest(), e["v1_sha256"])
            r = subprocess.run(CLI + ["decode", p("c.jsonl"), "--out", p("d.jsonl")], capture_output=True, timeout=60)
            self.assertEqual((r.returncode, open(p("d.jsonl"), "rb").read()), (0, v1(labs)))
            r = subprocess.run(CLI + ["encode", p("v1.jsonl"), "--out", p("c2.jsonl"), "--cap", "100"], capture_output=True, timeout=60)
            self.assertEqual(r.returncode, 5)
            self.assertFalse(os.path.exists(p("c2.jsonl")))
            open(p("nc.jsonl"), "w").write(json.dumps(labs[0]) + "\n")  # not canonical v1 bytes
            r = subprocess.run(CLI + ["encode", p("nc.jsonl"), "--out", p("c3.jsonl")], capture_output=True, timeout=60)
            self.assertEqual(r.returncode, 2)
            open(p("bad.jsonl"), "w").write("{}\n")
            for args in (["decode", p("bad.jsonl")], ["decode", p("missing")], ["bogus"], []):
                r = subprocess.run(CLI + args, capture_output=True, timeout=60)
                self.assertEqual(r.returncode, 2, args)


def wl4():
    return open(os.path.join(L5B, "wl4_live_labels_v2.jsonl"), "rb").read(), json.load(open(os.path.join(L5B, "wl4_live_expect.json")))


def v2_parts(c2):
    h, b = c2.decode().split("\n")[:2]
    return json.loads(h), b


def v2_join(hd, body):
    return (json.dumps(hd, sort_keys=True, separators=(",", ":")) + "\n" + body + "\n").encode()


class V2(unittest.TestCase):
    """W6a FX4 L5: codec v2 = canonical JSON header line + base64 line of the raw deflate (level 9, wbits -15) of the exact compact-v1 bytes."""

    def test_v2_layout(self):
        labs, e = live()
        c1, c2 = LC.encode(labs), LC.encode_v2(labs)
        c2.decode("ascii")
        self.assertEqual(c2.count(b"\n"), 2)
        self.assertTrue(c2.endswith(b"\n"))
        hd, body = v2_parts(c2)
        self.assertEqual(hd, {"codec": "vqa.oracle.label-compact/v2", "inner": LC.CODEC, "inner_bytes": len(c1), "inner_sha256": hashlib.sha256(c1).hexdigest(),
                              "n": e["n_labels"], "run_id": e["run_id"], "schema": LO.SCHEMA})
        self.assertEqual(zlib.decompress(base64.b64decode(body), -15), c1)
        co = zlib.compressobj(9, zlib.DEFLATED, -15, 8)
        self.assertEqual(base64.b64decode(body), co.compress(c1) + co.flush())

    def test_v2_deflate_params_pinned(self):
        """level 9, wbits -15 AND memLevel 8: on a large high-entropy inner the memLevel 9 stream differs (non-vacuous)."""
        x, labs = 7, []
        for i in range(1500):
            ts = []
            for _ in range(12):
                x = (x * 1103515245 + 12345) % 2147483648
                ts.append((x >> 9) % 2040)
            labs.append(dict(lab(i, ts), source_ref=str(x)))
        c1, (hd, body) = LC.encode(labs), v2_parts(LC.encode_v2(labs))
        streams = {}
        for ml in (8, 9):
            co = zlib.compressobj(9, zlib.DEFLATED, -15, ml)
            streams[ml] = co.compress(c1) + co.flush()
        self.assertNotEqual(streams[8], streams[9])
        self.assertEqual(base64.b64decode(body), streams[8])

    def test_v2_live_wl1s_round_trip(self):
        labs, e = live()
        c2 = LC.encode_v2(labs)
        self.assertLess(len(c2), len(LC.encode(labs)))
        self.assertEqual(hashlib.sha256(LC.canonical_v1(LC.decode(c2))).hexdigest(), e["v1_sha256"])
        self.assertEqual(LC.decode_v2(c2), labs)
        self.assertEqual(LC.encode_v2(LC.decode(c2)), c2)
        self.assertEqual(LC.encode_v2(labs), c2)  # deterministic

    def test_v2_live_wl4_fixture(self):
        c2, e = wl4()
        labs = LC.decode(c2)
        self.assertEqual(len(labs), e["n_labels"])
        b1 = LC.canonical_v1(labs)
        self.assertEqual((len(b1), hashlib.sha256(b1).hexdigest()), (e["v1_bytes"], e["v1_sha256"]))
        c1 = LC.encode(labs)
        self.assertEqual((len(c1), hashlib.sha256(c1).hexdigest()), (e["c1_bytes"], e["c1_sha256"]))  # 49917 B > the 48 KB oracle dir budget
        self.assertEqual((len(c2), hashlib.sha256(c2).hexdigest()), (e["v2_bytes"], e["v2_sha256"]))
        self.assertLessEqual(len(c2), 8 * 1024)
        self.assertEqual(LC.encode_v2(labs), c2)  # pinned bytes: level/wbits/memLevel/base64 are part of the format

    def test_v2_live_wl4_derive(self):
        if not os.path.exists(WL4_SAMPLES):
            self.skipTest("live WL-4 samples (C evidence, 64 KB) absent; the pinned fixture test covers the bytes")
        c2, e = wl4()
        d = json.load(open(WL4_SAMPLES))
        self.assertEqual(LC.encode_v2(LO.derive(d["samples"], d["ops"], e["w"], e["h"], e["run_id"])), c2)

    def test_v2_empty(self):
        c2 = LC.encode_v2([])
        hd, _ = v2_parts(c2)
        self.assertEqual((hd["n"], hd["run_id"]), (0, None))
        self.assertEqual(LC.decode(c2), [])
        self.assertEqual(LC.encode_v2(LC.decode(c2)), c2)


class V2Strict(unittest.TestCase):
    def _c(self, labs=None):
        labs = labs or [lab(0, [1, 2, 3]), lab(1, [9])]
        c1 = LC.encode(labs)
        hd, body = v2_parts(LC.encode_v2(labs))
        return labs, c1, hd, body

    def _bad(self, data, rc=2):
        with self.assertRaises(LC.CodecError) as cm:
            LC.decode(data)
        self.assertEqual(cm.exception.rc, rc)

    def _deflated(self, c1, raw):
        return base64.b64encode(raw).decode()

    def test_v2_refuses_layout(self):
        _, _, hd, body = self._c()
        good = v2_join(hd, body)
        self.assertEqual(len(LC.decode(good)), 2)
        self._bad(good[:-1])  # no final newline
        self._bad(good + b"\n")  # third (blank) line
        self._bad(good.replace(b"\n", b"\r\n"))
        self._bad(good.replace(b'","', b'", "', 1))  # non-canonical header line
        self._bad(v2_join(dict(hd, zz=1), body))  # extra header key (canonical line)
        h2 = dict(hd)
        del h2["inner_bytes"]
        self._bad(v2_join(h2, body))
        self._bad(v2_join(dict(hd, schema="x"), body))
        self._bad(v2_join(dict(hd, inner="vqa.oracle.label-compact/v0"), body))
        self._bad(v2_join(dict(hd, inner_sha256=hd["inner_sha256"].upper()), body))

    def test_v2_refuses_inner_mismatch(self):
        _, c1, hd, body = self._c()
        sha = hd["inner_sha256"]
        self._bad(v2_join(dict(hd, inner_sha256=("0" if sha[0] != "0" else "1") + sha[1:]), body))  # sha only
        self._bad(v2_join(dict(hd, inner_bytes=hd["inner_bytes"] + 1), body))  # length only
        self._bad(v2_join(dict(hd, inner_bytes=hd["inner_bytes"] - 1), body))
        self._bad(v2_join(dict(hd, n=hd["n"] + 1), body))  # outer n differs from the inner header
        _, _, h1, b1 = self._c([lab(0, [1])])
        self._bad(v2_join(dict(h1, n=True), b1))  # bool is not the int 1
        self._bad(v2_join(dict(hd, run_id="other"), body))
        self._bad(v2_join(dict(hd, run_id=None), body))
        inner = v2_join(hd, body)  # a v2 file nested as the "v1" inner bytes: refused (the inner must be the v1-compact form itself)
        co = zlib.compressobj(9, zlib.DEFLATED, -15, 8)
        self._bad(v2_join(dict(hd, inner_bytes=len(inner), inner_sha256=hashlib.sha256(inner).hexdigest()), base64.b64encode(co.compress(inner) + co.flush()).decode()))

    def test_v2_refuses_non_canonical_deflate_and_base64(self):
        _, c1, hd, body = self._c([dict(lab(i, list(range(i, 3 * i + 1)) + [5000 + 7 * i]), source_ref=str(i * 31)) for i in range(400)])
        for lvl, wb in ((1, -15), (9, -9)):
            co = zlib.compressobj(lvl, zlib.DEFLATED, wb)
            raw = co.compress(c1) + co.flush()
            self.assertEqual(zlib.decompress(raw, -15), c1)
            self.assertNotEqual(raw, base64.b64decode(body))
            self._bad(v2_join(hd, self._deflated(c1, raw)))  # same inner bytes/sha, other deflate stream
        co = zlib.compressobj(9, zlib.DEFLATED, -15)
        raw = co.compress(c1) + co.flush(zlib.Z_SYNC_FLUSH)  # all data out, no final block
        self._bad(v2_join(hd, self._deflated(c1, raw)))
        self._bad(v2_join(hd, self._deflated(c1, base64.b64decode(body) + b"\x00")))  # trailing bytes
        self._bad(v2_join(hd, body[:-4]))  # truncated
        self._bad(v2_join(hd, body.rstrip("=")))  # unpadded
        self._bad(v2_join(hd, body[:8] + " " + body[8:]))
        self._bad(v2_join(hd, "!" + body[1:]))
        raw = base64.b64decode(body)  # non-zero padding bits: decodes to the same raw bytes, not canonical text
        k = 0
        while len(raw) % 3 == 0 and k < 40:
            k += 1
            _, c1, hd, body = self._c([lab(0, [1, 2, 3]), dict(lab(1, [9]), source_ref="x" * k)])
            raw = base64.b64decode(body)
        self.assertNotEqual(len(raw) % 3, 0)
        abc = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
        s = body.rstrip("=")
        alt = s[:-1] + abc[abc.index(s[-1]) + 1] + body[len(s):]
        self.assertEqual(base64.b64decode(alt), raw)
        self.assertTrue(LC.decode(v2_join(hd, body)))
        self._bad(v2_join(hd, alt))

    def test_v2_inner_not_canonical_v1(self):
        _, c1, hd, body = self._c()
        inner = c1.replace(b'"t":[1,3]', b'"t":[1,1,2,2]')
        raw = zlib.compressobj(9, zlib.DEFLATED, -15, 8)
        raw = raw.compress(inner) + raw.flush()
        self._bad(v2_join(dict(hd, inner_bytes=len(inner), inner_sha256=hashlib.sha256(inner).hexdigest()), base64.b64encode(raw).decode()))

    def test_v2_inflate_bound_rc5(self):
        _, _, hd, body = self._c()
        self._bad(v2_join(dict(hd, inner_bytes=LC.INNER_MAX + 1), body), rc=5)
        big = b"\n" * (LC.INNER_MAX + 1)
        co = zlib.compressobj(9, zlib.DEFLATED, -15)
        self._bad(v2_join(dict(hd, inner_bytes=LC.INNER_MAX), base64.b64encode(co.compress(big) + co.flush()).decode()))  # inflate stops at the bound

    def test_encode_non_object_rc2(self):  # ADC4-W6A-01
        for bad in ([1], ["x"], [None]):
            with self.assertRaises(LC.CodecError):
                LC.encode(bad)
            with self.assertRaises(LC.CodecError):
                LC.encode_v2(bad)


class V2Cli(unittest.TestCase):
    def test_cli_v2_default_and_v1(self):
        labs, e = live()
        with tempfile.TemporaryDirectory() as td:
            p = lambda n: os.path.join(td, n)
            open(p("v1.jsonl"), "wb").write(v1(labs))
            r = subprocess.run(CLI + ["encode", p("v1.jsonl"), "--out", p("c2.jsonl")], capture_output=True, timeout=60)
            self.assertEqual((r.returncode, open(p("c2.jsonl"), "rb").read()), (0, LC.encode_v2(labs)))
            r = subprocess.run(CLI + ["encode", p("v1.jsonl"), "--codec", "v1", "--out", p("c1.jsonl")], capture_output=True, timeout=60)
            self.assertEqual((r.returncode, open(p("c1.jsonl"), "rb").read()), (0, LC.encode(labs)))
            for f in ("c1.jsonl", "c2.jsonl"):
                r = subprocess.run(CLI + ["decode", p(f)], capture_output=True, timeout=60)
                self.assertEqual((r.returncode, hashlib.sha256(r.stdout).hexdigest()), (0, e["v1_sha256"]), f)
            n2 = os.path.getsize(p("c2.jsonl"))
            r = subprocess.run(CLI + ["encode", p("v1.jsonl"), "--out", p("c3.jsonl"), "--cap", str(n2)], capture_output=True, timeout=60)
            self.assertEqual(r.returncode, 0)
            r = subprocess.run(CLI + ["encode", p("v1.jsonl"), "--out", p("c4.jsonl"), "--cap", str(n2 - 1)], capture_output=True, timeout=60)
            self.assertEqual(r.returncode, 5)
            self.assertFalse(os.path.exists(p("c4.jsonl")))
            hd, body = v2_parts(open(p("c2.jsonl"), "rb").read())
            open(p("big.jsonl"), "wb").write(v2_join(dict(hd, inner_bytes=LC.INNER_MAX + 1), body))
            open(p("sha.jsonl"), "wb").write(v2_join(dict(hd, inner_sha256="0" * 64), body))
            with open(p("obj.jsonl"), "w") as f:
                f.write("[1]\n")
            for args, rc in ((["decode", p("big.jsonl")], 5), (["decode", p("sha.jsonl")], 2), (["encode", p("obj.jsonl")], 2), (["encode", p("v1.jsonl"), "--codec", "v3"], 2)):
                r = subprocess.run(CLI + args, capture_output=True, timeout=60)
                self.assertEqual(r.returncode, rc, args)


if __name__ == "__main__":
    unittest.main()

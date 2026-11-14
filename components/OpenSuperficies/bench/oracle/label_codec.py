"""Compact, lossless codec for vqa.oracle.label/v1 label files (W6a FX3 L5). Python 3 stdlib only, integers only.

Compact form (vqa.oracle.label-compact/v1), canonical JSON lines (sort_keys, separators (",", ":")), one "\\n" after every line:
  line 0  header {"codec", "n", "run_id", "schema"}: the fields every label shares are hoisted (schema; run_id, null iff n == 0);
  line k  label k-1 without schema/run_id/event_id (event_id == its index) and with tiles replaced by "t": the maximal runs of
          consecutive ranks (+1 steps, in list order; tiles are dense Morton ranks) as a flat [start, len, start, len, ...].
decode(encode(L)) == L and canonical_v1(decode(C)) is byte-for-byte the v1 JSONL; decode refuses any non-canonical compact input, so
encode(decode(C)) == C as well.

Codec v2 (vqa.oracle.label-compact/v2, W6a FX4 L5): a TEXT file of exactly two lines, each ending in "\n":
  line 0  canonical JSON header {"codec", "inner": vqa.oracle.label-compact/v1, "inner_bytes", "inner_sha256", "n", "run_id", "schema"};
  line 1  standard base64 (RFC 4648, padded) of the raw DEFLATE stream (zlib level 9, wbits -15, memLevel 8, default strategy) of the
          exact compact-v1 bytes. decode bounds the inflate (inner_bytes <= INNER_MAX, else rc 5), checks length and sha256 of the inner
          bytes, re-deflates them and requires the identical stream and base64 text (canonical), then the strict v1 decode, and n/run_id
          must equal the inner header. So encode_v2(decode(C)) == C. labels_oracle writes v2; the episode cap (labels_oracle.CAP) applies
          to the v2 bytes written. decode() accepts both the v1-compact and the v2 form.

CLI: label_codec.py decode IN [--out OUT]                          v1-compact or v2 -> canonical v1 JSONL (stdout without --out)
     label_codec.py encode IN [--out OUT] [--cap N] [--codec v2|v1] canonical v1 JSONL -> v2 (default) or v1-compact (round-trip checked)
Exit codes: 0 ok, 2 usage/schema/io/non-canonical input, 5 over a cap or round-trip mismatch; never 1.
"""
import argparse
import base64
import binascii
import hashlib
import json
import os
import sys
import tempfile
import zlib

CODEC = "vqa.oracle.label-compact/v1"
SCHEMA = "vqa.oracle.label/v1"
KEYS = ("schema", "run_id", "event_id", "f0", "f1", "rects", "tiles", "ideal", "mv", "period_ms", "flags", "source", "source_ref", "confidence")
HOISTED = ("schema", "run_id", "event_id", "tiles")
REC_KEYS = frozenset(k for k in KEYS if k not in HOISTED) | {"t"}
HEAD_KEYS = frozenset(("codec", "n", "run_id", "schema"))
CODEC_V2 = "vqa.oracle.label-compact/v2"
HEAD2_KEYS = frozenset(("codec", "inner", "inner_bytes", "inner_sha256", "n", "run_id", "schema"))
INNER_MAX = 4 * 1024 * 1024  # inflate bound: a v2 header announcing more inner bytes is refused with rc 5 before any inflate
DEFLATE_LEVEL, DEFLATE_WBITS, DEFLATE_MEMLEVEL = 9, -15, 8


class CodecError(ValueError):
    def __init__(self, msg, rc=2):
        ValueError.__init__(self, msg)
        self.rc = rc


def _isint(v):
    return type(v) is int


def _line(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":")) + "\n"


def encode_runs(tiles):
    """[0,1,2,7] -> [0,3,7,1]: maximal runs of +1 steps, in list order (any int list, order and duplicates kept)."""
    out = []
    for t in tiles:
        if out and t == out[-2] + out[-1]:
            out[-1] += 1
        else:
            out += [t, 1]
    return out


def decode_runs(flat):
    if not isinstance(flat, list) or len(flat) % 2 or not all(_isint(v) for v in flat):
        raise CodecError("t must be a flat [start, len, ...] list of ints")
    out = []
    for i in range(0, len(flat), 2):
        s, n = flat[i], flat[i + 1]
        if s < 0 or n < 1:
            raise CodecError("t span start >= 0 and len >= 1")
        if i and s == flat[i - 2] + flat[i - 1]:
            raise CodecError("t spans not maximal (non-canonical)")
        out.extend(range(s, s + n))
    return out


def canonical_v1(labels):
    return "".join(_line(l) for l in labels).encode()


def encode(labels):
    """v1 label dicts -> compact bytes. ValueError when a label does not carry the exact v1 keys or a hoisted field is not shared."""
    run_id = labels[0].get("run_id") if (labels and isinstance(labels[0], dict)) else None  # ADC4-W6A-01: rc 2, never AttributeError
    out = [_line({"codec": CODEC, "n": len(labels), "run_id": run_id, "schema": SCHEMA})]
    for i, l in enumerate(labels):
        if not isinstance(l, dict) or set(l) != set(KEYS):
            raise CodecError("label %d keys must be exactly %s" % (i, KEYS))
        if l["schema"] != SCHEMA or l["run_id"] != run_id or not isinstance(run_id, str):
            raise CodecError("label %d schema/run_id not shared (cannot hoist)" % i)
        if not _isint(l["event_id"]) or l["event_id"] != i:
            raise CodecError("label %d event_id must equal its index" % i)
        if not isinstance(l["tiles"], list) or not all(_isint(t) and t >= 0 for t in l["tiles"]):
            raise CodecError("label %d tiles must be non-negative ints" % i)
        r = {k: v for k, v in l.items() if k not in HOISTED}
        r["t"] = encode_runs(l["tiles"])
        out.append(_line(r))
    return "".join(out).encode()


def _deflate(inner):
    c = zlib.compressobj(DEFLATE_LEVEL, zlib.DEFLATED, DEFLATE_WBITS, DEFLATE_MEMLEVEL, zlib.Z_DEFAULT_STRATEGY)
    return c.compress(inner) + c.flush()


def encode_v2(labels):
    """v1 label dicts -> v2 bytes (header line + base64 line of the raw deflate of encode(labels)). Deterministic."""
    inner = encode(labels)
    ih = json.loads(inner[:inner.index(b"\n")])
    hd = {"codec": CODEC_V2, "inner": CODEC, "inner_bytes": len(inner), "inner_sha256": hashlib.sha256(inner).hexdigest(),
          "n": ih["n"], "run_id": ih["run_id"], "schema": SCHEMA}
    return (_line(hd) + base64.b64encode(_deflate(inner)).decode("ascii") + "\n").encode()


def decode_v2(data):
    """v2 bytes -> v1 label dicts. CodecError rc 2 on anything non-canonical, rc 5 when inner_bytes exceeds INNER_MAX."""
    try:
        text = data.decode("ascii")
    except (AttributeError, UnicodeDecodeError) as e:
        raise CodecError("v2 input: %s" % e)
    if not text.endswith("\n"):
        raise CodecError("v2 input must end with a newline")
    lines = text[:-1].split("\n")
    if len(lines) != 2:
        raise CodecError("v2 input must be exactly a header line and a base64 line")
    try:
        hd = json.loads(lines[0])
    except ValueError as e:
        raise CodecError("v2 header json: %s" % e)
    if not isinstance(hd, dict) or _line(hd) != lines[0] + "\n":
        raise CodecError("v2 header line not canonical JSON")
    if set(hd) != HEAD2_KEYS or hd["codec"] != CODEC_V2 or hd["schema"] != SCHEMA:
        raise CodecError("v2 header must be exactly %s with codec %s" % (sorted(HEAD2_KEYS), CODEC_V2))
    if hd["inner"] != CODEC:
        raise CodecError("v2 inner codec must be %s" % CODEC)
    ib, ish = hd["inner_bytes"], hd["inner_sha256"]
    if not _isint(ib) or ib < 0 or not isinstance(ish, str) or len(ish) != 64 or any(ch not in "0123456789abcdef" for ch in ish):
        raise CodecError("v2 inner_bytes must be an int >= 0 and inner_sha256 64 lowercase hex")
    if ib > INNER_MAX:
        raise CodecError("v2 inner_bytes %d exceed the %d byte inflate bound" % (ib, INNER_MAX), 5)
    try:
        raw = base64.b64decode(lines[1], validate=True)
    except (binascii.Error, ValueError) as e:
        raise CodecError("v2 base64: %s" % e)
    if base64.b64encode(raw).decode("ascii") != lines[1]:
        raise CodecError("v2 base64 not canonical")
    z = zlib.decompressobj(DEFLATE_WBITS)
    try:
        inner = z.decompress(raw, ib + 1)
    except zlib.error as e:
        raise CodecError("v2 deflate: %s" % e)
    if len(inner) != ib:
        raise CodecError("v2 inner length != inner_bytes %d" % ib)
    if hashlib.sha256(inner).hexdigest() != ish:
        raise CodecError("v2 inner sha256 mismatch")
    if _deflate(inner) != raw:
        raise CodecError("v2 deflate stream not canonical (level 9, wbits -15, memLevel 8)")
    if not inner.startswith(b'{"codec":"%s",' % CODEC.encode()):
        raise CodecError("v2 inner bytes are not the %s form" % CODEC)
    labels = decode(inner)
    ih = json.loads(inner[:inner.index(b"\n")])
    if hd["n"] != ih["n"] or type(hd["n"]) is not type(ih["n"]) or hd["run_id"] != ih["run_id"]:
        raise CodecError("v2 header n/run_id differ from the inner header")
    return labels


def decode(data):
    """compact bytes (v1-compact, or v2 when the first line is a v2 header) -> v1 label dicts.
    CodecError (rc 2) on anything that is not the canonical compact form (rc 5: v2 inflate bound)."""
    if isinstance(data, bytes) and data.startswith(b'{"codec":"%s",' % CODEC_V2.encode()):
        return decode_v2(data)
    try:
        text = data.decode("utf-8")
    except (AttributeError, UnicodeDecodeError) as e:
        raise CodecError("compact input: %s" % e)
    if not text.endswith("\n"):
        raise CodecError("compact input must end with a newline")
    lines = text[:-1].split("\n")
    try:
        objs = [json.loads(ln) for ln in lines]
    except ValueError as e:
        raise CodecError("compact json: %s" % e)
    for ln, o in zip(lines, objs):
        if not isinstance(o, dict) or _line(o) != ln + "\n":
            raise CodecError("compact line not canonical JSON")
    hd, recs = objs[0], objs[1:]
    if set(hd) != HEAD_KEYS or hd["codec"] != CODEC or hd["schema"] != SCHEMA:
        raise CodecError("compact header must be {codec: %s, n, run_id, schema: %s}" % (CODEC, SCHEMA))
    if not _isint(hd["n"]) or hd["n"] != len(recs):
        raise CodecError("compact header n %r != %d records" % (hd["n"], len(recs)))
    if not (isinstance(hd["run_id"], str) if recs else hd["run_id"] is None):
        raise CodecError("compact header run_id must be a string (null iff n == 0)")
    out = []
    for i, r in enumerate(recs):
        if set(r) != REC_KEYS:
            raise CodecError("compact record %d keys must be exactly %s" % (i, sorted(REC_KEYS)))
        l = {k: v for k, v in r.items() if k != "t"}
        l.update(schema=SCHEMA, run_id=hd["run_id"], event_id=i, tiles=decode_runs(r["t"]))
        out.append(l)
    return out


def write_atomic(path, data):
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".lab")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="label_codec.py")
    sub = ap.add_subparsers(dest="cmd")
    for c in ("decode", "encode"):
        p = sub.add_parser(c)
        p.add_argument("input")
        p.add_argument("--out")
        if c == "encode":
            p.add_argument("--cap", type=int)
            p.add_argument("--codec", choices=("v2", "v1"), default="v2")
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return 0 if e.code == 0 else 2
    if not a.cmd:
        ap.print_usage(sys.stderr)
        return 2
    try:
        with open(a.input, "rb") as f:
            data = f.read()
        if a.cmd == "decode":
            out = canonical_v1(decode(data))
        else:
            try:
                labels = [json.loads(ln) for ln in data.decode("utf-8").splitlines()]
            except ValueError as e:
                raise CodecError("v1 json: %s" % e)
            if canonical_v1(labels) != data:
                raise CodecError("v1 input is not canonical JSONL (sort_keys, compact separators)")
            out = encode_v2(labels) if a.codec == "v2" else encode(labels)
            if canonical_v1(decode(out)) != data:
                raise CodecError("round-trip mismatch", 5)
            if a.cap is not None and len(out) > a.cap:
                raise CodecError("encoded %d bytes exceed the %d byte cap" % (len(out), a.cap), 5)
        if a.out:
            write_atomic(a.out, out)
        else:
            sys.stdout.buffer.write(out)
            sys.stdout.flush()
    except CodecError as e:
        print("E_CODEC: %s" % e, file=sys.stderr)
        return e.rc
    except OSError as e:
        print("E_IO: %s" % e, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

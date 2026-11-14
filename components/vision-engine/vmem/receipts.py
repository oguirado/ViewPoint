# @module {"id":"vm.receipts","layer":5,"stage":"S7","owner":"VMEM","deps":["vc.canon","vc.errors","vm.blobs"],"book":"HB-VC-s7-vmem"}
"""@module vm.receipts - hash-chained journal with segment rotation (SPEC_VMEM §6.4) and run receipts."""
import json
import os
import re

from visioncore import canon
from visioncore.errors import VqaError
from .blobs import fsync_dir

VM_JSEG_CAP = 4 * 1024 * 1024
VM_JREC_MAX = 64 * 1024
TYPES = ("ingest_begin", "flip", "compact_begin", "gc", "pin", "rollback")
_SEG = re.compile(r"^(\d{6})\.jsonl$")
_BASE = re.compile(r"^base_(\d{6})\.json$")
GENESIS = "0" * 64


def _write_all(fd, data):
    """Acknowledge only complete writes; a zero write cannot make progress."""
    offset = 0
    while offset < len(data):
        written = os.write(fd, memoryview(data)[offset:])
        if written <= 0:
            raise VqaError("E_IO", {"reason": "journal_short_write"})
        offset += written


def receipt_sha256(manifest_bytes):
    """receipt_sha256 of a unit = sha256 of runs/<run_id>/manifest.json."""
    return canon.sha256_bytes(manifest_bytes)


def rotates(used, rec, cap=VM_JSEG_CAP):
    """Subtraction form (VG-0219 C3): no sum can wrap."""
    return rec > cap - used


def _seg_name(n):
    return "%06d.jsonl" % n


class Journal:  # @anchor:vm.receipts.Journal
    def __init__(self, root, seg_cap=VM_JSEG_CAP, rec_max=VM_JREC_MAX):
        self.dir = os.path.join(str(root), "journal")
        self.seg_cap, self.rec_max = seg_cap, rec_max
        os.makedirs(self.dir, exist_ok=True)
        self._tail_cache = None
        self._tail_stamp = None

    def segments(self):
        return sorted(int(m.group(1)) for n in os.listdir(self.dir) for m in [_SEG.match(n)] if m)

    def _stamp(self):
        """Detect another writer or torn-tail repair without rereading sealed segments."""
        segs = self.segments()
        if not segs:
            return ((), None)
        st = os.stat(self.path(segs[-1]))
        return (tuple(segs), (st.st_ino, st.st_size, st.st_mtime_ns))

    def path(self, n):
        return os.path.join(self.dir, _seg_name(n))

    def recover_torn(self):
        """Drop a torn tail line of the last segment; returns bytes dropped."""
        segs = self.segments()
        if not segs:
            return 0
        p = self.path(segs[-1])
        with open(p, "rb") as f:
            data = f.read()
        keep = len(data)
        if data and not data.endswith(b"\n"):
            keep = data.rfind(b"\n") + 1
        else:
            last = data[:-1].rfind(b"\n") + 1 if data else 0
            if data and not self._valid_line(data[last:-1]):
                keep = last
        if keep != len(data):
            self._tail_cache = None
            self._tail_stamp = None
            with open(p, "r+b") as f:
                f.truncate(keep)
                f.flush()
                os.fsync(f.fileno())
        return len(data) - keep

    @staticmethod
    def _valid_line(b):
        try:
            o = json.loads(b.decode("utf-8"))
            h = o.pop("h")
            return canon.sha256_obj(o) == h
        except (ValueError, KeyError, AttributeError):
            return False

    def records(self):
        out = []
        for n in self.segments():
            with open(self.path(n), "rb") as f:
                for line in f.read().split(b"\n"):
                    if line:
                        out.append(json.loads(line.decode("utf-8")))
        return out

    def base(self):
        """Newest compaction base {gen, seq, h} (VG-0219 C6 applied), or None."""
        names = sorted(n for n in os.listdir(self.dir) if _BASE.match(n))
        if not names:
            return None
        with open(os.path.join(self.dir, names[-1]), "rb") as f:
            return json.loads(f.read().decode("utf-8"))

    def _tail(self):
        recs = self.records()
        if recs:
            return recs[-1]["seq"] + 1, recs[-1]["h"]
        b = self.base()
        return (b["seq"] + 1, b["h"]) if b else (1, GENESIS)

    def checkpoint(self, upto_seq):
        """Seal the chain at record `upto_seq`: write base_<gen>.json = {gen, seq, h}, fsync file and dir, then unlink
        sealed (non-last) segments whose records all have seq <= upto_seq. Returns the base or None if nothing to do."""
        recs = self.records()
        hit = [r for r in recs if r["seq"] == upto_seq]
        if not hit:
            return None
        gen = len([n for n in os.listdir(self.dir) if _BASE.match(n)]) + 1
        base = {"gen": gen, "seq": upto_seq, "h": hit[0]["h"]}
        p = os.path.join(self.dir, "base_%06d.json" % gen)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            _write_all(fd, canon.dumps(base).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        fsync_dir(self.dir)
        segs = self.segments()
        for n in segs[:-1]:
            with open(self.path(n), "rb") as f:
                seqs = [json.loads(l.decode("utf-8"))["seq"] for l in f.read().split(b"\n") if l]
            if seqs and max(seqs) <= upto_seq:
                os.unlink(self.path(n))
        fsync_dir(self.dir)
        return base

    def append(self, rtype, body=None):
        """Append one hash-chained record, fsync, return it."""
        if rtype not in TYPES:
            raise VqaError("E_USAGE", {"reason": "bad_journal_type", "type": str(rtype)})
        stamp = self._stamp()
        if self._tail_cache is None or stamp != self._tail_stamp:
            self.recover_torn()
            self._tail_cache = self._tail()
        seq, prev = self._tail_cache
        rec = {"seq": seq, "prev": prev, "type": rtype, "body": dict(body or {})}
        rec["h"] = canon.sha256_obj(rec)
        line = canon.dumps(rec).encode("utf-8")
        if len(line) > self.rec_max:
            raise VqaError("E_BUDGET", {"reason": "journal_record_max", "bytes": len(line)})
        segs = self.segments()
        cur = segs[-1] if segs else 1
        used = os.path.getsize(self.path(cur)) if segs else 0
        if used and rotates(used, len(line), self.seg_cap):
            cur += 1
            used = 0
        new = not os.path.exists(self.path(cur))
        fd = os.open(self.path(cur), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            _write_all(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)
        if new:
            fsync_dir(self.dir)
        self._tail_cache = (seq + 1, rec["h"])
        self._tail_stamp = self._stamp()
        return rec

    def verify_chain(self):
        """True iff seq is dense, prev links match and every h recomputes."""
        b = self.base()
        prev, seq = (b["h"], b["seq"] + 1) if b else (GENESIS, 1)
        for r in self.records():
            if b and r.get("seq", 0) <= b["seq"]:
                continue  # sealed by the base (an unsealed segment may still hold pre-base records)
            h = r.get("h")
            body = {k: v for k, v in r.items() if k != "h"}
            if r.get("seq") != seq or r.get("prev") != prev or canon.sha256_obj(body) != h:
                return False
            prev, seq = h, seq + 1
        return True

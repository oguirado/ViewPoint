"""R1 checkpoint store (Track P, claude-fable-5-2): ring of .blend points under RT/ckpt/. Stdlib only.

Interface of CONTRACT.md section 6 (same as fakes.FakeStore) plus ring, byte budget, dedup, manifest:

    Store(rt, slots=8, budget_bytes=None)
    path_for(point_id) -> str                 where the mark job must write the .blend
    dedup_candidate(digest) -> point_id|None  newest point if its digest equals `digest` (mark job can be skipped)
    admit(point_id, tick, scene_epoch, digest, frame, pinned=()) -> rec:ckpt   (CKPT_MISSING | CKPT_BUDGET)
    verify(point_id) -> path                  sha256 re-checked before every restore (CKPT_CORRUPT | CKPT_MISSING)
    get(point_id) -> rec:ckpt | None ; points() -> [point_id, ...] oldest first

Ring rules:
  * A slot is one physical .blend. A point whose digest equals the newest point's digest is a dedup row
    (`dedup_of`), shares that file and costs no slot and no bytes.
  * `pinned` = the points the caller still needs (last_good, the base of an open transaction). With
    pinned=() the newest existing point is taken as last_good.
  * Evictable = older than the newest pinned point and not pinned. Points newer than the newest pinned
    point are unconfirmed (marked, their commit not yet known) and are never evicted.
  * STALL GUARD: when room cannot be made from evictable points (more than `slots` files, or more than
    `budget_bytes`), admit refuses CKPT_BUDGET and discards the new, unadmitted file. Unconfirmed points
    therefore stall the ring at S-1 next to last_good instead of overwriting it or each other.
State is the manifest RT/ckpt/manifest.jsonl (append-only rows admit/evict), replayed on construction.
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "ref_rewind"))
import schema as RW  # noqa: E402  (R0 contract, imported, never redefined)

MANIFEST = "manifest.jsonl"


class Store:
    def __init__(self, rt, slots=RW.RING_SLOTS, budget_bytes=None, check_sha=True, stall_guard=True,
                 heal_tail=True):
        if slots < 2:
            raise ValueError("ring needs at least 2 slots (last_good + one unconfirmed point)")
        self.dir = os.path.join(rt, RW.CKPT_DIRNAME)
        os.makedirs(self.dir, exist_ok=True)
        self.slots, self.budget_bytes = slots, budget_bytes
        self.check_sha = check_sha          # False only in negative controls
        self.stall_guard = stall_guard      # False only in negative controls
        self.heal_tail = heal_tail          # False only in negative controls
        self.p_manifest = os.path.join(self.dir, MANIFEST)
        self._rows = {}                     # point_id -> {"rec": ckpt record, "file": canonical point_id}
        self._load()

    # ---- manifest
    def _load(self):
        if not os.path.exists(self.p_manifest):
            return
        with open(self.p_manifest, "rb") as f:
            data = f.read()
        good = data.rfind(b"\n") + 1         # a row counts only once its newline is on disk
        for line in data[:good].splitlines():
            try:
                row = json.loads(line.decode("utf-8"))
            except ValueError:
                continue                    # damaged complete line: skipped, later rows still load
            if not isinstance(row, dict):
                continue
            if row.get("op") == "admit":
                self._rows[row["rec"]["point_id"]] = {"rec": row["rec"], "file": row["file"]}
            elif row.get("op") == "evict":
                self._rows.pop(row.get("point_id"), None)
        if self.heal_tail and good < len(data):
            # torn last line of a crashed writer: cut it BEFORE the next append, or the next admit row
            # is glued onto it and becomes unreadable (the point is then lost on the following load)
            with open(self.p_manifest, "r+b") as f:
                f.truncate(good)
                f.flush()
                os.fsync(f.fileno())

    def _log(self, rows):
        with open(self.p_manifest, "a") as f:
            for row in rows:
                f.write(json.dumps(row, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())

    # ---- queries
    def path_for(self, point_id):
        if not isinstance(point_id, str) or not RW.POINT_ID_RE.match(point_id):
            raise RW.SchemaError("BAD_POINT", "%r" % (point_id,))
        return os.path.join(self.dir, point_id + ".blend")

    def get(self, point_id):
        row = self._rows.get(point_id)
        return dict(row["rec"]) if row else None

    def points(self):
        return list(self._rows)

    def files(self):
        """Canonical point ids that own a physical .blend, oldest first."""
        seen = []
        for row in self._rows.values():
            if row["file"] not in seen:
                seen.append(row["file"])
        return seen

    def bytes_used(self):
        return sum(self._file_bytes(self._rows).values())

    @staticmethod
    def _file_bytes(rows):
        out = {}
        for row in rows.values():
            out[row["file"]] = row["rec"]["bytes"]
        return out

    def dedup_candidate(self, digest):
        if not self._rows:
            return None
        last = next(reversed(self._rows))
        return last if RW.digest_match(self._rows[last]["rec"]["digest"], digest) else None

    # ---- admit
    def admit(self, point_id, tick, scene_epoch, digest, frame, pinned=()):
        path = self.path_for(point_id)
        if point_id in self._rows:
            raise RW.SchemaError("POINT_REUSED", point_id)
        RW.check_digest(digest)
        dup = self.dedup_candidate(digest)
        if dup is not None:                                   # DEDUP: share the previous point's file
            base = self._rows[dup]
            rec = RW.ckpt_rec(point_id, tick, scene_epoch, base["rec"]["blend_sha256"], base["rec"]["bytes"],
                              digest, frame, dedup_of=base["file"])
            self._discard(path)
            self._rows[point_id] = {"rec": rec, "file": base["file"]}
            self._log([{"op": "admit", "rec": rec, "file": base["file"]}])
            return dict(rec)
        if not os.path.isfile(path):
            raise RW.CkptError(RW.CKPT_MISSING, path)
        nbytes = os.path.getsize(path)
        rec = RW.ckpt_rec(point_id, tick, scene_epoch, RW.sha256_file(path), nbytes, digest, frame)
        rows = dict(self._rows)
        rows[point_id] = {"rec": rec, "file": point_id}
        evict = self._plan_eviction(rows, point_id, pinned)
        if evict is None:
            self._discard(path)
            raise RW.CkptError(RW.CKPT_BUDGET, "no evictable point: ring %d slots, budget %s bytes, pinned %s"
                               % (self.slots, self.budget_bytes, sorted(pinned)))
        log = [{"op": "admit", "rec": rec, "file": point_id}]
        for pid in evict:
            rows.pop(pid)
            log.append({"op": "evict", "point_id": pid})
        live = set(r["file"] for r in rows.values())
        self._log(log)                                        # manifest first, files second
        for pid in evict:
            if self._rows[pid]["file"] not in live:
                self._discard(self.path_for(self._rows[pid]["file"]))
        self._rows = rows
        return dict(rec)

    def _over(self, rows):
        fb = self._file_bytes(rows)
        return len(fb) > self.slots or (self.budget_bytes is not None and sum(fb.values()) > self.budget_bytes)

    def _plan_eviction(self, rows, new_id, pinned):
        """Point ids to evict (oldest first) so `rows` fits, or None if that needs a protected point."""
        order = list(rows)
        pins = set(p for p in pinned if p in rows)
        if not pins and len(order) > 1:
            pins = {order[-2]}                                # implicit last_good: the newest older point
        newest_pin = max((order.index(p) for p in pins), default=-1)
        rows = dict(rows)
        evict = []
        for i, pid in enumerate(order):
            if not self._over(rows):
                break
            if pid == new_id or pid in pins:                  # PIN GUARD: last_good / open-txn base stay
                continue
            if i > newest_pin and self.stall_guard:           # STALL GUARD: unconfirmed points stay
                continue
            rows.pop(pid)
            evict.append(pid)
        return None if self._over(rows) else evict

    @staticmethod
    def _discard(path):
        try:
            os.remove(path)
        except OSError:
            pass

    # ---- verify (before every restore)
    def verify(self, point_id):
        row = self._rows.get(point_id)
        if row is None:
            raise RW.CkptError(RW.CKPT_MISSING, "%s is not in the ring (never admitted or evicted)" % point_id)
        path = self.path_for(row["file"])
        if not os.path.isfile(path):
            raise RW.CkptError(RW.CKPT_MISSING, path)
        if self.check_sha and RW.sha256_file(path) != row["rec"]["blend_sha256"]:   # SHA CHECK
            raise RW.CkptError(RW.CKPT_CORRUPT, "%s: sha256 differs from the ckpt record" % path)
        return path

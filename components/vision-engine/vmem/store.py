# @module {"id":"vm.store","layer":5,"stage":"S7","owner":"VMEM","deps":["vc.canon","vc.errors","vm.blobs","vm.receipts"],"book":"HB-VC-s7-vmem"}
"""@module vm.store - single-writer store: lock, commit (blobs -> gen -> flip -> journal), snapshot reads, rollback."""
import fcntl
import os
from contextlib import contextmanager

from visioncore import canon
from visioncore.errors import VqaError
from . import gen as G
from .blobs import put_blob, blob_path, verify_blob, iter_blobs, delete_blob, clean_tmps
from .receipts import Journal
from . import compact as C, units as U

_UNIT_FILES = ("units/active.jsonl", "units/archive.jsonl")


def _parse_units(raw, strict=True):
    """JSONL bytes -> unit list. strict: every line must be a valid vmem.unit/v1; lenient: skip bad lines."""
    out = []
    for ln in raw.decode("utf-8", "replace").splitlines():
        if not ln.strip():
            continue
        try:
            u = canon.loads(ln)
            if strict:
                U.validate_unit(u)
            out.append(u)
        except VqaError:
            if strict:
                raise
    return out


def _check_fg_edits(parent, child):
    """Published FG evidence is immutable; only last_gen and supported supersession may change."""
    byid = {u["id"]: u for u in child}
    for old in parent:
        if not U.is_fg(old):
            continue
        new = byid[old["id"]]  # g_fg_monotone has already checked presence
        before = {k: v for k, v in old.items() if k not in ("status", "last_gen")}
        after = {k: v for k, v in new.items() if k not in ("status", "last_gen")}
        allowed_status = new["status"] == old["status"] or (
            old["status"] == "active" and new["status"] == "superseded" and any(
                l["rel"] == "supersedes" and l["to"] == old["id"] for u in child for l in u["links"]))
        if before != after or not allowed_status or new["last_gen"] < old["last_gen"]:
            raise VqaError("E_GATE", {"reason": "g_fg_monotone", "edited": old["id"]})


class Snapshot:
    """Lock-free reader pinned to one generation (SPEC_VMEM §9: resolve CURRENT once, re-check at the end)."""

    def __init__(self, store, n):
        self.store, self.gen = store, n
        self.manifest = G.read_manifest(store.root, n)
        self.manifest_sha = G.manifest_sha(store.root, n)

    def read(self, rel):
        try:
            with open(os.path.join(G.gen_dir(self.store.root, self.gen), rel), "rb") as f:
                return f.read()
        except OSError:
            raise VqaError("E_ID", {"reason": "file_missing", "path": rel[:60]})

    def validate(self):
        """rc 4 if CURRENT moved or the manifest changed during the read."""
        if G.current_gen(self.store.root) != self.gen or G.manifest_sha(self.store.root, self.gen) != self.manifest_sha:
            raise VqaError("E_STALE", {"reason": "current_changed", "gen": self.gen})
        return True


BLOB_POINTS = ("blob_tmp_written", "blob_chmod", "gc_journaled")  # extra hook points beyond gen.POINTS


class Store:  # @anchor:vm.store.Store
    def __init__(self, root):
        self.root = str(root)
        self.hook = None  # test seam: called with a durability point name (gen.POINTS)
        self._lockfd = None
        os.makedirs(self.root, exist_ok=True)
        self.journal = Journal(self.root)

    def _point(self, name):
        if self.hook:
            self.hook(name)

    @contextmanager
    def lock(self):
        """Non-blocking exclusive lock; held -> rc 4. Recovery runs under the lock."""
        fd = os.open(os.path.join(self.root, ".lock"), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise VqaError("E_STALE", {"reason": "lock_held"})
        self._lockfd = fd
        try:
            self.recover()
            yield self
        finally:
            self._lockfd = None
            os.close(fd)  # closing releases the flock

    def _need_lock(self):
        if self._lockfd is None:
            raise VqaError("E_USAGE", {"reason": "lock_required"})

    def recover(self):
        """Drop torn journal tail, unacked gens/tmp files; reconcile a durable CURRENT missing its flip record."""
        self.journal.recover_torn()
        clean_tmps(self.root)
        cur = G.current_gen(self.root)
        flips = [r["body"].get("gen", 0) for r in self.journal.records() if r["type"] == "flip"]
        acked = max([cur or 0] + flips)
        removed = G.cleanup_unacked(self.root, acked)
        if cur is not None and cur not in flips:
            self.journal.append("flip", {"gen": cur, "manifest_sha256": G.manifest_sha(self.root, cur), "reconciled": 1})
        return removed

    def current(self):
        return G.current_gen(self.root)

    def snapshot(self):
        n = G.current_gen(self.root)
        if n is None:
            raise VqaError("E_STALE", {"reason": "empty_store"})
        return Snapshot(self, n)

    def gen_units(self, n, strict=False):
        """Units of generation n (active + archive); lenient by default (raw test gens are tolerated)."""
        out = []
        for rel in _UNIT_FILES:
            try:
                with open(os.path.join(G.gen_dir(self.root, n), rel), "rb") as f:
                    out += _parse_units(f.read(), strict)
            except OSError:
                pass
        return out

    def commit(self, files, blobs=(), extra=None, strict=True):  # @anchor:vm.store.commit
        """Write blobs, then gen n+1 with `files`, flip CURRENT, journal the flip. Returns {gen, manifest_sha256}.
        strict (SPEC_VMEM §5 step 7): units are validated, FG ids must be a superset of the parent's, the FG
        catalogue must match and every blob_ref must exist; any failure raises E_GATE/E_SCHEMA before the flip
        (CURRENT, the previous generation and the manifest are untouched). Manifest counters are computed here."""
        self._need_lock()
        cur = G.current_gen(self.root)
        n = max([cur or 0] + G.list_gens(self.root)) + 1
        G.gen_name(n)  # gen_exhausted -> rc 5 before any write
        extra = dict(extra or {})
        units = None
        if strict:
            units = _parse_units(files.get("units/active.jsonl", b"")) + _parse_units(files.get("units/archive.jsonl", b""))
            if cur is not None:
                parent_units = self.gen_units(cur)
                C.g_fg_monotone(parent_units, units)
                _check_fg_edits(parent_units, units)
            from . import gates
            for name in ("g_grid", "g_cite"):
                if not getattr(gates, "_" + name)({"units": units}):
                    raise VqaError("E_GATE", {"reason": name})
            fgset = [u for u in units if U.is_fg(u)]
            if "catalogue/foreground.tsv" in files:
                U.check_catalogue(units, files["catalogue/foreground.tsv"].decode("utf-8"))
            elif fgset:
                raise VqaError("E_GATE", {"reason": "g_catalogue", "why": "missing_catalogue"})
        shas = [put_blob(self.root, data, ext, self._point) for data, ext in blobs]
        if units is not None:
            have = {s for s, _ in iter_blobs(self.root)}
            lost = sorted(set().union(*[U.blob_refs(u) for u in units]) - have)
            if lost:
                raise VqaError("E_GATE", {"reason": "g_blob_integrity", "missing": lost[:8]})
            refs = set().union(*[U.blob_refs(u) for u in units])
            corrupt = sorted(s for s, e in iter_blobs(self.root) if s in refs and not verify_blob(self.root, s, e))
            if corrupt:
                raise VqaError("E_GATE", {"reason": "g_blob_integrity", "corrupt": corrupt[:8]})
            sizes = {s: os.stat(blob_path(self.root, s, e)).st_size for s, e in iter_blobs(self.root)}
            extra.update({"unit_count": len(units), "fg_count": sum(1 for u in units if U.is_fg(u)),
                          "blob_bytes": sum(sizes.values()), "fg_blob_bytes": C.fg_bytes(units, sizes),
                          "fg_ids_sha256": C.fg_ids_sha256(units)})
        extra["blob_count"] = len(iter_blobs(self.root))
        msha = G.publish_gen(self.root, n, cur, files, extra, self._point)
        G.flip(self.root, n, self._point)
        self.journal.append("flip", {"gen": n, "manifest_sha256": msha})
        return {"gen": n, "manifest_sha256": msha, "blobs": shas}

    def window_refs(self):
        """Blob shas that gc must keep: every blob_ref of every generation in [CURRENT-VM_GEN_KEEP+1, newest]."""
        cur = G.current_gen(self.root)
        if cur is None:
            return set()
        refs = set()
        for n in G.list_gens(self.root):
            if n >= cur - G.VM_GEN_KEEP + 1:
                for u in self.gen_units(n):
                    try:
                        refs |= U.blob_refs(u)
                    except (KeyError, TypeError):
                        raise VqaError("E_GATE", {"reason": "g_window", "why": "malformed_unit", "gen": n})
        return refs

    def rollback(self, to):  # @anchor:vm.store.rollback
        """Repoint CURRENT to a generation inside the window [CURRENT-VM_GEN_KEEP+1, CURRENT]."""
        self._need_lock()
        if not G.in_window(self.root, to):
            raise VqaError("E_ID", {"reason": "gen_outside_window", "to": to})
        G.flip(self.root, to, self._point)
        self.journal.append("rollback", {"to": to})
        return to

    def gc_blobs(self, referenced=None):
        """Delete blobs outside the rollback-window union (computed here, SPEC_VMEM §7). A caller-supplied
        `referenced` may only add to the keep set; one missing a window ref -> rc 5 E_GATE g_window."""
        self._need_lock()
        cur = self.current()
        if cur is None or not any(r["type"] == "flip" and r["body"].get("gen") == cur
                                  and r["body"].get("manifest_sha256") == G.manifest_sha(self.root, cur)
                                  for r in self.journal.records()):
            raise VqaError("E_GATE", {"reason": "g_window", "why": "flip_unacknowledged"})
        keep = self.window_refs()
        if referenced is not None:
            miss = sorted(keep - set(referenced))
            if miss:
                raise VqaError("E_GATE", {"reason": "g_window", "missing": miss[:8]})
            keep |= set(referenced)
        doomed = [(sha, ext) for sha, ext in iter_blobs(self.root) if sha not in keep]
        if doomed:
            # journal first (a fixed-size record: count, digest of the sorted list, 16-sha head), then unlink; the
            # full list goes to journal/gc_<seq>.txt so a crash after the record leaves the deletion accounted for
            names = sorted(sha for sha, _e in doomed)
            rec = self.journal.append("gc", {"n": len(names), "deleted_sha256": canon.sha256_bytes(
                "\n".join(names).encode("ascii")), "deleted_head": names[:16]})
            with open(os.path.join(self.journal.dir, "gc_%06d.txt" % rec["seq"]), "w") as f:
                f.write("\n".join(names) + "\n")
            self._point("gc_journaled")
            for sha, ext in doomed:
                delete_blob(self.root, sha, ext)
        self._prune_gens(cur)
        return [sha for sha, _e in doomed]

    def _prune_gens(self, cur):
        """FX-GC2: delete generations below the rollback window [cur-VM_GEN_KEEP+1, cur], oldest first. Idempotent;
        a SIGKILL mid-rmtree leaves a gen outside the window that the next gc finishes (commit takes max(list_gens)+1)."""
        cut = cur - G.VM_GEN_KEEP + 1
        for n in G.list_gens(self.root):
            if n < cut:
                G.delete_gen(self.root, n)

    def compact_journal(self):
        """Checkpoint at the newest flip older than the rollback window (SPEC_VMEM §7, VG-0219 C6); None if none."""
        self._need_lock()
        cur = G.current_gen(self.root)
        if cur is None:
            return None
        cut = cur - G.VM_GEN_KEEP + 1
        old = [r["seq"] for r in self.journal.records() if r["type"] == "flip" and r["body"].get("gen", cut) < cut]
        return self.journal.checkpoint(max(old)) if old else None

    def verify_blobs(self, quick=False):
        """Return list of corrupt shas; quick samples the first 64 by sha order."""
        items = iter_blobs(self.root)
        if quick:
            items = items[:64]
        return [s for s, e in items if not verify_blob(self.root, s, e)]

"""Pre-registered capture manifests for the PX experiments (SPEC_INT 3 ADV-15.1, ADV_SEAL1 1).

A manifest freezes, before any analysis, the (key) -> capture_sha256 of every calibration and held-out test capture.
Drivers load it, check the file sha against the pinned value, refuse any key it does not register and verify the
capture sha of every rendered capture before classifying it. Every refusal is rc 5 E_GATE reason split_leak.
Stdlib only; this module never renders nor analyses anything (see build.py for the generator)."""
import hashlib
import json
from pathlib import Path

from visioncore.errors import VqaError

HERE = Path(__file__).resolve().parent
X4_V2 = HERE / "x4_v2.json"   # CONSUMED (W6 held-out at a66d99e): audit only, drivers no longer bind it
X4_V3 = HERE / "x4_v3.json"   # live pre-registered split: test 2000..2029, cal 30..199 (X3 cal 40..69)
SCHEMA = "vqa.split-manifest/v1"
KEYS = {"x4": ("control", "seed"), "x3": ("mutant", "level", "seed")}


def leak(why, x, **kw):
    raise VqaError("E_GATE", dict(kw, reason="split_leak", x=x, why=why))


def capture_sha(frames):
    """sha256 over the concatenated raw rgb24 bytes of the capture frames (same as the X4 row capture_sha256)."""
    h = hashlib.sha256()
    for f in frames:
        h.update(f.tobytes())
    return h.hexdigest()


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Manifest:
    def __init__(self, doc, sha):
        self.doc, self.sha = doc, sha
        self.config = doc["config"]
        self.index = {}
        for x, names in KEYS.items():
            idx = {}
            for e in doc.get(x, []):
                k = tuple(e[n] for n in names)
                if k in idx:
                    leak("duplicate_manifest_key", x.upper(), key=list(k))
                idx[k] = e
            self.index[x] = idx
        for x, idx in self.index.items():
            caps = [e["capture_sha256"] for e in idx.values()]
            if len(caps) != len(set(caps)):   # one capture, one key: no capture shared across seeds or splits
                leak("duplicate_capture", x.upper())
            cal = {k[-1] for k, e in idx.items() if e["split"] == "cal"}
            test = {k[-1] for k, e in idx.items() if e["split"] == "test"}
            if cal & test:
                leak("seed_in_both_splits", x.upper(), seeds=sorted(cal & test))

    def entry(self, x, key):
        """Registered entry for key, else rc 5 split_leak (refuse any seed/key not in the manifest)."""
        e = self.index[x].get(tuple(key))
        if e is None:
            leak("unregistered_key", x.upper(), key=list(key), manifest_sha256=self.sha)
        return e

    def keys(self, x, split):
        return sorted(k for k, e in self.index[x].items() if e["split"] == split)

    def verify(self, x, key, capture_sha256):
        """Return the entry after checking the rendered capture sha against the registered one."""
        e = self.entry(x, key)
        if e["capture_sha256"] != capture_sha256:
            leak("capture_sha_mismatch", x.upper(), key=list(key), manifest_sha256=self.sha)
        return e


def load(path=X4_V3, pinned=None):
    """Load a manifest; with pinned set, the file sha must equal it (pre-registration), else rc 5."""
    p = Path(path)
    if not p.is_file():
        leak("manifest_absent", "SPLIT", path=str(p))
    sha = file_sha(p)
    if pinned is not None and sha != pinned:
        leak("manifest_sha_mismatch", "SPLIT", got=sha, want=pinned)
    doc = json.loads(p.read_text())
    if doc.get("schema") != SCHEMA:
        leak("manifest_schema", "SPLIT", got=doc.get("schema"))
    return Manifest(doc, sha)

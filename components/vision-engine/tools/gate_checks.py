#!/usr/bin/env python3
"""Heavier gate bodies split out of gates.py (size gate): catalogue staleness, golden replay, route budget. Stdlib only.

Loaded by path from gates.py; each function takes the tree root and returns [(code, detail)]. Pure: no clock, no randomness.
"""
import importlib.util
import json
import os
import sys
from pathlib import Path


def load_py(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_tool(root, name):
    """tools/<name>.py of the tree under test, else of this checkout (mutant trees carry no tools/)."""
    p = root / "tools" / (name + ".py")
    if not p.is_file():
        p = Path(__file__).resolve().parent / (name + ".py")
    return load_py(p, "_gate_tool_" + name)


def catalogue(root):
    """generated catalogue files must equal a fresh build_catalogue run (staleness)."""
    cat = root / "catalogue"
    if not (cat / "layers.src").is_file():
        return []  # trees without catalogue sources (gate mutant bases)
    bc = load_tool(root, "build_catalogue")
    try:
        out, _ = bc.build(str(root))
    except bc.BuildError as e:
        return [("E_GATE_CATALOGUE", f"build failed: {e.code} {e.msg}")]
    return [("E_STALE", f"catalogue/{name} is stale: run tools/build_catalogue.py")
            for name, text in sorted(out.items())
            if not (cat / name).is_file() or (cat / name).read_text(encoding="utf-8") != text]


def with_tree(root, fn):
    """run fn() with `root` first on sys.path and the visioncore/vadapt packages freshly imported; then forget them."""
    old = list(sys.path)
    purge = lambda: [sys.modules.pop(k) for k in [k for k in sys.modules if k.split(".")[0] in ("visioncore", "vadapt", "vmem")]]  # noqa: E731
    sys.path.insert(0, str(root))
    purge()
    try:
        return fn()
    finally:
        sys.path[:] = old
        purge()


def golden(root):
    """W1 exit gate: regenerate every synth golden, compare frame shas and the S0-S2 class histograms."""
    gd = root / "tests" / "golden"
    if not gd.is_dir():
        return []

    def run():
        import importlib
        np = importlib.import_module("numpy")
        synth = importlib.import_module("vadapt.synth")
        syn = importlib.import_module("vadapt.synth.synthetic")
        pipeline = importlib.import_module("visioncore.pipeline")
        fr = importlib.import_module("visioncore.frame")
        out = []
        for d in sorted(x for x in gd.iterdir() if (x / "expect").is_dir()):
            ex = d / "expect"
            try:
                exp = json.loads((ex / "synth.json").read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                out.append(("E_GATE_GOLDEN", f"{d.name}: synth.json unreadable: {e}"))
                continue
            frames, _, _ = synth.render_scenario(exp["scenario"], exp["seed"])
            shas = [syn.frame_sha(f) for f in frames]
            if shas != exp["frame_sha256"] or syn.frames_sha256(shas) != exp["frames_sha256"]:
                out.append(("E_GATE_GOLDEN", f"{d.name}: regenerated frames differ from expect/synth.json"))
            fs = ex / "frames.sha256"
            if not fs.is_file() or fs.read_text(encoding="utf-8") != exp["frames_sha256"] + "\n":
                out.append(("E_GATE_GOLDEN", f"{d.name}: expect/frames.sha256 differs from synth.json"))
            cp = ex / "classes.json"
            if not cp.is_file():
                out.append(("E_GATE_GOLDEN", f"{d.name}: expect/classes.json missing (class histograms are the S2 golden)"))
                continue
            h, w = frames[0].shape[:2]
            src = fr.MemoryFrameSource([fr.Frame(i, i * 33333, i, np.ascontiguousarray(f)) for i, f in enumerate(frames)],
                                       {"width": w, "height": h})
            run_ = pipeline.analyze(src)
            hist = [[int((t["cls"] == c).sum()) for c in range(7)] for t in run_.tiles]
            want = json.loads(cp.read_text(encoding="utf-8")).get("class_hist")
            if hist != want:
                bad = [i for i, (a, b) in enumerate(zip(hist, want or [])) if a != b][:3]
                out.append(("E_GATE_GOLDEN", f"{d.name}: class histogram differs from expect/classes.json class_hist (frames {bad})"))
        return out
    return with_tree(root, run)


def route_budget(root):
    """navigability: every catalogue/route_queries.tsv query answers an anchor with a code path in the top 3 (<= 2000 B),
    ABSTAIN queries must abstain (rc 3)."""
    qf = root / "catalogue" / "route_queries.tsv"
    if not qf.is_file():
        return [("E_GATE_ROUTE", "catalogue/route_queries.tsv missing")] if (root / "catalogue" / "routes.tsv").is_file() else []
    rc_mod = load_tool(root, "route_code")
    out = []
    n = 0
    for ln, line in enumerate(qf.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        cols = line.split("\t")
        if len(cols) != 2:
            out.append(("E_GATE_ROUTE", f"route_queries.tsv:{ln} expected 2 columns"))
            continue
        n += 1
        q, want = cols
        res = rc_mod.route(q, 3, str(root))
        if len(rc_mod.canon(res).encode("utf-8")) > rc_mod.MAX_B:
            out.append(("E_GATE_ROUTE", f"route_queries.tsv:{ln} answer over {rc_mod.MAX_B} B"))
        if want == "ABSTAIN":
            if not res["abstain"]["flag"]:
                out.append(("E_GATE_ROUTE", f"route_queries.tsv:{ln} {q!r} should abstain, answered {[r[0] for r in res['r']]}"))
        elif not any(r[0] == want and r[1] and r[2] > 0 for r in res["r"][:3]):
            out.append(("E_GATE_ROUTE", f"route_queries.tsv:{ln} {q!r}: {want} (with path) not in top-3 {[r[0] for r in res['r'][:3]]}"))
    if n < 20:
        out.append(("E_GATE_ROUTE", f"route_queries.tsv has {n} queries < 20"))
    return out


def hint_neutral(root):
    """NA until the hint path exists in W3+ pipelines with a hint-neutral fixture; reported in the receipt as NA."""
    return []


def _git(root, *args):
    import subprocess
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=60)


def sealed_untouched(root):
    """STORAGE LAW §4: visioncore/ and vmem/ are byte-identical to tag ve-sealed-1 (tracked diff + untracked files). NA outside a git checkout."""
    if not (root / ".git").exists():
        return []
    if _git(root, "rev-parse", "--verify", "-q", "ve-sealed-1^{commit}").returncode != 0:
        return [("E_GATE_SEALED", "tag ve-sealed-1 not found")]
    d = _git(root, "diff", "--name-only", "ve-sealed-1", "--", "visioncore", "vmem")
    u = _git(root, "ls-files", "--others", "--exclude-standard", "--", "visioncore", "vmem")
    if d.returncode or u.returncode:
        return [("E_GATE_SEALED", "git failed: " + (d.stderr or u.stderr)[-200:])]
    changed = d.stdout.splitlines()
    untracked = [n for n in u.stdout.splitlines() if "__pycache__" not in n]
    allowed = {}
    errors = []
    # Each card owns its proof binding; absence never grants an exception.
    for card_id, directory in (("RT0D01", "rt0"), ("RT0D02", "rt0d02")):
        receipt_path = root / "eval/results" / directory / "EQUIV_RECEIPT.json"
        if not receipt_path.exists():
            continue
        import hashlib
        import subprocess
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if receipt["schema"] != "vqa.rt0.equiv/v1" or receipt["card_id"] != card_id:
                raise ValueError("receipt identity")
            proof_path = "eval/results/" + directory + "/PROOF_SHA_TABLE.json"
            if receipt["proof_table"] != proof_path:
                raise ValueError("proof table path")
            proof_bytes = (root / proof_path).read_bytes()
            proof_sha = hashlib.sha256(proof_bytes).hexdigest()
            proof = json.loads(proof_bytes)
            if receipt["proof_table_sha256"] != proof_sha or proof.get("verdict") != "PASS":
                raise ValueError("proof table hash/verdict")
            entries = receipt["files"]
            if not isinstance(entries, list) or not entries:
                raise ValueError("empty file list")
            card_allowed = {}
            for entry in entries:
                name = entry["path"]
                path = root / name
                if (not isinstance(name, str) or name in allowed or name in card_allowed or
                        not name.startswith(("visioncore/", "vmem/")) or
                        path.resolve() != root.resolve() / name or ".." in Path(name).parts or
                        not path.is_file() or path.is_symlink()):
                    raise ValueError("invalid/duplicate sealed path")
                if entry["card_id"] != card_id or entry["proof_table_sha256"] != proof_sha:
                    raise ValueError("file proof binding")
                old = subprocess.run(["git", "-C", str(root), "show", "ve-sealed-1:" + name],
                                     capture_output=True, timeout=60)
                if old.returncode or hashlib.sha256(old.stdout).hexdigest() != entry["old_sha256"]:
                    raise ValueError("old_sha256 mismatch: " + name)
                if hashlib.sha256(path.read_bytes()).hexdigest() != entry["new_sha256"]:
                    raise ValueError("new_sha256 mismatch: " + name)
                card_allowed[name] = entry
            allowed.update(card_allowed)
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            errors.append(("E_GATE_SEALED", "invalid " + card_id + " receipt: " + str(exc)[:200]))
    return errors + [("E_GATE_SEALED", f"{n} differs from ve-sealed-1") for n in changed if n not in allowed] + \
           [("E_GATE_SEALED", f"{n} untracked in sealed tree") for n in untracked]


def _live_engines():
    out = []
    for p in Path("/proc").iterdir():
        if p.name.isdigit():
            try:
                comm = (p / "comm").read_text().strip().lower()
            except OSError:
                continue
            if comm.startswith(("blender", "godot")):
                out.append(f"{p.name}:{comm}")
    return out


def disk_frames(root):
    """STORAGE LAW §4: the engine smoke (eval/x1_determinism, 1 repeat) leaves no frame-like file in build/ newer than a stamp,
    /dev/shm/vqa-ramws/engine/ empty and 0 NEW Blender/Godot procs (pre-existing ones, e.g. an operator editor, are not ours). NA in trees without eval/ (mutant bases)."""
    import shutil
    import subprocess
    import tempfile
    if not (root / "eval" / "x1_determinism.py").is_file():
        return []
    ram = load_py(root / "vadapt" / "ramws.py", "_gate_ramws")
    build = root / "build"
    (build / "gate_disk").mkdir(parents=True, exist_ok=True)
    out_dir = Path(tempfile.mkdtemp(prefix="g", dir=build / "gate_disk"))
    try:
        stamp = out_dir / ".stamp"
        stamp.write_text("")
        since = stamp.stat().st_mtime_ns
        before = set(_live_engines())
        r = subprocess.run([sys.executable, "-B", "-m", "eval.x1_determinism", "--out", str(out_dir / "o"), "--repeats", "1"],
                           cwd=str(root), capture_output=True, text=True, timeout=280, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        out = []
        if r.returncode != 0:
            out.append(("E_GATE_DISK", f"smoke rc {r.returncode}: {r.stderr[-200:]}"))
        out += [("E_GATE_DISK", f"frame-like file outside RAM workspace: build/{n}") for n in ram.check_disk(build, since=since)]
        left = Path(ram.ROOT) / "engine"
        if left.is_dir() and any(left.iterdir()):
            out.append(("E_GATE_DISK", f"{left} not empty"))
        out += [("E_GATE_DISK", f"engine process alive {n}") for n in sorted(set(_live_engines()) - before)]
        return out
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)

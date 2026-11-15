#!/usr/bin/env python3
"""Deterministic B0 repository gates. rc 5 indicates an unmet tree contract."""
# @module {"id":"tools.gates","layer":4,"owner":"BINT"}
import argparse
import ast
import csv
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_ID = re.compile(r"^(bvqa|vqa)\.[a-z][a-z0-9-]*/v[1-9][0-9]*$")
DATA_ID = re.compile(r"^[a-z][a-z0-9_.-]*$")
BANNED_CITATIONS = ("SW-0215", "PM-238.4", "PM-127.1", "PM-366.1")


def canon(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"


def digest(b):
    return hashlib.sha256(b).hexdigest()


def read(root, path):
    return json.loads((root / path).read_text())


def git(repo, *argv):
    return subprocess.check_output(["git", "-C", str(Path(str(repo)).expanduser()), *argv], stderr=subprocess.DEVNULL, timeout=15)


def sources(root):
    for base in ("blender_visual_qa", "bpy_side", "tools"):
        yield from sorted(p for p in (root / base).rglob("*.py") if "__pycache__" not in p.parts)


def imports(node):
    if isinstance(node, ast.Import):
        return [a.name for a in node.names]
    if isinstance(node, ast.ImportFrom):
        names = [node.module or ""]
        if node.module == "bpy_side":
            names += ["bpy_side." + a.name for a in node.names]
        return names
    # Literal dynamic imports are subject to the same boundary.
    if isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute)):
        name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        if name in ("import_module", "__import__") and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            return [node.args[0].value]
    return []


def g_tier1(root):
    out = []
    for name, cap in (("llms.txt", 600), ("AGENTS.md", 6000)):
        p = root / name
        if not p.is_file() or not p.stat().st_size or p.stat().st_size > cap:
            out.append(name + ":missing_or_over_budget")
    for name in ("README.md", "QUICKSTART.md", "pyproject.toml"):
        if not (root / name).is_file(): out.append(name + ":missing")
    return out


def g_vendor(root):
    v = read(root, "tools/VENDORED.json")
    out = []
    if not re.fullmatch(r"[0-9a-f]{40}", v["commit"]): return ["invalid_commit"]
    for row in v["files"]:
        source = git(v["src_repo"], "show", v["commit"] + ":" + row["path"])
        expected = source.replace(b"(vc|vm|va|tools)", b"(bv|bpy|tools)") if row["patch"] == "anchor-prefix" else source
        if row["patch"] not in (None, "anchor-prefix") or digest(source) != row["src_sha256"] or (root / row["path"]).read_bytes() != expected or row["sha256"] != digest(expected):
            out.append(row["path"] + ":vendor_mismatch")
    if {r["path"] for r in v["files"]} != {"tools/route_code.py", "tools/build_catalogue.py"}:
        out.append("vendor_inventory")
    return out


def g_pin(root):
    v = read(root, "tools/VENDORED.json")
    cfg = read(root, "config/vision.json")
    pin = cfg["pin"]
    out = []
    if cfg.get("tooling_commit", pin["commit"]) != v["commit"]: out.append("commit_mismatch")
    api = git(v["src_repo"], "show", pin["commit"] + ":visioncore/__init__.py").decode()
    found = re.search(r'API_VERSION\s*=\s*"([^"]+)"', api)
    if not found or pin["version"] != found[1] or pin["version"] != "1.1.0": out.append("version_mismatch")
    wheel = Path(cfg["engine_path"])
    if not wheel.is_absolute(): wheel = root / wheel
    if not isinstance(pin.get("wheel_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", pin["wheel_sha256"]):
        out.append("wheel_sha256:AUSENTE")
    elif not wheel.is_file() or digest(wheel.read_bytes()) != pin["wheel_sha256"]:
        out.append("wheel_content_mismatch")
    else:
        import zipfile
        with zipfile.ZipFile(wheel) as z:
            names = [n for n in z.namelist() if n.endswith(".py") and n.split("/")[0] in ("visioncore", "vmem", "vadapt")]
            expected = {name for name in git(v["src_repo"], "ls-tree", "-r", "--name-only", pin["commit"]).decode().splitlines() if name.endswith(".py") and name.split("/")[0] in ("visioncore", "vmem", "vadapt")}
            if set(names) != expected: out.append("wheel_package_inventory")
            if not names: out.append("wheel_package_missing")
            for name in names:
                if z.read(name) != git(v["src_repo"], "show", pin["commit"] + ":" + name):
                    out.append(name + ":wheel_source_mismatch")
    return out


READOPTION_WAVES = ("W9", "W10F")


def readoption_ok(receipt):
    """Re-adoption receipt of an adopted file edited after migration: exactly {old_sha256, new_sha256, wave}."""
    rev = receipt.get("readoption", {})
    return isinstance(rev, dict) and rev.get("wave") in READOPTION_WAVES and rev == {
        "old_sha256": receipt["migration_sha256"], "new_sha256": receipt["sha256"], "wave": rev.get("wave")}


def g_migration(root):
    baseline = read(root, "tools/MIGRATION_SOURCE.json")
    source, pin = baseline["repo"], baseline["commit"]
    expected = set(git(source, "ls-tree", "-r", "--name-only", pin).decode().splitlines())
    with (root / "copy_manifest.tsv").open() as f: rows = list(csv.DictReader(f, delimiter="\t"))
    policy = read(root, "tools/MIGRATION_POLICY.json")
    out = []
    adopted = read(root, "tools/ADOPTED.json")["files"]
    receipts = {r["path"]: r for r in adopted}
    for receipt in adopted:
        if "migration_sha256" in receipt:
            revision = receipt.get("readoption", {})
            if not readoption_ok(receipt):
                out.append(receipt["path"] + ":readoption_receipt_mismatch")
    legacy = [r for r in rows if not r["src"].startswith("@adopt/")]
    if len(legacy) != len(expected) or {r["src"] for r in legacy} != expected: out.append("source_inventory_not_bijective")
    if set(policy) != expected: out.append("migration_policy_inventory")
    for row in legacy:
        if policy.get(row["src"]) != {"action": row["action"], "dst": row["dst"]}:
            out.append(row["src"] + ":migration_policy")
    allowed = {"COPY", "ADAPT", "SPLIT", "DROP", "REGENERATE", "RESET", "REWRITE", "ADOPT"}
    seen = set()
    for r in rows:
        if r["src"] in seen: out.append(r["src"] + ":duplicate")
        seen.add(r["src"])
        if r["action"] not in allowed: out.append(r["src"] + ":bad_action")
        if r["action"] == "DROP": continue
        note = json.loads(r["note"])
        if note.get("state") == "PLANNED" and note.get("wave") != "B0" and r["action"] != "COPY":
            continue
        p = root / r["dst"]
        if not p.resolve().is_relative_to(root.resolve()):
            out.append(r["dst"] + ":path_escape"); continue
        if not p.is_file(): out.append(r["dst"] + ":missing"); continue
        note = json.loads(r["note"])
        actual = digest(p.read_bytes())
        if r["action"] == "COPY" and p.read_bytes() != git(source, "show", pin + ":" + r["src"]): out.append(r["dst"] + ":copy_byte")
        if r["action"] == "ADOPT" and actual != receipts.get(r["src"][7:], {}).get("sha256"): out.append(r["dst"] + ":adopt_mutated")
        if "dst_sha256" in note and actual != note["dst_sha256"]: out.append(r["dst"] + ":adapt_drift")
    adopted = read(root, "tools/ADOPTED.json")["files"]
    baseline_hashes = {r["path"]: r.get("migration_sha256", r["sha256"]) for r in adopted}
    for row in rows:
        if row["action"] == "ADOPT" and json.loads(row["note"]).get("sha256") != baseline_hashes.get(row["src"][7:]):
            out.append(row["src"] + ":adopt_receipt_mismatch")
    if {r["src"][7:] for r in rows if r["action"] == "ADOPT"} != {r["path"] for r in adopted}: out.append("adopt_inventory")
    # Public installation entry point does not replace the specified checkout script.
    for required in ("bin/blender-visual-qa", "migration/copy_manifest.tsv"):
        if not (root / required).is_file(): out.append(required + ":missing")
    return out


def g_bpy_boundary(root):
    out = []
    twins = {"bpy_side.q", "bpy_side.canon"}
    decoder = root / "bpy_side/decode_exr.py"
    decoder_pure = False
    if decoder.is_file():
        # Scan every import, including lazy imports. Dependencies are restricted
        # to stdlib and numpy; relative and dynamic import escapes fail closed.
        nodes = list(ast.walk(ast.parse(decoder.read_text())))
        decoder_pure = all(name.split(".")[0] in sys.stdlib_module_names | {"numpy", "__future__"}
                           for node in nodes for name in imports(node))
        decoder_pure = decoder_pure and not any(
            (isinstance(node, ast.ImportFrom) and node.level)
            or (isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute))
                and (node.func.id if isinstance(node.func, ast.Name) else node.func.attr)
                in {"__import__", "import_module", "exec", "eval"})
            for node in nodes)
        if not decoder_pure:
            out.append("bpy_side/decode_exr.py:impure_decoder")
    for path in sources(root):
        rel = path.relative_to(root).as_posix()
        bpy = rel.startswith("bpy_side/")
        twin = rel in ("bpy_side/q.py", "bpy_side/canon.py")
        for node in ast.walk(ast.parse(path.read_text())):
            for name in imports(node):
                top = name.split(".")[0]
                if not bpy and top in ("bpy", "bmesh", "mathutils", "gpu"): out.append(rel + ":host_bpy")
                if bpy and top == "blender_visual_qa": out.append(rel + ":bpy_host")
                if not bpy and top == "bpy_side" and name not in twins and name != "bpy_side" and not (rel == "blender_visual_qa/truth/decode_worker.py" and name == "bpy_side.decode_exr" and decoder_pure): out.append(rel + ":cross_boundary")
                if not bpy and isinstance(node, ast.Import) and name == "bpy_side": out.append(rel + ":unrestricted_bpy_side")
                if twin and top not in sys.stdlib_module_names and top != "__future__": out.append(rel + ":impure_twin")
    for twin in ("bpy_side/q.py", "bpy_side/canon.py"):
        if not (root / twin).is_file(): out.append(twin + ":missing")
    return sorted(set(out))


def g_numpy_scope(root):
    out = []
    for path in sources(root):
        rel = path.relative_to(root).as_posix()
        allowed = rel.startswith(("bpy_side/", "blender_visual_qa/truth/")) or rel in ("blender_visual_qa/oracles/pixel.py", "blender_visual_qa/vision_stage.py")
        if not allowed and any(n.split(".")[0] == "numpy" for node in ast.walk(ast.parse(path.read_text())) for n in imports(node)):
            out.append(rel + ":numpy_scope")
    return out


def g_no_engine_leak(root):
    out = []
    for p in sources(root):
        if re.search(r"\b(go" + r"dot|gd" + r"script)\b", p.read_text(), re.I): out.append(p.relative_to(root).as_posix() + ":foreign_engine")
    for name in ("visioncore", "vadapt", "vmem"):
        if (root / name).exists(): out.append(name + ":engine_vendored")
    return out


def g_schema_prefix(root):
    out = []
    schemas = sorted((root / "blender_visual_qa").rglob("*.schema.json"))
    if not schemas: return ["schemas:AUSENTE"]
    def walk(value, path):
        if isinstance(value, dict):
            for k, v in value.items():
                if k == "schema" and isinstance(v, str) and not SCHEMA_ID.fullmatch(v): out.append(path + ":schema_id")
                if k == "schema" and isinstance(v, dict) and "const" in v and not SCHEMA_ID.fullmatch(str(v["const"])): out.append(path + ":schema_const")
                if k in ("id", "mutant_id") and isinstance(v, str) and not DATA_ID.fullmatch(v): out.append(path + ":data_id")
                walk(v, path)
        elif isinstance(value, list):
            for v in value: walk(v, path)
    for p in schemas:
        rel = p.relative_to(root).as_posix()
        if not re.fullmatch(r"[a-z][a-z0-9-]*\.v[1-9][0-9]*\.schema\.json", p.name): out.append(rel + ":filename")
        walk(json.loads(p.read_text()), rel)
    # Scan authored data only; do not load scratch runs or arbitrary credentials.
    for base in ("config", "examples", "adapters"):
        for p in sorted((root / base).rglob("*.json")):
            if p.name not in ("auth.json",) and not p.name.endswith(".key"):
                walk(json.loads(p.read_text()), p.relative_to(root).as_posix())
    return sorted(set(out))


def g_wave_dag(root):
    p = root / "migration/wave_plan.json"
    if not p.is_file(): return ["migration/wave_plan.json:AUSENTE"]
    plan = json.loads(p.read_text())
    if plan.get("schema") != "bvqa.wave-plan/v1": return ["wave_schema"]
    waves = plan["waves"]
    ids = [w["id"] for w in waves]
    if len(set(ids)) != len(ids): return ["wave_duplicate"]
    deps = {w["id"]: set(w["needs"]) for w in waves}
    expected = {"B0": set(), "B1": {"B0"}, "B2": {"B1"}, "B3": {"B1"}, "B4": {"B1", "B2", "B3"}, "B5": {"B4"}}
    if deps != expected: return ["wave_dependency_contract"]
    done = set()
    for _ in waves:
        ready = {i for i, d in deps.items() if i not in done and d <= done}
        if not ready: break
        done |= ready
    if done != set(ids): return ["wave_cycle_or_unknown_need"]
    return []


def g_threshold(root):
    out = []
    p = root / "catalogue/thresholds.tsv"
    with p.open() as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    if not rows: return ["thresholds:AUSENTE"]
    if set(rows[0]) != {"id", "value", "unit", "owner", "status", "source"}: return ["threshold_columns"]
    ids = set()
    for row in rows:
        name = row["id"]
        if name in ids or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name): out.append(name + ":id")
        ids.add(name)
        if not re.fullmatch(r"-?[0-9]+", row["value"]): out.append(name + ":not_integer")
        if row["status"] not in ("CITED", "MEASURED", "DESIGN_CHOICE", "TARGET"): out.append(name + ":status")
        if row["status"] == "CITED":
            if any(x in row["source"] for x in BANNED_CITATIONS): out.append(name + ":unsupported_citation")
            else: out.append(name + ":citation_receipt_unproven")
        if row["status"] == "MEASURED" and not (root / row["source"]).is_file(): out.append(name + ":measurement_receipt_missing")
        if row["status"] == "DESIGN_CHOICE" and not re.search(r"C[0-6]|X3|uncalibrated", row["source"]): out.append(name + ":calibration_missing")
    required = {"BO_WIND_MAX", "BO_UV_TV_PPM", "BO_UV_DEG_PPM", "BO_ANISO", "BO_SUBJ_MIN_PPM", "BO_ZF_EPS_UM", "BO_VIS_MIN_PPM", "BUDGET_TRIS_OBJECT", "BUDGET_TRIS_SCENE", "RIGID_SHARE_PCT", "EDGE_RING", "MOV_Q4", "OCC_PERMILLE", "FRAME_CAP", "ARTIFACT_MIB", "MP4_MIB", "STILL_N"}
    out += [name + ":missing" for name in sorted(required - ids)]
    registered = {int(row["value"]) for row in rows if re.fullmatch(r"-?[0-9]+", row["value"])}
    for path in sources(root):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Compare):
                for part in ast.walk(node):
                    if isinstance(part, ast.Constant) and type(part.value) is int and part.value not in registered:
                        out.append(path.relative_to(root).as_posix() + ":unregistered_integer:" + str(part.value))
    return out


def g_pure(root):
    out = []
    for p in (root / "tools").glob("*gates.py"):
        tree = ast.parse(p.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = ast.unparse(node.func)
                if name in ("time.time", "time.monotonic", "datetime.now", "random.random", "os.urandom"):
                    out.append(p.relative_to(root).as_posix() + ":volatile_gate")
    return out


# --- STORAGE LAW v1 (campaigns/visual-qa-0/v12m/STORAGE_LAW.md 4) -----------------------------------------------------
RAM_TOOL, RAM_ROOT = "bvqa", "/dev/shm/vqa-ramws"
SMOKE_ARGS = ("--frames", "8", "--res", "320x180", "--spp", "4", "--benches", "B1,B6", "--seeds", "fit:0-0,gate:1-1", "--jobs", "2")
OUT_CAP, FAIL_EP_CAP, FAIL_EP_FILES = 64 << 20, 100 << 10, 6
_SMOKE = {}


def _wheel(root):
    cfg = read(root, "config/vision.json")
    wheel = Path(cfg["engine_path"])
    return (wheel if wheel.is_absolute() else root / wheel), cfg["pin"]["wheel_sha256"]


def _ramws(root):
    wheel, _ = _wheel(root)
    if str(wheel) not in sys.path:
        sys.path.insert(0, str(wheel))
    from vadapt import ramws
    return ramws


def _tree(p):
    return sum(f.stat().st_size for f in Path(p).rglob("*") if f.is_file() and not f.is_symlink())


def _own_procs(marker):
    out = []
    for d in Path("/proc").iterdir():
        try:
            if d.name.isdigit() and marker.encode() in (d / "cmdline").read_bytes():
                out.append(d.name)
        except OSError:
            pass
    return out


def smoke(root):
    """Run the BVQA storage smoke once per process into build/gate_disk/<pid>/ -> facts dict (cached)."""
    if root in _SMOKE:
        return _SMOKE[root]
    import os
    base = root / "build" / "gate_disk" / str(os.getpid())
    base.mkdir(parents=True, exist_ok=True)
    stamp = base / "stamp"
    stamp.write_text("stamp\n")
    since = stamp.stat().st_mtime_ns
    run_id = "gate-%d" % os.getpid()
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    env.setdefault("BVQA_CPU_CAP", "15-19")  # room host reservation for Blender, cpus 10-14 = os-cu-0 (override via env)
    cmd = [sys.executable, "-B", "-m", "blender_visual_qa.truth.run_bench", "--out", str(base / "out"), *SMOKE_ARGS,
           "--run-id", run_id, "--campaign", "gate"]
    try:
        proc = subprocess.run(cmd, cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=900)
        rc, log = proc.returncode, proc.stdout[-4000:].decode(errors="replace")
    except subprocess.TimeoutExpired:
        rc, log = -1, "timeout"
    ramws = _ramws(root)
    facts = {"rc": rc, "log": log, "out": base / "out", "since": since, "run_id": run_id,
             "disk": [p for p in ramws.check_disk(root / "build", since=since) if not p.endswith("stamp")],
             "ws_left": (Path(RAM_ROOT) / RAM_TOOL / "gate" / run_id).exists(), "procs": _own_procs(run_id)}
    try:
        facts["receipt"] = json.loads((base / "out" / "ram_receipt.json").read_text())
        facts["report"] = json.loads((base / "out" / "calib_report.json").read_text())
    except (OSError, ValueError):
        facts["receipt"] = facts["report"] = None
    _SMOKE[root] = facts
    return facts


def g_disk_frames(root):
    f = smoke(root)
    out = []
    if f["receipt"] is None or f["report"] is None or f["rc"] not in (0, 5):
        out.append("smoke_failed:rc=%s:%s" % (f["rc"], f["log"][-300:].replace("\n", " ")))
    elif f["report"].get("failures"):
        out.append("smoke_units_failed:%d" % len(f["report"]["failures"]))
    out += ["frame_on_disk:" + p for p in f["disk"]]
    if f["ws_left"]:
        out.append("ramws_not_released")
    if f["receipt"] is not None and not 0 < f["receipt"].get("ram_peak_bytes", 0) <= f["receipt"].get("quota", 0):
        out.append("ram_peak_out_of_quota")
    if f["receipt"] is not None and f["receipt"].get("fstype") not in ("tmpfs", "ramfs"):
        out.append("ramws_not_tmpfs")
    if f["procs"]:
        out.append("procs_alive:%d" % len(f["procs"]))
    rows = (f["out"] / "rows.jsonl").read_text() if (f["out"] / "rows.jsonl").is_file() else ""
    if "/dev/shm" in rows:
        out.append("workspace_path_in_rows")
    return out


def g_disk_size(root):
    f = smoke(root)
    out = []
    if f["out"].is_dir() and _tree(f["out"]) > OUT_CAP:
        out.append("smoke_out_over_64MB")
    build = root / "build"
    for ep in sorted(build.rglob("fail_evidence/*")) if build.is_dir() else ():
        if ep.is_dir():
            files = [q for q in ep.rglob("*") if q.is_file()]
            if _tree(ep) > FAIL_EP_CAP or len(files) > FAIL_EP_FILES + 1:  # + its sha manifest
                out.append("fail_evidence_over_cap:" + ep.relative_to(build).as_posix())
    now_ns = (build / "gate_disk").stat().st_mtime_ns if (build / "gate_disk").is_dir() else 0
    kept = build / "kept_frames"
    for d in sorted(kept.iterdir()) if kept.is_dir() else ():
        try:
            keep = json.loads((d / "KEEP.json").read_text())
            if int(keep["ttl_until"]) * 10 ** 9 < now_ns or int(keep["cap_mb"]) > 256:
                out.append("kept_expired_or_over_cap:" + d.name)
        except (OSError, ValueError, KeyError, TypeError):
            out.append("kept_without_KEEP_json:" + d.name)
    return out


def g_ramws_pin(root):
    import zipfile
    wheel, want = _wheel(root)
    if not wheel.is_file() or digest(wheel.read_bytes()) != want:
        return ["wheel_sha_mismatch"]
    with zipfile.ZipFile(wheel) as z:
        names = set(z.namelist())
    return ["wheel_missing:" + n for n in ("vadapt/ramws.py", "vadapt/blender/__init__.py", "vadapt/blender/from_render_dir.py")
            if n not in names]


GATES = {fn.__name__: fn for fn in (g_migration, g_vendor, g_pin, g_tier1, g_no_engine_leak, g_bpy_boundary, g_numpy_scope, g_schema_prefix, g_wave_dag, g_threshold, g_pure, g_ramws_pin, g_disk_frames, g_disk_size)}


# @anchor:tools.gates.main
def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("gates", nargs="*", choices=tuple(GATES))
    p.add_argument("--all", action="store_true")
    p.add_argument("--root", type=Path, default=ROOT)
    args = p.parse_args(argv)
    names = list(GATES) if args.all else args.gates
    if not names:
        print(canon({"code": "E_USAGE", "rc": 2, "reason": "no gates selected"}), end="")
        return 2
    results = []
    for name in names:
        try:
            failures = GATES[name](args.root.resolve())
        except Exception as exc:
            failures = ["precondition:" + type(exc).__name__ + ":" + str(exc)]
        results.append({"gate": name, "rc": 5 if failures else 0, "failures": sorted(failures)})
    rc = 5 if any(r["rc"] for r in results) else 0
    print(canon({"code": "E_GATE" if rc else "OK", "rc": rc, "gates": results}), end="")
    return rc


if __name__ == "__main__":
    sys.exit(main())

# @module {"id":"vc.cli","layer":4,"stage":"L4","owner":"ARCH","deps":["vc.canon","vc.change","vc.cite","vc.errors","vc.frame","vc.layout","vc.p","vc.pack","vc.pipeline"],"inputs":"argv","outputs":"one canonical JSON object on stdout","invariants":["rc-contract","never-rc-1","refusal-leaves-out"],"anti_uses":["clock"],"oracle":"tests/test_pipeline_cli.py"}
"""@module vc.cli L4 - `vcore` CLI: selftest, analyze, pack, cite-check, offline rejudge (SPEC_INT R4)."""
import sys
from pathlib import Path

import numpy as np

from . import API_VERSION, canon, change, errors, frame as fr, layout, params as P, pipeline
from .errors import VqaError

SELFTEST_SCENARIOS = ("clean_static", "scroll", "flicker", "move")


def _usage(msg):
    raise VqaError("E_USAGE", {"reason": msg})


def _opts(argv, flags, nargs):
    """Tiny parser: positional args + `--name value` flags (flags: allowed names). Returns (pos, opts)."""
    pos, opts, i = [], {}, 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            if a[2:] not in flags or i + 1 >= len(argv):
                _usage("bad flag %s" % a)
            opts[a[2:]] = argv[i + 1]
            i += 2
        else:
            pos.append(a)
            i += 1
    if len(pos) != nargs:
        _usage("expected %d positional argument(s)" % nargs)
    return pos, opts


def _load_json(path):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise VqaError("E_IO", {"path": str(path), "reason": str(e)[:80]})
    return canon.loads(text)


def _load_jsonl(path):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise VqaError("E_IO", {"path": str(path), "reason": str(e)[:80]})
    return [canon.loads(l) for l in text.splitlines() if l]


def cmd_analyze(argv):
    pos, o = _opts(argv, ("out", "hints", "profile"), 1)
    manifest = Path(pos[0])
    table = P.effective(_load_json(o["profile"]) if "profile" in o else None)
    hints = None
    if "hints" in o:
        hints = _load_jsonl(o["hints"])
    elif fr.read_manifest(manifest).get("hints") and (manifest.parent / fr.read_manifest(manifest)["hints"]).is_file():
        hints = _load_jsonl(manifest.parent / fr.read_manifest(manifest)["hints"])
    out = Path(o["out"]) if "out" in o else manifest.parent / "vision"
    run = pipeline.analyze(pipeline.ManifestSource(manifest), table, hints, out)
    return {"code": "OK", "rc": 0, "detail": {"out": str(out), "n_frames": len(run.tiles), "grid": run.grid.ident,
                                              "params_sha256": run.params_sha256, "frames_sha256": run.frames_sha256,
                                              "stages": run.stages}}


def _vision_batch(vdir, manifest=None):
    """Plain-dict batch for pack.pack from a vision dir (tiles.bin read lazily per frame; rgb only with a manifest)."""
    vdir = Path(vdir)
    v = _load_json(vdir / "vision.json")
    rows = _load_jsonl(vdir / "frames.jsonl")
    raw = (vdir / "tiles.bin").read_bytes()
    h = layout.parse_tile_header(raw)
    row_of = {r["frame"]: k for k, r in enumerate(rows)}
    tiles = np.frombuffer(raw, dtype=change.TILE_DT, offset=layout.TILE_HDR_SIZE).reshape(len(rows), h["N"])

    def tiles_of(n):
        if n not in row_of:
            raise VqaError("E_VC_ID_RANGE", {"frame": n})
        return tiles[row_of[n]]

    batch = {"W": v["width"], "H": v["height"], "params_sha256": v["params_sha256"], "tiles": tiles_of,
             "regions": {r["id"]: r for r in _load_jsonl(vdir / "regions.jsonl")},
             "gops": _load_jsonl(vdir / "gops.jsonl") if (vdir / "gops.jsonl").is_file() else []}
    if manifest is not None:
        by = {pipeline.frame_number(f): f.rgb for f in pipeline.ManifestSource(manifest)}
        batch["frames"] = lambda n: by[n]
    return batch


def cmd_pack(argv):
    pos, o = _opts(argv, ("out", "cap", "shuffle-window", "manifest", "batch"), 1)
    try:
        from . import pack
    except ImportError:
        raise VqaError("E_USAGE", {"reason": "pack stage unavailable"})
    batch = _vision_batch(pos[0], o.get("manifest"))
    if not batch["gops"]:
        raise VqaError("E_USAGE", {"reason": "vision dir has no gops.jsonl (S5 absent)"})
    kw = {"batch_no": int(o.get("batch", 0))}
    if "cap" in o:
        kw["cap_bytes"] = int(o["cap"])
    if "shuffle-window" in o:
        kw["shuffle_window"] = int(o["shuffle-window"])
    res = pack.pack(batch, **kw)  # E_BUDGET (rc 6) propagates before anything is written
    out = Path(o["out"]) if "out" in o else Path(pos[0]) / "pack" / ("b%d" % kw["batch_no"])
    res.write(out)
    return {"code": "OK", "rc": 0, "detail": {"out": str(out), "files": sorted(res.files)}}


def cmd_cite_check(argv):
    pos, o = _opts(argv, ("pack", "judge-output"), 1)
    if "pack" not in o or "judge-output" not in o:
        _usage("--pack and --judge-output are required")
    try:
        from . import cite
    except ImportError:
        raise VqaError("E_USAGE", {"reason": "cite stage unavailable"})
    pk = _load_json(o["pack"])
    if "api_version" not in pk:  # pack.py does not stamp it; the vision dir is the authority (DEVIATION D2)
        pk["api_version"] = _load_json(Path(pos[0]) / "vision.json")["api_version"]
    verdict = cite.verify_dir(pos[0], pk, _load_json(o["judge-output"]))
    rc = verdict.get("rc", 0)
    if rc not in errors.RC_CLASSES or rc == 1:
        raise VqaError("E_USAGE", {"reason": "verifier returned rc %r" % (rc,)})
    return {"code": verdict.get("code", "OK" if rc == 0 else "E_CITE"), "rc": rc, "detail": verdict}


def cmd_rejudge(argv):
    """Re-verify stored judge output against stored evidence; no inference or capture (ADV-16)."""
    _, o = _opts(argv, ("from-run", "pack", "judge-output", "out"), 0)
    if any(k not in o for k in ("from-run", "pack", "judge-output")):
        _usage("--from-run, --pack and --judge-output are required")
    out = Path(o["out"]) if "out" in o else None
    if out is not None:
        tmp = out.with_name(out.name + ".tmp")
        if any(p.exists() or p.is_symlink() for p in (out, tmp)):
            raise VqaError("E_VC_OUTPUT_EXISTS", {"path": str(out)})
        run_dir = Path(o["from-run"]).resolve()
        if out.resolve().is_relative_to(run_dir):
            _usage("--out must be outside the stored run")
    result = cmd_cite_check([o["from-run"], "--pack", o["pack"], "--judge-output", o["judge-output"]])
    if out is not None and result["rc"] == 0:
        canon.write_atomic(out, canon.dumps(result))
    return result


def _selftest_source(scenario, W=64, H=48, n=10):
    """Built-in deterministic integer scenes (engine-free; the vadapt synth adapter is not importable from L4)."""
    yy, xx = np.mgrid[0:H, 0:W]
    base = np.stack([(xx * 3 + yy) & 255, (xx + yy * 5) & 255, (xx * yy) & 255], axis=2).astype(np.uint8)
    frames = []
    for i in range(n):
        f = base.copy()
        if scenario == "scroll":
            f = np.roll(base, 4 * i, axis=1)
        elif scenario == "flicker" and i % 2:
            f[16:32, 16:32] = 255 - f[16:32, 16:32]
        elif scenario == "move":
            f[16:32, 4 * i:4 * i + 16] = 200
        frames.append(fr.Frame(i, i * 33333, i, np.ascontiguousarray(f)))
    meta = {"width": W, "height": H}
    return fr.MemoryFrameSource(frames, meta)


def cmd_selftest(argv):
    _opts(argv, (), 0)
    digests = {}
    for name in SELFTEST_SCENARIOS:
        seen = set()
        for _ in range(3):
            run = pipeline.analyze(_selftest_source(name))
            seen.add(canon.sha256_obj(run.vision_json()))
        if len(seen) != 1:
            raise VqaError("E_DETERMINISM", {"scenario": name, "digests": len(seen)})
        digests[name] = seen.pop()
    return {"code": "OK", "rc": 0, "detail": {"api_version": API_VERSION, "digests": digests}}


VERBS = {"selftest": cmd_selftest, "analyze": cmd_analyze, "pack": cmd_pack, "cite-check": cmd_cite_check,
         "verify-citations": cmd_cite_check, "rejudge": cmd_rejudge}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if not argv or argv[0] not in VERBS:
            _usage("verb must be one of %s" % ", ".join(sorted(VERBS)))
        obj = VERBS[argv[0]](argv[1:])
    except VqaError as e:
        obj = e.to_obj()
        sys.stderr.write("%s\n" % e)
    except (KeyError, TypeError, ValueError) as e:
        obj = errors.result_obj("E_SCHEMA", None, {"reason": "%s: %s" % (type(e).__name__, str(e)[:100])})
    sys.stdout.write(errors.result_line(obj))
    return obj["rc"]


if __name__ == "__main__":
    sys.exit(main())

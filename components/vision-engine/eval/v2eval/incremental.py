"""Crash-safe, resumable variant evaluation and incremental leaderboard writer."""
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time

import numpy as np
from eval.gui_synth import prereg
from eval.gui_synth.gen import Src
from eval.gui_synth.labels import derive
from eval.gui_synth.score import aggregate, score
from visioncore import pipeline as _pipeline
from visioncore.desc import luma_of_padded
from visioncore.grid import Grid
from visioncore.pipeline import analyze as _default_analyze
from . import exploration as _exploration
from . import run as _run
from .exploration import OUT, leaderboard, quick_selection, write_leaderboard
from .plugins import REFERENCES, Variant, parse_variant
from .run import (
    SCRATCH as _DEFAULT_SCRATCH,
    add_honesty,
    identity,
    lean,
    metrics_from_predictions,
    omit_scorer_default_timings,
    write_json,
)

SCRATCH = _DEFAULT_SCRATCH
analyze = _default_analyze

ROW_SCHEMA = "vqa.v2eval-row/v1"
ROW_KEYS = (
    "schema",
    "variant",
    "scene_id",
    "status",
    "error",
    "row",
    "false_relabels",
    "static_jitter_tile_frames",
    "python_opcodes",
    "calls",
    "module_sha256",
    "harness_sha256",
)
HARNESS_FILES = ("run.py", "plugins.py", "exploration.py", "postpass.py")
COST_SCOPE = (
    "executed Python bytecodes in frame API calls and Python dependencies; "
    "NumPy/C internals excluded; baseline uninstrumented"
)


def harness_sha256():
    return hashlib.sha256(
        b"".join((Path(__file__).parent / name).read_bytes() for name in HARNESS_FILES)
    ).hexdigest()


def safe_variant_name(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "variant"


def variant_path(out, name):
    return Path(out) / f"variant_{safe_variant_name(name)}.json"


def _get_scratch(scratch=None):
    if scratch is not None:
        return Path(scratch)
    if SCRATCH != _DEFAULT_SCRATCH:
        return Path(SCRATCH)
    return Path(_run.SCRATCH)


def _get_analyze():
    if analyze is not _default_analyze:
        return analyze
    if _run.analyze is not _default_analyze:
        return _run.analyze
    if _exploration.analyze is not _default_analyze:
        return _exploration.analyze
    return _pipeline.analyze


def write_atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def write_atomic_json(path, data):
    write_atomic_text(
        path, json.dumps(data, sort_keys=True, indent=2, allow_nan=False) + "\n"
    )


def append_row(rows_path, record):
    rows_path = Path(rows_path)
    rows_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True, allow_nan=False) + "\n"
    with rows_path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def format_leaderboard_md(board):
    lines = [
        "# DEV variant leaderboard",
        "",
        "No claim; DEV only. Cost counts Python operations; native work is opaque.",
        "DIAGNOSTIC wall time: [variant_diagnostics.json](variant_diagnostics.json). VM wall time is not evidence.",
        "",
        "| Variant | Status | MOVED recall / precision | FLICKER recall / precision | mv exact | False relabel ppm | Silent miss ppm | Python ops | Honesty flags |",
        "|---|---|---|---|---|---|---|---|---|",
    ]

    def ratio(r):
        return r["fraction"] if r and r["defined"] else "AUSENTE"

    for r in board["rows"]:
        def pair(label):
            return (
                " / ".join(ratio(r[label][k]) for k in ("recall", "precision"))
                if r[label]
                else "AUSENTE"
            )

        def show(v):
            return "AUSENTE" if v is None else str(v)

        flags = json.dumps(r["honesty_flags"], sort_keys=True, separators=(",", ":"))
        lines.append(
            f'| {r["variant"]} | {r["status"]} | {pair("MOVED")} | {pair("FLICKER")} | '
            f'{ratio(r["mv_exact"])} | {show(r["false_relabels_static_jitter_ppm"])} | '
            f'{show(r["silent_miss_ppm"])} | {show(r["python_opcodes"])} | {flags} |'
        )
    return "\n".join(lines) + "\n"


def baseline_tiles(scene, scratch=None):
    """Compute baseline engine tiles once per scene and reuse SCRATCH cache when sha matches."""
    scratch_dir = _get_scratch(scratch)
    scratch_dir.mkdir(parents=True, exist_ok=True)
    ident = identity(scene)
    tiles_path = scratch_dir / (ident + ".tiles")
    meta_path = scratch_dir / (ident + ".baseline.json")
    if tiles_path.exists() and meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            with tiles_path.open("rb") as cache_handle:
                tiles = np.load(cache_handle, allow_pickle=False)
            digest = hashlib.sha256(tiles.tobytes()).hexdigest()
            if (
                isinstance(meta, dict)
                and meta.get("scene_id") == scene["scene_id"]
                and digest == meta.get("engine_tiles_sha256")
            ):
                return tiles, 0.0
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    start = time.perf_counter()
    engine = _get_analyze()(Src(scene), workers=1, cpus=(2, 3, 4))
    elapsed = time.perf_counter() - start
    tiles = np.stack(engine.tiles)
    grid = getattr(engine, "grid", None) or Grid(scene["w"], scene["h"])
    events = derive(scene, grid)
    row = lean(
        score(
            tiles["cls"],
            tiles["mv_dx"],
            tiles["mv_dy"],
            events,
            scene=scene,
            engine_ms=elapsed * 1000,
        )
    )
    row["engine_tiles_sha256"] = hashlib.sha256(tiles.tobytes()).hexdigest()
    row["frames_sha256"] = getattr(engine, "frames_sha256", None)
    with tiles_path.open("wb") as cache_handle:
        np.save(cache_handle, tiles, allow_pickle=False)
    write_json(meta_path, row)
    return tiles, elapsed


def evaluate_scene(variant_name, scene, budget_ms, harness_sha, prereg_sha, scratch=None):
    grid = Grid(scene["w"], scene["h"])
    tiles, base_elapsed = baseline_tiles(scene, scratch=scratch)
    events = derive(scene, grid)
    base = {k: tiles[k].astype(np.int64) for k in ("cls", "mv_dx", "mv_dy")}
    sj_frames = (
        grid.N * scene["n"]
        if scene["kind"] in ("static_clean", "aa_jitter")
        else 0
    )
    if variant_name == "baseline":
        row = metrics_from_predictions(base, events, scene)
        row["prereg_sha"] = prereg_sha
        record = {
            "schema": ROW_SCHEMA,
            "variant": "baseline",
            "scene_id": scene["scene_id"],
            "status": "OK",
            "error": None,
            "row": row,
            "false_relabels": 0,
            "static_jitter_tile_frames": sj_frames,
            "python_opcodes": None,
            "calls": None,
            "module_sha256": None,
            "harness_sha256": harness_sha,
        }
        return record, base_elapsed

    adapter = Variant(variant_name, grid, budget_ms)
    false_relabels = 0
    try:
        if adapter.status == "OK":
            predictions = {k: np.empty_like(v) for k, v in base.items()}
            prev = None
            for frame in Src(scene):
                cur = np.ascontiguousarray(luma_of_padded(frame.rgb), dtype=np.uint8)
                output = adapter.update(prev, cur, tiles[frame.i])
                if output is None:
                    break
                for key in output:
                    predictions[key][frame.i] = output[key]
                if scene["kind"] in ("static_clean", "aa_jitter"):
                    false_relabels += int(
                        ((output["cls"] != base["cls"][frame.i]) & (output["cls"] >= 2)).sum()
                    )
                prev = cur
        if adapter.status == "OK":
            row = metrics_from_predictions(predictions, events, scene)
            row["prereg_sha"] = prereg_sha
            opcodes = int(adapter.ops)
        else:
            row = None
            opcodes = None
        record = {
            "schema": ROW_SCHEMA,
            "variant": variant_name,
            "scene_id": scene["scene_id"],
            "status": adapter.status,
            "error": adapter.error,
            "row": row,
            "false_relabels": false_relabels,
            "static_jitter_tile_frames": sj_frames,
            "python_opcodes": opcodes,
            "calls": int(adapter.frames),
            "module_sha256": adapter.module_sha256,
            "harness_sha256": harness_sha,
        }
        return record, adapter.seconds
    finally:
        adapter.close()


def _valid_row_structure(obj):
    return (
        isinstance(obj, dict)
        and obj.get("schema") == ROW_SCHEMA
        and all(k in obj for k in ROW_KEYS)
        and obj.get("status") in ("OK", "FAILED")
    )


def read_resume_rows(rows_path, out_dir, scene_ids, harness_sha, prereg_sha):
    """Read rows.jsonl for --resume, ignoring a truncated last line and validating seals."""
    rows_path = Path(rows_path)
    out_dir = Path(out_dir)
    partial_path = out_dir / "LEADERBOARD.partial.json"
    if partial_path.exists():
        partial = json.loads(partial_path.read_text(encoding="utf-8"))
        if partial.get("harness_sha256") != harness_sha:
            raise ValueError("resume refused: partial leaderboard harness_sha256 differs")
        if partial.get("prereg_sha256") != prereg_sha:
            raise ValueError("resume refused: partial leaderboard prereg_sha256 differs")
        if partial.get("scene_ids") != scene_ids:
            raise ValueError("resume refused: partial leaderboard scene list differs")

    for vfile in sorted(out_dir.glob("variant_*.json")):
        if vfile.name in ("variant_results.json", "variant_diagnostics.json"):
            continue
        vdata = json.loads(vfile.read_text(encoding="utf-8"))
        if vdata.get("harness_sha256") not in (None, harness_sha):
            raise ValueError("resume refused: variant file harness_sha256 differs")
        if vdata.get("prereg_sha256") not in (None, prereg_sha):
            raise ValueError("resume refused: variant file prereg_sha256 differs")
        if vdata.get("scene_ids") not in (None, scene_ids):
            raise ValueError("resume refused: variant file scene list differs")

    if not rows_path.exists():
        return [], False

    raw = rows_path.read_text(encoding="utf-8")
    lines = raw.splitlines()
    parsed = []
    truncated = False
    for idx, line in enumerate(lines):
        is_last = idx == len(lines) - 1
        if not line.strip():
            if is_last:
                truncated = True
                continue
            raise ValueError("corrupted empty line in rows.jsonl")
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            if is_last:
                truncated = True
                continue
            raise ValueError("corrupted JSON line in rows.jsonl") from exc
        if not _valid_row_structure(obj):
            if is_last:
                truncated = True
                continue
            raise ValueError("invalid row schema in rows.jsonl")
        parsed.append(obj)

    scene_set = set(scene_ids)
    by_variant = {}
    variant_order = []
    for obj in parsed:
        if obj.get("harness_sha256") != harness_sha:
            raise ValueError("resume refused: row harness_sha256 differs")
        if obj.get("prereg_sha256") not in (None, prereg_sha):
            raise ValueError("resume refused: row prereg_sha256 differs")
        row_inner = obj.get("row")
        if isinstance(row_inner, dict) and row_inner.get("prereg_sha") not in (None, prereg_sha):
            raise ValueError("resume refused: row prereg_sha differs")
        sid = obj.get("scene_id")
        if sid not in scene_set:
            raise ValueError("resume refused: row scene_id not in scene list")
        vname = obj["variant"]
        if vname not in by_variant:
            by_variant[vname] = []
            variant_order.append(vname)
        by_variant[vname].append(obj)

    for idx, vname in enumerate(variant_order):
        vrows = by_variant[vname]
        vscenes = [r["scene_id"] for r in vrows]
        if vscenes != scene_ids[: len(vscenes)]:
            raise ValueError(f"resume refused: scene list order differs for {vname}")
        failed = any(r["status"] == "FAILED" for r in vrows)
        if idx < len(variant_order) - 1 and not failed and vscenes != scene_ids:
            raise ValueError(f"resume refused: incomplete scene list for finished variant {vname}")

    return parsed, truncated


def _build_variant_record(name, vrows):
    if any(r["status"] == "FAILED" for r in vrows):
        failed_row = next(r for r in vrows if r["status"] == "FAILED")
        idx = vrows.index(failed_row)
        used = vrows[: idx + 1]
        rec = {
            "status": "FAILED",
            "error": failed_row["error"],
            "metrics": None,
            "false_relabels": sum(int(r["false_relabels"]) for r in used),
            "static_jitter_tile_frames": sum(int(r["static_jitter_tile_frames"]) for r in used),
            "python_opcodes": None,
            "calls": None if name == "baseline" else sum(int(r["calls"] or 0) for r in used),
        }
        if name != "baseline":
            rec["module_sha256"] = failed_row.get("module_sha256")
        return rec

    metric_rows = []
    for r in vrows:
        if r["row"] is not None:
            rc = copy.deepcopy(r["row"])
            rc["prereg_sha"] = None
            metric_rows.append(rc)
    metrics = add_honesty(aggregate(metric_rows)) if metric_rows else None
    rec = {
        "status": "OK",
        "error": None,
        "metrics": metrics,
        "false_relabels": sum(int(r["false_relabels"]) for r in vrows),
        "static_jitter_tile_frames": sum(int(r["static_jitter_tile_frames"]) for r in vrows),
        "python_opcodes": None if name == "baseline" else sum(int(r["python_opcodes"] or 0) for r in vrows),
        "calls": None if name == "baseline" else sum(int(r["calls"] or 0) for r in vrows),
    }
    if name != "baseline":
        rec["module_sha256"] = vrows[-1].get("module_sha256") if vrows else None
    omit_scorer_default_timings(rec)
    return rec


def _write_variant_file(out_dir, name, rec, scene_ids, budget_ms, prereg_sha, harness_sha):
    payload = {
        "variant": name,
        "status": rec["status"],
        "error": rec["error"],
        "metrics": rec["metrics"],
        "false_relabels": rec["false_relabels"],
        "static_jitter_tile_frames": rec["static_jitter_tile_frames"],
        "python_opcodes": rec["python_opcodes"],
        "calls": rec["calls"],
        "module_sha256": rec.get("module_sha256"),
        "scene_ids": scene_ids,
        "frame_budget_ms": budget_ms,
        "prereg_sha256": prereg_sha,
        "harness_sha256": harness_sha,
    }
    write_atomic_json(variant_path(out_dir, name), payload)


def write_partial_leaderboard(
    out_dir,
    scene_ids,
    budget_ms,
    prereg_sha,
    harness_sha,
    all_variants,
    complete_records,
    scenes_done_by_variant,
):
    partial_data = {
        "schema": "vqa.v2eval-variants/v1",
        "split": "DEV",
        "claims": "no claim; DEV only",
        "scene_ids": scene_ids,
        "frame_budget_ms": budget_ms,
        "prereg_sha256": prereg_sha,
        "measurement_tag": "MEASURED",
        "harness_sha256": harness_sha,
        "cost_scope": COST_SCOPE,
        "variants": copy.deepcopy(complete_records),
    }
    omit_scorer_default_timings(partial_data)
    board = leaderboard(partial_data)
    complete_variants = [v for v in all_variants if v in complete_records]
    pending_variants = [v for v in all_variants if v not in complete_records]
    board["complete_variants"] = complete_variants
    board["pending_variants"] = pending_variants
    board["scenes_done"] = {
        v: int(scenes_done_by_variant.get(v, 0)) for v in pending_variants
    }
    out_dir = Path(out_dir)
    write_atomic_json(out_dir / "LEADERBOARD.partial.json", board)
    write_atomic_text(out_dir / "LEADERBOARD.partial.md", format_leaderboard_md(board))
    return board


def run_variants_incremental(
    scenes,
    specs,
    budget_ms=1000,
    *,
    out=OUT,
    resume=False,
    scratch=None,
):
    scenes = quick_selection(scenes, None)
    if not scenes:
        raise ValueError("nonempty DEV scene list required")
    specs = sorted(set(specs) | REFERENCES)
    for spec in specs:
        parse_variant(spec)
    if not math.isfinite(budget_ms) or budget_ms <= 0:
        raise ValueError("positive finite frame budget required")

    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows_path = out_dir / "rows.jsonl"
    scene_ids = [s["scene_id"] for s in scenes]
    all_variants = ["baseline", *specs]
    current_harness_sha = harness_sha256()
    current_prereg_sha = prereg.sha256()

    rows_by_variant = {v: [] for v in all_variants}
    diagnostics = {v: 0.0 for v in all_variants}

    if resume:
        existing_rows, truncated = read_resume_rows(
            rows_path, out_dir, scene_ids, current_harness_sha, current_prereg_sha
        )
        for r in existing_rows:
            if r["variant"] not in rows_by_variant:
                raise ValueError(f"resume refused: unknown variant {r['variant']} in rows.jsonl")
            rows_by_variant[r["variant"]].append(r)
        if truncated or (rows_path.exists() and not rows_path.read_text(encoding="utf-8").endswith("\n")):
            rewritten = "".join(
                json.dumps(r, sort_keys=True, allow_nan=False) + "\n" for r in existing_rows
            )
            write_atomic_text(rows_path, rewritten)
    else:
        if rows_path.exists():
            rows_path.unlink()
        for fname in ("LEADERBOARD.partial.json", "LEADERBOARD.partial.md", "LEADERBOARD.json", "LEADERBOARD.md"):
            fpath = out_dir / fname
            if fpath.exists():
                fpath.unlink()
        for v in all_variants:
            vpath = variant_path(out_dir, v)
            if vpath.exists():
                vpath.unlink()

    complete_records = {}
    scenes_done = {v: len(rows_by_variant[v]) for v in all_variants}

    for v in all_variants:
        vrows = rows_by_variant[v]
        if vrows and (any(r["status"] == "FAILED" for r in vrows) or len(vrows) == len(scenes)):
            rec = _build_variant_record(v, vrows)
            complete_records[v] = rec
            _write_variant_file(
                out_dir, v, rec, scene_ids, budget_ms, current_prereg_sha, current_harness_sha
            )
    if any(rows_by_variant.values()):
        write_partial_leaderboard(
            out_dir,
            scene_ids,
            budget_ms,
            current_prereg_sha,
            current_harness_sha,
            all_variants,
            complete_records,
            scenes_done,
        )

    for variant_name in all_variants:
        if variant_name in complete_records:
            continue
        vrows = rows_by_variant[variant_name]
        done_sids = {r["scene_id"] for r in vrows}
        for scene in scenes:
            sid = scene["scene_id"]
            if sid in done_sids:
                continue
            record, elapsed = evaluate_scene(
                variant_name,
                scene,
                budget_ms,
                current_harness_sha,
                current_prereg_sha,
                scratch=scratch,
            )
            diagnostics[variant_name] += elapsed
            vrows.append(record)
            done_sids.add(sid)
            scenes_done[variant_name] = len(vrows)
            append_row(rows_path, record)
            print(
                json.dumps(
                    {
                        "phase": "variants",
                        "variant": variant_name,
                        "scene": sid,
                        "status": record["status"],
                    }
                ),
                flush=True,
            )
            if record["status"] == "FAILED" or len(vrows) == len(scenes):
                rec = _build_variant_record(variant_name, vrows)
                complete_records[variant_name] = rec
                _write_variant_file(
                    out_dir,
                    variant_name,
                    rec,
                    scene_ids,
                    budget_ms,
                    current_prereg_sha,
                    current_harness_sha,
                )
                write_partial_leaderboard(
                    out_dir,
                    scene_ids,
                    budget_ms,
                    current_prereg_sha,
                    current_harness_sha,
                    all_variants,
                    complete_records,
                    scenes_done,
                )
                if record["status"] == "FAILED":
                    break
            else:
                write_partial_leaderboard(
                    out_dir,
                    scene_ids,
                    budget_ms,
                    current_prereg_sha,
                    current_harness_sha,
                    all_variants,
                    complete_records,
                    scenes_done,
                )

    write_partial_leaderboard(
        out_dir,
        scene_ids,
        budget_ms,
        current_prereg_sha,
        current_harness_sha,
        all_variants,
        complete_records,
        scenes_done,
    )

    result = {
        "schema": "vqa.v2eval-variants/v1",
        "split": "DEV",
        "claims": "no claim; DEV only",
        "scene_ids": scene_ids,
        "frame_budget_ms": budget_ms,
        "prereg_sha256": current_prereg_sha,
        "measurement_tag": "MEASURED",
        "harness_sha256": current_harness_sha,
        "cost_scope": COST_SCOPE,
        "variants": copy.deepcopy(complete_records),
    }
    omit_scorer_default_timings(result)
    write_json(out_dir / "variant_results.json", result)
    write_json(
        out_dir / "variant_diagnostics.json",
        {
            "tag": "DIAGNOSTIC",
            "seconds": {
                name: (
                    diagnostics[name]
                    if name == "baseline" or complete_records[name]["calls"]
                    else None
                )
                for name in all_variants
            },
            "scope": "successful frame API calls only; failed calls unavailable; baseline engine analyze only",
            "warning": "VM wall time is not performance evidence",
        },
    )
    write_leaderboard(result, out=out_dir)
    return result


run_incremental = run_variants_incremental
run_variants = run_variants_incremental

"""Owned host child: pinned engine, RAM PNG decoding, S0-S4 region evidence.

The local vadapt extension only builds frames; algorithms run from the committed
vision wheel. No engine source is copied or patched. Nothing is written here.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys


def run(req):
    wheel = Path(req["wheel"])
    if hashlib.sha256(wheel.read_bytes()).hexdigest() != req["engine_sha256"]:
        return {"rc": 4, "reason_codes": ["engine_pin_stale"]}
    adapter = Path(req["adapter"])
    if hashlib.sha256(adapter.read_bytes()).hexdigest() != req["adapter_sha256"]:
        return {"rc": 4, "reason_codes": ["adapter_stale"]}
    sys.path.insert(0, str(wheel))
    import numpy as np
    from visioncore import canon, decode, pipeline
    from visioncore.regions import region_tiles
    spec = importlib.util.spec_from_file_location("sculpt_ram_adapter", adapter)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root, rows = Path(req["root"]), []
    for view in req["views"]:
        pixels = [decode.png((root / (role + "-" + view + ".png")).read_bytes())
                  for role in ("reference", "candidate")]
        if any(rgb.shape != (req["resolution"], req["resolution"], 3) for rgb in pixels):
            return {"rc": 2, "reason_codes": ["render_dimensions_invalid"]}
        foreground = [int(np.count_nonzero(np.any(rgb != 0, axis=2))) for rgb in pixels]
        if not all(foreground):
            return {"rc": 3, "reason_codes": ["empty_view"]}
        a, b = (rgb.tobytes() for rgb in pixels)
        hashes = [hashlib.sha256(rgb).hexdigest() for rgb in (a, b)]
        source, manifest, original = module.residual_source(
            a, b, width=req["resolution"], height=req["resolution"],
            reference_sha256=hashes[0], candidate_sha256=hashes[1])
        result = pipeline.analyze(source)
        changed = np.any(pixels[0] != pixels[1], axis=2)
        n = int(np.count_nonzero(changed))
        # Exact decoded residual is beside S2's thresholded regions. It prevents
        # a sub-threshold edit or a motion classification from becoming invisible.
        absolute = np.abs(pixels[0].astype(np.int16) - pixels[1].astype(np.int16))
        delta = int(absolute.sum())
        region_scores = []
        covered = np.zeros(changed.shape, dtype=np.bool_)
        grid = result.grid
        for region in result.regions[1]:
            # Use the engine's exact spans, never its covering bbox: the latter
            # can include minority evidence belonging to a different island.
            selected = np.isin(grid.mortons, region_tiles(region))
            count = region_changed = region_delta = 0
            for index in np.nonzero(selected)[0]:
                x, y = int(grid.tx[index]) * grid.s, int(grid.ty[index]) * grid.s
                rect = (slice(y, min(y + grid.s, changed.shape[0])),
                        slice(x, min(x + grid.s, changed.shape[1])))
                covered[rect] = True
                count += int(changed[rect].size)
                region_changed += int(np.count_nonzero(changed[rect]))
                region_delta += int(absolute[rect].sum())
            change_ppm = (region_changed * 1000000 + count - 1) // count
            delta_ppm = (region_delta * 1000000 + count * 765 - 1) // (count * 765)
            region_scores.append({"id": region["id"], "spans": region["spans"],
                                  "pixel_count": count, "changed_pixels": region_changed,
                                  "changed_pixels_ppm": change_ppm, "mean_abs_delta_ppm": delta_ppm,
                                  "score_ppm": min(1000000 - change_ppm, 1000000 - delta_ppm)})
        uncovered = int(np.count_nonzero(changed & ~covered))
        rows.append({"view": view, "reference_frame_sha256": hashes[0],
                     "candidate_frame_sha256": hashes[1],
                     "frame_manifest_sha256": canon.sha256_obj(original),
                     "residual_manifest_sha256": canon.sha256_obj(manifest),
                     "comparison_encoding": "exact_changed_pixels_binary_rgb24/v1",
                     "params_sha256": result.params_sha256,
                     "reference_foreground_pixels": foreground[0],
                     "candidate_foreground_pixels": foreground[1],
                     "changed_pixels": n, "pixel_count": int(changed.size),
                     "changed_pixels_ppm": (n * 1000000 + changed.size - 1) // changed.size,
                     "mean_abs_delta_ppm": (delta * 1000000 + changed.size * 765 - 1) // (changed.size * 765),
                     "regions": result.regions[1],
                     "region_scores": region_scores,
                     "uncovered_changed_pixels": uncovered,
                     "tiles_sha256": result.frame_rows[1]["tiles_sha256"]})
    return {"rc": 0, "reason_codes": [], "views": rows,
            "engine_sha256": req["engine_sha256"], "adapter_sha256": req["adapter_sha256"]}


def main():
    try:
        request = json.loads(sys.stdin.buffer.read(65536))
        result = run(request)
    except ImportError:
        result = {"rc": 3, "reason_codes": ["vision_runtime_unavailable"]}
    except FileNotFoundError:
        result = {"rc": 3, "reason_codes": ["view_missing"]}
    except Exception as exc:
        result = {"rc": 2, "reason_codes": ["vision_worker_contract"], "error_type": type(exc).__name__}
    sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
    return result["rc"]


if __name__ == "__main__":
    raise SystemExit(main())

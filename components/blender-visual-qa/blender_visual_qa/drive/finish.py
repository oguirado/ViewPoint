"""Canonicalise raw captures and emit adapter input and an anchored receipt."""
import argparse
import platform
import shutil
from pathlib import Path
from .journal import DriveError, canonical, file_sha, frame_list, load, seed_for, sha, validate, write
from .png import decode, encode


def canonicalise(raw, target, expected_res):
    width, height, rgb = decode(raw.read_bytes())
    if [width, height] != expected_res:
        raise DriveError("E_DECODE", "resolution")
    target.write_bytes(encode(width, height, rgb))
    return sha(rgb), file_sha(target)


# @anchor:bv.finish.finish

def finish(run, *, keep_raw=False):
    run = Path(run)
    j = validate(load(run / "journal.json"))
    profile = load(run / "profile.json")
    meta = load(run / "render" / "drive.meta.json")
    base = load(run / "capture.inputs.json")
    frames = frame_list(j)
    expected_names = {"frame_%06d.png" % ri for ri in frames}
    if {p.name for p in (run / "render" / "raw").glob("*.png")} != expected_names:
        raise DriveError("E_DRIVE", "no_output")
    seed_rows = meta.get("seeds")
    if seed_rows != [{"render_index": ri, "seed": seed_for(j, ri)} for ri in frames]:
        raise DriveError("E_GATE", "seed_receipt", 5)
    items = []
    for ri in frames:
        name = "frame_%06d.png" % ri
        px, file_hash = canonicalise(run / "render" / "raw" / name, run / "render" / name, j["res"])
        items.append({"render_index": ri, "sha256": px, "file_sha256": file_hash, "seed": seed_for(j, ri)})
    count = next((op["count"] for op in j["ops"] if op["op"] == "still"), 0)
    still = []
    for ri in range(1, count+1):
        name = "still_%06d.png" % ri
        px, _ = canonicalise(run / "render" / "still" / "raw" / name,
                             run / "render" / "still" / name, j["res"])
        still.append({"ri": ri, "sha256": px})
    write(run / "render" / "scene.json", {"app": "blender", "scene": j["scene"],
          "build_seq": int(base["blend_sha256"][:7], 16), "fps_num": j["fps"][0], "fps_den": j["fps"][1],
          "adapter_version": "1+" + profile["id"] + "+" + meta["backend_fp"][:8]})
    if not keep_raw:
        shutil.rmtree(run / "render" / "raw")
        if count:
            shutil.rmtree(run / "render" / "still" / "raw")
    manifest = load(run / "scene.manifest.json")
    result = {"schema": "bvqa.capture-receipt/v1", **base,
              "blender": {**meta["blender"], "binary_sha256": base["blender_binary_sha256"]},
              "machine": platform.machine(), "backend_fp": meta["backend_fp"], "profile_id": profile["id"],
              "frames": items, "still": still, "frames_digest": sha(canonical([[r["render_index"], r["sha256"]] for r in items])),
              "warnings": sorted(set(manifest["warnings"] + meta["warnings"])), "rc": 0}
    result.pop("blender_binary_sha256")
    write(run / "capture.receipt.json", result)
    return result


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("run")
    p.add_argument("--keep-raw", action="store_true")
    try:
        a = p.parse_args(argv)
        finish(a.run, keep_raw=a.keep_raw)
        result = {"code": "OK", "rc": 0}
    except DriveError as e:
        result = e.result()
    except OSError as e:
        result = {"code": "E_IO", "rc": 2, "reason": str(e)}
    print(canonical(result).decode(), end="")
    return result["rc"]


if __name__ == "__main__":
    raise SystemExit(main())

"""Vision brief encoder vqa.vision-brief/v1 for P2 gamedev.

Encodes engine report rows (vqa.core-frame/v1 and vqa.core-region/v1) into a canonical vision brief.
"""

import argparse
import json
import math
from pathlib import Path

REQUIRED_FRAME_KEYS = [
    "schema",
    "frame",
    "grid",
    "n",
    "cov_ppm",
    "global_mv",
    "regions",
    "tiles_sha256",
]

REQUIRED_REGION_KEYS = [
    "schema",
    "id",
    "frame",
    "grid",
    "cls",
    "n_tiles",
    "bbox_t",
    "bbox_px",
    "mv",
    "r_max",
    "r_sum",
    "edge_ppm",
    "kind",
    "shade_only",
    "spans",
]


def canonical(brief):
    """Return canonical JSON representation with sort_keys=True, compact separators, ensure_ascii=True."""
    return json.dumps(brief, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def size_proxy(brief):
    """Token budget size proxy: canonical JSON bytes divided by 4, rounded up."""
    b = len(canonical(brief).encode("utf-8"))
    return (b + 3) // 4


def _parse_grid_wh(grid_str):
    try:
        parts = grid_str.split(":")
        dims = parts[1].split("x")
        return int(dims[0]), int(dims[1])
    except Exception:
        return 1280, 720


def _merge_crop_boxes(boxes):
    if not boxes:
        return []
    merged = [list(b) for b in boxes]
    changed = True
    while changed:
        changed = False
        new_merged = []
        visited = [False] * len(merged)
        for i in range(len(merged)):
            if visited[i]:
                continue
            cur = merged[i]
            for j in range(i + 1, len(merged)):
                if visited[j]:
                    continue
                other = merged[j]
                if (
                    cur[0] <= other[2]
                    and other[0] <= cur[2]
                    and cur[1] <= other[3]
                    and other[1] <= cur[3]
                ):
                    cur = [
                        min(cur[0], other[0]),
                        min(cur[1], other[1]),
                        max(cur[2], other[2]),
                        max(cur[3], other[3]),
                    ]
                    visited[j] = True
                    changed = True
            visited[i] = True
            new_merged.append(cur)
        merged = new_merged
    merged.sort(key=lambda b: (b[1], b[0], b[3], b[2]))
    return merged


def encode(
    frames,
    regions,
    max_regions=12,
    max_crops=4,
    pad_px=8,
    budget_tokens=600,
):
    """Encode engine report rows into a vision brief dictionary (vqa.vision-brief/v1)."""
    # 1. Validate inputs
    for f in frames:
        for k in REQUIRED_FRAME_KEYS:
            if k not in f:
                raise ValueError(f"Missing required key '{k}' in frame row")

    for r in regions:
        for k in REQUIRED_REGION_KEYS:
            if k not in r:
                raise ValueError(f"Missing required key '{k}' in region row")

    if not frames:
        min_f, max_f = 0, 0
        grid = "grid:1280x720:s16"
        cov_ppm = []
        global_mv = [0, 0]
    else:
        sorted_frames = sorted(frames, key=lambda f: int(f["frame"]))
        min_f = min(int(f["frame"]) for f in sorted_frames)
        max_f = max(int(f["frame"]) for f in sorted_frames)
        grid = str(sorted_frames[0]["grid"])
        cov_ppm = [int(f["cov_ppm"]) for f in sorted_frames]

        sum_dx = sum(
            int(f["global_mv"][0])
            for f in sorted_frames
            if f.get("global_mv") is not None
        )
        sum_dy = sum(
            int(f["global_mv"][1])
            for f in sorted_frames
            if f.get("global_mv") is not None
        )
        global_mv = [sum_dx, sum_dy]

    W, H = _parse_grid_wh(grid)

    # 2. Group regions across frames
    filtered_regions = [
        r for r in regions if min_f <= int(r["frame"]) <= max_f
    ]
    # Sort for determinism
    filtered_regions.sort(key=lambda r: (int(r["frame"]), str(r["id"])))

    groups = {}
    for r in filtered_regions:
        rid = str(r["id"])
        anchor = rid.split(":", 1)[1] if ":" in rid else rid
        groups.setdefault(anchor, []).append(r)

    merged_regions = []
    # Sort groups by anchor for deterministic processing order
    for anchor in sorted(groups.keys()):
        group = groups[anchor]
        group_sorted = sorted(group, key=lambda r: int(r["frame"]))
        first_f = min(int(r["frame"]) for r in group)
        last_f = max(int(r["frame"]) for r in group)
        rep = group_sorted[0]
        id_val = str(rep["id"])

        is_flicker = any(str(r["cls"]) == "FLICKER" for r in group)
        cls = "FLICKER" if is_flicker else str(rep["cls"])
        kind = str(rep["kind"])
        n_tiles = max(int(r["n_tiles"]) for r in group)

        mv = [int(rep["mv"][0]), int(rep["mv"][1])]

        x0 = min(int(r["bbox_px"][0]) for r in group)
        y0 = min(int(r["bbox_px"][1]) for r in group)
        x1 = max(int(r["bbox_px"][0]) + int(r["bbox_px"][2]) for r in group)
        y1 = max(int(r["bbox_px"][1]) + int(r["bbox_px"][3]) for r in group)
        bbox_px = [x0, y0, max(0, x1 - x0), max(0, y1 - y0)]

        present_frames = set(int(r["frame"]) for r in group)
        toggles = sum(
            1
            for f in range(min_f, max_f)
            if (f in present_frames) != ((f + 1) in present_frames)
        )

        merged_regions.append(
            {
                "id": id_val,
                "cls": cls,
                "kind": kind,
                "bbox_px": bbox_px,
                "mv": mv,
                "n_tiles": n_tiles,
                "first_frame": first_f,
                "last_frame": last_f,
                "toggles": toggles,
            }
        )

    # 3. Rank regions by n_tiles descending then id ascending
    merged_regions.sort(key=lambda r: (-r["n_tiles"], r["id"]))
    total_unique_regions = len(merged_regions)
    kept_regions = merged_regions[:max_regions]
    dropped_regions_count = total_unique_regions - len(kept_regions)

    # 4. Generate crop boxes from top regions
    gen_boxes = []
    for r in kept_regions:
        x, y, w, h = r["bbox_px"]
        bx0 = max(0, min(W - 1, x - pad_px))
        by0 = max(0, min(H - 1, y - pad_px))
        bx1 = max(0, min(W - 1, x + w - 1 + pad_px))
        by1 = max(0, min(H - 1, y + h - 1 + pad_px))
        if bx1 >= bx0 and by1 >= by0:
            gen_boxes.append([bx0, by0, bx1, by1])

    all_crop_boxes = _merge_crop_boxes(gen_boxes)
    total_crops_count = len(all_crop_boxes)
    kept_crops = all_crop_boxes[:max_crops]
    dropped_crops_count = total_crops_count - len(kept_crops)

    truncated = dropped_regions_count + dropped_crops_count

    brief = {
        "schema": "vqa.vision-brief/v1",
        "frame_range": [min_f, max_f],
        "grid": grid,
        "cov_ppm": cov_ppm,
        "global_mv": global_mv,
        "regions": kept_regions,
        "crops": kept_crops,
        "truncated": truncated,
    }

    # 5. Enforce budget
    while size_proxy(brief) > budget_tokens:
        if brief["regions"]:
            brief["regions"].pop()
            brief["truncated"] += 1
        elif brief["crops"]:
            brief["crops"].pop()
            brief["truncated"] += 1
        else:
            break

    return brief


def main():
    parser = argparse.ArgumentParser(
        description="CLI for encoding vision brief from a run directory."
    )
    parser.add_argument(
        "run_dir", type=str, help="Run directory containing frames.jsonl and regions.jsonl"
    )
    parser.add_argument(
        "--frame-range",
        nargs=2,
        type=int,
        required=True,
        help="Start and end frame range (inclusive)",
    )
    parser.add_argument("--max-regions", type=int, default=12)
    parser.add_argument("--max-crops", type=int, default=4)
    parser.add_argument("--pad-px", type=int, default=8)
    parser.add_argument("--budget-tokens", type=int, default=600)

    args = parser.parse_args()

    run_path = Path(args.run_dir)
    frames_path = run_path / "frames.jsonl"
    regions_path = run_path / "regions.jsonl"

    f0, f1 = args.frame_range

    frames = []
    if frames_path.is_file():
        with open(frames_path, "r", encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if line:
                    row = json.loads(line)
                    if f0 <= row.get("frame", -1) <= f1:
                        frames.append(row)

    regions = []
    if regions_path.is_file():
        with open(regions_path, "r", encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if line:
                    row = json.loads(line)
                    if f0 <= row.get("frame", -1) <= f1:
                        regions.append(row)

    brief = encode(
        frames=frames,
        regions=regions,
        max_regions=args.max_regions,
        max_crops=args.max_crops,
        pad_px=args.pad_px,
        budget_tokens=args.budget_tokens,
    )

    print(canonical(brief))


if __name__ == "__main__":
    main()

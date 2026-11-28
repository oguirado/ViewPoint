#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Cold-build a staged shot in Blender and compare two pinned render frames."""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

from asset_db import Parser, Refusal, canonical, writable
from asset_sources import sha_file

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib/viewpoint-studio"))
from viewpoint.episode_local import common, host
from viewpoint.episode_local.episode import command


def checked(path, expected):
    if sha_file(path) != expected:
        raise Refusal("input_digest_mismatch", str(path), "restore the pinned input")
    return path


def compare(first, second, limit):
    import numpy as np
    from PIL import Image

    with Image.open(first) as image:
        left = np.asarray(image.convert("RGB"), dtype=np.int16)
    with Image.open(second) as image:
        right = np.asarray(image.convert("RGB"), dtype=np.int16)
    if left.shape != right.shape:
        raise Refusal("replay_shape_drift", str(right.shape), "use frames from the same shot at the same resolution")
    gap = np.abs(left - right)
    result = {"status": "PASS", "byte_identical": sha_file(first) == sha_file(second),
              "max_abs_diff": int(gap.max()), "differing_channels": int((gap > 0).sum()), "tolerance": limit}
    if result["max_abs_diff"] > limit:
        raise Refusal("replay_not_reproducible", json.dumps(result | {"status": "REFUSED"}, sort_keys=True),
                      "restore the pinned render inputs and replay again")
    return result


def cold_build(plan, root, target):
    if target.exists():
        raise Refusal("output_exists", str(target), "choose a new output directory")
    if plan["schema"] != "sn-c-reuse.build/v1" or not isinstance(plan["inputs"], dict):
        raise Refusal("invalid_build_plan", "", "supply a staged shot plan")
    if plan.get("control", "AUSENTE") != "AUSENTE" or plan.get("alterations"):
        raise Refusal("control_not_allowed", "", "supply a production build plan")
    read_roots = {HERE, root}
    for item in plan["inputs"].values():
        relative = common.relative(item["path"])
        path = (root / relative).resolve(strict=True)
        if not path.is_relative_to(root) or path != root / relative:
            raise Refusal("invalid_staged_path", str(path), "use canonical paths under the cold root")
        item["path"] = str(checked(path, item["sha256"]))
    target.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".reuse-", dir=target.parent) as directory:
        work = Path(directory)
        settings = work / "build.json"
        settings.write_bytes(canonical(plan))
        argv = ["/usr/bin/blender", "-b", "--factory-startup", "--disable-autoexec", "-noaudio",
                "--python-exit-code", "2", "--python", HERE / "blender_cold_build.py", "--", settings]
        scope = host.scoped(work, sorted(read_roots), [command("cold-build", argv, 1800)])
        receipt_path = work / "build.receipt.json"
        rc = scope["commands"][-1]["rc"]
        receipt = json.loads(receipt_path.read_bytes()) if receipt_path.exists() else {}
        if rc == 1 and receipt.get("status") == "REJECT":
            raise Refusal(receipt["reason"], receipt["detail"], "correct the recorded shot inputs")
        if rc or receipt.get("status") != "PASS":
            raise RuntimeError(receipt.get("detail", f"cold Blender build exited {rc}"))
        if target.exists():
            raise Refusal("output_exists", str(target), "choose a new output directory")
        work.rename(target)
    return receipt


def main():
    parser = Parser(description=__doc__)
    sub = parser.add_subparsers(dest="verb", required=True)
    build = sub.add_parser("cold-build")
    build.add_argument("--plan", type=Path, required=True)
    build.add_argument("--sha256", required=True)
    build.add_argument("--cold-root", type=Path, required=True)
    build.add_argument("--out", type=Path, required=True)
    replay = sub.add_parser("compare")
    replay.add_argument("--first", type=Path, required=True)
    replay.add_argument("--first-sha256", required=True)
    replay.add_argument("--second", type=Path, required=True)
    replay.add_argument("--second-sha256", required=True)
    replay.add_argument("--tolerance", type=int, choices=range(256), default=1)
    args = parser.parse_args()
    if args.verb == "cold-build":
        raw = checked(args.plan, args.sha256).read_bytes()
        if common.digest(raw) != args.sha256:
            raise Refusal("input_digest_mismatch", str(args.plan), "restore the pinned plan")
        return cold_build(json.loads(raw), args.cold_root.resolve(strict=True), writable(args.out))
    return compare(checked(args.first, args.first_sha256), checked(args.second, args.second_sha256), args.tolerance)


if __name__ == "__main__":
    try:
        print(canonical(main()).decode(), end="")
    except (Refusal, common.Refusal) as exc:
        print(json.dumps({"reason": exc.reason, "detail": str(exc), "next_command": sys.argv,
                          "retry_when": getattr(exc, "retry_when", "correct the staged inputs")}))
        sys.exit(getattr(exc, "rc", 1))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print(json.dumps({"reason": "harness_error", "detail": str(exc), "next_command": sys.argv,
                          "retry_when": "correct the inputs or runtime"}))
        sys.exit(2)

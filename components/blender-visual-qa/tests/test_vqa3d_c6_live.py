"""Explicit serial CUDA cache acceptance; unittest discovery starts no GPU job."""
import argparse
import json
from pathlib import Path
import time

from vqa3d import profile
from vqa3d.adapters.blender import BlenderAdapter
from vqa3d.canon import sha256_canonical
from tests.test_vqa3d_profiles import selected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--kernel-cache", type=Path)
    parser.add_argument("--blender-env", action="append", default=[])
    parser.add_argument("--cpus", default="0-15")
    parser.add_argument("--oneshot", action="store_true")
    parser.add_argument("--expect", choices=("PASS", "CANNOT_SEE"), default="PASS")
    args = parser.parse_args()
    env = dict(item.split("=", 1) for item in args.blender_env)
    adapter = BlenderAdapter(args.source, profile_session=not args.oneshot, timeout=420,
        kernel_cache=args.kernel_cache, blender_env=env, nice=10, cpus=args.cpus)
    policy = selected(profile.load("film-animation"), "gpu-render")
    started = time.monotonic()
    records, summary, trace = [], {}, {}
    try:
        records, summary, trace = profile.run(policy, adapter)
    finally:
        if adapter._session_log is not None:
            adapter.flush_process_logs()
            for name, suffix in (("session.log", ".stdout.log"), ("session.stderr.log", ".stderr.log")):
                args.output.with_suffix(suffix).write_bytes((adapter.scratch / name).read_bytes())
        elif adapter.process_logs:
            args.output.with_suffix(".stdout.log").write_text("\n".join(p.get("output", "") for p in adapter.process_logs))
            args.output.with_suffix(".stderr.log").write_text("\n".join(p.get("stderr_tail", "") for p in adapter.process_logs))
        adapter.close()
        elapsed_ms = round((time.monotonic() - started) * 1000)
        result = {"receipts": records, "summary": summary, "probe": trace.get("probe"),
                  "kernel_cache": adapter.kernel_cache_receipt, "processes": adapter.process_logs,
                  "wall_ms": elapsed_ms}
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        args.output.with_suffix(".trace.json").write_text(json.dumps(trace, indent=2) + "\n")
    target = next(r for r in records if r.get("check_id") == "gpu-render:scene")
    receipt_hash = target["receipt_sha256"]
    assert receipt_hash == sha256_canonical({k: v for k, v in target.items() if k != "receipt_sha256"})
    print(json.dumps({"path": str(args.output), "wall_ms": elapsed_ms,
        "verdict": target["final_verdict"], "receipt_sha256": receipt_hash,
        "kernel_cache": target.get("gpu_probe", {}).get("CUDA", {}).get("kernel_cache")}))
    assert target["final_verdict"] == args.expect, target.get("error")


if __name__ == "__main__":
    main()

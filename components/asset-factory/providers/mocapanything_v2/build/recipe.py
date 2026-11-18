"""Read-only offline recipe verification. Never clones, installs or runs a model."""

import importlib.util
from pathlib import Path
import re
import subprocess
import time

from asset_factory import core, proc as af_proc
from providers.mocapanything_v2.bvh import Refusal, require, check_time
from providers.mocapanything_v2.contract import ROOT, digest, load, read_bytes


def verify_inventory(root, inventory, *, deadline_ns=None):
    if deadline_ns is None:
        deadline_ns = time.monotonic_ns() + 30 * 10**9
    check_time(deadline_ns)
    root = Path(root)
    require(root.is_dir(), 3, "local_root_missing")
    root = root.resolve(strict=True)
    require(type(inventory) is dict and re.fullmatch(r"[0-9a-f]{40}", inventory.get("revision", "")),
            2, "weight_revision_unpinned")
    files = inventory.get("files")
    require(type(files) is list and bool(files), 3, "weight_inventory_missing")
    checked, paths = [], set()
    for spec in files:
        check_time(deadline_ns)
        relative = spec["path"]
        require(type(relative) is str and relative and not Path(relative).is_absolute()
                and ".." not in Path(relative).parts and relative not in paths, 2, "inventory_path_escape")
        paths.add(relative)
        target = root / relative
        require(not target.is_symlink() and target.resolve().is_relative_to(root), 2, "inventory_path_escape")
        require(target.is_file(), 3, "local_weight_missing")
        require(type(spec["bytes"]) is int and 0 < spec["bytes"] <= 2**30,
                6, "weight_byte_budget")
        data = read_bytes(target, spec["bytes"])
        require(len(data) == spec["bytes"] and digest(data) == spec["sha256"], 4, "weight_stale")
        checked.append({"path": relative, "sha256": spec["sha256"], "bytes": len(data)})
    check_time(deadline_ns)
    return checked


def preflight(*, motion_root=None, birefnet_root=None, source_root=None):
    started = time.monotonic_ns()
    recipe = load(ROOT / "build" / "recipe.json")
    record = {"schema": "af.motion-preflight/v1", "rc": 3, "status": "INCOMPLETE",
              "verdict": "CANNOT_SEE", "recipe_sha256": core.sha256_json(recipe),
              "checked_files": {}, "reason_codes": [], "wall_ms": 0}
    try:
        require(recipe["segmentation"] == "local_birefnet_only" and recipe["network"] == "deny",
                2, "network_recipe_invalid")
        require(re.fullmatch(r"[0-9a-f]{40}", recipe["source"]["revision"]) is not None,
                2, "source_revision_unpinned")
        for name, root in (("motion", motion_root), ("birefnet", birefnet_root)):
            if root is None:
                record["reason_codes"].append(name + "_root_missing")
            else:
                record["checked_files"][name] = verify_inventory(root, recipe["models"][name])
        if source_root is None:
            record["reason_codes"].append("source_checkout_missing")
        else:
            source_root = Path(source_root).resolve(strict=True)
            # Bounded git calls, no shells, hooks, network or source mutation.
            head = af_proc.popen(["git", "-C", str(source_root), "rev-parse", "HEAD"], start_new_session=True)
            try:
                out, _ = af_proc.communicate(head, timeout=5)
                require(head.returncode == 0 and out.decode().strip() == recipe["source"]["revision"],
                        4, "source_stale")
            finally:
                head.stop()
            dirty = af_proc.popen(["git", "-C", str(source_root), "diff", "--quiet", "HEAD", "--"],
                                  start_new_session=True)
            try:
                dirty.wait(5)
                require(dirty.returncode == 0, 4, "source_dirty")
            finally:
                dirty.stop()
            record["reason_codes"].append("source_closure_unverified")
        for module in ("torch", "transformers", "safetensors", "timm"):
            if importlib.util.find_spec(module) is None:
                record["reason_codes"].append("runtime_missing_" + module)
        record["reason_codes"].extend(recipe["blocked_reasons"])
        # Even pinned existing bytes do not establish a complete build.
        rc = 3
    except af_proc.SignalRefusal as exc:
        rc, record["reason_codes"] = exc.rc, [exc.code]
    except Refusal as exc:
        rc, record["reason_codes"] = exc.rc, [exc.reason]
    except core.ContractError as exc:
        rc, record["reason_codes"] = 2, [exc.code]
    except subprocess.TimeoutExpired:
        rc, record["reason_codes"] = 6, ["source_probe_timeout"]
    except (OSError, ValueError, KeyError, TypeError):
        rc, record["reason_codes"] = 2, ["recipe_invalid"]
    record.update(rc=rc, status=core.STATUS[rc], verdict=core.VERDICT[rc],
                  wall_ms=(time.monotonic_ns() - started) // 1000000)
    return record

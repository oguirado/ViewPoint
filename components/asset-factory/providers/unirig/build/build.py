"""Offline recipe validation and atomic context preparation; never builds or runs.

The shipped recipe is deliberately incomplete. Null pins block preparation.
Explicit operator approval belongs to a separate digest-bound build receipt.
"""
import argparse
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
import tempfile
import time

from asset_factory import core
from ..common import Refusal, ROOT, digest, load, outcome, require, validate

EXTENSIONS = ("spconv", "torch_scatter", "torch_cluster", "flash_attn")
MAX_FILE_BYTES = 8 * 2**30  # [TARGET: streamed offline inputs, DESIGN_CHOICE]


def contained(root, name):
    require(type(name) is str and name and not PurePosixPath(name).is_absolute()
            and ".." not in PurePosixPath(name).parts and "\\" not in name, 2, "path_escape")
    path = (Path(root) / name).resolve()
    require(path.is_relative_to(Path(root).resolve()), 2, "path_escape")
    return path


def hash_file(root, name, expected):
    require(digest(expected), 2, "digest_invalid")
    path = contained(root, name)
    require(path.is_file(), 3, "offline_input_missing")
    require(path.stat().st_size <= MAX_FILE_BYTES, 6, "offline_input_budget")
    require(core.sha256_file(path) == expected, 4, "offline_input_stale")
    return path


def preflight(recipe, inputs_root, *, weights_root=None):
    """Authenticate all bytes; no subprocess, network, install or model import."""
    try:
        started = time.monotonic()
        require(type(recipe) is dict, 2, "recipe_invalid")
        validate(recipe, "recipe")
        require(recipe.get("schema") == "af.unirig-build-recipe/v1", 2, "recipe_invalid")
        shipped = load(ROOT / "build" / "recipe.json")
        require(recipe["source"]["repo"] == shipped["source"]["repo"] and
                recipe["source"]["revision"] == shipped["source"]["revision"] and
                recipe["weights"] == shipped["weights"] and
                recipe["launch_scripts_sha256"] == shipped["launch_scripts_sha256"], 4, "pin_drift")
        missing = [k for k in ("base_image", "requirements_lock_sha256", "wheel_files",
                              "runtime_probe_sha256", "licence_review_sha256") if recipe.get(k) is None]
        if recipe["source"].get("archive_sha256") is None:
            missing.append("source_archive")
        missing += [k for k in EXTENSIONS if recipe.get("native_extensions", {}).get(k) is None]
        require(not missing, 3, "pins_or_extension_audit_missing")
        require(recipe.get("network") == "none" and recipe.get("architecture") == "aarch64", 2, "build_mode_invalid")
        require(type(recipe["base_image"]) is str and re.fullmatch(
            r"[^\s@]+@sha256:[0-9a-f]{64}", recipe["base_image"]), 2, "base_unpinned")
        paths = {}
        for name, expected in (("source.tar", recipe["source"]["archive_sha256"]),
                               ("requirements.lock", recipe["requirements_lock_sha256"]),
                               ("runtime-probe.json", recipe["runtime_probe_sha256"]),
                               ("licence-review.json", recipe["licence_review_sha256"])):
            paths[name] = hash_file(inputs_root, name, expected)
        require(recipe["wheel_files"], 3, "wheel_lock_missing")
        require(len(recipe["wheel_files"]) <= 4096, 6, "wheel_budget")
        for item in recipe["wheel_files"]:
            require(time.monotonic()-started <= 300, 6, "preflight_deadline")
            name = item["path"]
            require(name.startswith("wheels/") and name.endswith(".whl") and name not in paths,
                    2, "wheel_path_invalid")
            paths[name] = hash_file(inputs_root, name, item["sha256"])
        require(weights_root is not None, 3, "offline_weights_missing")
        weights = {}
        for repo in recipe["weights"]:
            require(repo["files"], 3, "weight_inventory_missing")
            for item in repo["files"]:
                require(time.monotonic()-started <= 300, 6, "preflight_deadline")
                require(item["path"] not in weights, 2, "weight_path_duplicate")
                hash_file(weights_root, item["path"], item["sha256"])
                weights[item["path"]] = item["sha256"]
        # Hash pinning also applies to probe and rights evidence; a boolean is insufficient.
        probe, rights = load(paths["runtime-probe.json"]), load(paths["licence-review.json"])
        require(probe.get("rc") == 0 and type(probe.get("rc")) is int and
                probe.get("architecture") == "aarch64", 3, "runtime_probe_unqualified")
        for name in EXTENSIONS:
            require(digest(recipe["native_extensions"][name]) and
                    probe.get("extensions", {}).get(name) == recipe["native_extensions"][name],
                    3, "extension_unqualified")
        require(rights.get("rc") == 0 and type(rights.get("rc")) is int and
                rights.get("source_revision") == recipe["source"]["revision"] and
                rights.get("weights_sha256") == core.sha256_json(recipe["weights"]), 3, "rights_unqualified")
        lock = paths["requirements.lock"].read_text(encoding="utf-8")
        lines = [line.strip() for line in lock.splitlines() if line.strip() and not line.lstrip().startswith("#")]
        require(lines and all(re.fullmatch(r"[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?==[^\s@;]+"
                                          r"(?:\s+--hash=sha256:[0-9a-f]{64})+", line) for line in lines),
                2, "requirements_not_hash_locked")
        # Reject archive escapes, links, special files and unexpected launch bytes.
        with tarfile.open(paths["source.tar"], "r:") as archive:
            seen, roots, found, total = set(), set(), {}, 0
            for member in archive:
                require(time.monotonic()-started <= 300, 6, "preflight_deadline")
                name = PurePosixPath(member.name)
                require(not name.is_absolute() and ".." not in name.parts and
                        member.name not in seen and (member.isfile() or member.isdir()), 2, "archive_unsafe")
                seen.add(member.name)
                require(len(name.parts) > 0, 2, "archive_unsafe")
                roots.add(name.parts[0])
                require(type(member.size) is int and 0 <= member.size <= MAX_FILE_BYTES, 6, "archive_budget")
                total += member.size
                require(total <= MAX_FILE_BYTES and len(seen) <= 100000, 6, "archive_budget")
                short = str(PurePosixPath(*name.parts[1:]))
                if short in recipe["launch_scripts_sha256"]:
                    require(member.size <= 2**20, 6, "launch_budget")
                    import hashlib
                    found[short] = hashlib.sha256(archive.extractfile(member).read()).hexdigest()
            require(len(roots) == 1 and found == recipe["launch_scripts_sha256"], 4, "launch_script_drift")
        return outcome("af.unirig-build-preflight/v1", 0, [], recipe_sha256=core.sha256_json(recipe),
                       input_files={name: core.sha256_file(path) for name, path in sorted(paths.items())},
                       weight_files=weights)
    except Refusal as exc:
        return outcome("af.unirig-build-preflight/v1", exc.rc, [exc.code])
    except (OSError, ValueError, KeyError, TypeError, core.ContractError, tarfile.TarError):
        return outcome("af.unirig-build-preflight/v1", 2, ["recipe_or_inputs_invalid"])


def prepare(recipe, inputs_root, target, *, weights_root=None):
    report = preflight(recipe, inputs_root, weights_root=weights_root)
    if report["rc"]:
        return report
    target = Path(target)
    if target.exists() or target.is_symlink() or not target.parent.is_dir():
        return outcome("af.unirig-build-preflight/v1", 2, ["new_context_required"])
    scratch = Path(tempfile.mkdtemp(prefix=".unirig-build-", dir=target.parent))
    try:
        for name, expected in report["input_files"].items():
            source = hash_file(inputs_root, name, expected)
            dest = scratch / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, dest)
            require(core.sha256_file(dest) == expected, 4, "staging_drift")
        shutil.copyfile(ROOT / "build" / "Dockerfile", scratch / "Dockerfile")
        core.atomic_write_json(scratch / "recipe.json", recipe)
        core.atomic_write_json(scratch / "preflight.json", report)
        # An exclusive destination is mandatory; an existing caller directory is never replaced.
        from ..publication import publish_directory
        publish_directory(scratch, target)
        return report
    except Refusal as exc:
        return outcome("af.unirig-build-preflight/v1", exc.rc, [exc.code])
    except OSError:
        return outcome("af.unirig-build-preflight/v1", 2, ["context_io"])
    finally:
        if scratch.exists():
            shutil.rmtree(scratch)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--recipe", default=str(ROOT / "build" / "recipe.json"))
    parser.add_argument("--inputs-root", required=True)
    parser.add_argument("--weights-root")
    parser.add_argument("--context")
    args = parser.parse_args(argv)
    try:
        recipe = load(args.recipe)
        result = (prepare(recipe, args.inputs_root, args.context, weights_root=args.weights_root) if args.context
                  else preflight(recipe, args.inputs_root, weights_root=args.weights_root))
    except (core.ContractError, OSError):
        result = outcome("af.unirig-build-preflight/v1", 2, ["recipe_invalid"])
    return core.emit(result)


if __name__ == "__main__":
    raise SystemExit(main())

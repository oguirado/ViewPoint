"""Motion stage entry points: read-only preflight, rig intake and BVH validation.

Generation remains fail closed while the shipped provider is not runnable.
The fixture-only publication path qualifies the slot contract without claiming
model inference, skinning quality, video decoding or production acceptance.
"""

import argparse
import ctypes
import errno
import os
from pathlib import Path
import resource
import shutil
import sys
import tempfile
import time

from asset_factory import core, contracts
from . import offline
from .bvh import MAX_BYTES, Refusal, check_time, require
from .build.recipe import preflight as recipe_preflight
from .contract import ROOT, digest, inspect_rig, load, read_bytes
from .gate import evaluate

MAX_RIG_BYTES = 64 * 2**20  # [TARGET, DESIGN_CHOICE]
_BIREFNET_REPO = "ZhengPeng7/BiRefNet"
_BIREFNET_REVISION = "e2bf8e4460fc8fa32bba5ea4d94b3233d367b0e4"
# Canonical SHA-256 of the recipe's file descriptors sorted by path.
_BIREFNET_INVENTORY_SHA256 = "8b2ccedbddb5c5848e9b125682c2f82c99f49adce2568e25317dfead826301c8"


def _check_segmentation(recipe):
    require(type(recipe) is dict and recipe.get("segmentation") == "local_birefnet_only",
            2, "network_recipe_invalid")
    require(recipe.get("network") == "deny", 2, "network_recipe_invalid")
    models = recipe.get("models")
    model = models.get("birefnet") if isinstance(models, dict) else None
    require(isinstance(model, dict) and model.get("repo") == _BIREFNET_REPO
            and model.get("revision") == _BIREFNET_REVISION, 4, "birefnet_recipe_stale")
    files = model.get("files")
    require(isinstance(files, list) and all(isinstance(pin, dict)
            and isinstance(pin.get("path"), str) for pin in files), 4, "birefnet_recipe_stale")
    inventory = sorted(files, key=lambda pin: pin["path"])
    require(core.sha256_json(inventory) == _BIREFNET_INVENTORY_SHA256, 4, "birefnet_recipe_stale")


def load_segmentation(birefnet_root):
    """Return the pinned BiRefNet RGBA foreground callable in an owned worker."""
    recipe = load(ROOT / "build" / "recipe.json")
    _check_segmentation(recipe)
    require(birefnet_root is not None, 3, "birefnet_root_missing")
    root = Path(birefnet_root).resolve()
    require(root.is_dir(), 3, "local_root_missing")
    inventory = recipe["models"]["birefnet"]["files"]
    expected = {pin["path"] for pin in inventory}
    metadata = {".cache", ".gitattributes", ".gitignore"}
    actual = {path.name for path in root.iterdir()}
    require(actual <= expected | metadata, 4, "unlisted_model_file")
    require(expected <= actual, 3, "local_weight_missing")
    return offline.load_birefnet(root)


def preflight(**local_roots):
    started = time.monotonic_ns()
    recipe = load(ROOT / "build" / "recipe.json")
    try:
        _check_segmentation(recipe)
    except Refusal as exc:
        return {"schema": "af.motion-preflight/v1", "rc": exc.rc,
                "status": core.STATUS[exc.rc], "verdict": core.VERDICT[exc.rc],
                "recipe_sha256": core.sha256_json(recipe), "checked_files": {},
                "reason_codes": [exc.reason],
                "wall_ms": (time.monotonic_ns() - started) // 1000000}
    return recipe_preflight(**local_roots)


def _request(request):
    contracts.validate_slot(request, "motion", "request")
    manifest = load(ROOT / "provider.json")
    contracts.validate_provider(manifest)
    require(request["provider_id"] == manifest["id"]
            and request["provider_revision"] == manifest["revision"], 4, "provider_stale")
    require({i["role"] for i in request["inputs"]} == {"rig_glb", "video"}, 2, "text_unsupported")
    require(type(request["params"].get("seed")) is int and 0 <= request["params"]["seed"] < 2**32,
            2, "seed_invalid")
    require(0 < request["context"]["timeout_s"] <= 1800, 2, "timeout_invalid")
    return manifest


def _result(request, rc, started):
    return {"schema": "af.motion/v1", "kind": "result", "slot": "motion",
            "request_id": request["request_id"], "provider_id": request["provider_id"],
            "provider_revision": request["provider_revision"], "rc": rc, "status": core.STATUS[rc],
            "outputs": [], "receipts": [],
            "metrics": {"wall_ms": (time.monotonic_ns() - started) // 1000000,
                        "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024},
            "absence_reasons": {}}


def inspect_input(path):
    started = time.monotonic_ns()
    report = {"schema": "af.motion-intake/v1", "rc": 3, "status": "INCOMPLETE",
              "verdict": "CANNOT_SEE", "source_sha256": None, "joints": None,
              "reason_codes": [], "wall_ms": 0}
    try:
        data = read_bytes(path, MAX_RIG_BYTES)
        report["source_sha256"] = digest(data)
        report["joints"] = inspect_rig(data)
        # Hierarchy inspection cannot certify skin weights or motion.
        rc, report["reason_codes"] = 3, ["skin_quality_and_motion_unobserved"]
    except Refusal as exc:
        rc, report["reason_codes"] = exc.rc, [exc.reason]
    report.update(rc=rc, status=core.STATUS[rc], verdict=core.VERDICT[rc],
                  wall_ms=(time.monotonic_ns() - started) // 1000000)
    return report


def generate(request, **local_roots):
    """Never invokes the archived upstream entrypoint or downloads a helper."""
    started = time.monotonic_ns()
    _request(request)
    proof = preflight(**local_roots)
    result = _result(request, proof["rc"], started)
    # Remains status INCOMPLETE, even if inventory hashes matched every byte.
    return result, proof


def accept_bvh(request, bvh_path, reference_path, observation_path, output_dir, *, synthetic_fixture=False):
    started = time.monotonic_ns()
    _request(request)
    deadline_ns = started + request["context"]["timeout_s"] * 10**9
    target = Path(output_dir)
    gate = None
    try:
        data = read_bytes(bvh_path, MAX_BYTES)
        inputs = {item["role"]: item for item in request["inputs"]}
        rig = read_bytes(inputs["rig_glb"]["path"], MAX_RIG_BYTES)
        require(digest(rig) == inputs["rig_glb"]["sha256"], 4, "rig_stale")
        # Authenticate video bytes without decoding; real capture remains unseen.
        video_path = Path(inputs["video"]["path"])
        require(not video_path.is_symlink() and video_path.stat().st_size <= 2**30, 6, "video_byte_budget")
        require(core.sha256_file(video_path) == inputs["video"]["sha256"], 4, "video_stale")
        reference, observation = load(reference_path, "reference"), load(observation_path, "observation")
        require(request["params"].get("reference_sha256") == core.sha256_json(reference),
                4, "reference_stale")
        require(type(synthetic_fixture) is bool, 2, "scope_invalid")
        require(not synthetic_fixture or request["params"].get("synthetic_fixture") is True,
                2, "synthetic_scope_not_declared")
        gate = evaluate(data, rig, reference, observation, request_sha256=core.sha256_json(request),
                        video_sha256=inputs["video"]["sha256"], deadline_ns=deadline_ns,
                        scope="synthetic_fixture" if synthetic_fixture else "observed_motion")
        if gate["rc"] != 0:
            return _result(request, gate["rc"], started), gate
        check_time(deadline_ns)
        require(not target.exists() and not target.is_symlink(), 5, "output_exists")
        require(target.parent.is_dir() and target.parent.resolve() == target.parent.absolute(),
                2, "output_parent_invalid")
        result = _result(request, 0, started)
        result["outputs"] = [{"role": "action_bvh", "path": str(target / "action.bvh"),
                              "media_type": "application/x-bvh", "sha256": digest(data)}]
        result["receipts"] = [{"schema": "af.motion-contact/v1", "path": str(target / "contact.json"),
                               "sha256": core.sha256_json(gate)}]
        contracts.validate_slot(result, "motion", "result")
        _publish(target, data, request, reference, observation, gate, result, deadline_ns)
        return result, gate
    except Refusal as exc:
        return _result(request, exc.rc, started), {"rc": exc.rc, "verdict": core.VERDICT[exc.rc],
                                                  "reason_codes": [exc.reason]}
    except core.ContractError as exc:
        return _result(request, 2, started), exc.as_dict()
    except FileNotFoundError:
        return _result(request, 3, started), {"rc": 3, "verdict": "CANNOT_SEE", "reason_codes": ["input_missing"]}
    except OSError:
        return _result(request, 2, started), {"rc": 2, "verdict": "ERROR", "reason_codes": ["publication_io"]}


def _publish(target, data, request, reference, observation, gate, result, deadline_ns):
    lock = target.with_name("." + target.name + ".motion-lock")
    stage = None
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise Refusal(5, "publication_busy") from exc
    try:
        os.close(fd)
        require(not target.exists() and not target.is_symlink(), 5, "output_exists")
        stage = Path(tempfile.mkdtemp(prefix=".motion-stage-", dir=target.parent))
        with (stage / "action.bvh").open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        for name, obj in (("request.json", request), ("reference.json", reference),
                          ("observation.json", observation), ("contact.json", gate), ("result.json", result)):
            check_time(deadline_ns)
            core.atomic_write_json(stage / name, obj)
            require(core.sha256_file(stage / name) == core.sha256_json(obj), 4, "staged_bytes_stale")
        require(core.sha256_file(stage / "action.bvh") == digest(data), 4, "staged_bytes_stale")
        directory_fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        check_time(deadline_ns)
        require(not target.exists() and not target.is_symlink(), 5, "output_exists")
        _commit_generation(stage, target)
        stage = None
    finally:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)
        try:
            lock.unlink()
        except OSError:
            # Cleanup cannot turn a visible commit into a preserving refusal.
            pass


def _commit_generation(stage, target):
    # Same Linux publication primitive as the TRELLIS.2 adapter. There is no
    # ordinary rename fallback: it could replace a racing empty caller directory.
    try:
        rename = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as exc:
        raise Refusal(3, "publication_noreplace_unavailable") from exc
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    parent_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        if rename(-100, os.fsencode(stage), -100, os.fsencode(target), 1):
            code = ctypes.get_errno()
            if code in (errno.EEXIST, errno.ENOTEMPTY):
                raise Refusal(5, "output_exists")
            if code in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP):
                raise Refusal(3, "publication_noreplace_unavailable")
            raise OSError(code, os.strerror(code))
        try:
            os.fsync(parent_fd)
        except OSError:
            pass  # Publication has committed; do not return a preserving refusal.
    finally:
        os.close(parent_fd)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise core.ContractError("arguments_invalid", "$argv")


def main(argv=None):
    parser = Parser(add_help=False, allow_abbrev=False)
    parser.add_argument("command", choices=("preflight", "inspect-rig", "accept-bvh", "generate"))
    for key in ("motion-root", "birefnet-root", "source-root", "request", "bvh", "reference", "observation", "output", "in"):
        parser.add_argument("--" + key, dest=key.replace("-", "_"))
    parser.add_argument("--synthetic-fixture", action="store_true")
    try:
        args = parser.parse_args(argv)
        roots = {k: getattr(args, k) for k in ("motion_root", "birefnet_root", "source_root")}
        if args.command == "preflight":
            return core.emit(preflight(**roots))
        if args.command == "inspect-rig":
            require(args.__dict__["in"] is not None, 2, "input_missing")
            return core.emit(inspect_input(args.__dict__["in"]))
        require(args.request is not None, 2, "request_missing")
        request = load(args.request)
        if args.command == "generate":
            result, proof = generate(request, **roots)
        else:
            require(all(getattr(args, k) is not None for k in ("bvh", "reference", "observation", "output")),
                    2, "arguments_missing")
            result, proof = accept_bvh(request, args.bvh, args.reference, args.observation, args.output,
                                       synthetic_fixture=args.synthetic_fixture)
        # Proof always reaches stdout on refusal; no caller output is published.
        print(core.canonical_json({"result": result, "evidence": proof}), end="")
        return result["rc"]
    except Refusal as exc:
        return core.emit({"rc": exc.rc, "status": core.STATUS[exc.rc],
                          "verdict": core.VERDICT[exc.rc], "reason_codes": [exc.reason]})
    except core.ContractError as exc:
        return core.emit(exc.as_dict())
    except OSError:
        return core.emit({"rc": 2, "status": "ERROR", "reason_codes": ["input_io"]})


if __name__ == "__main__":
    raise SystemExit(main())

"""Seeded offline CoACD boundary. Publication requires a measured G11 PASS.

The shipped provider is blocked: runtime, licence text and measured-peak proof
are absent. Inspection and candidate-gating are available without CoACD. They
do not confer production readiness. Output is a NEW directory, committed by a
no-replace directory rename; every refusal preserves all caller output bytes.
"""
from asset_factory import proc as af_proc
import argparse
import ctypes
import errno
import hashlib
import importlib.util
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile

from asset_factory import core
from .contracts import (HERE, MAX_BYTES, MAX_TIMEOUT_S, REVISION, Refusal, finish,
                        receipt, require, result, schema, validate_policy,
                        validate_request)

ROOT = HERE.parents[1]


def toolchain_identity():
    files = ("adapter.py", "worker.py", "contracts.py", "geometry.py", "qualify.py", "runtime-lock.json",
             "collision.schema.json", "provider.schema.json", "gate-policy.schema.json",
             "gate-receipt.schema.json", "manifest.schema.json")
    return core.sha256_json({name: core.sha256_file(HERE / name) for name in files})


def runtime_paths():
    """Forward only discovered package roots, for an offline split installation.

    The parent must already see these packages. Never inherit arbitrary
    PYTHONPATH entries or locate/install a missing package on the child's behalf.
    """
    roots = [str(ROOT)]
    for name in ("numpy", "trimesh", "scipy", "coacd"):
        spec = importlib.util.find_spec(name)
        if spec is not None and spec.origin:
            root = str(Path(spec.origin).resolve().parent.parent)
            if root not in roots:
                roots.append(root)
    return os.pathsep.join(roots)


def read_source(path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        require(stat.S_ISREG(os.fstat(fd).st_mode), 2, "non_regular_input")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(MAX_BYTES + 1)
    finally:
        os.close(fd)
    require(len(data) <= MAX_BYTES, 6, "input_budget_exceeded")
    require(bool(data), 3, "empty_input")
    return data


def _spawn(folder, timeout_s):
    """Native code runs outside the caller, with one deadline and owned group."""
    with tempfile.TemporaryFile() as logs:
        child = af_proc.popen([sys.executable, "-B", "-m", "providers.coacd.worker", str(folder)],
            cwd=ROOT, env={"PATH": os.defpath, "HOME": str(Path.home()), "LC_ALL": "C",
                           "PYTHONPATH": runtime_paths(), "PYTHONDONTWRITEBYTECODE": "1",
                           "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"},
            stdout=logs, stderr=logs, start_new_session=True)
        try:
            af_proc.wait_exit(child, timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise Refusal(6, "deadline_exceeded") from exc
        finally:
            try:
                af_proc.signal_group(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
        require(child.returncode in core.RC, 2, "backend_contract")
    rec = core.load_json(folder / "worker-receipt.json", max_bytes=1024 * 1024)
    core.validate(rec, schema("gate-receipt.schema.json"))
    require(type(rec["rc"]) is int and rec["rc"] == child.returncode
            and rec["status"] == core.STATUS[rec["rc"]]
            and rec["verdict"] == core.VERDICT[rec["rc"]], 2, "backend_contract")
    return rec


def _job(operation, source=None, policy=None, candidate=None, params=None):
    return {"operation": operation, "source_sha256": hashlib.sha256(source).hexdigest() if source else None,
            "candidate_sha256": hashlib.sha256(candidate).hexdigest() if candidate else None,
            "policy_sha256": core.sha256_json(policy) if policy else None,
            "toolchain_sha256": toolchain_identity(), "gate_policy": policy, "params": params}


def _measure(job, source=None, candidate=None, timeout_s=MAX_TIMEOUT_S):
    rec = receipt(job["source_sha256"], job["policy_sha256"])
    try:
        with tempfile.TemporaryDirectory(prefix="af-coacd-") as temp:
            folder = Path(temp)
            core.atomic_write_json(folder / "job.json", job)
            if source is not None:
                (folder / "source.glb").write_bytes(source)
            if candidate is not None:
                (folder / "collision.glb").write_bytes(candidate)
            return _spawn(folder, timeout_s)
    except af_proc.SignalRefusal as exc:
        return finish(rec, exc.rc, [exc.code])
    except Refusal as exc:
        return finish(rec, exc.rc, [exc.code])
    except (OSError, core.ContractError):
        return finish(rec, 2, ["io_or_schema_error"])


def probe_runtime():
    return _measure(_job("probe"), timeout_s=10)


def inspect_source(path, *, expected_sha256):
    rec = receipt(expected_sha256)
    try:
        source = read_source(path)
        require(hashlib.sha256(source).hexdigest() == expected_sha256, 4, "source_stale")
        return _measure(_job("inspect", source), source)
    except af_proc.SignalRefusal as exc:
        return finish(rec, exc.rc, [exc.code])
    except Refusal as exc:
        return finish(rec, exc.rc, [exc.code])
    except (OSError, core.ContractError):
        return finish(rec, 2, ["io_or_schema_error"])


def evaluate_candidate(source_path, candidate_path, *, expected_source_sha256,
                       expected_candidate_sha256, policy):
    rec = receipt(expected_source_sha256)
    try:
        validate_policy(policy)
        source, candidate = read_source(source_path), read_source(candidate_path)
        require(hashlib.sha256(source).hexdigest() == expected_source_sha256, 4, "source_stale")
        require(hashlib.sha256(candidate).hexdigest() == expected_candidate_sha256, 4, "candidate_stale")
        return _measure(_job("gate", source, policy, candidate), source, candidate, policy["timeout_s"])
    except af_proc.SignalRefusal as exc:
        return finish(rec, exc.rc, [exc.code])
    except Refusal as exc:
        return finish(rec, exc.rc, [exc.code])
    except (OSError, core.ContractError):
        return finish(rec, 2, ["io_or_schema_error"])


def _rename_new(source, target):
    """Linux no-replace publication; no unsafe fallback on unsupported hosts."""
    libc = ctypes.CDLL(None, use_errno=True)
    require(hasattr(libc, "renameat2"), 3, "atomic_publication_unavailable")
    rename = libc.renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1) != 0:
        code = ctypes.get_errno()
        if code in (errno.EEXIST, errno.ENOTEMPTY):
            raise Refusal(2, "output_exists")
        raise Refusal(2, "publication_failed")


def _authorise(request, provider, factory, territory_policy):
    from asset_factory.contracts import validate_provider
    from licence.check import check_provider
    core.validate(provider, schema("provider.schema.json"))
    validate_provider(provider)
    require(provider["id"] == "coacd" and provider["slot"] == "collision"
            and provider["kind"] == "tool" and provider["revision"] == REVISION, 2, "provider_invalid")
    # The F0 licence checker still knows eight slots until W3-CONTRACTS lands.
    # Its refusal must not be bypassed even if the draft schema accepts collision.
    require("collision" in core.SLOTS, 3, "slot_registration_pending")
    decision = check_provider(factory, provider, operation="collision", policy=territory_policy)
    require(decision["rc"] == 0 and decision["allowed"], decision["rc"] or 5, "licence_refused")
    context = request["context"]
    require(context["commercial_use"] == factory["commercial_use"]
            and context["jurisdiction"] == factory["jurisdiction"]
            and context["policy_sha256"] == core.sha256_json(territory_policy), 4, "decision_stale")
    require(provider["runnable"], 5, "provider_blocked")
    peak = provider["runtime"]["ram_peak_gb_measured"]
    ref = provider["runtime"]["ram_peak_measurement_ref"]
    require(type(peak) is int and peak > 0 and isinstance(ref, dict), 3, "peak_unmeasured")
    require(core.sha256_file(Path(ref["path"])) == ref["sha256"], 4, "measurement_stale")
    return decision


def verify_bundle(folder, *, expected_source_sha256, policy):
    """Authenticate every separately consumed hull and remeasure committed GLB.

    A stored PASS is not reused as an oracle. Relative names are fixed by this
    format and symlinks are refused, including the manifest and source snapshot.
    """
    rec = receipt(expected_source_sha256)
    try:
        from . import geometry
        np, trimesh = geometry.dependencies()
        validate_policy(policy)
        folder = Path(folder)
        def owned(name):
            path = folder / name
            require(not path.is_symlink() and path.is_file(), 2, "bundle_path_invalid")
            return path
        manifest = core.load_json(owned("manifest.json"), max_bytes=1024 * 1024)
        core.validate(manifest, schema("manifest.schema.json"))
        require(manifest["source_sha256"] == expected_source_sha256, 4, "source_stale")
        require(manifest["policy_sha256"] == core.sha256_json(policy)
                and manifest["toolchain_sha256"] == toolchain_identity(), 4, "receipt_stale")
        require(manifest["gate"]["path"] == "worker-receipt.json", 2, "bundle_path_invalid")
        require(core.sha256_file(owned("worker-receipt.json")) == manifest["gate"]["sha256"],
                4, "receipt_stale")
        collision = read_source(owned("collision.glb"))
        require(hashlib.sha256(collision).hexdigest() == manifest["collision_sha256"], 4, "candidate_stale")
        parts = geometry.load_glb(collision)
        require(len(parts) == len(manifest["hulls"]) and bool(parts), 2, "hull_inventory_invalid")
        for index, (part, row) in enumerate(zip(parts, manifest["hulls"])):
            name = f"hull_{index:04d}"
            require(row["node"] == name and row["path"] == name + ".obj", 2, "bundle_path_invalid")
            require(row["source_sha256"] == expected_source_sha256, 4, "hull_source_stale")
            path = owned(row["path"])
            require(core.sha256_file(path) == row["sha256"], 4, "hull_bytes_stale")
            mesh = trimesh.load_mesh(path, file_type="obj", process=False)
            require(np.array_equal(mesh.vertices, part.vertices)
                    and np.array_equal(mesh.faces, part.faces), 4, "hull_geometry_stale")
            require(row["faces"] == len(part.faces) and row["vertices"] == len(part.vertices),
                    2, "hull_inventory_invalid")
        return evaluate_candidate(owned("source.glb"), owned("collision.glb"),
            expected_source_sha256=expected_source_sha256,
            expected_candidate_sha256=manifest["collision_sha256"], policy=policy)
    except af_proc.SignalRefusal as exc:
        return finish(rec, exc.rc, [exc.code])
    except Refusal as exc:
        return finish(rec, exc.rc, [exc.code])
    except (OSError, core.ContractError, TypeError, ValueError, KeyError):
        return finish(rec, 2, ["io_or_schema_error"])


def decompose(request, policy, *, provider, factory, territory_policy, output_dir):
    """Production path: current licence proof, source authentication, gate, commit.

    No model is loaded; callers retain the scheduler/resource admission duty.
    The request toolchain digest must equal this wrapper's content identity.
    """
    rec = receipt()
    try:
        registration = validate_request(request)
        validate_policy(policy)
        require(policy["max_hulls"] == request["params"]["max_hulls"], 2, "hull_budget_mismatch")
        _authorise(request, provider, factory, territory_policy)
        require(request["context"]["toolchain_sha256"] == toolchain_identity(), 4, "toolchain_stale")
        source_path = Path(request["inputs"][0]["path"])
        source = read_source(source_path)
        source_hash = hashlib.sha256(source).hexdigest()
        require(source_hash == request["inputs"][0]["sha256"], 4, "source_stale")
        output_dir = Path(output_dir)
        require(not output_dir.exists() and not output_dir.is_symlink(), 2, "output_exists")
        require(output_dir.parent.is_dir(), 2, "output_parent_missing")
        require(not source_path.resolve().is_relative_to(output_dir.resolve()), 2, "output_aliases_source")
        with tempfile.TemporaryDirectory(prefix=".af-coacd-", dir=output_dir.parent) as temp:
            stage = Path(temp) / "bundle"
            stage.mkdir()
            job = _job("generate", source, policy, params=request["params"])
            (stage / "source.glb").write_bytes(source)
            core.atomic_write_json(stage / "job.json", job)
            rec = _spawn(stage, min(policy["timeout_s"], request["context"]["timeout_s"]))
            require(rec["source_sha256"] == source_hash and rec["policy_sha256"] == core.sha256_json(policy)
                    and rec["toolchain_sha256"] == toolchain_identity(), 4, "receipt_stale")
            if rec["rc"] != 0:
                return result(request, rec), rec
            collision = read_source(stage / "collision.glb")
            require(hashlib.sha256(collision).hexdigest() == rec["candidate_sha256"], 4, "candidate_stale")
            hulls = []
            for index, row in enumerate(rec["hulls"]):
                name = f"hull_{index:04d}"
                part = stage / (name + ".obj")
                require(part.is_file() and not part.is_symlink(), 2, "backend_contract")
                hulls.append({"node": name, "path": part.name, "sha256": core.sha256_file(part),
                              "source_sha256": source_hash, "faces": row["faces"], "vertices": row["vertices"]})
            require(bool(hulls), 3, "hulls_unavailable")
            manifest = {"schema": "af.collision-manifest/v1", "source_sha256": source_hash,
                        "collision_sha256": rec["candidate_sha256"], "request_sha256": core.sha256_json(request),
                        "policy_sha256": core.sha256_json(policy), "toolchain_sha256": toolchain_identity(),
                        "provider_sha256": core.sha256_json(provider), "provider_revision": REVISION,
                        "seed": request["params"]["seed"], "params": request["params"], "hulls": hulls,
                        "gate": {"path": "worker-receipt.json", "sha256": core.sha256_file(stage / "worker-receipt.json")},
                        "replay_verified": False, "schema_registration": registration}
            core.validate(manifest, schema("manifest.schema.json"))
            core.atomic_write_json(stage / "manifest.json", manifest)
            outputs = [{"role": role, "path": str(output_dir / name), "media_type": media,
                        "sha256": core.sha256_file(stage / name)} for role, name, media in
                       (("collision_glb", "collision.glb", "model/gltf-binary"),
                        ("collision_manifest", "manifest.json", "application/json"))]
            refs = [{"schema": rec["schema"], "path": str(output_dir / "worker-receipt.json"),
                     "sha256": core.sha256_file(stage / "worker-receipt.json")}]
            completed = result(request, rec, outputs, refs)
            core.atomic_write_json(stage / "result.json", completed)
            # Recheck caller bytes immediately before committing the derivative.
            require(core.sha256_file(source_path) == source_hash, 4, "source_stale")
            _rename_new(stage, output_dir)
            return completed, rec
    except Refusal as exc:
        rec = finish(rec, exc.rc, [exc.code])
    except core.ContractError as exc:
        rec = finish(rec, 2, [exc.code])
    except (OSError, KeyError, TypeError, ValueError):
        rec = finish(rec, 2, ["io_or_schema_error"])
    try:
        return result(request, rec), rec
    except (core.ContractError, KeyError, TypeError):
        return {"rc": rec["rc"], "status": rec["status"], "reason_codes": rec["reason_codes"]}, rec


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Refusal(2, "usage_error")


def main(argv=None):
    try:
        parser = Parser(description=__doc__)
        parser.add_argument("operation", choices=("probe", "inspect", "gate", "run"))
        for name in ("source", "source-sha256", "candidate", "candidate-sha256", "gate-policy",
                     "request", "provider", "factory", "territory-policy", "output-dir"):
            parser.add_argument("--" + name)
        args = parser.parse_args(argv)
        if args.operation == "probe":
            rec = probe_runtime()
        elif args.operation == "inspect":
            require(args.source and args.source_sha256, 2, "usage_error")
            rec = inspect_source(args.source, expected_sha256=args.source_sha256)
        elif args.operation == "gate":
            require(all((args.source, args.source_sha256, args.candidate, args.candidate_sha256,
                         args.gate_policy)), 2, "usage_error")
            rec = evaluate_candidate(args.source, args.candidate,
                expected_source_sha256=args.source_sha256, expected_candidate_sha256=args.candidate_sha256,
                policy=core.load_json(Path(args.gate_policy), max_bytes=1024 * 1024))
        else:
            require(all((args.request, args.gate_policy, args.factory, args.territory_policy,
                         args.output_dir)), 2, "usage_error")
            load = lambda p: core.load_json(Path(p), max_bytes=1024 * 1024)
            rec, _ = decompose(load(args.request), load(args.gate_policy),
                provider=load(args.provider) if args.provider else schema("provider.json"),
                factory=load(args.factory), territory_policy=load(args.territory_policy),
                output_dir=Path(args.output_dir))
        return core.emit(rec)
    except Refusal as exc:
        return core.emit(finish(receipt(), exc.rc, [exc.code]))
    except (core.ContractError, OSError, ValueError):
        return core.emit(finish(receipt(), 2, ["io_or_schema_error"]))


if __name__ == "__main__":
    sys.exit(main())

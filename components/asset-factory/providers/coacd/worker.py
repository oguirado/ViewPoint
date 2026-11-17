"""Owned subprocess for optional native CoACD and bounded G11 measurements."""
import hashlib
import importlib.metadata
import importlib.util
import inspect
from pathlib import Path
import platform
import resource
import sys
import time

from asset_factory import core
from . import geometry
from .contracts import (HERE, VERSION, Refusal, finish, receipt, require, schema,
                        validate_policy, validate_job)


def runtime_info():
    np, trimesh = geometry.dependencies()
    info = {"python": platform.python_version(), "numpy": np.__version__,
            "trimesh": trimesh.__version__, "coacd_available": False,
            "coacd_version": None, "code_sha256": None}
    if importlib.util.find_spec("coacd") is None:
        return info
    info["coacd_available"] = True
    try:
        dist = importlib.metadata.distribution("coacd")
        info["coacd_version"] = dist.version
        files = {str(f): core.sha256_file(dist.locate_file(f)) for f in dist.files or []
                 if str(f).startswith("coacd/") and str(f).endswith((".py", ".so"))}
        require(bool(files), 3, "runtime_identity_unavailable")
        info["code_sha256"] = core.sha256_json(files)
    except importlib.metadata.PackageNotFoundError as exc:
        raise Refusal(3, "runtime_identity_unavailable") from exc
    return info


def require_runtime(info):
    require(info["coacd_available"], 3, "coacd_unavailable")
    require(info["coacd_version"] == VERSION, 4, "runtime_version_stale")
    lock = schema("runtime-lock.json")
    require(lock["qualified"] and lock["runtime_binary_sha256"] is not None,
            3, "runtime_pin_unproven")
    require(info["code_sha256"] == lock["runtime_binary_sha256"], 4, "runtime_code_stale")


def native_decompose(meshes, params):
    """README documents Mesh/run_coacd and the CLI parameter names.

    The optional Python keyword boundary is checked on the actual runtime before
    invocation. A missing/uninspectable keyword is CANNOT_SEE, never guessed.
    Preprocessing is disabled: an unseen repair must not replace the raw source.
    """
    import coacd
    _, trimesh = geometry.dependencies()
    kwargs = {"seed": params["seed"], "threshold": params["threshold_ppm"] / 1000000,
              "resolution": params["resolution"], "max_convex_hull": params["max_hulls"],
              "preprocess_mode": "off"}
    try:
        parameters = inspect.signature(coacd.run_coacd).parameters
    except (TypeError, ValueError) as exc:
        raise Refusal(3, "runtime_api_unproven") from exc
    require(set(kwargs) <= set(parameters), 3, "runtime_api_unproven")
    source = trimesh.util.concatenate(meshes)
    parts = coacd.run_coacd(coacd.Mesh(source.vertices, source.faces), **kwargs)
    require(isinstance(parts, list), 2, "backend_contract")
    return [trimesh.Trimesh(vertices=v, faces=f, process=False) for v, f in parts]


def source_inventory(parts, rec):
    normalised, rows = [], []
    for part in parts:
        part, row = geometry.inventory(part)
        normalised.append(part)
        rows.append(row)
    rec["source_inventory"] = rows
    rec["metrics"].update(source_faces=sum(r["faces"] for r in rows),
                          source_vertices=sum(r["vertices"] for r in rows),
                          source_boundary_edges=sum(r["boundary_edges"] for r in rows))
    return normalised


def measure(job, folder):
    started = time.monotonic_ns()
    rec = receipt()
    rc, reasons = 3, []
    try:
        validate_job(job)
        rec.update(source_sha256=job["source_sha256"], policy_sha256=job["policy_sha256"],
                   toolchain_sha256=job["toolchain_sha256"])
        rec["runtime"] = runtime_info()
        operation = job["operation"]
        if operation == "probe":
            require_runtime(rec["runtime"])
            rc = 0
        else:
            source_bytes = (folder / "source.glb").read_bytes()
            require(hashlib.sha256(source_bytes).hexdigest() == job["source_sha256"],
                    4, "source_stale")
            sources = source_inventory(geometry.load_glb(source_bytes), rec)
            closed = all(s["closed"] and s["winding_consistent"] and s["positive_volume"]
                         and s["components"] == 1
                         and not s["degenerate_faces"] for s in rec["source_inventory"])
            require(closed, 3, "source_volume_unavailable")
            if operation == "inspect":
                # Inspection proves topology visibility, never a collision PASS.
                raise Refusal(3, "collision_not_evaluated")
            policy = job["gate_policy"]
            validate_policy(policy)
            require(core.sha256_json(policy) == job["policy_sha256"], 4, "policy_stale")
            if operation in ("generate", "qualify"):
                if operation == "generate":
                    require_runtime(rec["runtime"])
                else:
                    # Qualification is scratch-only, pinned by a pre-run freeze.
                    # It does not authorise production or alter provider readiness.
                    require(rec["runtime"]["coacd_available"], 3, "coacd_unavailable")
                    require(rec["runtime"]["coacd_version"] == VERSION, 4, "runtime_version_stale")
                    require(rec["runtime"] == job["qualification_runtime"], 4, "runtime_code_stale")
                hulls = native_decompose(sources, job["params"])
                _, trimesh = geometry.dependencies()
                scene = trimesh.Scene()
                for index, hull in enumerate(hulls):
                    scene.add_geometry(hull, node_name=f"hull_{index:04d}",
                                       geom_name=f"hull_{index:04d}")
                candidate = scene.export(file_type="glb")
                (folder / "collision.glb").write_bytes(candidate)
            else:
                candidate = (folder / "collision.glb").read_bytes()
                require(hashlib.sha256(candidate).hexdigest() == job["candidate_sha256"],
                        4, "candidate_stale")
            # Gate the exported bytes, not the pre-export in-memory arrays.
            hulls = geometry.load_glb(candidate)
            rec["candidate_sha256"] = hashlib.sha256(candidate).hexdigest()
            rec["metrics"]["hull_count"] = len(hulls)
            metrics, stats, reasons = geometry.evaluate(sources, hulls, policy)
            rec["metrics"].update(metrics)
            rec["hulls"] = stats
            rec["absence_reasons"]["volume_uncertainty_ppm"] = (
                "Uncalibrated seeded sampling estimate; no exact-volume or confidence-bound claim")
            # A point estimate cannot establish the acceptance ceiling. The only
            # available exact certificate is identical, exactly convex surfaces.
            identity = geometry.exact_surface_identity(sources, hulls) if not reasons else False
            rec["metrics"].update(volume_bound_method="exact_surface_identity" if identity else None,
                                  lost_volume_upper_ppm=0 if identity else None,
                                  extra_volume_upper_ppm=0 if identity else None)
            rc = 5 if reasons else 0 if identity else 3
            if rc == 3:
                reasons = ["volume_bound_unavailable"]
                rec["absence_reasons"].update({k: "volume_bound_unavailable" for k in
                    ("volume_bound_method", "lost_volume_upper_ppm", "extra_volume_upper_ppm")})
            if rc == 0 and operation in ("generate", "qualify"):
                for index, hull in enumerate(hulls):
                    (folder / f"hull_{index:04d}.obj").write_text(hull.export(
                        file_type="obj", digits=17, include_normals=False,
                        include_color=False, include_texture=False))
    except Refusal as exc:
        rc, reasons = exc.rc, [exc.code]
    except core.ContractError as exc:
        rc, reasons = 2, [exc.code]
    except (OSError, ValueError, TypeError, KeyError, IndexError, ImportError) as exc:
        rc, reasons = 2, ["worker_contract:" + type(exc).__name__]
    finally:
        rec["metrics"]["wall_ms"] = (time.monotonic_ns() - started) // 1000000
        rec["metrics"]["peak_rss_bytes"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    if rc != 0:
        rec["metrics"].setdefault("lost_volume_ppm", None)
        rec["metrics"].setdefault("extra_volume_ppm", None)
        rec["absence_reasons"].update({k: ",".join(reasons) for k in
            ("lost_volume_ppm", "extra_volume_ppm") if rec["metrics"][k] is None})
    return finish(rec, rc, reasons)


def main():
    folder = Path(sys.argv[1])
    job = core.load_json(folder / "job.json", max_bytes=1024 * 1024)
    rec = measure(job, folder)
    core.atomic_write_json(folder / "worker-receipt.json", rec)
    return rec["rc"]


if __name__ == "__main__":
    sys.exit(main())

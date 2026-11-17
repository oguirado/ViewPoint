#!/usr/bin/env python3
"""Import/compiler prerequisite probe, never an asset or geometry PASS."""
import argparse
import contextlib
import importlib
import importlib.util
from pathlib import Path
import platform
import re
import sys
import tempfile

# Namespace module on the host; isolated explicit sibling loading in the image.
if __package__:
    from .build import Refusal, STATUS, canonical, read_json, run_owned, sha
else:
    spec = importlib.util.spec_from_file_location("_af_trellis_build", Path(__file__).with_name("build.py"))
    _build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(_build)
    Refusal, STATUS, canonical, read_json, run_owned, sha = (_build.Refusal, _build.STATUS, _build.canonical,
                                                         _build.read_json, _build.run_owned, _build.sha)

EXTENSIONS = ("cumesh", "flex_gemm", "o_voxel")
RESTRICTED = ("nvdiffrast", "nvdiffrec")


def check_links(machine, extensions, compiler, cache_arch, device_arch):
    """Pure prerequisite validator; its synthetic tests do not prove ABI."""
    if machine != "aarch64":
        raise Refusal(4, "architecture_stale")
    if set(extensions) != set(EXTENSIONS) or any(extensions[name] is not True for name in EXTENSIONS):
        raise Refusal(3, "extension_link_unproven")
    if compiler is not True:
        raise Refusal(3, "compiler_identity_unproven")
    if device_arch is None or cache_arch is None:
        raise Refusal(3, "architecture_unmeasured")
    if not re.fullmatch(r"sm_[0-9]+", device_arch) or device_arch != cache_arch:
        raise Refusal(4, "cache_architecture_stale")


def acceptance(prerequisite_rc, geometry_rc=None, glb_rc=None):
    """A prerequisite never substitutes for an actual runtime/GLB evaluator."""
    values = (prerequisite_rc, geometry_rc, glb_rc)
    if any(type(v) is not int or v not in STATUS for v in values if v is not None):
        return 2
    if 2 in values:
        return 2
    if 5 in values:
        return 5
    if any(v is None or v in (3, 4, 6) for v in values):
        return 3
    return 0


@contextlib.contextmanager
def local_import_path():
    original = sys.path[:]
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        yield
    finally:
        sys.path[:] = original


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ptxas", type=Path)
    parser.add_argument("--cache-arch", type=Path)
    try:
        args = parser.parse_args(argv)
        lock = read_json(Path(__file__).resolve().parent / "build-inputs.json")
        # Hash expectation must come from the selected build input, not live bytes.
        expected = lock["compiler_sha256"]
        if expected is None or args.ptxas is None or args.cache_arch is None:
            raise Refusal(3, "runtime_inputs_absent")
        if not args.ptxas.is_file() or sha(args.ptxas.read_bytes()) != expected:
            raise Refusal(4, "compiler_stale")
        # Installed source may be read-only or owned by another uid. The
        # supervisor spools into cwd, so give it explicitly owned scratch.
        with tempfile.TemporaryDirectory(prefix="af-runtime-probe-") as scratch:
            version = run_owned([str(args.ptxas), "--version"], cwd=Path(scratch)).decode()
        if not re.search(r"ptxas.*release [0-9]+\.[0-9]+", version, re.DOTALL):
            raise Refusal(5, "compiler_version_invalid")
        for name in RESTRICTED:
            if importlib.util.find_spec(name) is not None:
                raise Refusal(5, "restricted_dependency_present")
        loaded = {}
        with contextlib.redirect_stdout(sys.stderr), local_import_path():
            for name in EXTENSIONS:
                importlib.import_module(name)
                loaded[name] = True
            from trellis2.pipelines.trellis2_image_to_3d import Trellis2GeometryPipeline
            import torch
            if not torch.cuda.is_available():
                raise Refusal(3, "gpu_unavailable")
            major, minor = torch.cuda.get_device_capability()
        if any(name == prefix or name.startswith(prefix + ".") for name in sys.modules for prefix in RESTRICTED):
            raise Refusal(5, "restricted_import")
        measured = "sm_" + str(major) + str(minor)
        # Read only: a stale cache marker is never rewritten to fit the device.
        check_links(platform.machine(), loaded, True, args.cache_arch.read_text().strip(), measured)
        if any(name.startswith("tex_") for name in Trellis2GeometryPipeline.model_names_to_load):
            raise Refusal(5, "texture_model_dependency")
        rc = acceptance(0)
        result = {"rc": rc, "status": STATUS[rc], "verdict": "ERROR" if rc == 2 else "FAIL" if rc == 5 else "CANNOT_SEE" if rc else "PASS",
                  "prerequisite_rc": 0, "geometry_rc": None, "glb_rc": None,
                  "reason_codes": ["geometry_execution_unproven", "glb_execution_unproven"]}
    except SystemExit as exc:
        if exc.code == 0:
            return 0
        result = {"rc": 2, "status": "ERROR", "verdict": "ERROR", "reason_codes": ["usage_invalid"]}
    except Refusal as exc:
        result = {"rc": exc.rc, "status": STATUS[exc.rc],
                  "verdict": "ERROR" if exc.rc == 2 else "FAIL" if exc.rc == 5 else "CANNOT_SEE",
                  "reason_codes": [exc.reason]}
    except (ValueError, KeyError, TypeError):
        result = {"rc": 2, "status": "ERROR", "verdict": "ERROR", "reason_codes": ["runtime_input_invalid"]}
    except (OSError, ImportError):
        result = {"rc": 3, "status": "INCOMPLETE", "verdict": "CANNOT_SEE",
                  "reason_codes": ["runtime_dependency_unavailable"]}
    except Exception:
        result = {"rc": 5, "status": "FAIL", "verdict": "FAIL", "reason_codes": ["extension_import_failed"]}
    print(canonical(result), end="")
    return result["rc"]


if __name__ == "__main__":
    raise SystemExit(main())

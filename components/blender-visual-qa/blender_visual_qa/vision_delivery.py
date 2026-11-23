"""Resolve the Vision pin and restrict engine imports to the admitted artifact."""

import hashlib
from importlib.machinery import PathFinder
import json
import os
from pathlib import Path
import stat
import sys
from zipimport import ZipImportError, zipimporter

from .package_layout import is_source_layout


ENGINE_PKGS = ("visioncore", "vmem", "vadapt")
WHEEL_ENV = "BVQA_VISION_WHEEL"
_ROOT = Path(__file__).resolve().parents[1]
_VISION_CONFIG = "vision.json"
_REQUIRED_ORIGINS = {
    "vadapt": "vadapt/__init__.py",
    "vadapt.blender": "vadapt/blender/__init__.py",
    "vadapt.blender.from_render_dir": "vadapt/blender/from_render_dir.py",
    "vadapt.ramws": "vadapt/ramws.py",
    "visioncore": "visioncore/__init__.py",
    "visioncore.params": "visioncore/params.py",
    "visioncore.pipeline": "visioncore/pipeline.py",
    "vmem": "vmem/__init__.py",
}


class DeliveryError(ValueError):
    pass


def resolve_wheel(config=None, *, relative_root=None, expected_commit=None):
    """Read the existing pin and verify the wheel. A default wheel can be relocated only in the installed layout."""
    standalone = config is None and not is_source_layout(_ROOT)
    if standalone:
        config = _ROOT / "blender_visual_qa/assets/_runtime_config" / _VISION_CONFIG
    elif config is None:
        config = _ROOT / "config" / _VISION_CONFIG
    else:
        config = Path(config)

    cfg = json.loads(config.read_text())
    pin = cfg["pin"]
    if expected_commit is not None and pin["commit"] != expected_commit:
        raise DeliveryError("configured engine commit differs from required commit")
    if standalone:
        wheel = _selected_wheel()
    else:
        wheel = Path(cfg["engine_path"])
        if not wheel.is_absolute():
            base = config.parent.parent if relative_root is None else relative_root
            wheel = base / wheel
    if not wheel.is_file():
        raise DeliveryError(f"engine wheel absent: {wheel}")
    with wheel.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != pin["wheel_sha256"]:
        raise DeliveryError(f"engine wheel hash mismatch ({digest} != {pin['wheel_sha256']})")
    if standalone:
        finder = EngineFinder(wheel)
        for name, member in _REQUIRED_ORIGINS.items():
            try:
                spec = finder.find_spec(name)
            except ImportError as exc:
                raise DeliveryError(f"required engine member absent: {member}") from exc
            if spec.origin != str(wheel / member):
                raise DeliveryError(f"required engine member or origin invalid: {member}")
    return wheel, pin


def _selected_wheel():
    wheel = Path(os.environ.get(WHEEL_ENV, ""))
    if not wheel.is_absolute() or wheel.suffix != ".whl":
        raise DeliveryError(f"{WHEEL_ENV} must be an absolute path to a local read-only .whl")
    try:
        wheel = wheel.resolve(strict=True)
    except RuntimeError as exc:
        raise DeliveryError(f"{WHEEL_ENV} cannot be resolved: {wheel}") from exc
    mode = wheel.stat().st_mode
    if not stat.S_ISREG(mode):
        raise DeliveryError(f"{WHEEL_ENV} must select a regular wheel file")
    if mode & 0o222 and not os.statvfs(wheel).f_flag & os.ST_RDONLY:
        raise DeliveryError(f"{WHEEL_ENV} must select a read-only wheel")
    return wheel


class EngineFinder:
    """Search only the pinned wheel, or the supplied fixture under the existing TRUTH test hook."""

    def __init__(self, path):
        self.path = Path(path).resolve()
        self.fixture = self.path.is_dir()

    def find_spec(self, fullname, path=None, target=None):
        parts = fullname.split(".")
        if parts[0] not in ENGINE_PKGS:
            return None
        parent = str(self.path.joinpath(*parts[:-1]))
        if self.fixture:
            spec = PathFinder.find_spec(fullname, [parent])
        else:
            try:
                spec = zipimporter(parent).find_spec(fullname)
            except ZipImportError:
                spec = None
        if spec is None or spec.origin is None:
            raise ModuleNotFoundError(f"E_PIN: {fullname} absent from {self.path}", name=fullname)
        return spec

    def check_origin(self, module):
        name = module.__name__
        try:
            spec = self.find_spec(name)
        except ImportError as exc:
            raise DeliveryError(f"module origin outside admitted engine: {name}") from exc
        if getattr(module, "__file__", None) != spec.origin:
            raise DeliveryError(f"module origin outside admitted engine: {name}")


def admit_engine_imports(engine):
    finder = EngineFinder(engine)
    for name, module in list(sys.modules.items()):
        if module is not None and name.split(".")[0] in ENGINE_PKGS:
            finder.check_origin(module)
    sys.meta_path[:] = [entry for entry in sys.meta_path if not isinstance(entry, EngineFinder)]
    sys.meta_path.insert(0, finder)
    return finder

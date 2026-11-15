"""Fail-closed pin of the vision-engine wheel. Import first, then call pin.activate().

Hardcoded constants are the only source of truth; config/vision.json and vision/VENDORED.json must agree with them.
Any mismatch is rc 5 E_PIN (never re-pin). Stdlib only.
"""
import hashlib
import json
import os
import sys
from pathlib import Path

TAG = "ve-sealed-1-law4"
EXPECTED_COMMIT = "2612b3acd67c49234adfaa77ab2b3803d5a81cb5"
EXPECTED_WHEEL_SHA256 = "5e2395d56709274f21123bb5ef714adf52f0cf58f78d4f4938da736c52a7a4a5"
WHEEL_NAME = "vision_engine-1.1.0-py3-none-any.whl"
VE_CHECKOUT = "/home/user/Documentos/vision-engine"
ENGINE_MODULES = ("visioncore", "vmem", "vadapt")
REPO = Path(__file__).resolve().parents[1]
DEFAULT_WHEEL = REPO / "build" / "wheels" / WHEEL_NAME
DEFAULT_CONFIG = REPO / "config" / "vision.json"
DEFAULT_VENDORED = REPO / "vision" / "VENDORED.json"
RC_PIN = 5


class PinError(Exception):
    code = "E_PIN"


def _hook(tp, val, tb):
    """Every uncaught exception maps to rc 5 (rc 1 is unreachable): PinError -> E_PIN, anything else -> E_GATE."""
    code = "E_PIN" if issubclass(tp, PinError) else "E_GATE"
    try:
        sys.stdout.write(json.dumps({"code": code, "detail": "%s: %s" % (tp.__name__, val)}, sort_keys=True) + "\n")
        sys.stdout.flush()
    finally:
        os._exit(RC_PIN)


sys.excepthook = _hook


def _fail(msg):
    raise PinError(msg)


def _under(path, base):
    try:
        p = os.path.realpath(path or os.getcwd())
    except (OSError, ValueError):
        return False
    b = os.path.realpath(base)
    return p == b or p.startswith(b + os.sep)


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def _read_json(p, what):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        _fail("%s unreadable: %s" % (what, e))


def _check_records(config_path, vendored_path):
    if not config_path or not vendored_path:
        _fail("config/vision.json and VENDORED.json are mandatory (None refused)")
    cfg = _read_json(config_path, "config/vision.json")
    pin = cfg.get("pin") if isinstance(cfg, dict) else None
    if not isinstance(pin, dict) or pin.get("tag") != TAG or pin.get("commit") != EXPECTED_COMMIT \
            or pin.get("wheel_sha256") != EXPECTED_WHEEL_SHA256:
        _fail("config/vision.json pin differs from the hardcoded pin (re-pin refused)")
    ven = _read_json(vendored_path, "VENDORED.json")
    if not isinstance(ven, dict) or ven.get("tag") != TAG or ven.get("wheel_sha256") != EXPECTED_WHEEL_SHA256 \
            or ven.get("commit") != EXPECTED_COMMIT:
        _fail("VENDORED.json differs from the hardcoded pin (re-pin refused)")


def _stdlib_dirs():
    """Real paths of the interpreter stdlib (and its zip), never a site/dist-packages dir."""
    import sysconfig
    dirs = set()
    for key in ("stdlib", "platstdlib"):
        d = sysconfig.get_paths().get(key)
        if d:
            dirs.add(os.path.realpath(d))
    return dirs


def _is_stdlib_entry(entry, stdlib):
    if not entry:
        return False
    r = os.path.realpath(entry)
    if "site-packages" in r or "dist-packages" in r:
        return False
    return any(r == d or r.startswith(d + os.sep) for d in stdlib) or (
        r.endswith(".zip") and os.path.dirname(r) in {os.path.dirname(d) for d in stdlib})


def verify_loaded(wreal):
    """Every loaded visioncore/vmem/vadapt module (submodules included) must come from the wheel."""
    for name in list(sys.modules):
        if name.split(".")[0] in ENGINE_MODULES:
            f = getattr(sys.modules[name], "__file__", None)
            if not f or not _under(f, wreal):
                _fail("module %s loaded from outside the wheel: %r" % (name, f))


def _activate(wheel, sha, cfg, ven, ve):
    wheel = Path(wheel)
    if not wheel.is_file():
        _fail("wheel missing: %s" % wheel)
    got = sha256_file(wheel)
    if got != sha:
        _fail("wheel sha256 %s != pinned %s" % (got, sha))
    _check_records(cfg, ven)
    wreal = os.path.realpath(wheel)
    for e in sys.path:
        if _under(e, ve):
            _fail("sys.path entry resolves under the vision-engine checkout: %r" % e)
    verify_loaded(wreal)
    try:  # GuiAdapter / visioncore.pipeline need numpy: load it into sys.modules before the site dirs are pruned below
        import numpy  # noqa: F401
    except ImportError:
        pass
    stdlib = _stdlib_dirs()
    keep = [e for e in sys.path if _is_stdlib_entry(e, stdlib)]
    # ONLY the wheel, this repo (for `vision.*`) and the stdlib: PYTHONPATH copies, .pth dirs, cwd and site dirs are dropped
    sys.path[:] = [wreal, str(REPO)] + [e for e in keep if os.path.realpath(e) not in (wreal, str(REPO))]
    sys.path_importer_cache.clear()
    for name in ENGINE_MODULES:
        try:
            mod = __import__(name)
        except ImportError as e:
            _fail("cannot import %s from the wheel: %s" % (name, e))
        if not _under(getattr(mod, "__file__", None), wreal):
            _fail("%s resolved outside the wheel: %r" % (name, getattr(mod, "__file__", None)))
    verify_loaded(wreal)
    return {"wheel": str(wheel), "sha256": got}


def activate():
    """Verify the pin and make the wheel the only engine source. No overrides. Raises PinError (excepthook -> rc 5)."""
    return _activate(DEFAULT_WHEEL, EXPECTED_WHEEL_SHA256, DEFAULT_CONFIG, DEFAULT_VENDORED, VE_CHECKOUT)


def _activate_for_test(wheel, expected_sha, config_path, vendored_path, ve_checkout=None):
    """Test seam: a synthetic wheel/sha, but config and VENDORED records are still mandatory and cross-checked."""
    return _activate(wheel, expected_sha, config_path, vendored_path, ve_checkout or VE_CHECKOUT)


if __name__ == "__main__":
    print(json.dumps(activate(), sort_keys=True))
    sys.exit(0)

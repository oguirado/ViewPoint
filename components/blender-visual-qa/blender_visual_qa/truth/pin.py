"""Engine pin (JW9c-1/JW9c-2): the ONE place the truth entry points get their vision engine from.

Imported FIRST by run_bench, labels, score and calibrate (calibrate through score). It puts the sha-verified wheel named
by <repo>/config/vision.json first on sys.path and never appends a live vision-engine checkout. Fail closed (E_PIN):
  - config/vision.json absent or unreadable, wheel unreadable, or its sha256 != pin.wheel_sha256;
  - an engine package (visioncore/vmem/vadapt) already imported from outside the admitted engine.
Test-only injection: BVQA_TEST_ENGINE=<dir> (alias BT_EXPECT_ENGINE, set by the tests/test_bt_kill.py mutant harness)
admits that one directory instead of the configured wheel, for scratch mutant trees that patch engine sources. Never set
by any production entry point.
"""
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config" / "vision.json"
TEST_ENV = ("BVQA_TEST_ENGINE", "BT_EXPECT_ENGINE")
ENGINE_PKGS = ("visioncore", "vmem", "vadapt")


class PinError(RuntimeError):
    code = "E_PIN"


def _fail(msg):
    raise PinError("E_PIN: " + msg)


def configured_wheel(config=CONFIG):
    """-> absolute wheel path after sha verification against config/vision.json; raises PinError."""
    if not config.is_file():
        _fail("%s absent: refusing to import an unpinned engine" % config)
    try:
        cfg = json.loads(config.read_text())
        wheel = Path(cfg["engine_path"])
        wheel = wheel if wheel.is_absolute() else config.parent.parent / wheel
        want = cfg["pin"]["wheel_sha256"]
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _fail("engine wheel unreadable: %s" % exc)
    if digest != want:
        _fail("engine wheel hash mismatch (%s != %s)" % (digest, want))
    return str(wheel)


def _admitted():
    for name in TEST_ENV:
        if os.environ.get(name):
            return str(Path(os.environ[name]).resolve()), "test:" + name
    return configured_wheel(), "wheel"


def _check_loaded(engine):
    for name, mod in list(sys.modules.items()):
        if name.split(".")[0] in ENGINE_PKGS:
            origin = str(getattr(mod, "__file__", "") or "")
            if origin and not str(Path(origin).resolve()).startswith(engine + os.sep) and not origin.startswith(engine + os.sep):
                _fail("%s already imported from outside the pinned engine (%s)" % (name, origin))


def pin():
    """Admit the engine and put it first on sys.path. Idempotent. -> (engine path, source)."""
    engine, source = _admitted()
    _check_loaded(engine)
    while engine in sys.path:
        sys.path.remove(engine)
    sys.path.insert(0, engine)
    return engine, source


def origins():
    """{module: file} for every loaded engine module (diagnostics/tests)."""
    return {n: str(getattr(m, "__file__", "") or "") for n, m in sorted(sys.modules.items())
            if n.split(".")[0] in ENGINE_PKGS}


ENGINE, SOURCE = pin()

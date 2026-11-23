"""Engine pin (JW9c-1/JW9c-2): the ONE place the truth entry points get their vision engine from.

Imported FIRST by run_bench, labels, score and calibrate (calibrate through score). It restricts engine imports to
the sha-verified wheel. Fail closed (E_PIN):
  - the pin or wheel is absent or unreadable, or the wheel sha256 != pin.wheel_sha256;
  - an engine package (visioncore/vmem/vadapt) already imported from outside the admitted engine.
Test-only injection: BVQA_TEST_ENGINE=<dir> (alias BT_EXPECT_ENGINE, set by the tests/test_bt_kill.py mutant harness)
admits that one directory instead of the configured wheel, for scratch mutant trees that patch engine sources. Never set
by any production entry point.
"""
import os
import sys
from pathlib import Path

from ..vision_delivery import ENGINE_PKGS, admit_engine_imports, resolve_wheel

TEST_ENV = ("BVQA_TEST_ENGINE", "BT_EXPECT_ENGINE")


class PinError(RuntimeError):
    code = "E_PIN"


def _fail(msg):
    raise PinError("E_PIN: " + msg)


def configured_wheel(config=None):
    """Return the wheel path after SHA verification against the selected config."""
    try:
        wheel, _ = resolve_wheel(config)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _fail(str(exc))
    return str(wheel)


def _admitted():
    for name in TEST_ENV:
        if os.environ.get(name):
            return str(Path(os.environ[name]).resolve()), "test:" + name
    return configured_wheel(), "wheel"


def pin():
    """Admit imports from the selected engine. Idempotent. -> (engine path, source)."""
    engine, source = _admitted()
    try:
        admit_engine_imports(engine)
    except ValueError as exc:
        _fail(str(exc))
    return engine, source


def origins():
    """{module: file} for every loaded engine module (diagnostics/tests)."""
    return {n: str(getattr(m, "__file__", "") or "") for n, m in sorted(sys.modules.items())
            if n.split(".")[0] in ENGINE_PKGS}


ENGINE, SOURCE = pin()

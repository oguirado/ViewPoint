"""The factory's in-process geometry imports must leave both search paths alone."""

from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
IMPORTERS = (
    "tools.mesh.worker",
    "tools.mesh.certificate",
    "tools.mesh.remesh_gate",
    "tools.mesh.raw_admission",
    "tools.mesh.remesh",
    "tools.blender.uv_quality",
)
PROBE = """
import importlib
from pathlib import Path
import sys
import unittest

import tools

case = unittest.TestCase()
before_sys = sys.path.copy()
before_tools = list(tools.__path__)
case.assertNotIn("tools.gates", sys.modules)
importlib.import_module(sys.argv[1])
from tools import gates

expected_gates = Path.cwd() / "tools" / "gates.py"
actual = (sys.path, list(tools.__path__), Path(gates.__file__).resolve())
case.assertEqual(actual, (before_sys, before_tools, expected_gates))
"""

SIBLING_PROBE = """
from importlib.machinery import ModuleSpec, PathFinder
from pathlib import Path
import sys
from types import ModuleType
from unittest import TestCase
from unittest.mock import patch

case = TestCase()
name = "blender_visual_qa"
mode = sys.argv[1]
if mode == "present":
    from tools.mesh import worker
    from blender_visual_qa.assets import glb_core

    expected = Path.cwd().parent / "blender-visual-qa" / name
    case.assertEqual(Path(glb_core.__file__).resolve().parent, expected / "assets")
elif mode == "preloaded":
    sibling = ModuleType(name)
    sys.modules[name] = sibling
    with patch.object(PathFinder, "find_spec", wraps=PathFinder.find_spec) as finder:
        from tools.mesh import worker

    case.assertIs(sys.modules[name], sibling)
    case.assertFalse(any(call.args[0] == name for call in finder.call_args_list))
else:
    spec = None if mode == "missing" else ModuleSpec(name, loader=None, is_package=True)
    find_spec = PathFinder.find_spec

    def find_without_sibling(fullname, path=None, target=None):
        if fullname == name:
            return spec
        return find_spec(fullname, path, target)

    with patch.object(PathFinder, "find_spec", side_effect=find_without_sibling):
        from tools.mesh import worker

    case.assertFalse(name in sys.modules, "an unavailable sibling entered the module cache")
"""


class ToolsNamespaceTests(unittest.TestCase):
    def check_in_fresh_interpreter(self, probe, argument):
        child = subprocess.run(
            [sys.executable, "-B", "-c", probe, argument],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(child.returncode, 0, child.stdout + child.stderr)

    def test_imports_preserve_factory_namespace(self):
        for module in IMPORTERS:
            with self.subTest(module=module):
                self.check_in_fresh_interpreter(PROBE, module)

    def test_sibling_package_resolves_from_checkout(self):
        self.check_in_fresh_interpreter(SIBLING_PROBE, "present")

    def test_preloaded_sibling_is_preserved(self):
        self.check_in_fresh_interpreter(SIBLING_PROBE, "preloaded")

    def test_missing_sibling_does_not_break_helper_imports(self):
        self.check_in_fresh_interpreter(SIBLING_PROBE, "missing")

    def test_namespace_only_sibling_is_not_loaded(self):
        self.check_in_fresh_interpreter(SIBLING_PROBE, "namespace")

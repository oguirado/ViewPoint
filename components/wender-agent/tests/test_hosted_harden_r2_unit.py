# @module {"id":"wa.tests.test_hosted_harden_r2_unit","layer":2,"owner":"ACT","deps":["wa.act.hosted_guard"],"book":"HB-WA-s2-act"}
"""WA-HOSTED-R2 regressions and named, in-memory negative mutations."""
from __future__ import annotations

import ast
import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from wender.act import hosted_guard as guard


def _mutated_function(name, mutation):
    tree = ast.parse(Path(guard.__file__).read_text(encoding="utf-8"))
    original = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    function = copy.deepcopy(original)
    mutation(function)
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    namespace = dict(vars(guard))
    exec(compile(module, f"<{mutation.__name__}>", "exec"), namespace)
    return namespace[name]


def mutate_env_replaces_process(function):
    """Restore the caller-env-only decision, ignoring the real process env."""
    matches = [n for n in ast.walk(function) if isinstance(n, ast.BoolOp)
               and any(isinstance(v, ast.Call) and isinstance(v.func, ast.Name)
                       and v.func.id == "_env_hosted_enabled" for v in n.values)]
    assert len(matches) == 1
    matches[0].values[0] = ast.Constant(False)


def mutate_drop_import_aliases(function):
    """Discard collected aliases immediately before visiting scanner calls."""
    matches = [n for n in ast.walk(function) if isinstance(n, ast.For)
               and isinstance(n.target, ast.Name) and n.target.id == "node"]
    assert len(matches) == 1
    matches[0].body.insert(0, ast.Assign(
        targets=[ast.Name(id="import_aliases", ctx=ast.Store())], value=ast.Dict(keys=[], values=[])))


def mutate_drop_bare_loader_names(function):
    """Restore pre-fix bare-loader blindness, including missing alias resolution."""
    mutate_drop_import_aliases(function)
    matches = [n for n in ast.walk(function) if isinstance(n, ast.Compare)
               and isinstance(n.left, ast.Attribute) and n.left.attr == "id"
               and any(isinstance(c, ast.Constant) and c.value == "exec_module"
                       for c in ast.walk(n))]
    assert len(matches) == 1
    matches[0].comparators = [ast.Tuple(elts=[], ctx=ast.Load())]


def mutate_getattr_exec_as_import(function):
    """Reclassify indirect exec/eval/compile as imports with inert target strings."""
    matches = [n for n in ast.walk(function) if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "is_dyn_import" for t in n.targets)]
    assert len(matches) == 1
    matches[0].value = ast.BoolOp(op=ast.Or(), values=[matches[0].value,
        ast.parse('getattr_name in ("exec", "eval", "compile")', mode="eval").body])
    matches = [n for n in ast.walk(function) if isinstance(n, ast.Compare)
               and isinstance(n.left, ast.Name) and n.left.id == "getattr_name"
               and any(isinstance(c, ast.Constant) and c.value == "exec" for c in ast.walk(n))]
    # Preserve the added dynamic-import condition; erase the execution condition.
    assert len(matches) == 2
    matches[-1].comparators = [ast.Tuple(elts=[], ctx=ast.Load())]


# @anchor:wa.tests.test_hosted_harden_r2_unit.HostedHardenR2Tests
class HostedHardenR2Tests(unittest.TestCase):
    def setUp(self):
        for name, value in {
            "_HOSTED_MODE_OVERRIDE": None, "_HOSTED_MODE_LATCHED": False,
            "_PLANNER_ENTRY_DEPTH": 0, "_PLANNER_SCOPE_DEPTH": 0,
            "_PLANNER_ENTRY_LATCHED": False,
        }.items():
            patcher = patch.object(guard, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in {"_wender_hosted_latched": False, "_wender_planner_entry_depth": 0}.items():
            patcher = patch.object(sys, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(guard.HOSTED_ENV_VAR, None)

    def _scan(self, source, scan=None):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "planner.py"
            fixture.write_text(source, encoding="utf-8")
            return (scan or guard.scan_plan_act_imports)(fixture)

    # @anchor:wa.tests.test_hosted_harden_r2_unit.HostedHardenR2Tests.test_explicit_env_cannot_clear_hosted_and_mutation
    def test_explicit_env_cannot_clear_hosted_and_mutation(self):
        guard.assert_actuation_allowed("standalone", env={})
        with self.assertRaises(guard.HostedActuationRefused):
            guard.assert_actuation_allowed("explicit", env={guard.HOSTED_ENV_VAR: "1"})
        os.environ[guard.HOSTED_ENV_VAR] = "1"
        for supplied in ({}, {guard.HOSTED_ENV_VAR: "0"}):
            with self.subTest(env=supplied), self.assertRaises(guard.HostedActuationRefused):
                guard.assert_actuation_allowed("process-env", env=supplied)
        mutant = _mutated_function("hosted_reason", mutate_env_replaces_process)
        with patch.object(guard, "hosted_reason", mutant):
            guard.assert_actuation_allowed("mutant-bypass", env={})
        guard.set_hosted_mode(True)
        os.environ.pop(guard.HOSTED_ENV_VAR)
        with self.assertRaises(guard.HostedActuationRefused):
            guard.assert_actuation_allowed("latched", env={})

    # @anchor:wa.tests.test_hosted_harden_r2_unit.HostedHardenR2Tests.test_aliased_dynamic_import_and_mutation
    def test_aliased_dynamic_import_and_mutation(self):
        source = 'from importlib import import_module as im\nim("wender.act")\n'
        self.assertEqual(self._scan(source)[0]["imported"], "wender.act")
        mutant = _mutated_function("scan_plan_act_imports", mutate_drop_import_aliases)
        self.assertEqual(self._scan(source, mutant), [])
        self.assertEqual(self._scan(source.replace("wender.act", "json")), [])
        self.assertTrue(self._scan('import importlib as il\nil.import_module("wender.act")'))

    # @anchor:wa.tests.test_hosted_harden_r2_unit.HostedHardenR2Tests.test_bare_loader_calls_and_mutation
    def test_bare_loader_calls_and_mutation(self):
        mutant = _mutated_function("scan_plan_act_imports", mutate_drop_bare_loader_names)
        for loader in ("spec_from_file_location", "module_from_spec", "exec_module", "load_module", "SourceFileLoader"):
            module = "importlib.machinery" if loader == "SourceFileLoader" else "importlib.util"
            prefix = "" if loader in ("exec_module", "load_module") else f"from {module} import {loader}\n"
            source = prefix + f'{loader}("payload", "payload.py")\n'
            with self.subTest(loader=loader):
                self.assertEqual(self._scan(source)[0]["imported"], "wender.act.<dynamic_exec>")
                self.assertEqual(self._scan(source, mutant), [])

    # @anchor:wa.tests.test_hosted_harden_r2_unit.HostedHardenR2Tests.test_getattr_dynamic_execution_and_mutation
    def test_getattr_dynamic_execution_and_mutation(self):
        mutant = _mutated_function("scan_plan_act_imports", mutate_getattr_exec_as_import)
        for name in ("eval", "exec", "compile"):
            source = f'import builtins\ngetattr(builtins, "{name[:2]}"+"{name[2:]}")("__import__(\'wender.act\')")\n'
            with self.subTest(executor=name):
                self.assertEqual(self._scan(source)[0]["imported"], "wender.act.<dynamic_exec>")
                self.assertEqual(self._scan(source, mutant), [])
        self.assertEqual(self._scan('import builtins\ngetattr(builtins, "len")("abc")'), [])

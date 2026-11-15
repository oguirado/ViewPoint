"""Packaging contract, synthetic controls, and isolated wheel-content checks."""
import ast
import fnmatch
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest import mock
import zipfile

import wender

ROOT = Path(__file__).resolve().parents[1]
_BASELINE = {
    "wender", "wender.act", "wender.learn", "wender.perceive",
    "wender.remember", "wender.scripts", "wender.state", "tools",
}


def _load_project(path):
    with path.open("rb") as stream:
        return tomllib.load(stream)


def _read_version(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assignments = []
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == "__version__"
                   for target in targets):
                assignments.append(node)
    if len(assignments) != 1:
        raise ValueError("version must have a single top-level assignment")
    value = assignments[0].value
    if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
        raise ValueError("version must be a literal string")
    return value.value


def _package_names(root):
    names = set()
    for branch in ("wender", "tools"):
        for marker in (root / branch).rglob("__init__.py"):
            directory = marker.parent
            ancestors = [directory]
            while ancestors[-1].parent != root:
                ancestors.append(ancestors[-1].parent)
            if all((parent / "__init__.py").is_file() for parent in ancestors):
                names.add(".".join(directory.relative_to(root).parts))
    return names


def _assert_contract(case, root):
    config = _load_project(root / "pyproject.toml")
    case.assertIn("build-system", config, "missing build-system")
    case.assertIn("project", config, "missing project")
    build = config["build-system"]
    project = config["project"]
    case.assertEqual(build.get("build-backend"), "setuptools.build_meta", "backend")
    case.assertEqual(build.get("requires"), ["setuptools>=68"], "build requirements")
    case.assertEqual(project.get("name"), "wender-agent", "distribution name")
    case.assertEqual(project.get("requires-python"), ">=3.11", "Python requirement")
    version = _read_version(root / "wender" / "__init__.py")
    case.assertEqual(project.get("version"), version, "version mismatch")
    case.assertEqual(version, "0.3.0", "release literal")
    # wender.learn imports numpy (demo_replay); a clean install must pull it (conductor check: clean-venv import of wender.learn.mw_profiles).
    case.assertEqual(project.get("dependencies"), ["numpy>=2.0"], "runtime dependencies")
    case.assertNotIn("optional-dependencies", project, "optional dependencies")
    case.assertNotIn("scripts", project, "console scripts")
    case.assertNotIn("gui-scripts", project, "GUI scripts")
    case.assertNotIn("entry-points", project, "entry points")
    case.assertNotIn("dynamic", project, "dynamic metadata")
    discovery = config["tool"]["setuptools"]["packages"]["find"]
    case.assertEqual(discovery.get("where"), ["."], "discovery root")
    case.assertIs(discovery.get("namespaces"), False, "regular packages")
    include = discovery.get("include")
    case.assertEqual(include, ["wender*", "tools*"], "discovery include")
    packages = _package_names(root)
    case.assertTrue(_BASELINE <= packages, "baseline packages")
    for package in packages:
        case.assertTrue(any(fnmatch.fnmatchcase(package, pattern) for pattern in include),
                        "uncovered package: " + package)
    return config, version


def _fixture(root):
    (root / "pyproject.toml").write_bytes((ROOT / "pyproject.toml").read_bytes())
    for name in _BASELINE:
        directory = root.joinpath(*name.split("."))
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "__init__.py").write_text("")
    (root / "wender" / "__init__.py").write_text('__version__ = "0.3.0"\n')


def _runtime_files(root):
    # Include every non-cache file, rather than guessing which extensions future
    # Path(__file__) or importlib.resources readers will use. Python source is
    # included too: adapter_protocol reads its own source at runtime.
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in (root / "wender").rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
        and path.suffix not in {".pyc", ".pyo"}
    }


def _assert_wheel_runtime_files(case, wheel, expected):
    with zipfile.ZipFile(wheel) as archive:
        members = set(archive.namelist())
        for name, content in expected.items():
            case.assertIn(name, members, "missing runtime file: " + name)
            case.assertEqual(archive.read(name), content,
                             "changed runtime file: " + name)


# @anchor:wa.learn.test_packaging_unit.TestPackaging
class TestPackaging(unittest.TestCase):
    # @anchor:wa.learn.test_packaging_unit.TestPackaging.test_built_wheel_runtime_files
    def test_built_wheel_runtime_files(self):
        expected = _runtime_files(ROOT)
        self.assertIn("wender/learn/cu_action.schema.json", expected)
        with tempfile.TemporaryDirectory(dir="/dev/shm") as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            shutil.copy2(ROOT / "pyproject.toml", source)
            for package in ("wender", "tools"):
                shutil.copytree(ROOT / package, source / package,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            output = root / "dist"
            output.mkdir()
            built = subprocess.run(
                [sys.executable, "-c",
                 "from setuptools.build_meta import build_wheel; "
                 "import sys; build_wheel(sys.argv[1])", str(output)],
                cwd=source, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, timeout=120, check=False)
            self.assertEqual(built.returncode, 0, built.stdout[-6000:])
            wheels = list(output.glob("*.whl"))
            self.assertEqual(len(wheels), 1, built.stdout[-6000:])
            wheel = wheels[0]
            _assert_wheel_runtime_files(self, wheel, expected)
            with zipfile.ZipFile(wheel) as archive:
                contents = {name: archive.read(name) for name in archive.namelist()}
            # Exercise the identical wheel validator on a genuine wheel with
            # each data asset independently removed (and on an intact control).
            for name in sorted(expected):
                if name.endswith(".py"):
                    continue
                with self.subTest(missing=name):
                    _assert_wheel_runtime_files(self, wheel, expected)
                    mutant = root / "missing.whl"
                    with zipfile.ZipFile(mutant, "w") as archive:
                        for member, content in contents.items():
                            if member != name:
                                archive.writestr(member, content)
                    with self.assertRaisesRegex(AssertionError, "missing runtime file"):
                        _assert_wheel_runtime_files(self, mutant, expected)
                    with zipfile.ZipFile(mutant, "w") as archive:
                        for member, content in contents.items():
                            archive.writestr(member, content + b"corrupt" if member == name
                                             else content)
                    with self.assertRaisesRegex(AssertionError, "changed runtime file"):
                        _assert_wheel_runtime_files(self, mutant, expected)

    # @anchor:wa.learn.test_packaging_unit.TestPackaging.test_real_contract
    def test_real_contract(self):
        config, version = _assert_contract(self, ROOT)
        self.assertEqual(config["project"]["version"], wender.__version__)
        self.assertEqual(version, wender.__version__)

    # @anchor:wa.learn.test_packaging_unit.TestPackaging.test_contract_rejections
    def test_contract_rejections(self):
        mutations = [
            ("missing build", '[build-system]', '[missing-build]', "missing build-system"),
            ("missing project", '[project]', '[missing-project]', "missing project"),
            ("backend", '"setuptools.build_meta"', '"other.backend"', "backend"),
            ("requirement", '["setuptools>=68"]', '[]', "build requirements"),
            ("name", '"wender-agent"', '"other"', "distribution name"),
            ("python", '">=3.11"', '">=3.12"', "Python requirement"),
            ("metadata version", 'version = "0.3.0"', 'version = "9.9.9"', "version mismatch"),
            ("dependencies", 'dependencies = ["numpy>=2.0"]', 'dependencies = ["numpy>=2.0", "external"]', "runtime dependencies"),
            ("no numpy", 'dependencies = ["numpy>=2.0"]', 'dependencies = []', "runtime dependencies"),
            ("optional", 'dependencies = ["numpy>=2.0"]', 'dependencies = ["numpy>=2.0"]\noptional-dependencies = {}', "optional dependencies"),
            ("scripts", 'dependencies = ["numpy>=2.0"]', 'dependencies = ["numpy>=2.0"]\nscripts = {}', "console scripts"),
            ("gui", 'dependencies = ["numpy>=2.0"]', 'dependencies = ["numpy>=2.0"]\ngui-scripts = {}', "GUI scripts"),
            ("entries", 'dependencies = ["numpy>=2.0"]', 'dependencies = ["numpy>=2.0"]\nentry-points = {}', "entry points"),
            ("dynamic", 'dependencies = ["numpy>=2.0"]', 'dependencies = ["numpy>=2.0"]\ndynamic = []', "dynamic metadata"),
            ("tools omission", '["wender*", "tools*"]', '["wender*"]', "discovery include"),
            ("narrow wender", '["wender*", "tools*"]', '["wender", "tools*"]', "discovery include"),
            ("root", 'where = ["."]', 'where = ["src"]', "discovery root"),
            ("namespace", 'namespaces = false', 'namespaces = true', "regular packages"),
        ]
        for label, before, after, diagnostic in mutations:
            with self.subTest(label=label), tempfile.TemporaryDirectory(dir="/dev/shm") as temp:
                root = Path(temp)
                _fixture(root)
                _assert_contract(self, root)
                path = root / "pyproject.toml"
                text = path.read_text()
                self.assertIn(before, text)
                path.write_text(text.replace(before, after, 1))
                with self.assertRaisesRegex(AssertionError, diagnostic):
                    _assert_contract(self, root)

    # @anchor:wa.learn.test_packaging_unit.TestPackaging.test_initializer_versions
    def test_initializer_versions(self):
        for source, diagnostic in [
            ('__version__ = "0.0.0"', "version mismatch"),
            ('__version__ = "0.3.0"\n__version__ = "0.3.0"', "single top-level"),
            ('__version__ = str(3)', "literal string"),
            ('pass', "single top-level"),
        ]:
            with self.subTest(source=source), tempfile.TemporaryDirectory(dir="/dev/shm") as temp:
                root = Path(temp)
                _fixture(root)
                _assert_contract(self, root)
                (root / "wender" / "__init__.py").write_text(source)
                error = AssertionError if "mismatch" in diagnostic else ValueError
                with self.assertRaisesRegex(error, diagnostic):
                    _assert_contract(self, root)
        with tempfile.TemporaryDirectory(dir="/dev/shm") as temp:
            root = Path(temp)
            _fixture(root)
            _assert_contract(self, root)
            path = root / "pyproject.toml"
            path.write_text(path.read_text().replace('version = "0.3.0"', 'version = "9.9.9"'))
            (root / "wender/__init__.py").write_text('__version__ = "9.9.9"')
            with self.assertRaisesRegex(AssertionError, "release literal"):
                _assert_contract(self, root)

    # @anchor:wa.learn.test_packaging_unit.TestPackaging.test_missing_and_malformed_toml
    def test_missing_and_malformed_toml(self):
        with tempfile.TemporaryDirectory(dir="/dev/shm") as temp:
            root = Path(temp)
            _fixture(root)
            _assert_contract(self, root)
            path = root / "pyproject.toml"
            path.write_text("[project\n")
            with self.assertRaises(tomllib.TOMLDecodeError):
                _assert_contract(self, root)
            path.unlink()
            with self.assertRaises(FileNotFoundError):
                _assert_contract(self, root)

    # @anchor:wa.learn.test_packaging_unit.TestPackaging.test_regular_package_inventory
    def test_regular_package_inventory(self):
        with tempfile.TemporaryDirectory(dir="/dev/shm") as temp:
            root = Path(temp)
            _fixture(root)
            for relative in ("wender/future/child", "wender/namespace/child"):
                directory = root / relative
                directory.mkdir(parents=True)
                (directory / "__init__.py").write_text("")
            (root / "wender/future/__init__.py").write_text("")
            _assert_contract(self, root)
            with mock.patch.object(fnmatch, "fnmatchcase", return_value=False):
                with self.assertRaisesRegex(AssertionError, "uncovered package"):
                    _assert_contract(self, root)
            self.assertIn("wender.future.child", _package_names(root))
            self.assertNotIn("wender.namespace.child", _package_names(root))
            (root / "wender/act/__init__.py").unlink()
            with self.assertRaisesRegex(AssertionError, "baseline packages"):
                _assert_contract(self, root)


if __name__ == "__main__":
    unittest.main()

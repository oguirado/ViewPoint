"""Contracts for tools/refresh_catalogue.py ownership boundary and owner --check delegation."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
ROUTER_CAT_FILES = ("failures.tsv", "routes.tsv", "symbols.jsonl", "thresholds.tsv")
EXPECTED_REFRESH_FILES = [
    "failures.tsv",
    "layers.json",
    "measurements.json",
    "routes.tsv",
    "subjects.json",
    "symbols.jsonl",
    "thresholds.tsv",
]


def _copy_component(dst_parent):
    dst = Path(dst_parent) / "component"
    shutil.copytree(
        ROOT,
        dst,
        ignore=shutil.ignore_patterns("build", "__pycache__", ".wavemind"),
    )
    return dst


def _run_py(script, *args, cwd=ROOT):
    return subprocess.run(
        [sys.executable, "-B", str(script), *args],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(cwd),
    )


class RefreshCatalogueOwnershipTests(unittest.TestCase):
    def test_worktree_refresh_catalogue_check_returns_zero(self):
        res = _run_py(ROOT / "tools/refresh_catalogue.py", "--check")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        payload = json.loads(res.stdout)
        self.assertEqual(payload.get("rc"), 0)
        self.assertEqual(payload.get("code"), "OK")

    def test_write_mode_preserves_router_catalogue_and_owner_check(self):
        with tempfile.TemporaryDirectory(prefix="bvqa_refresh_") as tmp:
            copy_root = _copy_component(tmp)
            rc_dir = copy_root / "catalogue/router/catalogue"
            before = {name: (rc_dir / name).read_bytes() for name in ROUTER_CAT_FILES}

            write_res = _run_py(copy_root / "tools/refresh_catalogue.py", "--root", str(copy_root), cwd=copy_root)
            self.assertEqual(write_res.returncode, 0, write_res.stdout + write_res.stderr)
            write_payload = json.loads(write_res.stdout)
            self.assertEqual(write_payload.get("files"), EXPECTED_REFRESH_FILES)

            after = {p.name: p.read_bytes() for p in sorted(rc_dir.iterdir()) if p.is_file()}
            self.assertEqual(set(after), set(before))
            for name in ROUTER_CAT_FILES:
                self.assertEqual(after[name], before[name], f"router/catalogue/{name} modified by refresh_catalogue.py")

            owner_res = _run_py(
                copy_root / "catalogue/router/build_router_catalogue.py",
                "--root",
                str(copy_root),
                "--check",
                cwd=copy_root,
            )
            self.assertEqual(owner_res.returncode, 0, owner_res.stdout + owner_res.stderr)

            check_res = _run_py(
                copy_root / "tools/refresh_catalogue.py",
                "--root",
                str(copy_root),
                "--check",
                cwd=copy_root,
            )
            self.assertEqual(check_res.returncode, 0, check_res.stdout + check_res.stderr)

    def test_check_mode_delegates_to_owner_and_fails_when_router_stale(self):
        with tempfile.TemporaryDirectory(prefix="bvqa_refresh_stale_") as tmp:
            copy_root = _copy_component(tmp)
            routes_path = copy_root / "catalogue/router/catalogue/routes.tsv"
            routes_path.write_text(routes_path.read_text(encoding="utf-8") + "stale\tstale.anchor\tR2\n", encoding="utf-8")

            check_res = _run_py(
                copy_root / "tools/refresh_catalogue.py",
                "--root",
                str(copy_root),
                "--check",
                cwd=copy_root,
            )
            self.assertNotEqual(check_res.returncode, 0, check_res.stdout + check_res.stderr)
            self.assertEqual(check_res.returncode, 4, check_res.stdout + check_res.stderr)
            payload = json.loads(check_res.stdout)
            self.assertEqual(payload.get("router_rc"), 4)
            self.assertIn("catalogue/router/catalogue/routes.tsv", payload.get("stale", []))

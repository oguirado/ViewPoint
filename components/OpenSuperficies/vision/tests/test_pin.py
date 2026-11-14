import json
import os
import unittest
from pathlib import Path

from vision.tests.helpers import FAKE_FILES, REPO, SCRATCH, make_wheel, run_py, sha256_file

VE = "/home/user/Documentos/vision-engine"


SHIPPED_CFG = REPO / "config" / "vision.json"
SHIPPED_VEN = REPO / "vision" / "VENDORED.json"


def _activate(wheel, sha, extra="", cfg=SHIPPED_CFG, vend=SHIPPED_VEN, pre="", pythonpath=None, post=""):
    code = (
        "import sys\n%s\nfrom vision import pin\n%s\n"
        "pin._activate_for_test(%r, %r, %r, %r)\n"
        "import vadapt.ramws as r\nprint('OK', r.__file__)\n%s\n"
    ) % (pre, extra, str(wheel), sha, str(cfg) if cfg else None, str(vend) if vend else None, post)
    return run_py(code, pythonpath=pythonpath)


class PinTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wheel = make_wheel(SCRATCH / "fake" / "vision_engine-0.0.0-py3-none-any.whl", FAKE_FILES)
        cls.sha = sha256_file(cls.wheel)

    def test_pin_ok_imports_from_wheel(self):
        r = _activate(self.wheel, self.sha)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(str(self.wheel), r.stdout)

    def test_pin_wrong_sha_refuses(self):
        r = _activate(self.wheel, "0" * 64)
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertIn("E_PIN", r.stdout + r.stderr)

    def test_pin_missing_wheel_refuses(self):
        r = _activate(SCRATCH / "nope.whl", self.sha)
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertIn("E_PIN", r.stdout + r.stderr)

    def test_pin_live_checkout_on_path_refuses(self):
        r = _activate(self.wheel, self.sha, pre="sys.path.insert(0, %r)" % VE)
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertIn("E_PIN", r.stdout + r.stderr)

    def test_pin_preimported_module_refuses(self):
        d = SCRATCH / "evil"
        (d / "vadapt").mkdir(parents=True, exist_ok=True)
        (d / "vadapt" / "__init__.py").write_text("", encoding="utf-8")
        r = _activate(self.wheel, self.sha, pre="sys.path.insert(0, %r)\nimport vadapt\nsys.path.remove(%r)" % (str(d), str(d)))
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertIn("E_PIN", r.stdout + r.stderr)

    def test_pin_config_repin_refuses(self):
        cfg = SCRATCH / "cfg_repin.json"
        cfg.write_text(json.dumps({"mode": "shadow", "engine_path": str(self.wheel),
                                   "pin": {"tag": "ve-sealed-1-law4", "commit": "0" * 40, "wheel_sha256": "1" * 64}}), encoding="utf-8")
        r = _activate(self.wheel, self.sha, cfg=cfg)
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertIn("E_PIN", r.stdout + r.stderr)

    def test_pin_vendored_repin_refuses(self):
        base = json.loads(SHIPPED_VEN.read_text(encoding="utf-8"))
        for k, v in (("wheel_sha256", "1" * 64), ("commit", "0" * 40), ("tag", "ve-sealed-9")):
            ven = SCRATCH / ("ven_%s.json" % k)
            ven.write_text(json.dumps(dict(base, **{k: v})), encoding="utf-8")
            r = _activate(self.wheel, self.sha, vend=ven)
            self.assertEqual(r.returncode, 5, (k, r.stdout, r.stderr))
            self.assertIn("E_PIN", r.stdout)

    def test_pin_config_each_field_repin_refuses(self):
        base = json.loads(SHIPPED_CFG.read_text(encoding="utf-8"))
        for k, v in (("wheel_sha256", "1" * 64), ("commit", "0" * 40), ("tag", "ve-sealed-9")):
            cfg = SCRATCH / ("cfg_%s.json" % k)
            cfg.write_text(json.dumps(dict(base, pin=dict(base["pin"], **{k: v}))), encoding="utf-8")
            r = _activate(self.wheel, self.sha, cfg=cfg)
            self.assertEqual(r.returncode, 5, (k, r.stdout, r.stderr))

    def test_pin_records_none_refused(self):
        for kw in ({"cfg": None}, {"vend": None}):
            r = _activate(self.wheel, self.sha, **kw)
            self.assertEqual(r.returncode, 5, (kw, r.stdout, r.stderr))
            self.assertIn("E_PIN", r.stdout)

    def test_pin_activate_rejects_override(self):
        r = run_py("from vision import pin\ntry:\n    pin.activate(wheel='/x', expected_sha='0')\nexcept TypeError:\n    print('TYPEERROR')\n")
        self.assertIn("TYPEERROR", r.stdout, r.stderr)
        r = run_py("from vision import pin\nimport inspect\nprint(len(inspect.signature(pin.activate).parameters))\n")
        self.assertEqual(r.stdout.strip(), "0", r.stderr)

    def test_pin_pythonpath_copy_stripped(self):
        cp = SCRATCH / "pp_copy"
        (cp / "vadapt").mkdir(parents=True, exist_ok=True)
        (cp / "vadapt" / "__init__.py").write_text("", encoding="utf-8")
        sp = SCRATCH / "fake_env" / "lib" / "site-packages"
        sp.mkdir(parents=True, exist_ok=True)
        post = ("import os\nreal=[os.path.realpath(e) for e in sys.path]\n"
                "print('PATH', real)\nassert os.path.realpath(%r) not in real\nassert os.path.realpath(%r) not in real\n"
                "assert real[0]==os.path.realpath(%r)\nassert real[1]==os.path.realpath(%r)\n"
                "assert all(e in (real[0], real[1]) or e.endswith('.zip') or e.startswith(os.path.realpath(os.path.dirname(os.__file__))) for e in real), real"
                ) % (str(cp), str(sp), str(self.wheel), str(REPO))
        r = _activate(self.wheel, self.sha, pythonpath=str(cp) + os.pathsep + str(sp), post=post)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_pin_site_dirs_under_stdlib_stripped(self):
        std = SCRATCH / "fake_std"
        dirs = [std / "site-packages", std / "dist-packages", std / "lib-dynload"]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)
        pre = ("import sysconfig\n_gp = sysconfig.get_paths\n"
               "sysconfig.get_paths = lambda *a, **k: dict(_gp(*a, **k), stdlib=%r, platstdlib=%r)") % (str(std), str(std))
        post = ("import os\nreal=[os.path.realpath(e) for e in sys.path]\n"
                "assert os.path.realpath(%r) in real\nassert os.path.realpath(%r) not in real\nassert os.path.realpath(%r) not in real\n"
                ) % (str(dirs[2]), str(dirs[0]), str(dirs[1]))
        r = _activate(self.wheel, self.sha, pre=pre, post=post, pythonpath=os.pathsep.join(str(d) for d in dirs))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_pin_loaded_submodule_outside_wheel_refuses(self):
        pre = "import types\nm = types.ModuleType('visioncore.rogue')\nm.__file__ = '/tmp/rogue.py'\nsys.modules['visioncore.rogue'] = m"
        r = _activate(self.wheel, self.sha, pre=pre)
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertIn("E_PIN", r.stdout)

    def test_pin_unforeseen_exception_never_rc1(self):
        r = run_py("from vision import pin\nraise KeyError('boom')\n")
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertIn("E_GATE", r.stdout)
        r = run_py("from vision import pin\nraise MemoryError()\n")
        self.assertEqual(r.returncode, 5, r.stderr)

    def test_pin_shipped_config_matches_constants(self):
        from vision import pin
        cfg = json.loads((REPO / "config" / "vision.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["mode"], "shadow")
        self.assertEqual(cfg["pin"]["tag"], "ve-sealed-1-law4")
        self.assertEqual(cfg["pin"]["commit"], pin.EXPECTED_COMMIT)
        self.assertEqual(cfg["pin"]["wheel_sha256"], pin.EXPECTED_WHEEL_SHA256)
        v = json.loads((REPO / "vision" / "VENDORED.json").read_text(encoding="utf-8"))
        self.assertEqual(v["wheel_sha256"], pin.EXPECTED_WHEEL_SHA256)
        self.assertEqual(pin.EXPECTED_COMMIT, "2612b3acd67c49234adfaa77ab2b3803d5a81cb5")
        self.assertEqual(pin.EXPECTED_WHEEL_SHA256, "5e2395d56709274f21123bb5ef714adf52f0cf58f78d4f4938da736c52a7a4a5")

    def test_pin_keeps_stdlib_on_path(self):
        r = _activate(self.wheel, self.sha, extra="import json, hashlib")
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()

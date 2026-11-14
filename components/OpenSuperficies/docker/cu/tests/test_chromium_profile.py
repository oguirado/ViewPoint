"""Lane C: static checks of the Chromium capsule profile (flags file, bundled fontconfig, smoke plan). No browser, no X."""
import json
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
OS = os.path.normpath(os.path.join(HERE, "..", "..", ".."))
FLAGS = os.path.join(OS, "docker", "cu", "profiles", "chromium.flags")
LAUNCHER = os.path.join(OS, "docker", "cu", "launch-chromium-cu.sh")
SCENES = os.path.join(OS, "bench", "wl", "scenes", "v2")
PLAN = os.path.join(OS, "bench", "cu", "chromium_smoke", "plan.json")
ENUM = ("namespace", "suid", "nosandbox")


def parse_flags():
    """Closed grammar, one record per line: `default_sandbox M` | `sandbox M [FLAG]` | `flag GROUP FLAG`."""
    recs = []
    with open(FLAGS, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            recs.append((n, line.split()))
    return recs


class FlagsFile(unittest.TestCase):
    def test_flags_file_closed_enum_sandbox_mode(self):
        recs = parse_flags()
        self.assertTrue(recs, "chromium.flags is empty")
        modes = [r[1] for _, r in recs if r[0] == "sandbox"]
        self.assertEqual(sorted(modes), sorted(ENUM))
        defaults = [r for _, r in recs if r[0] == "default_sandbox"]
        self.assertEqual(len(defaults), 1)
        self.assertEqual(defaults[0][1], "nosandbox")  # [H] until S-LIVE measures namespace/suid
        for n, r in recs:
            self.assertIn(r[0], ("default_sandbox", "sandbox", "flag"), "line %d: unknown record" % n)
            if r[0] == "flag":
                self.assertEqual(len(r), 3, "line %d" % n)
                self.assertRegex(r[1], r"^[a-z]+$")
                self.assertRegex(r[2], r"^--[a-z0-9-]+(=[^\s]+)?$")
        flags = [r[2] for _, r in recs if r[0] == "flag"] + [x for _, r in recs if r[0] == "sandbox" for x in r[2:]]
        self.assertFalse([x for x in flags if "remote-debugging" in x])
        self.assertEqual(len(flags), len(set(flags)), "duplicate flag")
        for need in ("--kiosk", "--disable-dev-shm-usage", "--force-device-scale-factor=1"):
            self.assertIn(need, flags)

    def test_launcher_has_no_cdp_and_umask(self):
        with open(LAUNCHER, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("umask 027", src)
        self.assertIn("set -euo pipefail", src)
        self.assertNotIn("remote-debugging", src)
        self.assertNotRegex(src, r"(^|\s)exit 1(\s|$)")


class Fontconfig(unittest.TestCase):
    def test_fontconfig_points_only_to_bundled_fonts(self):
        p = os.path.join(SCENES, "fonts", "fonts.conf")
        if not os.path.exists(p):
            self.skipTest("fonts.conf is lane W's file; absent at this pulse")
        with open(p, encoding="utf-8") as f:
            txt = f.read()
        dirs = re.findall(r"<dir[^>]*>([^<]+)</dir>", txt)
        self.assertTrue(dirs, "no <dir> in fonts.conf")
        for d in dirs:
            self.assertEqual(d.strip(), "/opt/cu/scenes/v2/fonts", "non-bundled font dir %r" % d)
        self.assertNotIn("<include", txt)  # no system config pulled in
        self.assertNotIn("/usr/share/fonts", txt)
        with open(LAUNCHER, encoding="utf-8") as f:
            self.assertIn("fonts/fonts.conf", f.read())


class SmokePlan(unittest.TestCase):
    def test_smoke_plan_assert_per_flag_group(self):
        with open(PLAN, encoding="utf-8") as f:
            plan = json.load(f)
        self.assertEqual(plan["schema"], "os.cu.chromium-smoke/v1")
        self.assertEqual(plan["sandbox_order"], ["namespace", "suid", "nosandbox"])
        groups = sorted({r[1] for _, r in parse_flags() if r[0] == "flag"})
        self.assertTrue(groups)
        self.assertEqual(sorted(plan["asserts_per_flag_group"]), groups)
        for g, a in plan["asserts_per_flag_group"].items():
            self.assertTrue(a and all(isinstance(s, str) and s for s in a), g)
        self.assertEqual(plan["static_snap_repeats"], 2)
        self.assertEqual(plan["log"], ["xdpyinfo -ext MIT-SHM"])

        def ints_only(o):
            if isinstance(o, float):
                self.fail("float in plan")
            if isinstance(o, dict):
                [ints_only(v) for v in o.values()]
            elif isinstance(o, list):
                [ints_only(v) for v in o]
        ints_only(plan)


if __name__ == "__main__":
    unittest.main()

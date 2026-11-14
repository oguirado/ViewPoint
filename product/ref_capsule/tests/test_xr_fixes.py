#!/usr/bin/env python3
"""Regression tests for the xr-capsule-auction cross-review (C1-C6, C11, C12).

Each case is a spec mutation that validated with 0 findings at 8408bbf.
Run:  python3 -I -B tests/test_xr_fixes.py
"""
import copy
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import render as R  # noqa: E402
import validate as V  # noqa: E402

SPEC_PATH = os.path.join(ROOT, "specs", "blender_4agents.json")


def good():
    with open(SPEC_PATH, encoding="utf-8") as f:
        return json.load(f)


def C(spec, role, i=0):
    return [c for c in spec["containers"] if c["role"] == role][i]


def rules(spec):
    return sorted({f["rule"] for f in V.validate(spec)})


class XrFixes(unittest.TestCase):
    def test_reference_spec_still_green(self):
        self.assertEqual(V.validate(good()), [])

    def test_c1_mount_dst_option_injection(self):
        for tail in (",type=bind,src=/home/user/.ssh", ",readonly", "=x", " x", '"x', ":x"):
            s = good()
            C(s, "agent")["mounts"][0]["dst"] += tail
            self.assertIn("V-01", rules(s), tail)

    def test_c2_volume_key_option_injection(self):
        s = good()
        s["volumes"]["ckpt,type=bind,src=/home/user"] = s["volumes"].pop("ckpt")
        for c in s["containers"]:
            for mt in c["mounts"]:
                if mt["volume"] == "ckpt":
                    mt["volume"] = "ckpt,type=bind,src=/home/user"
        self.assertIn("V-01", rules(s))

    def test_c3_container_netns_join_refused(self):
        for net in ("container:vpref-blender-program", "host", "ns:/proc/1/ns/net", "other-net"):
            s = good()
            s["networks"].append(net)
            C(s, "agent")["network"] = net
            self.assertIn("N-01", rules(s), net)

    def test_c3_only_one_declared_network(self):
        s = good()
        s["networks"].append("vpref-second")
        self.assertIn("N-01", rules(s))

    def test_c4_scratch_bind_aliases_rt(self):
        rt = good()["volumes"]["rt"]["backing"]["src"]
        for src in (rt, rt + "/intents", os.path.dirname(rt)):
            s = good()
            s["volumes"]["scr"] = {"kind": "scratch", "backing": {"type": "bind", "src": src}, "size_mib": 8}
            C(s, "agent")["mounts"].append({"volume": "scr", "dst": "/scratch", "mode": "rw"})
            self.assertIn("V-01", rules(s), src)

    def test_c4_disjoint_scratch_bind_still_allowed(self):
        s = good()
        s["volumes"]["scr"] = {"kind": "scratch", "size_mib": 8,
                               "backing": {"type": "bind", "src": "/dev/shm/vqa-cu/ref-capsule/scratch"}}
        C(s, "agent")["mounts"].append({"volume": "scr", "dst": "/scratch", "mode": "rw"})
        self.assertNotIn("V-01", rules(s))

    def test_c5_secret_env_keys_and_values(self):
        for k, v in (("GITHUB_PAT", "x"), ("DB_PASSWD", "x"), ("ANTHROPIC_AUTH", "x"),
                     ("SESSION_COOKIE", "x"), ("OPENAI", "sk-" + "proj-abcdefghijklmnopqrstuvwx"),
                     ("FOO", "sk-" + "ant-api03-abcdefghijklmnopqrstuvwx"), ("FOO", "ghp_" + "a" * 36),
                     ("FOO", "AKIA" + "A" * 16), ("FOO", 5)):
            s = good()
            C(s, "agent")["env"][k] = v
            self.assertIn("H-01", rules(s), (k, v))

    def test_c5_docker_host_refused_on_every_role(self):
        for role in ("program", "broker", "agent"):
            s = good()
            C(s, role)["env"]["DOCKER_HOST"] = "unix:///var/run/docker.sock"
            self.assertIn("H-01", rules(s), role)

    def test_c6_unknown_keys_fail_closed(self):
        for k, v in (("cgroup_parent", "x"), ("cgroupns_mode", "host"), ("security_opt", ["seccomp=unconfined"]),
                     ("pid", "host"), ("network_mode", "host"), ("group_add", ["docker"])):
            s = good()
            C(s, "agent")[k] = v
            self.assertEqual(rules(s), ["S-00"], k)
        s = good()
        s["surprise"] = 1
        self.assertEqual(rules(s), ["S-00"])

    def test_c11_tmpfs_dst_option_injection(self):
        s = good()
        C(s, "agent")["tmpfs"][0]["dst"] = "/tmp:exec,suid,size=1m"
        self.assertIn("V-01", rules(s))

    def test_c12_non_string_network_is_a_shape_error(self):
        for bad in ({}, [], 3):
            s = good()
            s["networks"].append(bad)
            self.assertEqual(rules(s), ["S-00"])
        s = good()
        C(s, "agent")["network"] = {}
        self.assertEqual(rules(s), ["S-00"])

    def test_a3_each_agent_has_its_own_bid_directory_and_only_its_own(self):
        s = good()
        for c in s["containers"]:
            if c["role"] == "agent":
                own = "bids/%s" % c["agent_id"]
                got = [mt for mt in c["mounts"] if mt.get("subpath", "").startswith("bids")]
                self.assertEqual([(mt["subpath"], mt["mode"]) for mt in got], [(own, "rw")], c["name"])
        a0, a1 = C(s, "agent", 0), C(s, "agent", 1)
        for sp in ("bids", "bids/%s" % a1["agent_id"]):
            t = copy.deepcopy(s)
            C(t, "agent", 0)["mounts"].append({"volume": "rt", "subpath": sp, "dst": "/rt/x", "mode": "rw"})
            self.assertIn("A-01", rules(t), sp)
        t = copy.deepcopy(s)
        C(t, "agent", 0)["mounts"] = [mt for mt in a0["mounts"] if not mt.get("subpath", "").startswith("bids")]
        self.assertIn("A-01", rules(t))

    def test_render_refuses_an_injected_field_even_without_the_validator(self):
        s = good()
        c = C(s, "agent")
        c["mounts"][0]["dst"] += ",type=bind,src=/home/user/.ssh"
        with self.assertRaises(ValueError):
            R.run_argv(s, c)

    def test_render_mount_args_have_exactly_the_expected_fields(self):
        """Structural backstop: every --mount value parses to type,src,dst[,readonly|volume-subpath]."""
        plan = json.dumps(R.run_argv(good(), C(good(), "agent")))
        for c in good()["containers"]:
            argv = R.run_argv(good(), c)
            for i, a in enumerate(argv):
                if a == "--mount":
                    keys = [p.split("=", 1)[0] for p in argv[i + 1].split(",")]
                    self.assertEqual(keys[:3], ["type", "src", "dst"], argv[i + 1])
                    self.assertTrue(set(keys[3:]) <= {"readonly", "volume-subpath"}, argv[i + 1])
        self.assertIn("--mount", plan)


if __name__ == "__main__":
    unittest.main(verbosity=1)

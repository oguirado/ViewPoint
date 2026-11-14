"""Tests for the capsule spec validator, the plan renderer and the observed-state check.

Run:  python3 -I -m unittest discover -s tests -v      (from product/ref_capsule)

Every rule has at least one negative control: a mutation of the reference spec that
must go RED with exactly that rule, and that goes GREEN again when the rule is
skipped. The second half is the proof that the test depends on the check: delete the
check and the bad spec passes.
"""
import copy
import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import observe as O  # noqa: E402
import render as R  # noqa: E402
import validate as V  # noqa: E402

SPEC_PATH = os.path.join(ROOT, "specs", "blender_4agents.json")
MIB = 1024 * 1024


def good():
    with open(SPEC_PATH, encoding="utf-8") as f:
        return json.load(f)


def C(spec, role, i=0):
    return [c for c in spec["containers"] if c["role"] == role][i]


def rules(findings):
    return sorted({f["rule"] for f in findings})


# ---- mutations: name -> (expected rule ids, mutator) ------------------------------
def m(expected, fn):
    return (expected if isinstance(expected, list) else [expected], fn)


def _agent_gets_xsock(s):
    C(s, "agent")["mounts"].append({"volume": "xsock", "dst": "/tmp/.X11-unix", "mode": "ro"})


def _agent_xsock_other_dst(s):
    C(s, "agent")["mounts"].append({"volume": "xsock", "dst": "/x", "mode": "ro"})


def _agent_shares_program_netns(s):
    C(s, "agent")["network"] = "container:vpref-blender-program"


def _swap_unset(s):
    del C(s, "agent")["memory_swap_mib"]


def _program_whole_rt(s):
    c = C(s, "program")
    c["mounts"] = [x for x in c["mounts"] if x["volume"] != "rt"] + [{"volume": "rt", "dst": "/rt", "mode": "rw"}]


def _agent_other_intents(s):
    C(s, "agent")["mounts"][0]["subpath"] = "intents/props-agent"


def _agent_inbox(s):
    C(s, "agent")["mounts"].append({"volume": "rt", "subpath": "inbox", "dst": "/rt/inbox", "mode": "rw"})


def _ckpt_on_tmpfs(s):
    C(s, "program")["tmpfs"].append({"dst": "/ckpt", "size_mib": 64})


MUTATIONS = {
    # S-01
    "name_without_prefix": m("S-01", lambda s: C(s, "agent").__setitem__("name", "blender-modeler")),
    "duplicate_name": m("S-01", lambda s: C(s, "agent", 1).__setitem__("name", C(s, "agent")["name"])),
    # T-01
    "duplicate_agent_id": m(["A-01", "T-01"], lambda s: C(s, "agent", 1).__setitem__("agent_id", "modeler-agent")),
    "no_agents": m("T-01", lambda s: s.__setitem__("containers", s["containers"][:2])),
    "bad_agent_id": m(["A-01", "T-01"], lambda s: C(s, "agent").__setitem__("agent_id", "../x")),
    # T-02
    "broker_depends_on_agent": m("T-02", lambda s: C(s, "broker")["depends_on"].append("vpref-blender-look-agent")),
    "agent_without_broker_dep": m("T-02", lambda s: C(s, "agent").__setitem__("depends_on", [])),
    # I-01
    "image_by_tag_only": m("I-01", lambda s: C(s, "program").__setitem__("image", "blender:latest")),
    # H-01
    "root_user": m("H-01", lambda s: C(s, "agent").__setitem__("user", "0:0")),
    "privileged_sidecar": m("H-01", lambda s: C(s, "agent").__setitem__("privileged", True)),
    "writable_rootfs": m("H-01", lambda s: C(s, "program").__setitem__("read_only", False)),
    "cap_add": m("H-01", lambda s: C(s, "broker").__setitem__("cap_add", ["SYS_PTRACE"])),
    "secret_in_env": m("H-01", lambda s: C(s, "agent")["env"].__setitem__("GEMINI_API_KEY", "x")),
    "kvm_device": m("H-01", lambda s: C(s, "program").__setitem__("devices", ["/dev/kvm"])),
    # N-01
    "agent_host_network": m("N-01", lambda s: C(s, "agent").__setitem__("network", "host")),
    "agent_shares_program_netns": m("N-01", _agent_shares_program_netns),
    "agent_joins_program_pidns": m("N-01", lambda s: C(s, "agent").__setitem__("pid_mode", "container:vpref-blender-program")),
    "agent_host_ipc": m("N-01", lambda s: C(s, "agent").__setitem__("ipc_mode", "host")),
    "program_on_network": m("N-01", lambda s: C(s, "program").__setitem__("network", "vpref-egress")),
    "volumes_from": m("N-01", lambda s: C(s, "agent").__setitem__("volumes_from", ["vpref-blender-broker"])),
    # V-01
    "bind_display_1": m("V-01", lambda s: s["volumes"]["xsock"].__setitem__("backing", {"type": "bind", "src": "/tmp/.X11-unix"})),
    "bind_hybrid": m("V-01", lambda s: s["volumes"]["rt"]["backing"].__setitem__("src", "/dev/shm/vqa-cu/hybrid/rt")),
    "bind_swarm": m("V-01", lambda s: s["volumes"]["rt"]["backing"].__setitem__("src", "/dev/shm/vqa-cu/swarm")),
    "bind_dotdot": m("V-01", lambda s: s["volumes"]["rt"]["backing"].__setitem__("src", "/dev/shm/vqa-cu/ref/../swarm")),
    "bind_docker_sock": m("V-01", lambda s: s["volumes"]["rt"]["backing"].__setitem__("src", "/var/run/docker.sock")),
    # X-01
    "agent_gets_xsock": m("X-01", _agent_gets_xsock),
    "agent_gets_xsock_elsewhere": m("X-01", _agent_xsock_other_dst),
    "agent_display_env": m("X-01", lambda s: C(s, "agent")["env"].__setitem__("DISPLAY", ":99")),
    "agent_declares_input": m("X-01", lambda s: C(s, "agent").__setitem__("input", True)),
    "broker_without_input": m("X-01", lambda s: C(s, "broker").__setitem__("input", False)),
    "program_holds_input": m("X-01", lambda s: C(s, "program").__setitem__("input", True)),
    # A-01
    "program_whole_rt_rw": m("A-01", _program_whole_rt),
    "agent_writes_other_intents": m("A-01", _agent_other_intents),
    "agent_mounts_inbox": m("A-01", _agent_inbox),
    "agent_frames_rw": m("A-01", lambda s: C(s, "agent")["mounts"][1].__setitem__("mode", "rw")),
    # L-01
    "no_memory_limit": m("L-01", lambda s: C(s, "agent").pop("memory_mib")),
    "swap_unset": m("L-01", _swap_unset),
    "no_pids_limit": m("L-01", lambda s: C(s, "agent").pop("pids_limit")),
    "pids_unlimited": m("L-01", lambda s: C(s, "agent").__setitem__("pids_limit", -1)),
    "unsized_tmpfs": m("L-01", lambda s: C(s, "agent")["tmpfs"][0].pop("size_mib")),
    "tmpfs_above_memory": m("L-01", lambda s: C(s, "agent")["tmpfs"][0].__setitem__("size_mib", 4096)),
    "over_budget": m("L-01", lambda s: C(s, "broker").update(memory_mib=2048, memory_swap_mib=2048)),
    "budget_above_policy": m("L-01", lambda s: s["budget"].__setitem__("memory_mib", 64000)),
    "agent_above_policy": m("L-01", lambda s: (C(s, "agent").update(memory_mib=4096, memory_swap_mib=4096),
                                               C(s, "program").update(memory_mib=512, memory_swap_mib=512, shm_mib=64))),
    # HC-01
    "no_health": m("HC-01", lambda s: C(s, "agent").pop("health")),
    "program_pid_probe": m("HC-01", lambda s: C(s, "program")["health"].__setitem__("kind", "process_exists")),
    "health_shell_injection": m("HC-01", lambda s: C(s, "broker")["health"]["cmd"].append("; rm -rf /rt")),
    "health_timeout_ge_interval": m("HC-01", lambda s: C(s, "broker")["health"].__setitem__("timeout_s", 10)),
    # R-01
    "sigkill_stop": m("R-01", lambda s: C(s, "agent")["stop"].__setitem__("signal", "SIGKILL")),
    "restart_always": m("R-01", lambda s: C(s, "agent")["restart"].__setitem__("policy", "always")),
    "program_self_restart": m("R-01", lambda s: C(s, "program").__setitem__("restart", {"policy": "on-failure", "max": 3})),
    "grace_shorter_than_ckpt": m("R-01", lambda s: C(s, "program")["stop"].__setitem__("grace_s", 5)),
    "restart_without_epoch_bump": m("R-01", lambda s: s["rewind"].__setitem__("on_program_restart", "resume")),
    # RW-01
    "rung_docker_checkpoint": m("RW-01", lambda s: s["rewind"].__setitem__("rung", "C1")),
    "rung_docker_commit": m("RW-01", lambda s: s["rewind"].__setitem__("rung", "C2")),
    "rung_criu": m("RW-01", lambda s: s["rewind"].__setitem__("rung", "P")),
    "ring_does_not_fit": m("RW-01", lambda s: s["rewind"].__setitem__("ring", 8)),
    "agent_mounts_ckpt": m("RW-01", lambda s: C(s, "agent")["mounts"].append({"volume": "ckpt", "dst": "/ckpt", "mode": "ro"})),
    "ckpt_on_program_tmpfs": m("RW-01", _ckpt_on_tmpfs),
    "program_ckpt_readonly": m("RW-01", lambda s: C(s, "program")["mounts"][3].__setitem__("mode", "ro")),
}


class ValidatorTest(unittest.TestCase):
    def test_reference_spec_is_green(self):
        self.assertEqual(V.validate(good()), [])

    def test_every_rule_has_a_negative_control(self):
        covered = set()
        for exp, _ in MUTATIONS.values():
            covered.update(exp)
        self.assertEqual(sorted(covered), sorted(r[0] for r in V.RULES))

    def test_mutations_go_red_with_the_expected_rule(self):
        for name, (exp, fn) in sorted(MUTATIONS.items()):
            with self.subTest(mutation=name):
                s = good()
                fn(s)
                self.assertEqual(rules(V.validate(s)), sorted(exp), V.validate(s))

    def test_removing_the_check_lets_the_bad_spec_through(self):
        # Negative control of the test itself: with the rule skipped the same bad
        # spec is GREEN, so only that check stands between the mutation and a pass.
        for name, (exp, fn) in sorted(MUTATIONS.items()):
            with self.subTest(mutation=name):
                s = good()
                fn(s)
                self.assertEqual(V.validate(s, skip=exp), [])

    def test_shape_errors_are_typed_not_crashes(self):
        for bad in ([], {}, {"v": "vp-capsule/v0"}, "x", None):
            self.assertEqual(rules(V.validate(bad)), ["S-00"])
        for key in ("volumes", "containers", "rewind", "budget"):
            s = good()
            del s[key]
            self.assertEqual(rules(V.validate(s)), ["S-00"], key)
        s = good()
        s["containers"][0]["mounts"] = [{"volume": 1}]
        self.assertEqual(rules(V.validate(s)), ["S-00"])

    def test_mutations_do_not_leak_between_cases(self):
        a = good()
        b = copy.deepcopy(a)
        MUTATIONS["agent_gets_xsock"][1](a)
        self.assertEqual(V.validate(b), [])

    def test_cli_exit_codes(self):
        def run(*args):
            return subprocess.run([sys.executable, "-I", os.path.join(ROOT, "validate.py")] + list(args),
                                  capture_output=True, text=True, timeout=30)
        self.assertEqual(run(SPEC_PATH).returncode, 0)
        bad = os.path.join(HERE, "_tmp_bad_spec.json")
        try:
            s = good()
            _agent_gets_xsock(s)
            with open(bad, "w", encoding="utf-8") as f:
                json.dump(s, f)
            r = run(bad)
            self.assertEqual(r.returncode, 1)
            self.assertIn("X-01", r.stdout)
            self.assertIn("RED", r.stdout)
            with open(bad, "w", encoding="utf-8") as f:
                f.write("{not json")
            self.assertEqual(run(bad).returncode, 2)
        finally:
            if os.path.exists(bad):
                os.remove(bad)


class RenderTest(unittest.TestCase):
    def test_plan_is_never_executed_and_ordered(self):
        p = R.plan(good())
        self.assertIs(p["executed"], False)
        self.assertEqual([s["name"] for s in p["start"]][:2], ["vpref-blender-program", "vpref-blender-broker"])
        self.assertEqual(p["stop"][-1]["name"], "vpref-blender-program")
        self.assertEqual(p["stop"][-2]["name"], "vpref-blender-broker")

    def test_no_agent_argv_mentions_x(self):
        p = R.plan(good())
        spec = good()
        roles = {c["name"]: c["role"] for c in spec["containers"]}
        x_holders = []
        for s in p["start"]:
            line = " ".join(s["argv"])
            if "X11-unix" in line or "xsock" in line or "DISPLAY" in line:
                x_holders.append(roles[s["name"]])
        self.assertEqual(sorted(x_holders), ["broker", "program"])

    def test_every_container_carries_limits_and_prefix(self):
        for s in R.plan(good())["start"]:
            a = s["argv"]
            self.assertTrue(a[a.index("--name") + 1].startswith("vpref-"))
            self.assertEqual(a[a.index("--memory") + 1], a[a.index("--memory-swap") + 1])
            for flag in ("--pids-limit", "--read-only", "--health-cmd", "--stop-timeout", "--shm-size"):
                self.assertIn(flag, a)
            self.assertNotIn("--privileged", a)

    def test_render_refuses_an_invalid_spec(self):
        s = good()
        _agent_gets_xsock(s)
        with self.assertRaises(ValueError):
            R.plan(s)

    def test_renderer_and_validator_cannot_execute(self):
        # Gate: the dry-run tools must stay unable to start anything.
        for mod in ("validate.py", "render.py", "observe.py"):
            with open(os.path.join(ROOT, mod), encoding="utf-8") as f:
                src = f.read()
            for needle in ("import subprocess", "os.system", "os.exec", "os.spawn", "os.popen", "import socket", "import pty"):
                self.assertNotIn(needle, src, "%s contains %r" % (mod, needle))


def synth_inspect(spec):
    """What `docker inspect` would report if the engine applied the spec exactly."""
    out = []
    for c in spec["containers"]:
        out.append({
            "Name": "/" + c["name"], "RestartCount": 0,
            "Config": {"User": c["user"], "Env": ["%s=%s" % kv for kv in c["env"].items()],
                       "Labels": {"vp.capsule": spec["name"]}},
            "State": {"OOMKilled": False, "Health": {"Status": "healthy"}},
            "HostConfig": {"Memory": c["memory_mib"] * MIB, "MemorySwap": c["memory_swap_mib"] * MIB,
                           "PidsLimit": c["pids_limit"], "ShmSize": c["shm_mib"] * MIB,
                           "ReadonlyRootfs": True, "Privileged": False, "CapDrop": ["ALL"], "CapAdd": None,
                           "SecurityOpt": ["no-new-privileges"], "NetworkMode": c["network"],
                           "PidMode": "", "IpcMode": "private", "VolumesFrom": None, "Devices": []},
            "Mounts": [{"Type": "bind", "Source": "/x/" + m_["volume"], "Destination": m_["dst"],
                        "RW": m_["mode"] == "rw"} for m_ in c["mounts"]],
        })
    return out


def D(ins, name):
    return [d for d in ins if d["Name"] == "/" + name][0]


AG = "vpref-blender-modeler-agent"
DRIFTS = {
    "memory_not_applied": ("O-01", lambda i: D(i, AG)["HostConfig"].__setitem__("Memory", 0)),
    "swap_doubled": ("O-01", lambda i: D(i, AG)["HostConfig"].__setitem__("MemorySwap", 1024 * MIB)),
    "pids_unlimited": ("O-01", lambda i: D(i, AG)["HostConfig"].__setitem__("PidsLimit", None)),
    "rootfs_writable": ("O-02", lambda i: D(i, AG)["HostConfig"].__setitem__("ReadonlyRootfs", False)),
    "host_network": ("O-02", lambda i: D(i, AG)["HostConfig"].__setitem__("NetworkMode", "host")),
    "joined_pidns": ("O-02", lambda i: D(i, AG)["HostConfig"].__setitem__("PidMode", "container:abc")),
    "extra_mount": ("O-03", lambda i: D(i, AG)["Mounts"].append({"Source": "/home/user", "Destination": "/host", "RW": True})),
    "ro_became_rw": ("O-03", lambda i: D(i, AG)["Mounts"][1].__setitem__("RW", True)),
    "agent_display_env": ("O-04", lambda i: D(i, AG)["Config"]["Env"].append("DISPLAY=:99")),
    "unhealthy": ("O-05", lambda i: D(i, "vpref-blender-program")["State"]["Health"].__setitem__("Status", "unhealthy")),
    "no_healthcheck_at_all": ("O-05", lambda i: D(i, "vpref-blender-broker")["State"].pop("Health")),
    "oom_killed": ("O-06", lambda i: D(i, AG)["State"].__setitem__("OOMKilled", True)),
    "program_restarted_by_engine": ("O-06", lambda i: D(i, "vpref-blender-program").__setitem__("RestartCount", 1)),
    "container_missing": ("O-00", lambda i: i.remove(D(i, AG))),
    "stray_labelled_container": ("O-07", lambda i: i.append({"Name": "/vpref-blender-rogue", "Config": {"Labels": {"vp.capsule": "vpref-blender"}}})),
}


class ObserveTest(unittest.TestCase):
    def test_exact_application_is_green(self):
        s = good()
        self.assertEqual(O.observe(s, synth_inspect(s)), [])

    def test_each_drift_is_reported_with_its_rule(self):
        for name, (rule, fn) in sorted(DRIFTS.items()):
            with self.subTest(drift=name):
                s = good()
                ins = synth_inspect(s)
                fn(ins)
                self.assertEqual(rules(O.observe(s, ins)), [rule])

    def test_agent_x_mount_is_reported_twice_over(self):
        s = good()
        ins = synth_inspect(s)
        D(ins, AG)["Mounts"].append({"Source": "/var/lib/docker/volumes/vpref-blender-xsock/_data",
                                     "Destination": "/tmp/.X11-unix", "RW": False})
        self.assertEqual(rules(O.observe(s, ins)), ["O-03", "O-04"])

    def test_empty_inspect_is_all_missing(self):
        s = good()
        self.assertEqual(len([f for f in O.observe(s, []) if f["rule"] == "O-00"]), len(s["containers"]))


if __name__ == "__main__":
    unittest.main()

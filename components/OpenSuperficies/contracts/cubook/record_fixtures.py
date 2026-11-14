"""Rebuilds fixtures/ from the case table below: argv/env/meta/inputs are authored here; expected_stdout, expected_rc and
byte-compared expected_files are RECORDED from the reference implementation in --bin-dir (today's Go binaries) and every
recorded rc is checked against the rc this table declares. python3 -B record_fixtures.py --bin-dir DIR"""
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import run_conformance as rcf  # noqa: E402

TD = os.path.join(REPO, "testdata", "cu")
PLAN = open(os.path.join(TD, "plans", "click_effect.plan.json")).read()
FRAMES = open(os.path.join(TD, "plans", "effect.frames.jsonl")).read()


def plan_with(mut):
    p = json.loads(PLAN)
    mut(p["actions"][0])
    return json.dumps(p, indent=1) + "\n"


def with_expect_roi(roi):
    return plan_with(lambda a: a.__setitem__("expect", [{"kind": "changed_roi", "roi": roi}]))


START = json.dumps({"schema": "os.cu.wlcap-start/v1", "clock": "CLOCK_MONOTONIC", "t0_ns": 1, "delta_ns": 16666667, "frames": 360,
                    "w": 1920, "h": 1080, "chunk_frames": 60, "mode": "wl"})
PREREG = {"schema": "os.cu.prereg-wl/v2", "resolutions": [[1920, 1080]], "items": [{"id": "WL-1", "cursor_px": 0, "steps": [
    {"op": "scroll", "t_ms": 0, "at_px": [960, 540], "dy": 40}, {"op": "scroll", "t_ms": 1, "at_px": [960, 540], "dy": 120}]}]}
PREREG_V2_HS = {"schema": "os.cu.prereg-wl/v2", "resolutions": [[1920, 1080]], "items": [{"id": "WL-1", "cursor_px": 0, "steps": [
    {"op": "hscroll", "t_ms": 0, "at_px": [960, 540], "dx": 40}]}]}
PREREG_V3 = {"schema": "os.cu.prereg-wl/v3", "resolutions": [[1920, 1080]], "items": [{"id": "WL-1h", "cursor_px": 0, "steps": [
    {"op": "hscroll", "t_ms": 0, "at_px": [960, 540], "dx": 40}]}]}
T1 = {"set": {"OS_CU_TEST": "1"}}
SESSION = [{"jsonrpc": "2.0", "id": 1, "method": "initialize"}, {"jsonrpc": "2.0", "method": "notifications/initialized"},
           {"jsonrpc": "2.0", "id": 2, "method": "ping"}, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
           {"jsonrpc": "2.0", "id": 4, "method": "nope"}, {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "cu_click", "arguments": {"x": 5, "y": 6}}},
           {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "cu_bogus"}}]
SESSION_TXT = "\n".join(json.dumps(m) for m in SESSION) + "\ngarbage\n"
REG = open(os.path.join(TD, "mcp", "registry.json")).read()
FAKE = open(os.path.join(TD, "mcp", "fake_beckett.json")).read()
MCP_BASE = ["--registry", "registry.json", "--cas-dir", "{WORK}/cas"]

# (cli, case, argv, env, meta, stdin, files, want_rc)
CASES = [
    ("exec", "usage_noargs", [], {}, {}, None, {}, 2),
    ("exec", "usage_unknown_verb", ["bogus"], {}, {}, None, {}, 2),
    ("exec", "usage_validate_no_plan", ["validate"], {}, {}, None, {}, 2),
    ("exec", "validate_ok", ["validate", "--plan", "p.json"], {}, {}, None, {"p.json": PLAN}, 0),
    ("exec", "validate_roi_target_ok", ["validate", "--plan", "p.json"], {}, {}, None, {"p.json": with_expect_roi("target")}, 0),
    ("exec", "validate_roi_rect_ok", ["validate", "--plan", "p.json"], {}, {}, None, {"p.json": with_expect_roi([1, 2, 3, 4])}, 0),
    ("exec", "validate_roi_target2_rc2", ["validate", "--plan", "p.json"], {}, {"stderr_contains": "E_SCHEMA"}, None, {"p.json": with_expect_roi("target2")}, 2),
    ("exec", "validate_float_rc2", ["validate", "--plan", "p.json"], {}, {"stderr_contains": "non-integer"}, None,
     {"p.json": open(os.path.join(TD, "plans", "bad_float.plan.json")).read()}, 2),
    ("exec", "validate_trailing_rc2", ["validate", "--plan", "p.json"], {}, {"stderr_contains": "trailing"}, None, {"p.json": PLAN.rstrip("\n") + "\n{}\n"}, 2),
    ("exec", "validate_unknown_field_rc2", ["validate", "--plan", "p.json"], {}, {"stderr_contains": "E_SCHEMA"}, None,
     {"p.json": plan_with(lambda a: a.__setitem__("bogus", 1))}, 2),
    ("exec", "validate_missing_plan_rc2", ["validate", "--plan", "nope.json"], {}, {"stderr_contains": "E_IO"}, None, {}, 2),
    ("exec", "run_without_test_env_rc5", ["run", "--plan", "p.json", "--frames", "f.jsonl", "--out", "s.jsonl", "--mock"], {"unset": ["OS_CU_TEST"]},
     {"stderr_contains": "E_GATE"}, None, {"p.json": PLAN, "f.jsonl": FRAMES}, 5),
    ("exec", "run_fake_ok", ["run", "--plan", "p.json", "--frames", "f.jsonl", "--out", "s.jsonl", "--mock"], T1,
     {"expect_files": [{"path": "s.jsonl", "bytes": True, "schema": "vqa.act-step/v1", "jsonl": True}]}, None, {"p.json": PLAN, "f.jsonl": FRAMES}, 0),
    ("exec", "replay_ok", ["replay", "--plan", "p.json", "--steps", "s.jsonl", "--frames", "f.jsonl"], {},
     {"stdout_schema": "vqa.act-step/v1"}, None, {"p.json": PLAN, "f.jsonl": FRAMES, "s.jsonl": "@exec/run_fake_ok/expected_files/s.jsonl"}, 0),
    ("exec", "replay_divergence_rc5", ["replay", "--plan", "p.json", "--steps", "s.jsonl", "--frames", "f.jsonl"], {}, {"stderr_contains": "E_"}, None,
     {"p.json": PLAN, "f.jsonl": FRAMES, "s.jsonl": "@exec/run_fake_ok/expected_files/s.jsonl|verdict"}, 5),
    ("exec", "wl_run_ok", ["run", "--prereg", "pre.json", "--item", "WL-1", "--start", "START", "--out", "act.jsonl", "--mock", "--start-wait-ms", "500"], T1,
     {"expect_files": [{"path": "act.jsonl", "schema": "os.cu.wl-act-step/v1", "jsonl": True}]}, None,
     {"pre.json": json.dumps(PREREG), "START": START}, 0),
    ("exec", "wl_start_timeout_rc5", ["run", "--prereg", "pre.json", "--item", "WL-1", "--start", "NOSTART", "--out", "act.jsonl", "--mock", "--start-wait-ms", "50"], T1,
     {"stderr_contains": "start_timeout"}, None, {"pre.json": json.dumps(PREREG)}, 5),
    ("exec", "wl_v2_refuses_v3_op_rc2", ["run", "--prereg", "pre.json", "--item", "WL-1", "--start", "START", "--out", "act.jsonl", "--mock", "--start-wait-ms", "100"], T1,
     {"stderr_contains": "prereg-wl/v3"}, None, {"pre.json": json.dumps(PREREG_V2_HS), "START": START}, 2),
    ("exec", "wl_v3_hscroll_ok", ["run", "--prereg", "pre.json", "--item", "WL-1h", "--start", "START", "--out", "act.jsonl", "--mock", "--start-wait-ms", "500"], T1,
     {"expect_files": [{"path": "act.jsonl", "schema": "os.cu.wl-act-step/v1", "jsonl": True}]}, None, {"pre.json": json.dumps(PREREG_V3), "START": START}, 0),
    ("mcp", "usage_noargs", [], {}, {"stderr_contains": "usage: srf-cu-mcp"}, None, {}, 2),
    ("mcp", "fake_without_test_env_rc2", MCP_BASE + ["--mock-gui"], {"unset": ["OS_CU_TEST"]}, {"stderr_contains": "test-only"}, "", {"registry.json": REG}, 2),
    ("mcp", "l2_fake_needs_deny_list_rc2", MCP_BASE + ["--l2-fake"], T1, {"stderr_contains": "--deny-list"}, "", {"registry.json": REG}, 2),
    ("mcp", "registry_missing_rc2", ["--registry", "nope.json", "--cas-dir", "{WORK}/cas", "--mock-gui"], T1, {"stderr_contains": "E_SCHEMA"}, "", {}, 2),
    ("mcp", "fake_fixture_unreadable_rc2", MCP_BASE + ["--fake", "nope.json"], T1, {"stderr_contains": "E_SCHEMA"}, "", {"registry.json": REG}, 2),
    ("mcp", "jsonrpc_session", MCP_BASE + ["--mock-gui"], T1, {"stdout_schema": "os.cu.mcp-wire/v1"}, SESSION_TXT, {"registry.json": REG}, 0),
    ("mcp", "tools_list", MCP_BASE + ["--mock-gui"], T1, {}, json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n", {"registry.json": REG}, 0),
    ("mcp", "fake_downstream_call", MCP_BASE + ["--mock-gui", "--fake", "fake_beckett.json"], T1, {"stdout_schema": "os.cu.mcp-wire/v1"},
     json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "cu_mcp_call", "arguments": {
         "server": "godot-beckett", "tool": "get_godot_version", "params": {}, "risk": "read"}}}) + "\n",
     {"registry.json": REG, "fake_beckett.json": FAKE}, 0),
    ("snap", "usage_noargs", [], {}, {"stderr_contains": "E_USAGE"}, None, {}, 2),
    ("snap", "fake_without_test_env_rc2", ["--fake-fb", "--frames", "2", "--ramws-dir", "{RAM}", "--out", "{WORK}/o"], {"unset": ["OS_CU_TEST"]}, {"ram": "managed"}, None, {}, 2),
    ("snap", "frames_65_rc2", ["--fake-fb", "--frames", "65", "--ramws-dir", "{RAM}", "--out", "{WORK}/o"], T1, {"ram": "managed"}, None, {}, 2),
    ("snap", "fake_and_fb_rc2", ["--fake-fb", "--fb", "x", "--frames", "1", "--ramws-dir", "{RAM}", "--out", "{WORK}/o"], T1, {"ram": "managed"}, None, {}, 2),
    ("snap", "unmanaged_run_dir_rc5", ["--fake-fb", "--frames", "2", "--ramws-dir", "{RAM}", "--out", "{WORK}/o"], T1,
     {"ram": "unmanaged", "stderr_contains": "unmanaged_run_dir"}, None, {}, 5),
    ("snap", "fake_ok", ["--fake-fb", "--frames", "2", "--ramws-dir", "{RAM}", "--out", "{WORK}/o"], T1,
     {"ram": "managed", "stdout_schema": "os.cu.capture-receipt/v1", "expect_files": [
         {"path": "o/frames.manifest.json", "schema": "vqa.frame/v1"}, {"path": "o/capture_receipt.json", "bytes": True, "schema": "os.cu.capture-receipt/v1"}]}, None, {}, 0),
    ("wlcap", "usage_noargs", [], {}, {"stderr_contains": "E_USAGE"}, None, {}, 2),
    ("wlcap", "fake_without_test_env_rc2", ["--fake-fb", "--frames", "6", "--ramws-dir", "{RAM}", "--out", "{WORK}/o", "--mode", "bench"], {"unset": ["OS_CU_TEST"]},
     {"ram": "managed", "stderr_contains": "fake_requires_test_env"}, None, {}, 2),
    ("wlcap", "frames_zero_rc2", ["--fake-fb", "--frames", "0", "--ramws-dir", "{RAM}", "--out", "{WORK}/o"], T1, {"ram": "managed"}, None, {}, 2),
    ("wlcap", "bad_det_rc2", ["--fake-fb", "--frames", "6", "--det", "maybe", "--ramws-dir", "{RAM}", "--out", "{WORK}/o"], T1, {"ram": "managed"}, None, {}, 2),
    ("wlcap", "unmanaged_run_dir_rc5", ["--fake-fb", "--frames", "6", "--ramws-dir", "{RAM}", "--out", "{WORK}/o", "--mode", "bench"], T1,
     {"ram": "unmanaged", "stderr_contains": "unmanaged_run_dir"}, None, {}, 5),
    ("wlcap", "bench_ok", ["--fake-fb", "--frames", "6", "--ramws-dir", "{RAM}", "--out", "{WORK}/o", "--mode", "bench"], T1,
     {"ram": "managed", "volatile": ["longest_run_ms", "segment_ms", "span_ms"],
      "expect_files": [{"path": "o/receipt.json", "schema": "os.cu.wlcap-bench/v1"}]}, None, {}, 0),
    ("wlcap", "reap_empty_ok", ["--reap", "--ramws-dir", "{RAM}"], {}, {"ram": "managed"}, None, {}, 0),
]


def write(p, data):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode())


def main():
    bin_dir = os.path.abspath(sys.argv[sys.argv.index("--bin-dir") + 1])
    root = os.path.join(HERE, "fixtures")
    shutil.rmtree(root, ignore_errors=True)
    work_base = os.environ.get("CUBOOK_WORK") or os.path.join(REPO, "build", "cubook_conf")
    os.makedirs(work_base, exist_ok=True)
    # pass 1 writes every case; pass 2 records (replay inputs reference recorded files, so order matters)
    for cli, case, argv, env, meta, stdin, files, want in CASES:
        fx = os.path.join(root, cli, case)
        write(os.path.join(fx, "argv.json"), json.dumps(argv) + "\n")
        write(os.path.join(fx, "env.json"), json.dumps(env, sort_keys=True) + "\n")
        write(os.path.join(fx, "meta.json"), json.dumps(meta, sort_keys=True, indent=1) + "\n")
        write(os.path.join(fx, "expected_rc"), "%d\n" % want)
        if stdin is not None:
            write(os.path.join(fx, "stdin"), stdin)
        for n, c in files.items():
            if c.startswith("@"):
                continue
            write(os.path.join(fx, "files", n), c)
        write(os.path.join(fx, "expected_stdout"), b"")
    bad = 0
    for cli, case, argv, env, meta, stdin, files, want in CASES:
        fx = os.path.join(root, cli, case)
        for n, c in files.items():
            if c.startswith("@"):
                src, _, mut = c[1:].partition("|")
                data = open(os.path.join(root, src)).read()
                if mut:  # corrupt one verdict so replay must diverge
                    data = data.replace("EFFECT_OK", "NO_EFFECT")
                write(os.path.join(fx, "files", n), data)
        # record: run the real runner machinery once with an empty expectation, harvest stdout/files
        rec = os.path.join(work_base, "rec_" + cli + "_" + case)
        shutil.rmtree(rec, ignore_errors=True)
        os.makedirs(rec)
        if os.path.isdir(os.path.join(fx, "files")):
            shutil.copytree(os.path.join(fx, "files"), rec, dirs_exist_ok=True)
        ram = rcf.Ram(meta.get("ram") == "managed", "rec") if meta.get("ram") else None
        sub = {"{WORK}": rec, "{RAM}": ram.path if ram else rcf.RAM_ROOT + "/none"}
        args = []
        for t in argv:
            for k, v in sub.items():
                t = t.replace(k, v)
            args.append(t)
        e = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": rec, "LANG": "C"}
        e.update(env.get("set", {}))
        for k in env.get("unset", []):
            e.pop(k, None)
        p = subprocess.run([os.path.join(bin_dir, "srf-cu-" + cli)] + args, input=(stdin or "").encode(), cwd=rec, env=e,
                           capture_output=True, timeout=60)
        if ram:
            ram.close()
        if p.returncode != want:
            print("RC MISMATCH", cli, case, p.returncode, "declared", want, p.stderr[:200])
            bad = 1
        write(os.path.join(fx, "expected_stdout"), p.stdout)
        for ef in meta.get("expect_files", []):
            if ef.get("bytes"):
                write(os.path.join(fx, "expected_files", ef["path"]), open(os.path.join(rec, ef["path"]), "rb").read())
        shutil.rmtree(rec, ignore_errors=True)
    return 5 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

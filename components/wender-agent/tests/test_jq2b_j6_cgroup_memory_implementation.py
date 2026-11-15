# @module {"id":"tests.test_jq2b_j6_cgroup_memory_implementation","layer":3,"owner":"TESTS","deps":["wa.learn.cgroup_memory","wa.state.schema","wa.learn.multiwin_scorecard"]}
"""Public-path implementation and adversarial mutation tests for wender.learn.cgroup_memory."""
from __future__ import annotations

import ast
from contextlib import ExitStack
from dataclasses import FrozenInstanceError, fields, replace
import hashlib
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from wender.learn import cgroup_memory as cm
from wender.learn.multiwin_scorecard import FakeClock
from wender.state import schema

BOOT_ID = "6f4b2c10-9d8a-4e3b-8c12-0123456789ab\n"
MOUNTINFO_TEXT = (
    "22 1 8:1 / / rw,relatime - ext4 /dev/sda1 rw\n"
    "29 22 0:26 / /sys/fs/cgroup rw,nosuid,nodev,noexec,relatime - cgroup2 cgroup2 rw,nsdelegate,memory_recursiveprot\n"
)


def _stat_line(pid: int, comm: str, ppid: int, starttime: int, state: str = "S") -> str:
    # Fields 1..2: pid (comm); Field 3: state; Field 4: ppid; Fields 5..21: 17 ints; Field 22: starttime; Fields 23..24: vsize rss
    zeros_5_to_21 = " ".join(["0"] * 17)
    return f"{pid} ({comm}) {state} {ppid} {zeros_5_to_21} {starttime} 4096000 1024\n"


def _smaps_rollup_text(pss_kb: int, *, rss_kb: int = 4096) -> str:
    return (
        "55a1b2c3d000-7ffd9e8f0000 ---p 00000000 00:00 0 [rollup]\n"
        f"Rss:                {rss_kb} kB\n"
        f"Pss:                {pss_kb} kB\n"
        "Shared_Clean:       1024 kB\n"
        "Private_Dirty:      2048 kB\n"
    )


def _chromium_proc_fixture() -> tuple[dict[int, dict], dict[int, str], dict[int, str]]:
    specs = (
        (101, "chrome", 1, 10010, "parent", 64000),
        (102, "chrome (type=zygote)", 101, 10012, "broker", 18000),
        (103, "chrome --type=gpu-process", 101, 10015, "gpu", 96000),
        (104, "chrome --type=utility", 101, 10018, "utility", 24000),
        (105, "chrome --type=renderer", 102, 10025, "renderer", 140000),
        (106, "chrome-sandbox-helper", 105, 10030, "descendant", 8000),
    )
    proc_table = {}
    smaps_by_pid = {}
    proc_cgroup = {}
    for pid, comm, ppid, st, role, pss_kb in specs:
        proc_table[pid] = {
            "role": role,
            "scope": "instance",
            "stat": _stat_line(pid, comm, ppid, st),
        }
        smaps_by_pid[pid] = _smaps_rollup_text(pss_kb)
        proc_cgroup[pid] = "0::/wender/instances/inst-cdp-01\n"
    return proc_table, smaps_by_pid, proc_cgroup


def _godot_proc_fixture() -> tuple[dict[int, dict], dict[int, str], dict[int, str]]:
    specs = (
        (201, "godot4-holdings", 1, 20010, "parent", 110000),
        (202, "godot-render-worker", 201, 20014, "gpu", 72000),
        (203, "godot-audio-broker", 201, 20016, "broker", 12000),
        (204, "godot-sim-descendant", 201, 20019, "descendant", 16000),
    )
    proc_table = {}
    smaps_by_pid = {}
    proc_cgroup = {}
    for pid, comm, ppid, st, role, pss_kb in specs:
        proc_table[pid] = {
            "role": role,
            "scope": "instance",
            "stat": _stat_line(pid, comm, ppid, st),
        }
        smaps_by_pid[pid] = _smaps_rollup_text(pss_kb)
        proc_cgroup[pid] = "0::/wender/instances/inst-godot-01\n"
    return proc_table, smaps_by_pid, proc_cgroup


def _make_state_descriptor(kind: str, title: str) -> dict:
    return schema.build_state(
        [],
        kind=kind,
        url=f"{kind}://local/{title}",
        title=title,
        ax_sha256="a" * 64,
        ax_stable_sha256="b" * 64,
        prune={"name": "prune_ax_v1", "version": 1},
        viewport={"w": 1280, "h": 720, "scroll_x": 0, "scroll_y": 0, "doc_w": 1280, "doc_h": 720, "dpr_ppm": 1000000},
        raw_count=0,
    )


def _healthy_sample(
    *,
    sample_seq: int = 0,
    logical_tick: int = 1,
    source_policy: str = "cdp",
    current_bytes: int = 380 * 1024 * 1024,
    peak_bytes: int | None = 420 * 1024 * 1024,
    swap_bytes: int | None = 0,
    skew_ns: int = 250_000,
) -> cm.MemorySample:
    if source_policy == "cdp":
        proc_table, smaps_by_pid, proc_cgroup = _chromium_proc_fixture()
        cgroup_dir = "/sys/fs/cgroup/wender/instances/inst-cdp-01"
        instance_id = "inst-cdp-01"
        expected_roles = cm.CHROMIUM_ROLES
        descriptor = _make_state_descriptor("cdp", "BrowserTab")
    else:
        proc_table, smaps_by_pid, proc_cgroup = _godot_proc_fixture()
        cgroup_dir = "/sys/fs/cgroup/wender/instances/inst-godot-01"
        instance_id = "inst-godot-01"
        expected_roles = ("parent", "gpu", "broker", "descendant")
        descriptor = _make_state_descriptor("godot", "Holdings")

    pids = sorted(proc_table.keys())
    launcher_metadata = {
        "trusted": True,
        "cgroup_dir": cgroup_dir,
        "root_pid": pids[0],
        "expected_pids": pids,
        "cgroup_procs": "".join(f"{p}\n" for p in pids),
    }
    resolve_check = cm.resolve_trusted_cgroup_dir(
        launcher_metadata,
        mountinfo=MOUNTINFO_TEXT,
        proc_cgroup_by_pid=proc_cgroup,
    )
    assert resolve_check == cgroup_dir

    ticks = [1_000_000_000 + sample_seq * 10_000_000, 1_000_000_000 + sample_seq * 10_000_000 + skew_ns]
    idx = {"i": 0}

    def fake_clock() -> int:
        val = ticks[min(idx["i"], len(ticks) - 1)]
        idx["i"] += 1
        return val

    cgroup_files = {
        "memory.current": f"{current_bytes}\n",
        "memory.peak": None if peak_bytes is None else f"{peak_bytes}\n",
        "memory.swap.current": None if swap_bytes is None else f"{swap_bytes}\n",
    }
    overhead_smaps = {
        "wender": _smaps_rollup_text(16384),
        "capsule": _smaps_rollup_text(12288),
        "shared": _smaps_rollup_text(8192),
    }
    return cm.sample_instance_memory(
        sample_seq=sample_seq,
        logical_tick=logical_tick,
        instance_id=instance_id,
        capsule_id="cap-01",
        launcher_metadata=launcher_metadata,
        mountinfo=MOUNTINFO_TEXT,
        boot_id_text=BOOT_ID,
        pre_proc_table=proc_table,
        post_proc_table=proc_table,
        smaps_by_pid=smaps_by_pid,
        cgroup_files=cgroup_files,
        clock=fake_clock,
        max_skew_ns=5_000_000,
        expected_pids=pids,
        expected_roles=expected_roles,
        source_policy=source_policy,
        source_descriptor=descriptor,
        gpu_accounting="drm_cgroup",
        overhead_smaps=overhead_smaps,
    )


class TestJq2bJ6CgroupMemoryImplementation(unittest.TestCase):
    def test_module_isolation_no_godot_cdp_or_sibling_jq2b_imports(self):
        src_path = Path(cm.__file__)
        tree = ast.parse(src_path.read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        forbidden_substrings = (
            "godot",
            "cdp",
            "browser",
            "atspi",
            "window_scheduler",
            "focus_policy",
            "episode",
            "corroboration",
        )
        for mod_name in imported:
            for bad in forbidden_substrings:
                self.assertNotIn(bad, mod_name, f"Forbidden dependency {mod_name!r} in cgroup_memory.py")

    def test_gate_g_j6_1_parse_cgroup_memory_strict_units_and_absence(self):
        # Positive control: unit-checked text fixtures and strict byte integers.
        scope = cm.parse_cgroup_memory("104857600\n", "209715200\n", swap_current="4096\n")
        self.assertEqual(scope.current_bytes, 104857600)
        self.assertEqual(scope.peak_bytes, 209715200)
        self.assertEqual(scope.swap_current_bytes, 4096)
        self.assertTrue(scope.peak_supported)
        self.assertTrue(scope.swap_supported)
        self.assertEqual(scope.peak_status, "ok")
        self.assertEqual(scope.swap_status, "ok")
        self.assertEqual(scope.status, "ok")
        self.assertEqual(scope["current"], 104857600)
        self.assertEqual(scope["peak"], 209715200)
        self.assertEqual(scope["swap_current"], 4096)

        # Absent peak preserved as unsupported (None), never 0 or substituted with current.
        no_peak = cm.parse_cgroup_memory("52428800\n", None, swap_current="0\n")
        self.assertEqual(no_peak.current_bytes, 52428800)
        self.assertIsNone(no_peak.peak_bytes)
        self.assertFalse(no_peak.peak_supported)
        self.assertEqual(no_peak.peak_status, "unsupported")
        self.assertEqual(no_peak.status, "peak_unsupported")
        self.assertEqual(no_peak.swap_current_bytes, 0)

        # Negative contrast: reject negative peak, malformed quantities, units, floats, bools.
        malformed_cases = (
            ("1024\n", "-1\n", None),
            ("1024\n", -5, None),
            ("-1024\n", "2048\n", None),
            ("1024\n", "2048\n", "-1\n"),
            ("1024 kB\n", "2048\n", None),
            ("1024\n", "2048 kB\n", None),
            ("1024\n", "2048 B\n", None),
            ("1024\n", "max\n", None),
            ("1024.5\n", "2048\n", None),
            ("00\n", "2048\n", None),
            ("+1024\n", "2048\n", None),
            ("", "2048\n", None),
            ("   \n", "2048\n", None),
            (None, "2048\n", None),
            (True, "2048\n", None),
            ("1024\n", False, None),
            (1024.0, "2048\n", None),
        )
        for cur, pk, swp in malformed_cases:
            with self.subTest(current=cur, peak=pk, swap_current=swp):
                with self.assertRaises(ValueError):
                    cm.parse_cgroup_memory(cur, pk, swap_current=swp)

    def test_gate_g_j6_1_pss_parsing_strict_kb_and_missing_never_zero(self):
        # Positive control: exact kB-to-bytes conversion.
        pss_bytes, status = cm.parse_smaps_rollup_pss(_smaps_rollup_text(350000))
        self.assertEqual(status, "ok")
        self.assertEqual(pss_bytes, 350000 * 1024)
        self.assertIs(type(pss_bytes), int)

        zero_bytes, zero_status = cm.parse_smaps_rollup_pss(_smaps_rollup_text(0))
        self.assertEqual(zero_status, "ok")
        self.assertEqual(zero_bytes, 0)

        # Negative contrast: missing Pss stays None with status, NEVER zero.
        no_pss_text = "Rss:                4096 kB\nShared_Clean:       1024 kB\n"
        missing_val, missing_status = cm.parse_smaps_rollup_pss(no_pss_text)
        self.assertIsNone(missing_val)
        self.assertEqual(missing_status, "missing_pss")

        none_val, none_status = cm.parse_smaps_rollup_pss(None)
        self.assertIsNone(none_val)
        self.assertEqual(none_status, "missing_smaps_rollup")

        with self.assertRaises(ValueError):
            cm.parse_pss_kb(no_pss_text, require_present=True)

        # Malformed Pss lines must raise ValueError, never coerce.
        bad_smaps = (
            "Pss:                -10 kB\n",
            "Pss:                1024 B\n",
            "Pss:                1024 KB\n",
            "Pss:                1024 MB\n",
            "Pss:                1024 KiB\n",
            "Pss:                1024\n",
            "Pss:                1024.5 kB\n",
            "Pss:                01024 kB\n",
            "Pss:                100 kB\nPss:                200 kB\n",
        )
        for text in bad_smaps:
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    cm.parse_smaps_rollup_pss(text)

    def test_trusted_cgroup_directory_and_mount_membership_verification(self):
        proc_table, _, proc_cgroup = _chromium_proc_fixture()
        pids = sorted(proc_table.keys())
        meta = {
            "trusted": True,
            "cgroup_dir": "/sys/fs/cgroup/wender/instances/inst-cdp-01",
            "root_pid": pids[0],
            "expected_pids": pids,
            "cgroup_procs": "".join(f"{p}\n" for p in pids),
        }
        resolved = cm.resolve_trusted_cgroup_dir(
            meta, mountinfo=MOUNTINFO_TEXT, proc_cgroup_by_pid=proc_cgroup
        )
        self.assertEqual(resolved, "/sys/fs/cgroup/wender/instances/inst-cdp-01")

        # Never guess a cgroup path from PID or accept untrusted/unmounted paths.
        bad_metas = (
            {"trusted": True, "root_pid": 101},  # missing cgroup_dir
            {"trusted": False, "cgroup_dir": meta["cgroup_dir"], "cgroup_procs": meta["cgroup_procs"]},
            {"trusted": True, "guessed_from_pid": True, "cgroup_dir": meta["cgroup_dir"], "cgroup_procs": meta["cgroup_procs"]},
            {"trusted": True, "discovery_mode": "pid_guess", "cgroup_dir": meta["cgroup_dir"], "cgroup_procs": meta["cgroup_procs"]},
            {"trusted": True, "cgroup_dir": "/sys/fs/cgroup/wender/../escape", "cgroup_procs": meta["cgroup_procs"]},
            {"trusted": True, "cgroup_dir": "/var/run/not-cgroup2/inst", "cgroup_procs": meta["cgroup_procs"]},
        )
        for bad in bad_metas:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    cm.resolve_trusted_cgroup_dir(bad, mountinfo=MOUNTINFO_TEXT)

        # Missing PID in cgroup.procs or mismatched /proc/<pid>/cgroup must fail.
        with self.assertRaises(ValueError):
            cm.resolve_trusted_cgroup_dir(
                {**meta, "cgroup_procs": "101\n102\n"},
                mountinfo=MOUNTINFO_TEXT,
            )
        with self.assertRaises(ValueError):
            cm.resolve_trusted_cgroup_dir(
                meta,
                mountinfo=MOUNTINFO_TEXT,
                proc_cgroup_by_pid={**proc_cgroup, 105: "0::/user.slice/other\n"},
            )

    def test_sample_fields_chromium_tree_roles_overhead_and_re_enumeration(self):
        sample = _healthy_sample(sample_seq=0, logical_tick=3, source_policy="cdp")
        self.assertEqual(tuple(f.name for f in fields(cm.MemorySample))[:13], cm.SAMPLE_FIELDS)
        self.assertTrue(sample.tree_complete)
        self.assertEqual(sample.status, "ok")
        self.assertEqual(sample.sample_skew_ns, 250_000)
        self.assertEqual(sample.gpu_accounting, "drm_cgroup")
        self.assertEqual(
            {p.role for p in sample.processes},
            set(cm.CHROMIUM_ROLES),
        )
        # Verify exact PSS sum (in bytes) across Chromium parent, broker, gpu, utility, renderer, descendant
        expected_pss = (64000 + 18000 + 96000 + 24000 + 140000 + 8000) * 1024
        self.assertEqual(sample.pss_total_bytes, expected_pss)
        # Verify separate Wender, capsule, and shared overhead
        self.assertEqual(sample.wender_overhead_bytes, 16384 * 1024)
        self.assertEqual(sample.capsule_overhead_bytes, 12288 * 1024)
        self.assertEqual(sample.shared_overhead_bytes, 8192 * 1024)
        with self.assertRaises(FrozenInstanceError):
            sample.status = "mutated"  # type: ignore[misc]

        # Pre/post re-enumeration detects membership change (renderer exited mid-sample)
        proc_table, smaps_by_pid, _ = _chromium_proc_fixture()
        post_table = dict(proc_table)
        post_table.pop(105)
        pids = sorted(proc_table.keys())
        meta = {
            "trusted": True,
            "cgroup_dir": "/sys/fs/cgroup/wender/instances/inst-cdp-01",
            "root_pid": pids[0],
            "expected_pids": pids,
            "cgroup_procs": "".join(f"{p}\n" for p in pids),
        }
        clock = FakeClock(5000)
        churn_sample = cm.sample_instance_memory(
            sample_seq=1,
            logical_tick=4,
            instance_id="inst-cdp-01",
            capsule_id="cap-01",
            launcher_metadata=meta,
            mountinfo=MOUNTINFO_TEXT,
            boot_id_text=BOOT_ID,
            pre_proc_table=proc_table,
            post_proc_table=post_table,
            smaps_by_pid=smaps_by_pid,
            cgroup_files={"memory.current": "1048576\n", "memory.peak": "2097152\n", "memory.swap.current": "0\n"},
            clock=clock,
            expected_pids=pids,
            source_policy="cdp",
        )
        self.assertFalse(churn_sample.tree_complete)
        self.assertIsNone(churn_sample.pss_total_bytes)
        self.assertEqual(churn_sample.status, "membership_changed")

        # Pre/post re-enumeration detects PID incarnation change (same PID, new starttime)
        reused_post = dict(proc_table)
        reused_post[105] = {
            "role": "renderer",
            "scope": "instance",
            "stat": _stat_line(105, "chrome --type=renderer", 102, 99999),
        }
        incarnation_sample = cm.sample_instance_memory(
            sample_seq=2,
            logical_tick=5,
            instance_id="inst-cdp-01",
            capsule_id="cap-01",
            launcher_metadata=meta,
            mountinfo=MOUNTINFO_TEXT,
            boot_id_text=BOOT_ID,
            pre_proc_table=proc_table,
            post_proc_table=reused_post,
            smaps_by_pid=smaps_by_pid,
            cgroup_files={"memory.current": "1048576\n", "memory.peak": "2097152\n", "memory.swap.current": "0\n"},
            clock=clock,
            expected_pids=pids,
            source_policy="cdp",
        )
        self.assertFalse(incarnation_sample.tree_complete)
        self.assertIsNone(incarnation_sample.pss_total_bytes)
        self.assertEqual(incarnation_sample.status, "incarnation_changed")

        # Missing member Pss makes the sample partial with null pss_total_bytes
        partial_smaps = dict(smaps_by_pid)
        partial_smaps[103] = None
        partial_sample = cm.sample_instance_memory(
            sample_seq=3,
            logical_tick=6,
            instance_id="inst-cdp-01",
            capsule_id="cap-01",
            launcher_metadata=meta,
            mountinfo=MOUNTINFO_TEXT,
            boot_id_text=BOOT_ID,
            pre_proc_table=proc_table,
            post_proc_table=proc_table,
            smaps_by_pid=partial_smaps,
            cgroup_files={"memory.current": "1048576\n", "memory.peak": "2097152\n", "memory.swap.current": "0\n"},
            clock=clock,
            expected_pids=pids,
            source_policy="cdp",
        )
        self.assertFalse(partial_sample.tree_complete)
        self.assertIsNone(partial_sample.pss_total_bytes)
        self.assertEqual(partial_sample.status, "partial")
        gpu_proc = [p for p in partial_sample.processes if p.pid == 103][0]
        self.assertIsNone(gpu_proc.pss_bytes)
        self.assertEqual(gpu_proc.status, "missing_smaps_rollup")

    def test_invariants_inv_j6_3_and_inv_j6_4_arithmetic_and_peak_provenance(self):
        # INV-J6-3: Never add PSS and cgroup charges, ancestor and child scopes, or divide shared browser by tab count.
        with self.assertRaises(ValueError):
            cm.forbid_invalid_ram_arithmetic(add_pss_and_cgroup=True)
        with self.assertRaises(ValueError):
            cm.forbid_invalid_ram_arithmetic(
                ancestor_cgroup_path="/sys/fs/cgroup/wender",
                child_cgroup_path="/sys/fs/cgroup/wender/instances/inst-cdp-01",
            )
        with self.assertRaises(ValueError):
            cm.forbid_invalid_ram_arithmetic(shared_browser_bytes=400_000_000, tab_count=4)

        # INV-J6-4: No peak-reset subtraction, no sampled-PSS substitution, no Python/frame-quota substitutes.
        s0 = _healthy_sample(sample_seq=0, logical_tick=1, current_bytes=300 * 1024 * 1024, peak_bytes=410 * 1024 * 1024)
        s1 = _healthy_sample(sample_seq=1, logical_tick=2, current_bytes=320 * 1024 * 1024, peak_bytes=440 * 1024 * 1024)
        peak_eval = cm.evaluate_lifetime_peak(
            (s0, s1), exclusive_ownership=True, freshness_evidence=True
        )
        self.assertTrue(peak_eval.lifetime_peak_admissible)
        self.assertEqual(peak_eval.cgroup_peak_bytes, 440 * 1024 * 1024)
        self.assertEqual(peak_eval.pss_peak_classification, "lower_bound_diagnostic")

        with self.assertRaises(ValueError):
            cm.evaluate_lifetime_peak(
                (s0, s1),
                exclusive_ownership=True,
                freshness_evidence=True,
                baseline_peak_bytes=100 * 1024 * 1024,
            )
        with self.assertRaises(ValueError):
            cm.evaluate_lifetime_peak(
                (s0, s1),
                exclusive_ownership=True,
                freshness_evidence=True,
                substitute_pss_for_peak=True,
            )
        with self.assertRaises(ValueError):
            cm.evaluate_lifetime_peak(
                (s0, s1),
                exclusive_ownership=True,
                freshness_evidence=True,
                python_allocation_peak_bytes=16 * 1024 * 1024,
            )
        with self.assertRaises(ValueError):
            cm.evaluate_lifetime_peak(
                (s0, s1),
                exclusive_ownership=True,
                freshness_evidence=True,
                reserved_frame_bytes=32 * 1024 * 1024,
            )

        # Lifetime peak requires both exclusive ownership and freshness evidence.
        non_excl = cm.evaluate_lifetime_peak(
            (s0, s1), exclusive_ownership=False, freshness_evidence=True
        )
        self.assertFalse(non_excl.lifetime_peak_admissible)
        self.assertIn("non_exclusive_cgroup_ownership", non_excl.violations)

        stale_cg = cm.evaluate_lifetime_peak(
            (s0, s1), exclusive_ownership=True, freshness_evidence=False
        )
        self.assertFalse(stale_cg.lifetime_peak_admissible)
        self.assertIn("missing_cgroup_freshness_evidence", stale_cg.violations)

        # Absent cgroup peak stays None even when PSS sampled peak is available.
        s_no_peak = _healthy_sample(sample_seq=0, logical_tick=1, peak_bytes=None)
        no_peak_eval = cm.evaluate_lifetime_peak(
            (s_no_peak,), exclusive_ownership=True, freshness_evidence=True
        )
        self.assertIsNone(no_peak_eval.cgroup_peak_bytes)
        self.assertIsNotNone(no_peak_eval.pss_sampled_peak_bytes)
        self.assertFalse(no_peak_eval.lifetime_peak_admissible)
        self.assertEqual(no_peak_eval.status, "peak_unsupported")

    def test_both_source_policies_holdings_inline_fixtures_and_no_relabeling(self):
        cdp_sample = _healthy_sample(sample_seq=0, logical_tick=1, source_policy="cdp")
        godot_sample = _healthy_sample(sample_seq=0, logical_tick=1, source_policy="godot")
        self.assertEqual(cdp_sample.source_policy, "cdp")
        self.assertEqual(godot_sample.source_policy, "godot")

        # Inline Holdings pause command and outcome validation (COMMANDS.md / CONTRACTS.md fields)
        pause_cmd = cm.build_holdings_pause_command(tick=7, seq=3)
        self.assertEqual(pause_cmd, {"tick": 7, "seq": 3, "cmd": "pause", "args": {}})
        self.assertEqual(cm.validate_holdings_pause_command(pause_cmd), pause_cmd)

        outcome = {
            "seq": 3,
            "requested_tick": 7,
            "applied_tick": 7,
            "order_seq_id": 11,
            "status": "accepted",
            "reason": None,
            "state_sha256": "e" * 64,
        }
        validated_outcome = cm.validate_holdings_outcome(
            outcome, expected_seq=3, expected_tick=7
        )
        self.assertEqual(validated_outcome, outcome)

        # Separate trusted stream/ticket/session fixtures
        handle = schema.Handle("godot_window", "holdings-main", 1)
        stream_spec = schema.StreamSpec(width=4, height=3, slots=2, quota_bytes=256)
        stream_token = schema.StreamToken(handle=handle, epoch=0)
        ticket = schema.Ticket(action_id="act-pause-3", handle=handle)
        session_rec = schema.TargetRecord(
            handle=handle,
            transport=SimpleNamespace(kind="fake_brokered_ipc"),
            owned=True,
            epoch=0,
            session_id="sess-holdings-1",
            attachment_owned=True,
            closed=False,
        )
        self.assertEqual(stream_token.handle, ticket.handle)
        self.assertEqual(session_rec.session_id, "sess-holdings-1")
        self.assertGreaterEqual(stream_spec.quota_bytes, 4 * 3 * 3 * 2)

        # Reject malformed pause commands and outcomes
        with self.assertRaises(ValueError):
            cm.validate_holdings_pause_command({"tick": 7, "seq": 3, "cmd": "advance", "args": {}})
        with self.assertRaises(ValueError):
            cm.validate_holdings_pause_command({"tick": 7, "seq": 3, "cmd": "pause", "args": {"extra": 1}})
        with self.assertRaises(ValueError):
            cm.validate_holdings_outcome({**outcome, "status": "unknown"})

        # Reject relabeling CDP as Godot or Godot as CDP
        proc_table, smaps_by_pid, _ = _chromium_proc_fixture()
        pids = sorted(proc_table.keys())
        meta = {
            "trusted": True,
            "cgroup_dir": "/sys/fs/cgroup/wender/instances/inst-cdp-01",
            "root_pid": pids[0],
            "expected_pids": pids,
            "cgroup_procs": "".join(f"{p}\n" for p in pids),
        }
        with self.assertRaises(ValueError):
            cm.sample_instance_memory(
                sample_seq=0,
                logical_tick=1,
                instance_id="inst-cdp-01",
                launcher_metadata=meta,
                mountinfo=MOUNTINFO_TEXT,
                boot_id_text=BOOT_ID,
                pre_proc_table=proc_table,
                smaps_by_pid=smaps_by_pid,
                cgroup_files={"memory.current": "1024\n", "memory.peak": "2048\n", "memory.swap.current": "0\n"},
                clock=FakeClock(100),
                source_policy="cdp",
                source_descriptor=_make_state_descriptor("godot", "RelabeledAsCdp"),
            )
        with self.assertRaises(ValueError):
            cm.sample_instance_memory(
                sample_seq=0,
                logical_tick=1,
                instance_id="inst-cdp-01",
                launcher_metadata=meta,
                mountinfo=MOUNTINFO_TEXT,
                boot_id_text=BOOT_ID,
                pre_proc_table=proc_table,
                smaps_by_pid=smaps_by_pid,
                cgroup_files={"memory.current": "1024\n", "memory.peak": "2048\n", "memory.swap.current": "0\n"},
                clock=FakeClock(100),
                source_policy="godot",
                source_descriptor=_make_state_descriptor("cdp", "RelabeledAsGodot"),
            )

    def test_write_spy_blocks_disk_io_and_rejects_pixel_or_receipt_leakage(self):
        rgb = np.zeros((4, 6, 3), dtype=np.uint8)
        rgb[:, :, 0] = 127
        frame = schema.FrameRef(
            handle=schema.Handle("cdp_tab", "tab-1", 1),
            epoch=0,
            frame_seq=1,
            width=6,
            height=4,
            rgb=rgb,
            digest=hashlib.sha256(rgb.tobytes()).hexdigest(),
        )
        self.assertFalse(frame.rgb.flags.writeable)

        emitted: list[bytes] = []
        with ExitStack() as stack:
            for target in (
                "builtins.open",
                "io.open",
                "os.open",
                "pathlib.Path.open",
                "pathlib.Path.write_bytes",
                "pathlib.Path.write_text",
                "socket.socket",
                "socket.create_connection",
                "subprocess.Popen",
                "os.system",
                "time.sleep",
                "time.time",
                "time.monotonic",
                "time.perf_counter",
                "numpy.save",
                "numpy.savez",
            ):
                stack.enter_context(
                    patch(target, side_effect=AssertionError(f"forbidden I/O or host clock: {target}"))
                )
            sample = _healthy_sample(sample_seq=0, logical_tick=1, source_policy="cdp")
            payload = cm.export_bounded_metadata(sample, sink=emitted.append)

        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0], payload)
        self.assertIn(b'"schema":"wender.learn.cgroup_memory.sample/v1"', payload)

        # Reject volatile pixel arrays, raw hook receipts, tree dumps, and checkpoint bytes before sink
        sink_calls: list[bytes] = []
        leaks = (
            {"sample_seq": 0, "rgb": frame.rgb},
            {"sample_seq": 0, "raw_receipt": {"id": "r1"}},
            {"sample_seq": 0, "hook_receipts": [{"id": "r1"}]},
            {"sample_seq": 0, "raw_tree_dump": "<html>...</html>"},
            {"sample_seq": 0, "checkpoint_bytes": b"\x00\x01\x02"},
        )
        for leak in leaks:
            with self.subTest(leak=list(leak.keys())):
                with self.assertRaises(ValueError):
                    cm.export_bounded_metadata(leak, sink=sink_calls.append)
        self.assertEqual(sink_calls, [])

    def test_gate_g_j6_1_healthy_passes_and_named_mutations_disagree(self):
        rev = "766aa91408b6cdc421dbb27b08b868302618d459"
        receipts = {
            "G-M1-1": {"status": "PASS", "revision": rev, "expected_failure": False, "evidence": "m1-owner-receipt"},
            "G-M7-1": {"status": "PASS", "revision": rev, "expected_failure": False, "evidence": "m7-owner-receipt"},
            "G-M8-1": {"status": "PASS", "revision": rev, "expected_failure": False, "evidence": "m8-owner-receipt"},
        }
        limits = cm.MemoryBudgetLimits(
            max_cgroup_current_bytes=512 * 1024 * 1024,
            max_cgroup_peak_bytes=640 * 1024 * 1024,
            max_swap_current_bytes=0,
            max_sample_skew_ns=5_000_000,
            max_pss_lower_bound_bytes=512 * 1024 * 1024,
        )
        s0 = _healthy_sample(sample_seq=0, logical_tick=1, source_policy="cdp")
        s1 = _healthy_sample(sample_seq=1, logical_tick=2, source_policy="godot")

        healthy_gate = cm.evaluate_gate_g_j6_1(
            (s0, s1),
            limits=limits,
            exclusive_ownership=True,
            freshness_evidence=True,
            earlier_wave_receipts=receipts,
            required_revision=rev,
            retail_migration_disabled=True,
            optional_training_failed=True,
        )
        self.assertTrue(healthy_gate.passed, healthy_gate.violations)
        self.assertEqual(healthy_gate.status, "PASS")
        self.assertEqual(healthy_gate.invariants, {inv: True for inv in cm.INVARIANTS})
        self.assertEqual(healthy_gate.conductor_admission_binding, "AUSENTE")
        self.assertFalse(healthy_gate.host_admitted)
        self.assertFalse(healthy_gate.retail_migration_blocked)
        self.assertEqual(healthy_gate.evidence["timing_scope"], "VM")

        # Configured limits are required, never inferred defaults.
        with self.assertRaises(ValueError):
            cm.evaluate_gate_g_j6_1((s0,), limits=None, earlier_wave_receipts=receipts, required_revision=rev)
        with self.assertRaises(ValueError):
            cm.evaluate_gate_g_j6_1(
                (s0,),
                limits={"max_cgroup_current_bytes": 1024},
                earlier_wave_receipts=receipts,
                required_revision=rev,
            )

        # Named mutations that MUST disagree with the healthy fixture:
        mutations = {
            "missing_cgroup_current": (replace(s0, cgroup_current_bytes=None, status="missing_cgroup_current"), s1),
            "absent_cgroup_peak": (replace(s0, cgroup_peak_bytes=None, status="peak_unsupported"), s1),
            "exceed_cgroup_current": (replace(s0, cgroup_current_bytes=600 * 1024 * 1024), s1),
            "exceed_cgroup_peak": (replace(s0, cgroup_peak_bytes=700 * 1024 * 1024), s1),
            "nonzero_swap_exceeded": (replace(s0, swap_current_bytes=4096), s1),
            "incomplete_process_tree": (replace(s0, tree_complete=False, status="partial"), s1),
            "sample_skew_exceeded": (replace(s0, sample_skew_ns=9_000_000, status="skew_exceeded"), s1),
            "gpu_unaccounted": (replace(s0, gpu_accounting="unaccounted", status="gpu_unaccounted"), s1),
            "missing_overhead": (replace(s0, overhead=None), s1),
        }
        for name, mutated_samples in mutations.items():
            with self.subTest(mutation=name):
                res = cm.evaluate_gate_g_j6_1(
                    mutated_samples,
                    limits=limits,
                    exclusive_ownership=True,
                    freshness_evidence=True,
                    earlier_wave_receipts=receipts,
                    required_revision=rev,
                )
                self.assertFalse(res.passed, f"Mutation {name!r} must fail Gate G-J6-1")

        # Ownership, freshness, and earlier-wave receipt mutations:
        for flag_name, kwargs in (
            ("non_exclusive", {"exclusive_ownership": False}),
            ("unfresh_cgroup", {"freshness_evidence": False}),
            ("missing_receipts", {"earlier_wave_receipts": None}),
            ("wrong_revision", {"required_revision": "0" * 40}),
            (
                "expected_failure_receipt",
                {
                    "earlier_wave_receipts": {
                        **receipts,
                        "G-M1-1": {**receipts["G-M1-1"], "expected_failure": True},
                    }
                },
            ),
            (
                "inferred_consumer_receipt",
                {
                    "earlier_wave_receipts": {
                        **receipts,
                        "G-M7-1": {**receipts["G-M7-1"], "inferred_consumer": True},
                    }
                },
            ),
        ):
            with self.subTest(flag_mutation=flag_name):
                base_kwargs = {
                    "limits": limits,
                    "exclusive_ownership": True,
                    "freshness_evidence": True,
                    "earlier_wave_receipts": receipts,
                    "required_revision": rev,
                }
                base_kwargs.update(kwargs)
                res = cm.evaluate_gate_g_j6_1((s0, s1), **base_kwargs)
                self.assertFalse(res.passed, f"Flag mutation {flag_name!r} must fail Gate G-J6-1")


if __name__ == "__main__":
    unittest.main()

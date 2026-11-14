# @module {"id":"tests.test_jq2b_j6_procfs_memory_implementation","layer":3,"owner":"TESTS","deps":["wa.learn.procfs_memory","wa.perceive.session_registry","wa.learn.multiwin_scorecard","wa.state.schema"],"book":"HB-WA-s3-remember"}
"""Public-path J6 tests for strict procfs/cgroup memory accounting and tree identity.

Exercises `wender/learn/procfs_memory.py` exclusively through its public API using
in-RAM procfs/cgroup text fixtures, fake clocks, fake CDP and brokered IPC fixtures,
inline Holdings command/outcome records, and numpy frames in RAM. Never opens live
`/proc` or `/sys/fs/cgroup`, launches processes, or writes frames/receipts to disk.
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import FrozenInstanceError, fields, replace
import hashlib
import json
import unittest
from unittest.mock import patch

import numpy as np

from wender.learn import multiwin_scorecard as m7
from wender.learn import procfs_memory as pm
from wender.perceive import session_registry as sr
from wender.state import schema

BOOT_ID_A = "550e8400-e29b-41d4-a716-446655440000\n"
BOOT_ID_B = "660e8400-e29b-41d4-a716-446655441111\n"
REVISION_SHA = "10d01a2c03a390c61e16ade3e4b151df854a5681"


def _stat_line(pid: int, comm: str, state: str, ppid: int, starttime: int) -> str:
    """Build a valid 52-field Linux `/proc/<pid>/stat` text line."""
    after = ["0"] * 50
    after[0] = state
    after[1] = str(ppid)
    after[2] = str(pid)
    after[3] = str(pid)
    after[19] = str(starttime)
    return f"{pid} ({comm}) " + " ".join(after) + "\n"


def _smaps_text(
    pss_kb: int,
    *,
    rss_kb: int | None = None,
    anon_kb: int | None = None,
    file_kb: int | None = None,
    shmem_kb: int | None = None,
    swap_kb: int = 0,
    swap_pss_kb: int = 0,
    include_header: bool = True,
) -> str:
    rss = rss_kb if rss_kb is not None else pss_kb + 256
    anon = anon_kb if anon_kb is not None else pss_kb // 2
    file_part = file_kb if file_kb is not None else pss_kb - anon
    shmem = shmem_kb if shmem_kb is not None else 0
    lines = []
    if include_header:
        lines.append("55a000000000-7fffffffe000 ---p 00000000 00:00 0 [rollup]")
    lines.extend([
        f"Rss:                {rss} kB",
        f"Pss:                {pss_kb} kB",
        f"Pss_Anon:           {anon} kB",
        f"Pss_File:           {file_part} kB",
        f"Pss_Shmem:          {shmem} kB",
        f"Swap:               {swap_kb} kB",
        f"SwapPss:            {swap_pss_kb} kB",
    ])
    return "\n".join(lines) + "\n"


def _healthy_chromium_fixtures():
    """Construct complete Chromium tree + separate Wender, capsule, and shared overhead fixtures."""
    pre_stat = {
        100: _stat_line(100, "chromium-browser", "S", 1, 1000),
        101: _stat_line(101, "chrome_zygotebroker", "S", 100, 1005),
        102: _stat_line(102, "chromium (gpu-process)", "S", 100, 1010),
        103: _stat_line(103, "chromium-utility", "S", 100, 1015),
        104: _stat_line(104, "chromium-renderer", "R", 101, 1020),
        105: _stat_line(105, "crashpad-handler", "S", 100, 1025),
        200: _stat_line(200, "wender-worker", "S", 1, 900),
        300: _stat_line(300, "xvfb-capsule", "S", 1, 800),
        400: _stat_line(400, "ipc-broker-daemon", "S", 1, 700),
    }
    role_map = {
        100: "parent",
        101: "broker",
        102: "gpu",
        103: "utility",
        104: "renderer",
        105: "descendant",
        200: "parent",
        300: "parent",
        400: "broker",
    }
    scope_map = {
        100: "instance",
        101: "instance",
        102: "instance",
        103: "instance",
        104: "instance",
        105: "instance",
        200: "wender",
        300: "capsule",
        400: "shared_overhead",
    }
    smaps = {
        100: _smaps_text(120000),
        101: _smaps_text(16000),
        102: _smaps_text(64000, shmem_kb=8000),
        103: _smaps_text(24000),
        104: _smaps_text(96000),
        105: _smaps_text(4000),
        200: _smaps_text(32000),
        300: _smaps_text(20000),
        400: _smaps_text(8000),
    }
    cgroup_files = {
        "memory.current": f"{340000 * 1024}\n",
        "memory.peak": f"{360000 * 1024}\n",
        "memory.swap.current": "0\n",
    }
    budget = pm.MemoryBudgetConfig(
        max_instance_ram_bytes=512 * 1024 * 1024,
        max_cgroup_peak_bytes=512 * 1024 * 1024,
        max_swap_bytes=0,
        max_sample_skew_ns=5_000_000,
        max_parse_bytes=16 * 1024 * 1024 * 1024,
    )
    receipts = {
        "G-M1-1": {
            "status": "PASS",
            "evidence": "owner-gate:G-M1-1",
            "owner_revision": REVISION_SHA,
            "expected_failure": False,
            "consumer_inferred": False,
        },
        "G-M7-1": {
            "status": "PASS",
            "evidence": "owner-gate:G-M7-1",
            "owner_revision": REVISION_SHA,
            "expected_failure": False,
            "consumer_inferred": False,
        },
        "G-M8-1": {
            "status": "PASS",
            "evidence": "owner-gate:G-M8-1",
            "owner_revision": REVISION_SHA,
            "expected_failure": False,
            "consumer_inferred": False,
        },
    }
    return pre_stat, role_map, scope_map, smaps, cgroup_files, budget, receipts


def _write_spy_stack() -> ExitStack:
    """Reject any live procfs, filesystem, network, process, or real clock calls."""
    stack = ExitStack()
    forbidden_targets = (
        "builtins.open",
        "io.open",
        "os.open",
        "pathlib.Path.open",
        "pathlib.Path.read_text",
        "pathlib.Path.read_bytes",
        "pathlib.Path.write_text",
        "pathlib.Path.write_bytes",
        "socket.socket",
        "socket.create_connection",
        "subprocess.Popen",
        "subprocess.run",
        "os.system",
        "time.sleep",
        "time.time",
        "time.monotonic",
        "time.perf_counter",
        "numpy.save",
        "numpy.savez",
    )
    for target in forbidden_targets:
        stack.enter_context(
            patch(target, side_effect=AssertionError(f"write/live-IO spy blocked {target}"))
        )
    return stack


class TestJq2bJ6ProcfsMemoryImplementation(unittest.TestCase):
    def test_gate_g_j6_1_strict_smaps_rollup_units_overflow_and_absence(self):
        with _write_spy_stack():
            rollup = pm.parse_smaps_rollup(
                _smaps_text(2048, rss_kb=4096, anon_kb=1024, file_kb=512, shmem_kb=512, swap_kb=64, swap_pss_kb=32)
            )
            self.assertEqual(rollup.pss_kb, 2048)
            self.assertEqual(rollup.pss_bytes, 2048 * 1024)
            self.assertEqual(int(rollup), 2048 * 1024)
            self.assertEqual(rollup["pss_bytes"], 2048 * 1024)
            self.assertEqual(rollup["Pss"], 2048 * 1024)
            self.assertEqual(rollup.rss_bytes, 4096 * 1024)
            self.assertEqual(rollup.pss_anon_bytes, 1024 * 1024)
            self.assertEqual(rollup.pss_file_bytes, 512 * 1024)
            self.assertEqual(rollup.pss_shmem_bytes, 512 * 1024)
            self.assertEqual(rollup.swap_bytes, 64 * 1024)
            self.assertEqual(rollup.swap_pss_bytes, 32 * 1024)
            self.assertEqual(rollup.status, "ok")

            # Negative contrast: missing Pss must NEVER parse as 0
            with self.assertRaises(pm.MissingProcfsError) as ctx_missing_pss:
                pm.parse_smaps_rollup("Rss:                1024 kB\nSwap:               0 kB\n")
            self.assertEqual(ctx_missing_pss.exception.code, "missing_pss")

            # Distinguish missing vs unreadable inputs
            missing_probe = pm.probe_smaps_rollup(None)
            self.assertEqual(missing_probe["status"], "missing")
            self.assertIsNone(missing_probe["pss_bytes"])

            fnf_probe = pm.probe_smaps_rollup(FileNotFoundError("no such proc"))
            self.assertEqual(fnf_probe["status"], "missing")
            self.assertIsNone(fnf_probe["pss_bytes"])

            unreadable_probe = pm.probe_smaps_rollup(PermissionError("eacces"))
            self.assertEqual(unreadable_probe["status"], "unreadable")
            self.assertIsNone(unreadable_probe["pss_bytes"])

            # Reject duplicate, negative, malformed, overflowing, and wrong-unit values
            invalid_cases = (
                ("duplicate_pss", "Pss: 100 kB\nPss: 100 kB\n", "duplicate"),
                ("duplicate_rss", "Rss: 200 kB\nRss: 200 kB\nPss: 100 kB\n", "duplicate"),
                ("negative_pss", "Pss: -1 kB\n", "negative"),
                ("negative_zero", "Pss: -0 kB\n", "negative"),
                ("wrong_unit_bytes", "Pss: 1024 B\n", "wrong_unit"),
                ("wrong_unit_mb", "Pss: 2 MB\n", "wrong_unit"),
                ("wrong_unit_kib", "Pss: 2 KiB\n", "wrong_unit"),
                ("wrong_unit_lowercase", "Pss: 2 kb\n", "wrong_unit"),
                ("missing_unit", "Pss: 2048\n", "wrong_unit"),
                ("float_value", "Pss: 12.5 kB\n", "malformed"),
                ("plus_prefix", "Pss: +10 kB\n", "malformed"),
                ("leading_zero", "Pss: 010 kB\n", "malformed"),
                ("hex_value", "Pss: 0x10 kB\n", "malformed"),
                ("non_ascii_digits", "Pss: ١٢٣ kB\n", "malformed"),
                ("tab_separator", "Pss:\t100 kB\n", "malformed"),
                ("extra_tokens", "Pss: 100 kB extra\n", "malformed"),
                ("overflow_limit", "Pss: 1025 kB\n", "overflow"),
            )
            for name, payload, expected_code in invalid_cases:
                with self.subTest(case=name):
                    with self.assertRaises(pm.MalformedProcfsError) as ctx:
                        pm.parse_smaps_rollup(payload, max_bytes=1024 * 1024)
                    self.assertEqual(ctx.exception.code, expected_code)

    def test_gate_g_j6_1_cgroup_distinct_byte_quantities_and_negative_peak_rejection(self):
        with _write_spy_stack():
            cg = pm.probe_cgroup_memory({
                "memory.current": "1048576\n",
                "memory.peak": "2097152\n",
                "memory.swap.current": "524288\n",
            })
            self.assertEqual(cg["cgroup_current_bytes"], 1048576)
            self.assertEqual(cg["cgroup_peak_bytes"], 2097152)
            self.assertEqual(cg["swap_current_bytes"], 524288)
            self.assertEqual(cg["status"], "ok")

            # Missing or unreadable cgroup files stay None with explicit status, never 0
            partial_cg = pm.probe_cgroup_memory({
                "memory.current": "1048576\n",
                "memory.peak": None,
                "memory.swap.current": PermissionError("eacces"),
            })
            self.assertEqual(partial_cg["cgroup_current_bytes"], 1048576)
            self.assertIsNone(partial_cg["cgroup_peak_bytes"])
            self.assertIsNone(partial_cg["swap_current_bytes"])
            self.assertEqual(partial_cg["file_status"]["memory.peak"], "missing")
            self.assertEqual(partial_cg["file_status"]["memory.swap.current"], "unreadable")
            self.assertEqual(partial_cg["status"], "partial")

            # Negative contrast: negative peak, wrong unit (kB on byte file), duplicate, overflow rejected
            bad_cgroup_cases = (
                ("negative_peak", "-1\n", "negative"),
                ("unit_on_byte_file", "1024 kB\n", "wrong_unit"),
                ("max_token", "max\n", "malformed"),
                ("duplicate_lines", "1024\n2048\n", "duplicate"),
                ("overflow_bytes", "4096\n", "overflow"),
            )
            for name, text, code in bad_cgroup_cases:
                with self.subTest(case=name):
                    with self.assertRaises(pm.MalformedProcfsError) as ctx:
                        pm.parse_cgroup_bytes(text, field_name="memory.peak", max_bytes=2048)
                    self.assertEqual(ctx.exception.code, code)

    def test_gate_g_j6_2_process_identity_whole_chromium_tree_and_reenumeration(self):
        with _write_spy_stack():
            stat = pm.parse_proc_stat(_stat_line(102, "chromium (gpu-process) [v2]", "S", 100, 987654), expected_pid=102)
            self.assertEqual(stat.pid, 102)
            self.assertEqual(stat.comm, "chromium (gpu-process) [v2]")
            self.assertEqual(stat.state, "S")
            self.assertEqual(stat.ppid, 100)
            self.assertEqual(stat.starttime, 987654)

            ident = pm.parse_process_identity(
                _stat_line(102, "chromium (gpu-process)", "S", 100, 987654),
                BOOT_ID_A,
                expected_pid=102,
            )
            self.assertEqual(ident, pm.ProcessIdentity(pid=102, starttime=987654, boot_id=BOOT_ID_A.strip()))

            pre_stat, role_map, scope_map, smaps, cgroup_files, budget, _ = _healthy_chromium_fixtures()
            clock = m7.FakeClock(1_000_000)
            sample, overheads = pm.sample_process_tree(
                sample_seq=0,
                logical_tick=10,
                clock=clock,
                instance_id="chromium-instance-1",
                capsule_id="capsule-alpha",
                root_pids=(100,),
                pre_stat_map=pre_stat,
                post_stat_map=pre_stat,
                smaps_map=smaps,
                pre_boot_id=BOOT_ID_A,
                post_boot_id=BOOT_ID_A,
                cgroup_files=cgroup_files,
                role_map=role_map,
                scope_map=scope_map,
                expect_gpu_process=True,
                sample_delay_ns=250_000,
                max_bytes=budget.max_parse_bytes,
                max_sample_skew_ns=budget.max_sample_skew_ns,
            )

            expected_fields = (
                "sample_seq",
                "logical_tick",
                "monotonic_ns",
                "instance_id",
                "capsule_id",
                "processes",
                "cgroup_current_bytes",
                "cgroup_peak_bytes",
                "swap_current_bytes",
                "tree_complete",
                "gpu_accounting",
                "sample_skew_ns",
                "status",
            )
            self.assertEqual(tuple(f.name for f in fields(pm.MemorySample)), expected_fields)
            with self.assertRaises(FrozenInstanceError):
                sample.status = "mutated"

            self.assertTrue(sample.tree_complete)
            self.assertEqual(sample.status, "ok")
            self.assertEqual(sample.sample_skew_ns, 250_000)
            self.assertEqual(sample.gpu_accounting, "host_pss_only")
            self.assertEqual(sample.pss_total_bytes, 324_000 * 1024)
            self.assertEqual(
                sample.role_pss_bytes,
                {
                    "broker": 16_000 * 1024,
                    "descendant": 4_000 * 1024,
                    "gpu": 64_000 * 1024,
                    "parent": 120_000 * 1024,
                    "renderer": 96_000 * 1024,
                    "utility": 24_000 * 1024,
                },
            )
            self.assertEqual(overheads.instance_pss_bytes, 324_000 * 1024)
            self.assertEqual(overheads.wender_pss_bytes, 32_000 * 1024)
            self.assertEqual(overheads.capsule_pss_bytes, 20_000 * 1024)
            self.assertEqual(overheads.shared_overhead_pss_bytes, 8_000 * 1024)

            # Incarnation change (PID 104 reused with new starttime during sample) -> partial
            post_incarnation = dict(pre_stat)
            post_incarnation[104] = _stat_line(104, "chromium-renderer", "R", 101, 2099)
            inc_sample, inc_overheads = pm.sample_process_tree(
                sample_seq=1,
                logical_tick=11,
                clock=m7.FakeClock(2_000_000),
                instance_id="chromium-instance-1",
                capsule_id="capsule-alpha",
                root_pids=(100,),
                pre_stat_map=pre_stat,
                post_stat_map=post_incarnation,
                smaps_map=smaps,
                pre_boot_id=BOOT_ID_A,
                post_boot_id=BOOT_ID_A,
                cgroup_files=cgroup_files,
                role_map=role_map,
                scope_map=scope_map,
            )
            self.assertFalse(inc_sample.tree_complete)
            self.assertEqual(inc_sample.status, "incarnation_changed")
            self.assertIsNone(inc_sample.pss_total_bytes)
            self.assertIsNone(inc_overheads.instance_pss_bytes)

            # Boot ID change between pre and post enumeration -> incarnation_changed
            boot_sample, _ = pm.sample_process_tree(
                sample_seq=2,
                logical_tick=12,
                clock=m7.FakeClock(3_000_000),
                instance_id="chromium-instance-1",
                capsule_id="capsule-alpha",
                root_pids=(100,),
                pre_stat_map=pre_stat,
                post_stat_map=pre_stat,
                smaps_map=smaps,
                pre_boot_id=BOOT_ID_A,
                post_boot_id=BOOT_ID_B,
                cgroup_files=cgroup_files,
                role_map=role_map,
                scope_map=scope_map,
            )
            self.assertFalse(boot_sample.tree_complete)
            self.assertEqual(boot_sample.status, "incarnation_changed")

            # Membership change (renderer process 104 exits before post enumeration)
            post_missing = dict(pre_stat)
            post_missing.pop(104)
            mem_sample, _ = pm.sample_process_tree(
                sample_seq=3,
                logical_tick=13,
                clock=m7.FakeClock(4_000_000),
                instance_id="chromium-instance-1",
                capsule_id="capsule-alpha",
                root_pids=(100,),
                pre_stat_map=pre_stat,
                post_stat_map=post_missing,
                smaps_map=smaps,
                pre_boot_id=BOOT_ID_A,
                post_boot_id=BOOT_ID_A,
                cgroup_files=cgroup_files,
                role_map=role_map,
                scope_map=scope_map,
            )
            self.assertFalse(mem_sample.tree_complete)
            self.assertEqual(mem_sample.status, "membership_changed")
            self.assertIsNone(mem_sample.pss_total_bytes)

            # Unreadable member smaps_rollup -> partial sample, member pss_bytes stays None
            smaps_unreadable = dict(smaps)
            smaps_unreadable[102] = PermissionError("gpu smaps unreadable")
            unread_sample, _ = pm.sample_process_tree(
                sample_seq=4,
                logical_tick=14,
                clock=m7.FakeClock(5_000_000),
                instance_id="chromium-instance-1",
                capsule_id="capsule-alpha",
                root_pids=(100,),
                pre_stat_map=pre_stat,
                post_stat_map=pre_stat,
                smaps_map=smaps_unreadable,
                pre_boot_id=BOOT_ID_A,
                post_boot_id=BOOT_ID_A,
                cgroup_files=cgroup_files,
                role_map=role_map,
                scope_map=scope_map,
                expect_gpu_process=True,
            )
            self.assertFalse(unread_sample.tree_complete)
            self.assertEqual(unread_sample.status, "partial")
            self.assertEqual(unread_sample.gpu_accounting, "unreadable_gpu_process")
            gpu_proc = [p for p in unread_sample.processes if p.pid == 102][0]
            self.assertIsNone(gpu_proc.pss_bytes)
            self.assertEqual(gpu_proc.status, "unreadable")
            self.assertIsNone(unread_sample.pss_total_bytes)

    def test_gate_g_j6_3_scope_isolation_no_pss_cgroup_sum_no_ancestor_child_no_tab_division(self):
        with _write_spy_stack():
            clean = pm.validate_scope_isolation(
                add_pss_and_cgroup=False,
                cgroup_paths=("/sys/fs/cgroup/wender/capsule-a/inst-1", "/sys/fs/cgroup/wender/capsule-a/inst-2"),
                tab_count=3,
                divide_shared_browser_by_tabs=False,
                source_policy="cdp",
                handle_kind="cdp_tab",
            )
            self.assertEqual(clean, ())

            self.assertIn(
                "pss_cgroup_addition_forbidden",
                pm.validate_scope_isolation(add_pss_and_cgroup=True),
            )
            self.assertIn(
                "ancestor_child_scope_addition_forbidden",
                pm.validate_scope_isolation(
                    cgroup_paths=("/sys/fs/cgroup/wender/capsule-a", "/sys/fs/cgroup/wender/capsule-a/inst-1")
                ),
            )
            self.assertIn(
                "shared_browser_tab_division_forbidden",
                pm.validate_scope_isolation(tab_count=4, divide_shared_browser_by_tabs=True),
            )
            self.assertIn(
                "source_policy_relabeling_forbidden",
                pm.validate_scope_isolation(source_policy="cdp", handle_kind="godot_window"),
            )
            self.assertIn(
                "source_policy_relabeling_forbidden",
                pm.validate_scope_isolation(source_policy="godot", handle_kind="cdp_tab"),
            )

    def test_gate_g_j6_4_peak_semantics_scoped_and_insufficient_diagnostics(self):
        with _write_spy_stack():
            pre_stat, role_map, scope_map, smaps, cgroup_files, budget, _ = _healthy_chromium_fixtures()
            sample, _ = pm.sample_process_tree(
                sample_seq=0,
                logical_tick=1,
                clock=m7.FakeClock(100),
                instance_id="inst-1",
                capsule_id="cap-1",
                root_pids=(100,),
                pre_stat_map=pre_stat,
                post_stat_map=pre_stat,
                smaps_map=smaps,
                pre_boot_id=BOOT_ID_A,
                cgroup_files=cgroup_files,
                role_map=role_map,
                scope_map=scope_map,
            )

            certified = pm.evaluate_peak_semantics(
                instance_id="inst-1",
                configured_limit_bytes=budget.max_cgroup_peak_bytes,
                samples=(sample,),
                exclusive_ownership=True,
                freshness_evidence="cgroup-epoch:inst-1:born-at-100ns",
                reported_peak_source="cgroup_peak",
                python_allocation_peak_bytes=12 * 1024 * 1024,
                reserved_frame_bytes=24 * 1024 * 1024,
            )
            self.assertEqual(certified.status, "ok")
            self.assertEqual(certified.classification, "certified_cgroup_lifetime_peak")
            self.assertTrue(certified.sufficient_for_whole_instance_ram)
            self.assertTrue(certified.within_limit)
            self.assertEqual(
                certified.diagnostics["pss_sampled_peak"],
                {
                    "bytes": 324_000 * 1024,
                    "classification": "lower_bound_diagnostic",
                    "sufficient_for_whole_instance_ram": False,
                },
            )
            self.assertEqual(
                certified.diagnostics["python_allocation_peak"],
                {
                    "bytes": 12 * 1024 * 1024,
                    "classification": "optional_diagnostic_insufficient",
                    "sufficient_for_whole_instance_ram": False,
                },
            )
            self.assertEqual(
                certified.diagnostics["reserved_frame_bytes"],
                {
                    "bytes": 24 * 1024 * 1024,
                    "classification": "optional_diagnostic_insufficient",
                    "sufficient_for_whole_instance_ram": False,
                },
            )

            py_peak = pm.evaluate_peak_semantics(
                instance_id="inst-1",
                configured_limit_bytes=budget.max_cgroup_peak_bytes,
                samples=(sample,),
                exclusive_ownership=True,
                freshness_evidence="cgroup-epoch:inst-1",
                reported_peak_source="python_allocation_peak",
                python_allocation_peak_bytes=12 * 1024 * 1024,
            )
            self.assertFalse(py_peak.sufficient_for_whole_instance_ram)
            self.assertEqual(py_peak.classification, "optional_diagnostic_insufficient")
            self.assertIn("python_peak_reported_as_instance_ram", py_peak.violations)

            frame_peak = pm.evaluate_peak_semantics(
                instance_id="inst-1",
                configured_limit_bytes=budget.max_cgroup_peak_bytes,
                samples=(sample,),
                exclusive_ownership=True,
                freshness_evidence="cgroup-epoch:inst-1",
                reported_peak_source="reserved_frame_bytes",
                reserved_frame_bytes=24 * 1024 * 1024,
            )
            self.assertFalse(frame_peak.sufficient_for_whole_instance_ram)
            self.assertEqual(frame_peak.classification, "optional_diagnostic_insufficient")
            self.assertIn("reserved_frame_bytes_reported_as_instance_ram", frame_peak.violations)

            pss_only = pm.evaluate_peak_semantics(
                instance_id="inst-1",
                configured_limit_bytes=budget.max_cgroup_peak_bytes,
                samples=(sample,),
                reported_peak_source="pss_sampled_peak",
            )
            self.assertEqual(pss_only.classification, "lower_bound_diagnostic")
            self.assertFalse(pss_only.sufficient_for_whole_instance_ram)

            for excl, fresh in ((False, "fresh-token"), (True, None), (True, "   ")):
                with self.subTest(exclusive=excl, freshness=fresh):
                    unscoped = pm.evaluate_peak_semantics(
                        instance_id="inst-1",
                        configured_limit_bytes=budget.max_cgroup_peak_bytes,
                        samples=(sample,),
                        exclusive_ownership=excl,
                        freshness_evidence=fresh,
                        reported_peak_source="cgroup_peak",
                    )
                    self.assertFalse(unscoped.sufficient_for_whole_instance_ram)
                    self.assertIn("unscoped_or_stale_cgroup_peak", unscoped.violations)

    def test_both_source_policies_inline_holdings_fixtures_and_mutation_disagreement(self):
        with _write_spy_stack():
            pre_stat, role_map, scope_map, smaps, cgroup_files, budget, receipts = _healthy_chromium_fixtures()
            cdp_sample, cdp_overheads = pm.sample_process_tree(
                sample_seq=0,
                logical_tick=1,
                clock=m7.FakeClock(10_000),
                instance_id="cdp-browser-1",
                capsule_id="capsule-cdp",
                root_pids=(100,),
                pre_stat_map=pre_stat,
                post_stat_map=pre_stat,
                smaps_map=smaps,
                pre_boot_id=BOOT_ID_A,
                cgroup_files=cgroup_files,
                role_map=role_map,
                scope_map=scope_map,
                expect_gpu_process=True,
                sample_delay_ns=100_000,
                max_bytes=budget.max_parse_bytes,
                max_sample_skew_ns=budget.max_sample_skew_ns,
            )
            cdp_peak = pm.evaluate_peak_semantics(
                instance_id="cdp-browser-1",
                configured_limit_bytes=budget.max_cgroup_peak_bytes,
                samples=(cdp_sample,),
                exclusive_ownership=True,
                freshness_evidence="fresh:cdp-browser-1:epoch-0",
                reported_peak_source="cgroup_peak",
                python_allocation_peak_bytes=8 * 1024 * 1024,
                reserved_frame_bytes=16 * 1024 * 1024,
            )
            cdp_report = pm.evaluate_j6_memory_gates(
                instance_id="cdp-browser-1",
                source_policy="cdp",
                handle_kind="cdp_tab",
                budget_config=budget,
                samples=(cdp_sample,),
                overheads=cdp_overheads,
                peak_evaluation=cdp_peak,
                prerequisite_receipts=receipts,
                required_revision=REVISION_SHA,
                scope_checks={
                    "add_pss_and_cgroup": False,
                    "cgroup_paths": ("/sys/fs/cgroup/wender/cdp-1", "/sys/fs/cgroup/wender/godot-1"),
                    "tab_count": 2,
                    "divide_shared_browser_by_tabs": False,
                },
                metadata_envelope={"instance_id": "cdp-browser-1", "timing_label": "VM"},
            )
            self.assertTrue(cdp_report.passed, cdp_report.violations)
            self.assertEqual(set(cdp_report.gates.keys()), set(pm.REQUIRED_J6_GATES))
            self.assertTrue(all(g["status"] == "PASS" for g in cdp_report.gates.values()))
            self.assertEqual(cdp_report.absent["conductor_admission_binding"], "AUSENTE")
            self.assertEqual(cdp_report.absent["timing_label"], "VM")

            # Verify target descriptor binding for both cdp and godot policies without relabeling
            cdp_state = schema.build_state(
                [],
                kind="cdp",
                url="https://example.test",
                title="CDP Tab",
                ax_sha256="a" * 64,
                ax_stable_sha256="b" * 64,
                prune="prune_ax_v1",
                raw_count=0,
                viewport={"w": 1280, "h": 720, "dpr_ppm": 1000000, "scroll_x": 0, "scroll_y": 0},
            )
            registry = sr.SessionRegistry(clock=m7.FakeClock(10_000))
            cdp_handle = registry.register("cdp_tab", "tab-1", object(), owned=True)
            self.assertEqual(cdp_handle, sr.Handle("cdp_tab", "tab-1", 1))
            with self.assertRaises(sr.RegistryError):
                sr.Handle("cdp_tab", "tab-1", 0)
            with self.assertRaises(sr.RegistryError):
                sr.Handle("invalid_kind", "tab-1", 1)
            cdp_desc = schema.build_target_descriptor(
                cdp_handle,
                0,
                cdp_state,
                frame_seq=1,
            )
            self.assertEqual(pm.bind_descriptor_to_memory_sample(cdp_desc, cdp_sample, source_policy="cdp"), ())
            zero_gen_desc = {
                **cdp_desc,
                "handle": {"kind": "cdp_tab", "canonical_id": "tab-1", "generation": 0},
            }
            self.assertTrue(
                any(
                    v.startswith("invalid_registry_handle:")
                    for v in pm.bind_descriptor_to_memory_sample(zero_gen_desc, cdp_sample, source_policy="cdp")
                )
            )
            self.assertIn(
                "descriptor_source_policy_relabeling_forbidden",
                pm.bind_descriptor_to_memory_sample(cdp_desc, cdp_sample, source_policy="godot"),
            )

            godot_stat = {
                500: _stat_line(500, "godot-holdings", "S", 1, 5000),
                501: _stat_line(501, "godot-audio-worker", "S", 500, 5002),
                200: _stat_line(200, "wender-worker", "S", 1, 900),
                300: _stat_line(300, "xvfb-capsule", "S", 1, 800),
            }
            godot_roles = {500: "parent", 501: "utility", 200: "parent", 300: "parent"}
            godot_scopes = {500: "instance", 501: "instance", 200: "wender", 300: "capsule"}
            godot_smaps = {
                500: _smaps_text(80000),
                501: _smaps_text(12000),
                200: _smaps_text(32000),
                300: _smaps_text(20000),
            }
            godot_cgroup = {
                "memory.current": f"{96000 * 1024}\n",
                "memory.peak": f"{110000 * 1024}\n",
                "memory.swap.current": "0\n",
            }
            pause_cmd = {"tick": 4, "seq": 1, "cmd": "pause", "args": {}}
            outcome_rec = {
                "seq": 1,
                "requested_tick": 4,
                "applied_tick": 4,
                "order_seq_id": 10,
                "status": "accepted",
                "reason": None,
                "state_sha256": hashlib.sha256(b"holdings-state-tick-4").hexdigest(),
            }
            holdings_mig = pm.validate_holdings_inline_fixtures(
                pause_command=pause_cmd,
                outcome_record=outcome_rec,
                training_enabled=False,
                optional_training_status="FAIL_OOM",
            )
            self.assertTrue(holdings_mig["retail_migration_admitted"])
            self.assertFalse(holdings_mig["optional_training_blocked_retail"])

            godot_sample, godot_overheads = pm.sample_process_tree(
                sample_seq=0,
                logical_tick=4,
                clock=m7.FakeClock(50_000),
                instance_id="godot-holdings-1",
                capsule_id="capsule-godot",
                root_pids=(500,),
                pre_stat_map=godot_stat,
                post_stat_map=godot_stat,
                smaps_map=godot_smaps,
                pre_boot_id=BOOT_ID_A,
                cgroup_files=godot_cgroup,
                role_map=godot_roles,
                scope_map=godot_scopes,
                gpu_mode="included_in_pss",
                sample_delay_ns=80_000,
                max_bytes=budget.max_parse_bytes,
                max_sample_skew_ns=budget.max_sample_skew_ns,
            )
            godot_peak = pm.evaluate_peak_semantics(
                instance_id="godot-holdings-1",
                configured_limit_bytes=budget.max_cgroup_peak_bytes,
                samples=(godot_sample,),
                exclusive_ownership=True,
                freshness_evidence="fresh:godot-holdings-1:epoch-0",
                reported_peak_source="cgroup_peak",
            )
            godot_report = pm.evaluate_j6_memory_gates(
                instance_id="godot-holdings-1",
                source_policy="godot",
                handle_kind="godot_window",
                budget_config=budget,
                samples=(godot_sample,),
                overheads=godot_overheads,
                peak_evaluation=godot_peak,
                prerequisite_receipts=receipts,
                required_revision=REVISION_SHA,
                holdings_migration=holdings_mig,
            )
            self.assertTrue(godot_report.passed, godot_report.violations)

            godot_state = schema.build_state(
                [],
                kind="godot",
                url="godot://holdings/main",
                title="Holdings",
                ax_sha256="c" * 64,
                ax_stable_sha256="d" * 64,
                prune="prune_ax_v1",
                raw_count=0,
                viewport={"w": 1280, "h": 720, "dpr_ppm": 1000000, "scroll_x": 0, "scroll_y": 0},
            )
            godot_handle = registry.register("godot_window", "holdings-1", object(), owned=True)
            self.assertEqual(godot_handle, sr.Handle("godot_window", "holdings-1", 2))
            godot_desc = schema.build_target_descriptor(
                godot_handle,
                0,
                godot_state,
                frame_seq=4,
            )
            self.assertEqual(pm.bind_descriptor_to_memory_sample(godot_desc, godot_sample, source_policy="godot"), ())

            partial_sample, _ = pm.sample_process_tree(
                sample_seq=0,
                logical_tick=1,
                clock=m7.FakeClock(10_000),
                instance_id="cdp-browser-1",
                capsule_id="capsule-cdp",
                root_pids=(100,),
                pre_stat_map=pre_stat,
                post_stat_map={k: v for k, v in pre_stat.items() if k != 104},
                smaps_map=smaps,
                pre_boot_id=BOOT_ID_A,
                cgroup_files=cgroup_files,
                role_map=role_map,
                scope_map=scope_map,
            )
            mutations = {
                "missing_budget_config": lambda: pm.evaluate_j6_memory_gates(
                    instance_id="cdp-browser-1",
                    source_policy="cdp",
                    handle_kind="cdp_tab",
                    budget_config=None,
                    samples=(cdp_sample,),
                    overheads=cdp_overheads,
                    peak_evaluation=cdp_peak,
                ),
                "swap_exceeded": lambda: pm.evaluate_j6_memory_gates(
                    instance_id="cdp-browser-1",
                    source_policy="cdp",
                    handle_kind="cdp_tab",
                    budget_config=budget,
                    samples=(replace(cdp_sample, swap_current_bytes=4096),),
                    overheads=cdp_overheads,
                    peak_evaluation=cdp_peak,
                ),
                "partial_tree_membership_changed": lambda: pm.evaluate_j6_memory_gates(
                    instance_id="cdp-browser-1",
                    source_policy="cdp",
                    handle_kind="cdp_tab",
                    budget_config=budget,
                    samples=(partial_sample,),
                    overheads=cdp_overheads,
                    peak_evaluation=cdp_peak,
                ),
                "add_pss_and_cgroup": lambda: pm.evaluate_j6_memory_gates(
                    instance_id="cdp-browser-1",
                    source_policy="cdp",
                    handle_kind="cdp_tab",
                    budget_config=budget,
                    samples=(cdp_sample,),
                    overheads=cdp_overheads,
                    peak_evaluation=cdp_peak,
                    scope_checks={"add_pss_and_cgroup": True},
                ),
                "ancestor_child_cgroup_overlap": lambda: pm.evaluate_j6_memory_gates(
                    instance_id="cdp-browser-1",
                    source_policy="cdp",
                    handle_kind="cdp_tab",
                    budget_config=budget,
                    samples=(cdp_sample,),
                    overheads=cdp_overheads,
                    peak_evaluation=cdp_peak,
                    scope_checks={
                        "cgroup_paths": (
                            "/sys/fs/cgroup/wender/capsule-a",
                            "/sys/fs/cgroup/wender/capsule-a/browser-1",
                        )
                    },
                ),
                "divide_shared_browser_by_tabs": lambda: pm.evaluate_j6_memory_gates(
                    instance_id="cdp-browser-1",
                    source_policy="cdp",
                    handle_kind="cdp_tab",
                    budget_config=budget,
                    samples=(cdp_sample,),
                    overheads=cdp_overheads,
                    peak_evaluation=cdp_peak,
                    scope_checks={"tab_count": 3, "divide_shared_browser_by_tabs": True},
                ),
                "relabel_cdp_as_godot": lambda: pm.evaluate_j6_memory_gates(
                    instance_id="cdp-browser-1",
                    source_policy="cdp",
                    handle_kind="godot_window",
                    budget_config=budget,
                    samples=(cdp_sample,),
                    overheads=cdp_overheads,
                    peak_evaluation=cdp_peak,
                ),
                "python_peak_as_instance_ram": lambda: pm.evaluate_j6_memory_gates(
                    instance_id="cdp-browser-1",
                    source_policy="cdp",
                    handle_kind="cdp_tab",
                    budget_config=budget,
                    samples=(cdp_sample,),
                    overheads=cdp_overheads,
                    peak_evaluation=pm.evaluate_peak_semantics(
                        instance_id="cdp-browser-1",
                        configured_limit_bytes=budget.max_cgroup_peak_bytes,
                        samples=(cdp_sample,),
                        reported_peak_source="python_allocation_peak",
                        python_allocation_peak_bytes=4096,
                    ),
                ),
                "expected_failure_prerequisite": lambda: pm.evaluate_j6_memory_gates(
                    instance_id="cdp-browser-1",
                    source_policy="cdp",
                    handle_kind="cdp_tab",
                    budget_config=budget,
                    samples=(cdp_sample,),
                    overheads=cdp_overheads,
                    peak_evaluation=cdp_peak,
                    prerequisite_receipts={
                        **receipts,
                        "G-M1-1": {**receipts["G-M1-1"], "expected_failure": True},
                    },
                    required_revision=REVISION_SHA,
                ),
                "inferred_consumer_prerequisite": lambda: pm.evaluate_j6_memory_gates(
                    instance_id="cdp-browser-1",
                    source_policy="cdp",
                    handle_kind="cdp_tab",
                    budget_config=budget,
                    samples=(cdp_sample,),
                    overheads=cdp_overheads,
                    peak_evaluation=cdp_peak,
                    prerequisite_receipts={
                        **receipts,
                        "G-M7-1": {**receipts["G-M7-1"], "consumer_inferred": True},
                    },
                    required_revision=REVISION_SHA,
                ),
            }
            for name, build_mutant in mutations.items():
                with self.subTest(mutation=name):
                    mutant_report = build_mutant()
                    self.assertFalse(mutant_report.passed, f"mutation {name} unexpectedly passed")
                    self.assertTrue(mutant_report.violations)

    def test_volatile_leakage_rejected_and_canonical_bytes_deterministic(self):
        with _write_spy_stack():
            pre_stat, role_map, scope_map, smaps, cgroup_files, budget, receipts = _healthy_chromium_fixtures()
            sample, overheads = pm.sample_process_tree(
                sample_seq=0,
                logical_tick=1,
                clock=m7.FakeClock(10_000),
                instance_id="cdp-browser-1",
                capsule_id="capsule-cdp",
                root_pids=(100,),
                pre_stat_map=pre_stat,
                post_stat_map=pre_stat,
                smaps_map=smaps,
                pre_boot_id=BOOT_ID_A,
                cgroup_files=cgroup_files,
                role_map=role_map,
                scope_map=scope_map,
            )
            peak = pm.evaluate_peak_semantics(
                instance_id="cdp-browser-1",
                configured_limit_bytes=budget.max_cgroup_peak_bytes,
                samples=(sample,),
                exclusive_ownership=True,
                freshness_evidence="fresh:cdp-browser-1",
                reported_peak_source="cgroup_peak",
            )
            report = pm.evaluate_j6_memory_gates(
                instance_id="cdp-browser-1",
                source_policy="cdp",
                handle_kind="cdp_tab",
                budget_config=budget,
                samples=(sample,),
                overheads=overheads,
                peak_evaluation=peak,
                prerequisite_receipts=receipts,
                required_revision=REVISION_SHA,
            )
            encoded = pm.canonical_sample_bytes(report)
            decoded = json.loads(encoded.decode("utf-8"))
            self.assertTrue(decoded["passed"])
            self.assertEqual(decoded["absent"]["timing_label"], "VM")

            ram_frame = np.zeros((4, 4, 3), dtype=np.uint8)
            for leak_name, payload in (
                ("numpy_frame", {"frame": ram_frame}),
                ("raw_bytes", {"blob": b"raw-bytes"}),
                ("rgb_key", {"rgb": [1, 2, 3]}),
                ("raw_hook_receipt", {"raw_hook_receipt": {"seq": 1}}),
                ("tree_dump", {"tree_dump": "<div>raw</div>"}),
                ("checkpoint_bytes", {"checkpoint_bytes": "deadbeef"}),
                ("float_metric", {"pss_ratio": 0.5}),
            ):
                with self.subTest(leak=leak_name):
                    with self.assertRaises(pm.MalformedProcfsError):
                        pm.canonical_sample_bytes(payload)

            self.assertEqual(schema.TARGET_KINDS, ("cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window"))


if __name__ == "__main__":
    unittest.main()

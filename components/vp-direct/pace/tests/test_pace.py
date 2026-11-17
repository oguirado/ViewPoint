import concurrent.futures
from dataclasses import replace
from graphlib import CycleError
import hashlib
import importlib.util
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch


MODULE = Path(__file__).resolve().parents[1] / "pace.py"
spec = importlib.util.spec_from_file_location("vp_pace", MODULE)
pace = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pace
spec.loader.exec_module(pace)


CLICK = ("vp_channel_call", "act", "click")
OBSERVE = ("vp_channel_call", "observe")
READ = ("vp_read_page",)
PASTE = ("vp_terminal_call", "paste")
COPY = ("vp_terminal_call", "copy")
CMD = ("vp_terminal_call", "cmd")
REPLAY = ("vp_replay_workflow",)
FORGE = ("vp_forge_list",)
SITE = pace.SitePolicy("example.test", 100, "irreversible")
BASE = {
    CLICK: pace.OperationPolicy("preparar", True, False),
    OBSERVE: pace.OperationPolicy("observar", False, False),
    READ: pace.OperationPolicy("leer", False, False),
}


def request(key, *, rid="one", session="s-one", caller="agent", epoch=1, dry_run=False,
            risk="preparar", extra=None):
    tool, *selectors = key
    args = {}
    if selectors:
        args["op"] = selectors[0]
    if tool == "vp_channel_call":
        args.update(backend="cdp", risk_class=risk, require_vision_witness=False)
        if len(selectors) > 1:
            args["action"] = selectors[1]
    args.update(extra or {})
    return {
        "schema": "vp.control.req/v1", "request_id": rid, "op": tool,
        "context": {"caller": caller, "agent_instance": "fixture-agent",
                    "session_id": session, "authority_epoch": epoch},
        "args": args, "dry_run": dry_run,
    }


def digest(value):
    return hashlib.sha256(pace._validation.canonical_bytes(value)).hexdigest()


class PaceTests(unittest.TestCase):
    def setUp(self):
        storage = TemporaryDirectory(dir=MODULE.parent / "tests")
        self.addCleanup(storage.cleanup)
        self.db = Path(storage.name) / "pace.sqlite3"
        self.now = 100_000_000
        self.store = self.new_store()
        self.sites = {
            "s-one": SITE, "s-two": SITE,
            "s-other": replace(SITE, site_group="other.test"),
            "s-unset": replace(SITE, site_group="unset.test", min_interval_ms=None),
        }
        self.approvals = set()

    def new_store(self, *, path=None, boot_id="fixture-boot"):
        return pace.SitePace(path or self.db, clock=lambda: self.now, boot_id=boot_id)

    def gate(self, policies=BASE, *, store=None):
        return pace.PaceGate(policies, site_for=lambda req: self.sites.get(req["context"]["session_id"]),
                             has_approval=lambda *approval: approval in self.approvals,
                             pace=store or self.store)

    def decide(self, gate, value, registry_risk="observar"):
        pace._validation.validate_contract("vp.control.req/v1", value)
        return gate(value, registry_risk)

    def check_shared_pace(self, one, two):
        self.assertIs(self.decide(one, request(CLICK)), True)
        self.now += 99_999_999
        self.assertEqual(self.decide(two, request(CLICK, rid="two", session="s-two")), "PACE_LIMITED")
        self.assertIs(self.decide(two, request(CLICK, rid="other", session="s-other")), True)
        self.now += 1
        self.assertIs(self.decide(two, request(CLICK, rid="boundary", session="s-two")), True)

    def test_paces_same_site_across_sessions_at_exact_boundary(self):
        self.check_shared_pace(self.gate(), self.gate(store=self.new_store()))

    def test_shared_pace_crosses_process_boundary(self):
        # The child admits an act into the same store using the same clock domain.
        child = """
import importlib.util
import sys
spec = importlib.util.spec_from_file_location('vp_pace_child', sys.argv[1])
pace = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pace
spec.loader.exec_module(pace)
site = pace.SitePolicy('example.test', 100, 'irreversible')
store = pace.SitePace(sys.argv[2], clock=lambda: 100_000_000, boot_id='fixture-boot')
if not store.admit(site, dry_run=False):
    sys.exit(1)
"""
        subprocess.run([sys.executable, "-I", "-B", "-c", child, str(MODULE), str(self.db)],
                       check=True, timeout=10)
        self.assertEqual(self.decide(self.gate(), request(CLICK, session="s-two")), "PACE_LIMITED")

    def test_concurrent_same_site_only_one_admitted(self):
        gates = self.gate(), self.gate(store=self.new_store())
        barrier = Barrier(2, timeout=5)

        def attempt(index):
            barrier.wait()
            return gates[index](request(CLICK, rid=str(index)), "preparar")

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(pool.map(attempt, range(2)), [True, "PACE_LIMITED"])

    def test_dry_run_reads_pace_without_consuming(self):
        gate = self.gate()
        self.assertIs(self.decide(gate, request(CLICK, dry_run=True)), True)
        self.assertIs(self.decide(gate, request(CLICK, rid="act")), True)
        self.assertEqual(self.decide(gate, request(CLICK, rid="preview", dry_run=True)), "PACE_LIMITED")

    def test_restart_keeps_pace_until_provable_elapsed(self):
        self.assertIs(self.decide(self.gate(), request(CLICK)), True)
        self.now = 1
        gate = self.gate(store=self.new_store(boot_id="next-boot"))
        self.assertEqual(self.decide(gate, request(CLICK, rid="early")), "PACE_LIMITED")
        self.now = 100_000_000
        self.assertIs(self.decide(gate, request(CLICK, rid="late")), True)

    def test_missing_pace_denies_above_leer_and_preserves_reads(self):
        gate = self.gate()
        self.assertEqual(self.decide(gate, request(CLICK, session="s-unset")), "PACE_LIMITED")
        self.assertEqual(self.decide(gate, request(CLICK, session="s-missing")), "PACE_LIMITED")
        self.assertIs(self.decide(gate, request(READ, session="s-unset")), True)

    def test_risk_ceiling_cannot_be_raised_by_approval(self):
        self.sites["s-one"] = replace(SITE, risk_ceiling="leer")
        value = request(CLICK, risk="leer")
        self.approvals.add((SITE.site_group, "preparar", digest(value)))
        self.assertEqual(self.decide(self.gate(), value), "NO_LEASE")

    def test_delegated_operation_cannot_lower_frozen_risk(self):
        row = pace.OperationPolicy("leer", False, False)
        gate = self.gate({REPLAY: replace(row, calls=(CMD,)), CMD: row})
        value = request(REPLAY)
        self.approvals.add((SITE.site_group, "leer", digest(value)))
        self.assertEqual(self.decide(gate, value), "HUMAN_NEEDED")
        self.approvals.add((SITE.site_group, "enviar", digest(value)))
        self.assertIs(self.decide(gate, value), True)

    def test_enviar_requires_approval_for_agent_and_operator(self):
        gate = self.gate()
        for caller in ("agent", "operator"):
            with self.subTest(caller=caller):
                value = request(CLICK, caller=caller, risk="enviar")
                self.assertEqual(self.decide(gate, value), "HUMAN_NEEDED")
        operator = request(CLICK, caller="operator", risk="enviar")
        self.approvals.add((SITE.site_group, "enviar", digest(operator)))
        self.assertIs(self.decide(gate, operator), True)

    def test_effect_sensitive_grant_is_bound_to_specific_request(self):
        gate = self.gate({**BASE, CLICK: replace(BASE[CLICK], effect_sensitive=True)})
        exact = request(CLICK, extra={"target": {"cdp_target_id": "fixture-a"}})
        self.approvals.add((SITE.site_group, "preparar", digest(exact)))
        for changed in (
            request(CLICK, rid="different", extra=exact["args"]),
            request(CLICK, epoch=2, extra=exact["args"]),
            request(CLICK, extra={"target": {"cdp_target_id": "fixture-b"}}),
            request(CLICK, session="s-other", extra=exact["args"]),
        ):
            with self.subTest(changed=changed):
                self.assertEqual(self.decide(gate, changed), "HUMAN_NEEDED")
        old_site = self.sites["s-one"]
        self.sites["s-one"] = self.sites["s-other"]
        self.assertEqual(self.decide(gate, exact), "HUMAN_NEEDED")
        self.sites["s-one"] = old_site
        self.assertIs(self.decide(gate, exact), True)

    def check_operation_specific(self, gate):
        self.assertIs(self.decide(gate, request(OBSERVE, risk="observar")), True)
        self.assertEqual(self.decide(gate, request(CLICK)), "HOLD")

    def test_policy_is_closed_at_operation_and_action(self):
        self.check_operation_specific(self.gate({OBSERVE: BASE[OBSERVE]}))
        self.assertEqual(self.decide(self.gate(), request(("vp_channel_call", "act", "focus"))), "HOLD")
        with self.assertRaises(ValueError):
            self.gate({("vp_terminal_call",): pace.OperationPolicy("leer", False, False)})

    def test_direct_selections_vetoed_even_with_grant(self):
        keys = (PASTE, COPY, ("vp_channel_call", "act", "key"), READ)
        row = pace.OperationPolicy("leer", False, False)
        gate = self.gate({**{key: row for key in keys}, READ: replace(row, selections=frozenset({"PRIMARY"}))})
        for value in (request(PASTE, extra={"session": "fixture"}), request(COPY, extra={"text": "synthetic"}),
                      request(keys[2], extra={"key": "Control+V"}), request(READ)):
            with self.subTest(tool=value["op"], args=value["args"]):
                self.approvals.add((SITE.site_group, "enviar", digest(value)))
                self.assertEqual(self.decide(gate, value, "enviar"), "NO_LEASE")

    def indirect_policies(self):
        row = pace.OperationPolicy("leer", False, False)
        return {REPLAY: replace(row, calls=(FORGE,)), FORGE: replace(row, calls=(PASTE,)), PASTE: row}

    def check_indirect_veto(self, gate):
        self.assertEqual(self.decide(gate, request(REPLAY)), "NO_LEASE")

    def test_indirect_clipboard_read_vetoed(self):
        self.check_indirect_veto(self.gate(self.indirect_policies()))

    def test_unreviewed_or_cyclic_delegate_refused(self):
        rows = self.indirect_policies()
        with self.assertRaises(ValueError):
            self.gate({key: row for key, row in rows.items() if key != PASTE})
        with self.assertRaises(CycleError):
            self.gate({**rows, PASTE: replace(rows[PASTE], calls=(REPLAY,))})

    def test_mutant_tool_name_only(self):
        gate = self.gate({OBSERVE: BASE[OBSERVE]})
        with patch.object(pace, "_operation_key", side_effect=lambda tool, args: (tool, "observe")):
            with self.assertRaises(AssertionError):
                self.check_operation_specific(gate)

    def test_mutant_pace_per_process(self):
        separate = self.new_store(path=self.db.with_name("other-process.sqlite3"))
        with self.assertRaises(AssertionError):
            self.check_shared_pace(self.gate(), self.gate(store=separate))

    def test_mutant_indirect_clipboard_read(self):
        rows = self.indirect_policies()
        rows[FORGE] = replace(rows[FORGE], calls=())
        with self.assertRaises(AssertionError):
            self.check_indirect_veto(self.gate(rows))

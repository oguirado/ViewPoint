"""Verify A1 with synthetic evidence and a non-GUI process for detach custody."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
import subprocess
import sys
from threading import Barrier
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import attach


class FixtureSources:
    def __init__(self, pid):
        self.window = attach.WindowIdentity(":0", 42, pid, "native-incarnation")
        cdp_ref = attach.EvidenceRef("evidence/cdp", 1, 1, "a" * 64)
        atspi_ref = attach.EvidenceRef("evidence/atspi", 1, 1, "b" * 64)
        self.cdp = (attach.CdpWitness(cdp_ref, self.window, "fixture-cdp", "fixture-target"),)
        self.atspi = (attach.AtspiWitness(atspi_ref, self.window, ":1.42", "/fixture/object"),)

    def current_window(self, xid):
        return self.window if xid == self.window.xid else None

    def cdp_targets(self, window):
        return self.cdp

    def atspi_objects(self, window):
        return self.atspi

    def request(self):
        return {
            "op": "attach",
            "xid": hex(self.window.xid),
            "witness": self.window.witness,
            "evidence": [asdict(witness.evidence) for witness in (*self.cdp, *self.atspi)],
        }


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.sources = FixtureSources(pid=101)
        self.service = attach.AttachmentService(self.sources, display=":0")
        self.binding = {
            "schema": "vp.surface_binding/v1", "endpoint": "fixture-broker-endpoint",
            "surface_id": "fixture-surface", "owner": "fixture-owner", "purpose": "attach",
            "incarnation": "fixture-binding-incarnation", "authority_epoch": 1, "obligations": [],
        }
        self.session_id = "fixture-session"

    def attach_window(self, args=None):
        return self.service.attach(
            self.sources.request() if args is None else args,
            session_id=self.session_id, binding=self.binding,
        )

    def detach_window(self):
        return self.service.detach({"op": "detach", "session_id": self.session_id})

    def assert_refused(self, code, args=None):
        with self.assertRaises(attach.AttachRefused) as refusal:
            self.attach_window(args)
        self.assertEqual(refusal.exception.code, code)
        self.assertIsNone(self.service.association(self.session_id))

    def test_associates_exact_window_and_independent_channel_objects(self):
        result = self.attach_window()
        self.assertEqual(result, {"session_id": self.session_id, "binding": self.binding})
        association = self.service.association(self.session_id)
        self.assertEqual(association.window, self.sources.window)
        self.assertEqual(association.cdp, self.sources.cdp[0])
        self.assertEqual(association.atspi, self.sources.atspi[0])

    def test_missing_or_reused_witness_is_refused(self):
        request = self.sources.request()
        with self.subTest("single reference violates the frozen minimum"):
            self.assert_refused("-32602", dict(request, evidence=request["evidence"][:1]))
        with self.subTest("an unverified digest cannot supplement one qualified witness"):
            wrong = dict(request["evidence"][1], sha256="c" * 64)
            self.assert_refused("identity_mismatch", dict(request, evidence=[request["evidence"][0], wrong]))
        for reference in (
            replace(self.sources.cdp[0].evidence, line_end=2, sha256="c" * 64),
            replace(self.sources.cdp[0].evidence, path="evidence/copied-cdp"),
        ):
            with self.subTest(reference=reference):
                self.sources.atspi = (replace(self.sources.atspi[0], evidence=reference),)
                self.assert_refused("identity_mismatch")

    def test_reused_xid_is_refused_before_backend_lookup(self):
        request = self.sources.request()
        self.sources.window = replace(self.sources.window, witness="new-incarnation")
        with patch.object(self.sources, "cdp_targets") as read_cdp:
            self.assert_refused("identity_mismatch", request)
        read_cdp.assert_not_called()

    def test_window_swap_during_corroboration_is_refused(self):
        window = self.sources.window
        reused = replace(window, witness="new-incarnation")
        with patch.object(self.sources, "current_window", side_effect=(window, reused)):
            self.assert_refused("identity_mismatch")

    def duplicate_title_fixture(self):
        window = self.sources.window
        other = replace(window, xid=43, pid=102, witness="second-incarnation")
        self.sources.cdp = (replace(self.sources.cdp[0], window=other),)
        return {window: "Same title", other: "Same title"}

    def test_duplicate_title_without_window_disambiguation_is_refused(self):
        self.duplicate_title_fixture()
        self.assert_refused("identity_mismatch")

    def test_shared_pid_without_window_disambiguation_is_refused(self):
        window = self.sources.window
        sibling = replace(window, xid=43, witness="sibling-incarnation")
        self.sources.atspi = (replace(self.sources.atspi[0], window=sibling),)
        self.assert_refused("identity_mismatch")

    def test_shared_pid_with_exact_window_evidence_is_accepted(self):
        request = self.sources.request()
        window = self.sources.window
        sibling = replace(window, xid=43, witness="sibling-incarnation")
        sibling_ref = attach.EvidenceRef("evidence/atspi", 2, 2, "c" * 64)
        self.sources.atspi += (
            replace(self.sources.atspi[0], window=sibling, object_path="/fixture/sibling", evidence=sibling_ref),
        )
        self.attach_window(request)
        self.assertEqual(self.service.association(self.session_id).atspi, self.sources.atspi[0])

    def test_two_cited_targets_for_the_same_window_are_ambiguous(self):
        other_ref = attach.EvidenceRef("evidence/cdp", 2, 2, "c" * 64)
        other_target = replace(self.sources.cdp[0], evidence=other_ref, target_id="second-target")
        self.sources.cdp += (other_target,)
        self.assert_refused("identity_mismatch")

    def test_attach_refuses_unexpected_wire_fields_and_invalid_binding(self):
        self.assert_refused("-32602", dict(self.sources.request(), title="Only a title"))
        self.binding["schema"] = "different-schema"
        self.assert_refused("-32602")

    def test_atspi_requires_unique_bus_name_and_object_path(self):
        valid = self.sources.atspi[0]
        for witness in (
            replace(valid, bus_name="org.a11y.atspi.Registry"),
            replace(valid, bus_name=":"),
            replace(valid, object_path="/fixture//object"),
        ):
            with self.subTest(witness=witness):
                self.sources.atspi = (witness,)
                self.assert_refused("identity_mismatch")

    def test_decimal_and_hex_xid_cannot_create_duplicate_bindings(self):
        self.attach_window()
        request = dict(self.sources.request(), xid=str(self.sources.window.xid))
        binding = dict(self.binding, surface_id="second-surface")
        with self.assertRaises(attach.AttachRefused) as refusal:
            self.service.attach(request, session_id="second-session", binding=binding)
        self.assertEqual(refusal.exception.code, "identity_mismatch")
        self.assertIsNotNone(self.service.association(self.session_id))
        self.assertIsNone(self.service.association("second-session"))

    def test_scoped_cdp_alias_cannot_be_bound_to_a_second_window(self):
        self.attach_window()
        self.sources.window = replace(self.sources.window, xid=43, witness="other-incarnation")
        self.sources.cdp = (replace(self.sources.cdp[0], window=self.sources.window),)
        self.sources.atspi = (
            replace(self.sources.atspi[0], window=self.sources.window, object_path="/fixture/other"),
        )
        with self.assertRaises(attach.AttachRefused) as refusal:
            self.service.attach(
                self.sources.request(), session_id="second-session",
                binding=dict(self.binding, surface_id="second-surface"),
            )
        self.assertEqual(refusal.exception.code, "identity_mismatch")
        self.assertIsNone(self.service.association("second-session"))

    def test_concurrent_attach_publishes_only_one_binding(self):
        request = self.sources.request()
        ready = Barrier(2, timeout=5)

        def register(session_id):
            ready.wait()
            try:
                self.service.attach(request, session_id=session_id, binding=self.binding)
            except attach.AttachRefused as error:
                return error.code
            return "attached"

        sessions = (self.session_id, "second-session")
        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertCountEqual(executor.map(register, sessions), ("attached", "identity_mismatch"))
        self.assertEqual(sum(self.service.association(s) is not None for s in sessions), 1)

    def assert_foreign_process_survives_detach(self, *, close_mutant=False):
        # The child models a foreign app. Its only shutdown operation is kept by the test.
        child = subprocess.Popen(
            [sys.executable, "-IB", "-c", "import sys; sys.stdin.buffer.read()"],
            stdin=subprocess.PIPE,
        )
        try:
            self.sources = FixtureSources(pid=child.pid)
            self.service = attach.AttachmentService(self.sources, display=":0")
            receipt = self.attach_window()
            self.assertEqual(receipt["session_id"], self.session_id)
            self.assertIsNone(child.poll(), "foreign app exited before detach")
            if close_mutant:
                detach = self.service.detach

                def closing_detach(args):
                    result = detach(args)
                    child.stdin.close()
                    child.wait(timeout=5)
                    return result

                self.service.detach = closing_detach
            with patch.object(self.sources, "current_window", side_effect=AssertionError("backend called")):
                self.assertEqual(self.detach_window(), {"detached": True, "closed_foreign": False})
            self.assertIsNone(self.service.association(self.session_id))
            self.assertIsNone(child.poll(), "foreign app was closed by detach")
        finally:
            child.stdin.close()
            child.wait(timeout=5)

    def test_detach_retires_local_binding_and_preserves_foreign_process(self):
        self.assert_foreign_process_survives_detach()

    def test_unknown_detach_refuses_without_touching_backend(self):
        with patch.object(self.sources, "current_window", side_effect=AssertionError("backend called")):
            with self.assertRaises(attach.AttachRefused) as refusal:
                self.detach_window()
        self.assertEqual(refusal.exception.code, "host_window_unavailable")

    def test_title_matching_mutant_is_detected(self):
        titles = self.duplicate_title_fixture()

        def select_by_title(candidates, references, window):
            return next(candidate for candidate in candidates if titles[candidate.window] == titles[window])

        with patch.object(attach, "_select_witness", side_effect=select_by_title):
            with self.assertRaisesRegex(AssertionError, "AttachRefused not raised"):
                self.assert_refused("identity_mismatch")

    def test_close_on_detach_mutant_is_detected(self):
        with self.assertRaisesRegex(AssertionError, "foreign app was closed by detach"):
            self.assert_foreign_process_survives_detach(close_mutant=True)


if __name__ == "__main__":
    unittest.main()

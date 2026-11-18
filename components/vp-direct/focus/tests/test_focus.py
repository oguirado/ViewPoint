import ast
from contextlib import contextmanager
import ctypes
from pathlib import Path
from struct import pack_into
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

DIRECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(DIRECT))

from focus import CoverageLost, FocusMonitor, FocusRefused, Readback
from focus import x11
from schemas.validation import validate_contract


def wire(response, *, xid=0, root=1, atom=5, sequence=0, subtype=0):
    data = ctypes.create_string_buffer(36)
    pack_into("=BB", data, 0, response, 99)
    if response in (9, 10):
        pack_into("=I", data, 4, xid)
    if response == 28:
        pack_into("=II", data, 4, root, atom)
        pack_into("=I", data, 32, sequence)
    if response == 35:
        pack_into("=H", data, 8, subtype)
        pack_into("=I", data, 24, xid)
    return data


class Source:
    def __init__(self, *, missing=False, active_after=0x10, focus_after=0x11):
        self.steps, self.queued = [], []
        self.missing, self.active_after, self.focus_after = missing, active_after, focus_after
        self.stamp = 0

    def clock(self):
        self.stamp += 1
        return self.stamp

    def __enter__(self):
        self.steps.append("open")
        return self

    def __exit__(self, *_):
        self.steps.append("close")

    def arm(self):
        self.steps.append("arm")
        if self.missing:
            raise CoverageLost("no checked subscription")
        return "0x10"

    def dispatch(self, *events):
        self.steps.append("dispatch")
        self.queued.extend(events)
        return "verified action"

    def finish(self):
        self.steps.append("finish")
        events = []
        for data in self.queued:
            kind, value = x11._decode_event(ctypes.addressof(data), root=1, active_atom=5, xi_opcode=99)
            if kind is not None:
                events.append((self.clock(), kind, value))
        self.queued.clear()
        return x11._readback(
            events, active_before=0x10, active_after=self.active_after, property_barrier=9,
            focus_before=0x11, focus_after=self.focus_after,
        )


class FocusTests(unittest.TestCase):
    def run_operation(self, source, action=None, *, qualified=True):
        return FocusMonitor(source, clock=source.clock).run_passive(
            "fixture", action or source.dispatch, qualified=qualified,
        )

    def test_hundred_passive_sequences_arm_before_dispatch(self):
        source = Source()
        for _ in range(100):
            result, trace = self.run_operation(source, lambda: source.dispatch(wire(35, subtype=13)))
            validate_contract("vp.focus_trace/v1", trace)
            self.assertEqual(result, "verified action")
            self.assertTrue(trace["continuous"])
            self.assertEqual([event["kind"] for event in trace["events"]], ["_NET_ACTIVE_WINDOW", "XInput2"])
            self.assertEqual(set(trace["events"][1]), {"kind", "mono_ns"})
        self.assertEqual(source.steps, ["open", "arm", "dispatch", "finish", "close"] * 100)

    def test_quiet_subscription_can_qualify_without_input(self):
        _, trace = self.run_operation(Source())
        self.assertTrue(trace["continuous"])
        self.assertEqual(len(trace["events"]), 1)  # Baseline, without invented activity.

    def test_nonqualifiable_operation_refuses_before_opening_source(self):
        source = Source()
        with self.assertRaises(FocusRefused) as refused:
            self.run_operation(source, qualified=False)
        self.assertEqual(refused.exception.code, "HUMAN_NEEDED")
        self.assertFalse(source.steps)

    def test_unavailable_subscription_refuses_before_dispatch(self):
        source = Source(missing=True)
        with self.assertRaises(FocusRefused) as refused:
            self.run_operation(source)
        self.assertEqual(refused.exception.code, "HUMAN_NEEDED")
        self.assertEqual(source.steps, ["open", "arm", "close"])

    def test_lost_connection_after_dispatch_refuses_without_retry(self):
        source = Source()
        with patch.object(source, "finish", side_effect=CoverageLost("disconnect")):
            with self.assertRaises(FocusRefused) as refused:
                self.run_operation(source)
        self.assertEqual(refused.exception.code, "UNCERTAIN_OUTCOME")
        self.assertEqual(source.steps, ["open", "arm", "dispatch", "close"])

    def test_endpoint_only_measurement_refuses_even_when_endpoints_equal(self):
        source = Source()
        end = ({"kind": "_NET_ACTIVE_WINDOW", "xid": "0x10", "mono_ns": 2},)
        with patch.object(source, "finish", return_value=Readback(end, False)):
            with self.assertRaises(FocusRefused) as refused:
                self.run_operation(source)
        self.assertEqual(refused.exception.code, "UNCERTAIN_OUTCOME")
        self.assertFalse(refused.exception.trace["continuous"])

    def assert_detects_steal_and_restore(self, source):
        events = (
            wire(10, xid=0x11), wire(9, xid=0x20),
            wire(35, subtype=10, xid=0x20), wire(35, subtype=9, xid=0x11),
        )
        with self.assertRaises(FocusRefused) as refused:
            self.run_operation(source, lambda: source.dispatch(*events))
        trace = refused.exception.trace
        self.assertEqual(refused.exception.code, "UNCERTAIN_OUTCOME")
        self.assertTrue(trace["continuous"])
        self.assertEqual([event["xid"] for event in trace["events"]],
                         ["0x10", "0x11", "0x20", "0x20", "0x11"])
        self.assertEqual([event["kind"] for event in trace["events"]],
                         ["_NET_ACTIVE_WINDOW", "FocusOut", "FocusIn", "FocusOut", "FocusIn"])

    def test_transient_steal_and_restore_is_rejected(self):
        self.assert_detects_steal_and_restore(Source())

    def test_mutant_sampling_only_endpoints_fails_steal_oracle(self):
        source = Source()
        # Deliberately discard the event queue, then claim the stable endpoints cover it.
        with patch.object(source, "finish", return_value=Readback((), True)):
            with self.assertRaises(AssertionError):
                self.assert_detects_steal_and_restore(source)
        self.assertEqual(len(source.queued), 4)
        self.assertEqual(source.steps.count("dispatch"), 1)

    def test_single_ordered_property_event_records_new_xid_and_refuses(self):
        source = Source(active_after=0x20)
        with self.assertRaises(FocusRefused) as refused:
            self.run_operation(source, lambda: source.dispatch(wire(28, sequence=8)))
        self.assertEqual(refused.exception.code, "UNCERTAIN_OUTCOME")
        trace = refused.exception.trace
        self.assertTrue(trace["continuous"])
        self.assertEqual(trace["events"][-1]["kind"], "_NET_ACTIVE_WINDOW")
        self.assertEqual(trace["events"][-1]["xid"], "0x20")

    def test_coalesced_or_late_property_writes_leave_coverage_unproven(self):
        for events in ((wire(28, sequence=7), wire(28, sequence=8)), (wire(28, sequence=9),)):
            with self.subTest(count=len(events)):
                source = Source()
                with self.assertRaises(FocusRefused) as refused:
                    self.run_operation(source, lambda: source.dispatch(*events))
                self.assertFalse(refused.exception.trace["continuous"])
                self.assertEqual(len(refused.exception.trace["events"]), 1)

    def test_coverage_gaps_refuse_topology_errors_and_unobserved_changes(self):
        cases = (
            (Source(), (wire(16),)),
            (Source(), (wire(17),)),
            (Source(), (wire(21),)),
            (Source(), (wire(0),)),
            (Source(), (wire(9 | 0x80),)),
            (Source(active_after=0x20), ()),
            (Source(focus_after=0x21), ()),
        )
        for source, events in cases:
            with self.subTest(source=source, events=events):
                with self.assertRaises(FocusRefused) as refused:
                    self.run_operation(source, lambda: source.dispatch(*events))
                self.assertEqual(refused.exception.code, "UNCERTAIN_OUTCOME")
                self.assertFalse(refused.exception.trace["continuous"])
                self.assertEqual(source.steps.count("dispatch"), 1)

    def test_input_decoder_reads_only_the_xinput2_event_header(self):
        original = x11._field
        for subtype in (13, 14, 15, 16, 17, 22, 23, 24):
            with self.subTest(subtype=subtype), patch.object(x11, "_field", wraps=original) as reads:
                data = wire(35, subtype=subtype)
                pack_into("=I", data, 16, 0x61616161)  # Raw detail, outside the header.
                self.assertEqual(x11._decode_event(
                    ctypes.addressof(data), root=1, active_atom=5, xi_opcode=99,
                ), ("XInput2", None))
                self.assertEqual([call.args[1] for call in reads.call_args_list], [0, 1, 8])

    def test_mutant_recording_keystroke_content_is_rejected_without_leak(self):
        source = Source()
        original = source.finish

        def record_key():
            readback = original()
            self.assertEqual(readback.events[0]["kind"], "XInput2")
            event = {**readback.events[0], "key": "SYNTHETIC_KEY"}
            return Readback((event,), readback.continuous)

        with patch.object(source, "finish", side_effect=record_key):
            with self.assertRaises(FocusRefused) as refused:
                self.run_operation(source, lambda: source.dispatch(wire(35, subtype=13)))
        self.assertEqual(refused.exception.code, "UNCERTAIN_OUTCOME")
        self.assertIsNone(refused.exception.trace)
        self.assertIsNone(refused.exception.__cause__)
        self.assertIsNone(refused.exception.__context__)
        self.assertNotIn("SYNTHETIC_KEY", str(refused.exception))

    def test_trace_rejects_events_outside_interval(self):
        source = Source()
        late = ({"kind": "XInput2", "mono_ns": 1000},)
        with patch.object(source, "finish", return_value=Readback(late, True)):
            with self.assertRaises(FocusRefused) as refused:
                self.run_operation(source)
        self.assertEqual(refused.exception.code, "UNCERTAIN_OUTCOME")
        self.assertIsNone(refused.exception.trace)

    def test_private_source_rejects_operator_and_remote_displays(self):
        for display in (":0", ":0.0", "localhost:0", "host:1", ":1.1", "xpra:1"):
            with self.subTest(display=display), self.assertRaises(CoverageLost):
                x11.X11FocusSource(display, 1)

    def test_native_symbols_resolve_without_opening_display(self):
        api = x11._XCB()
        self.assertTrue(callable(api.xi.xcb_input_xi_select_events_checked))
        self.assertTrue(callable(api.core.xcb_poll_for_queued_event))

    def test_native_subscribes_root_and_descendants_before_readback(self):
        source = x11.X11FocusSource(":9", 1)
        source._root, source._connection = 1, 2
        children = (ctypes.c_uint32 * 1)(0x11)
        setup = []

        def select_core(_conn, xid, value_mask, selection):
            self.assertEqual(value_mask, 1 << 11)
            return "core", xid, selection._obj.value

        def select_xi(_conn, xid, count, selection):
            mask = selection._obj
            self.assertEqual((count, mask.device, mask.length), (1, 1, 1))
            return "xi", xid, mask.bits

        @contextmanager
        def query_tree(request, xid):
            self.assertEqual(request, "xcb_query_tree")
            setup.append(("query", xid))
            reply = ctypes.create_string_buffer(32)
            pack_into("=H", reply, 16, int(xid == 1))
            yield ctypes.addressof(reply), 1

        source._api = SimpleNamespace(
            core=SimpleNamespace(xcb_change_window_attributes_checked=select_core,
                                 xcb_query_tree_children=lambda _: children),
            xi=SimpleNamespace(xcb_input_xi_select_events_checked=select_xi),
        )
        with patch.object(source, "_reply", query_tree), patch.object(source, "_check_request", setup.append):
            self.assertEqual(source._watch_tree(), {1, 0x11})
        focus = (1 << 9) | (1 << 10)
        raw_input = sum(1 << kind for kind in (13, 14, 15, 16, 17, 22, 23, 24))
        structure_and_focus = (1 << 19) | (1 << 21)
        self.assertEqual(setup, [
            ("core", 1, structure_and_focus | (1 << 22)), ("xi", 1, focus | raw_input), ("query", 1),
            ("core", 0x11, structure_and_focus), ("xi", 0x11, focus), ("query", 0x11),
        ])

    def test_live_script_is_syntax_checked_only(self):
        script = Path(__file__).with_name("LIVE_focus_trace.py")
        ast.parse(script.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

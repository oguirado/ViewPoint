"""P7 oracles using the integrated broker, demand coordinator and RAM stream."""

from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

DIRECT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(DIRECT))
from bus.broker import Broker
from bus.journal import Journal, JournalError
from perception.demand import DemandCoordinator
from perception.lifecycle import LifecycleError, PerceptionLifecycle, RuntimeEvidence
from perception import receipts
from perception.stream import PerceptionStream, StreamError
from perception.stream.tests.test_stream import put_screen
from schemas.validation import canonical_bytes, validate_contract
from vadapt import x11_ramcap


def example(name):
    return json.loads((DIRECT / "schemas/tests/examples" / (name + "_v1.valid.json")).read_bytes())


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory(dir=os.environ["P7_TEST_RAM"])
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.journal = Journal(self.root)
        self.checked = []
        self.references = []
        binding = example("vp.surface_binding")
        self.agent = dict(example("vp.demand"), surface_id=binding["surface_id"], consumer=binding["owner"])
        self.recording = dict(self.agent, consumer="recorder", demand_id="recording", purpose="recording")
        self.demand = DemandCoordinator(binding["surface_id"], interests=["VERIFIED"],
                                        authorize=lambda row: row in (self.agent, self.recording), drain=lambda: True)
        broker = Broker(self.root / "broker")
        self.addCleanup(broker.close)
        broker.bind("session", binding, lease_state="AGENT")
        put_screen(self.root / "screen", b"red" * 32)
        config = x11_ramcap.SourceConfig(str(self.root), "screen", 32, 1, "synthetic fixture")
        stream = PerceptionStream(binding, config, ring_slots=2, interval_ns=1000, ttl_ns=10**10,
                                  max_frame_bytes=4096, max_ring_bytes=8192,
                                  workspace_root=self.root / "rings")
        self.addCleanup(stream.close)
        self.lifecycle = PerceptionLifecycle(
            broker, "session", binding, self.demand, self.agent, stream, qualified=lambda row: row == binding,
            runtime=lambda: RuntimeEvidence(binding["incarnation"], (), 40),
            drain_agent=lambda row: row == binding, on_transition=self.publish,
        )

    def publish(self, receipt, proof):
        self.references.append(receipts.append_transition(self.journal, receipt, proof, agent="broker"))
        self.checked.append((receipt, proof))

    def call(self, op):
        receipt = self.lifecycle.vp_perception_call(dict(op=op, session_id="session", reason="fixture"))
        validate_contract("vp_perception_call." + op + ".result/v1", receipt)
        return receipt

    def expect_refusal(self, code, receipt, proof, **kwargs):
        with self.assertRaises(receipts.ReceiptError) as raised:
            receipts.verify_transition(receipt, proof, **kwargs)
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_integrated_cycle_binds_demand_identity_drain_charge_and_scrub(self):
        with self.demand.register(self.recording):
            awake, asleep, rewake, off, restored = [self.call(op) for op in ("wake", "sleep", "wake", "off", "wake")]
            self.call("off")
        self.assertEqual(len(self.checked), 6)
        self.assertTrue(all(row["complete"] for row, _ in self.checked))
        self.assertEqual(asleep["retained"], ["recording"])
        self.assertEqual(asleep["drained"], [self.agent["demand_id"]])
        self.assertEqual(asleep["charges"]["reserved_bytes"], awake["charges"]["reserved_bytes"])
        self.assertGreater(asleep["charges"]["reserved_bytes"], 0)
        self.assertEqual(asleep["tokens_retired"], [self.checked[0][1].observation.token.nonce])
        self.assertEqual(rewake["scrub"], dict(buffers=asleep["producers_stopped"], canary_ok=True))
        self.assertEqual(off["charges"]["reserved_bytes"], 0)
        self.assertTrue(off["scrub"]["canary_ok"])
        self.assertEqual(len({row["fresh_observation_id"] for row in (awake, rewake, restored)}), 3)

    def test_schema_valid_partial_ack_is_refused(self):
        self.call("wake")
        receipt, proof = self.checked[-1]
        partial = dict(receipt, acks=receipt["acks"][:-1])
        validate_contract(receipts.CONTRACT, partial)
        self.expect_refusal("HOLD", partial, proof)

    def test_failed_wake_keeps_retired_generation_and_next_wake_is_fresh(self):
        self.call("wake")
        self.call("sleep")
        with patch.object(self.lifecycle, "_qualified", side_effect=[True, False]):
            with self.assertRaisesRegex(LifecycleError, "^NO_LEASE$"):
                self.call("wake")
        recovered = self.call("wake")
        _, proof = self.checked[-1]
        self.assertGreater(proof.stream_before["stream_generation"],
                           proof.broker_records[0]["perception"]["stream_generation"])
        self.assertGreater(proof.observation.stream_generation, proof.stream_before["stream_generation"])
        self.assertTrue(recovered["complete"])

    def test_off_from_sleep_scrubs_previously_retired_ring_without_reissuing_stop(self):
        self.call("wake")
        asleep = self.call("sleep")
        off = self.call("off")
        self.assertEqual(off["tokens_retired"], [])
        self.assertEqual(off["producers_stopped"], [])
        self.assertEqual(off["scrub"], dict(buffers=asleep["producers_stopped"], canary_ok=True))

    def test_unsettled_reap_refuses_complete_receipt(self):
        self.call("wake")
        self.call("off")
        receipt, proof = self.checked[-1]
        old = proof.stream_before["ring_id"]
        self.assertEqual(proof.reap, {"reaped": [old], "retained": []})
        self.expect_refusal("HOLD", receipt, replace(proof, reap={"reaped": [], "retained": [old]}))
        self.expect_refusal("HOLD", receipt, replace(proof, reap=None))
        self.expect_refusal("HOLD", receipt, replace(proof, reap={"reaped": [old], "retained": ["foreign"]}))

    def test_absent_producer_or_observation_is_refused(self):
        self.call("wake")
        receipt, proof = self.checked[-1]
        self.expect_refusal("E_PRODUCER_ABSENT", receipt, replace(proof, observation=None))
        self.expect_refusal("E_PRODUCER_ABSENT", receipt,
                            replace(proof, stream_after=dict(proof.stream_after, ring_id=None)))
        stale = replace(proof.observation, observation_id="stale")
        self.expect_refusal("STALE_FRAME", receipt, replace(proof, observation=stale))

    def test_evidence_and_receipt_must_correlate(self):
        self.call("wake")
        self.call("sleep")
        receipt, proof = self.checked[-1]
        row = proof.broker_records[-1]
        wrong = dict(row, perception=dict(row["perception"], incarnation="foreign"))
        self.expect_refusal("identity_mismatch", receipt,
                            replace(proof, broker_records=(*proof.broker_records[:-1], wrong)))
        wrong_epoch = dict(row, perception=dict(row["perception"], authority_epoch=proof.binding["authority_epoch"] + 1))
        self.expect_refusal("identity_mismatch", receipt, replace(proof, broker_records=(wrong_epoch,)))
        self.expect_refusal("identity_mismatch", receipt, replace(proof, session_id="another-session"))
        self.expect_refusal("HOLD", receipt, replace(proof, broker_records=()))
        self.expect_refusal("HOLD", receipt,
                            replace(proof, demand_snapshot=dict(proof.demand_snapshot, claimed=99)))
        self.expect_refusal("HOLD", receipt, replace(proof, agent_drained=False))
        self.expect_refusal("HOLD", dict(receipt, tokens_retired=[]), proof)
        self.expect_refusal("HOLD", dict(receipt, charges=dict(receipt["charges"], reserved_bytes=0)), proof)
        missing = {key: value for key, value in proof.stream_after.items() if key != "fresh_observation_id"}
        self.expect_refusal("HOLD", receipt, replace(proof, stream_after=missing))
        self.expect_refusal("HOLD", self.call("status"), proof)

    def test_upstream_codes_preserved_and_w8u_stays_hold(self):
        for code in ("HOLD", "E_PRODUCER_ABSENT", "E_MODEL_NOT_CONFIGURED"):
            with self.subTest(code=code):
                refusal = self.expect_refusal(code, None, None, upstream_code=code)
                self.assertEqual(refusal.runtime_state, "HOLD")
                self.assertIs(refusal.worker_ui, False)

    def test_lineage_readback_hashes_the_full_line_and_replays_without_append(self):
        expected = [self.call(op) for op in ("wake", "sleep", "wake", "off")]
        path = self.root / "lineage.jsonl"
        raw = path.read_bytes()
        reopened = Journal(self.root)
        events = reopened.verify()
        self.assertEqual([event["payload"] for event in events], expected)
        lines = raw.splitlines(keepends=True)
        previous = "0" * 64
        for lamport, (line, reference, (receipt, proof)) in enumerate(
                zip(lines, self.references, self.checked, strict=True), start=1):
            entry = json.loads(line)
            digest = sha256(line).hexdigest()
            self.assertTrue(line.endswith(b"\n"))
            self.assertNotEqual(digest, sha256(line[:-1]).hexdigest())
            self.assertEqual(entry["prev_sha256"], previous)
            self.assertEqual(reference, dict(lamport=lamport, prev_hash=previous, event_hash=digest))
            self.assertEqual(append_reference(reopened, receipt, proof), reference)
            previous = digest
        self.assertEqual(path.read_bytes(), raw)

    def test_publication_uses_the_transition_snapshot_even_if_demand_changes(self):
        self.call("wake")
        original = self.lifecycle._on_transition

        def publish_after_new_demand(receipt, proof):
            with self.demand.register(self.recording):
                self.assertNotEqual(proof.demand_snapshot, self.demand.snapshot())
                self.assertEqual(receipt["demand_snapshot_sha256"],
                                 sha256(canonical_bytes(proof.demand_snapshot)).hexdigest())
                original(receipt, proof)

        with patch.object(self.lifecycle, "_on_transition", side_effect=publish_after_new_demand):
            receipt = self.call("sleep")
        self.assertEqual(receipt["retained"], [])
        self.assertEqual(self.journal.verify()[-1]["payload"], receipt)

    def test_lost_append_ack_fences_wake_and_retries_without_redispatch(self):
        path = self.root / "lineage.jsonl"
        writer = self.journal.append
        lost = []

        def lose_ack(**request):
            lost.append(writer(**request))
            raise OSError("append acknowledgement lost")

        with patch.object(self.lifecycle.stream, "wake", wraps=self.lifecycle.stream.wake) as wake:
            with patch.object(self.journal, "append", side_effect=lose_ack):
                with self.assertRaisesRegex(LifecycleError, "^HOLD$"):
                    self.call("wake")
            raw = path.read_bytes()
            self.journal = Journal(self.root)
            event, = self.journal.verify()
            with self.assertRaisesRegex(LifecycleError, "^HOLD$"):
                self.call("wake")
            with self.assertRaisesRegex(LifecycleError, "^HOLD$"):
                self.call("off")
            with self.assertRaisesRegex(LifecycleError, "^NO_LEASE$"):
                self.lifecycle.read_frame(event["payload"]["fresh_observation_id"])
            self.assertEqual(self.lifecycle.retry_transition_publication(), event["payload"])
            wake.assert_called_once()
        self.assertEqual(self.references, lost)
        self.assertEqual(path.read_bytes(), raw)
        self.assertTrue(self.lifecycle.read_frame(event["payload"]["fresh_observation_id"]))

    def test_redelivered_sleep_uses_original_broker_row_after_publisher_restart(self):
        self.call("wake")
        pending = []
        def before_append(receipt, proof):
            pending.append((receipt, proof))
            raise ConnectionError("P7 unavailable")

        with patch.object(self.lifecycle, "_on_transition", before_append):
            with self.assertRaisesRegex(LifecycleError, "^HOLD$"):
                self.call("sleep")
        receipt, proof = pending[0]
        path = self.root / "lineage.jsonl"
        first_line = path.read_bytes()
        self.assertEqual(len(self.journal.verify()), 1)
        ref = append_reference(Journal(self.root), receipt, proof)
        after_append = path.read_bytes()
        self.assertEqual(ref, dict(lamport=2, prev_hash=sha256(first_line).hexdigest(),
                                   event_hash=sha256(after_append[len(first_line):]).hexdigest()))
        self.journal = Journal(self.root)
        self.assertEqual(self.lifecycle.retry_transition_publication(), receipt)
        self.assertEqual(self.references[-1], ref, "redelivery changed the sleep reference")
        self.assertEqual(path.read_bytes(), after_append)
        self.call("wake")
        events = self.journal.verify()
        self.assertEqual([e["payload"]["to"] for e in events], ["ACTIVE", "ASLEEP", "ACTIVE"])
        self.assertEqual(events[1]["op_id"], sha256(canonical_bytes(proof.broker_records[-1])).hexdigest())

    def test_mutant_new_operation_id_on_redelivery_duplicates_sleep(self):
        publish = receipts.append_transition
        seen = set()
        def unstable_key(journal, receipt, proof, **kwargs):
            key = sha256(canonical_bytes(proof.broker_records[-1])).hexdigest()
            if key in seen:
                kwargs["op_id"] = "replacement-owner"
            seen.add(key)
            return publish(journal, receipt, proof, **kwargs)

        with patch.object(receipts, "append_transition", unstable_key):
            with self.assertRaisesRegex(AssertionError, "redelivery changed the sleep reference"):
                self.test_redelivered_sleep_uses_original_broker_row_after_publisher_restart()

    def test_retained_raw_reap_produces_no_new_lineage_event(self):
        self.call("wake")
        result = self.lifecycle.stream.status()
        path = self.root / "lineage.jsonl"
        raw = path.read_bytes()
        with patch.object(self.lifecycle.stream._capture, "Reap",
                          return_value={"reaped": [], "retained": [result["ring_id"]]}):
            with self.assertRaises(StreamError):
                self.call("off")
        self.assertEqual(path.read_bytes(), raw)
        self.call("off")
        self.assertEqual(len(self.journal.verify()), 2)

    def test_mismatched_payload_and_unknown_kind_do_not_append(self):
        self.call("wake")
        receipt, proof = self.checked[-1]
        path = self.root / "lineage.jsonl"
        raw = path.read_bytes()
        with self.assertRaisesRegex(receipts.ReceiptError, "snapshot digest differs"):
            append_reference(self.journal, dict(receipt, demand_snapshot_sha256="f" * 64), proof)
        with self.assertRaisesRegex(JournalError, "^unregistered event kind"):
            self.journal.append(agent="broker", op_id="wake-test", kind="transition.unknown", payload=receipt)
        self.assertEqual(path.read_bytes(), raw)

    def assert_mutant_admits_rejected_evidence(self, guard, receipt, proof, *, code="HOLD"):
        original = receipts._require
        hits = []

        def mutant(condition, detail, code="HOLD"):
            if detail == guard and not condition:
                hits.append(condition)
                return
            return original(condition, detail, code)

        with patch.object(receipts, "_require", side_effect=mutant):
            with self.assertRaises(self.failureException):
                self.expect_refusal(code, receipt, proof)
        self.assertEqual(hits, [False])

    def test_mutant_complete_on_partial_ack_is_killed(self):
        self.call("wake")
        receipt, proof = self.checked[-1]
        self.assert_mutant_admits_rejected_evidence(
            "transition acknowledgements differ from required participants",
            dict(receipt, acks=receipt["acks"][:-1]), proof,
        )

    def test_mutant_complete_with_retained_reap_is_killed(self):
        self.call("wake")
        self.call("off")
        receipt, proof = self.checked[-1]
        self.assert_mutant_admits_rejected_evidence(
            "Reap or scrub readback unverified", receipt,
            replace(proof, reap={"reaped": [], "retained": [proof.stream_before["ring_id"]]}),
        )

    def test_mutant_accepting_a_substituted_epoch_is_killed(self):
        self.call("wake")
        self.call("sleep")
        receipt, proof = self.checked[-1]
        row = proof.broker_records[-1]
        changed = dict(row, perception=dict(row["perception"], authority_epoch=proof.binding["authority_epoch"] + 1))
        self.assert_mutant_admits_rejected_evidence(
            "broker identity differs from binding", receipt, replace(proof, broker_records=(changed,)),
            code="identity_mismatch")


def append_reference(journal, receipt, proof):
    return receipts.append_transition(journal, receipt, proof, agent="broker")

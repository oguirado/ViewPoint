from concurrent.futures import ProcessPoolExecutor
import hashlib
import json
from multiprocessing import get_context
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from jsonschema import ValidationError

SURFACE = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(SURFACE))

from bus.journal import Journal, journal
from schemas.validation import validate_contract


def example(schema):
    filename = schema.replace("/", "_") + ".valid.json"
    return json.loads((SURFACE / "schemas/tests/examples" / filename).read_bytes())


def append_human(directory, op_id):
    return journal.Journal(directory).append(
        agent="operator", op_id=op_id, kind=journal.HUMAN, payload=example(journal.HUMAN)
    )


def transition_request():
    receipt = dict(example(journal.TRANSITION), acks=["identity", "capability", "stream"])
    return dict(agent="broker", op_id="transition-01", kind=journal.TRANSITION, payload=receipt)


def exit_after_transition_write(directory):
    class InterruptedJournal(Journal):
        @staticmethod
        def _write(*args):
            Journal._write(*args)
            os._exit(79)

    InterruptedJournal(directory).append(**transition_request())


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def resign(event):
    row = {key: value for key, value in event.items() if key != "sha256"}
    return canonical(dict(row, sha256=digest(canonical(row)))) + b"\n"


def run_example():
    return {
        "schema": journal.RUN,
        "run_id": "run-0001",
        "ts_open": 10,
        "ts_close": 11,
        "agent_id": "operator",
        "agent_version": "a" * 64,
        "seat": {"model": "fixture", "effort": "medium", "mode": "headless"},
        "task": {"id": "task-001", "kind": "verify", "language": "en"},
        "outcome": {"verdict": "green", "repair_loops": 0,
                    "findings": {"confirmed": 0, "refuted": 0}},
        "cost": {"tokens_in": None, "tokens_out": None, "tokens_cache": None,
                 "duration_ms": None, "cost_micro_usd": None},
        "memory": {"unit_ids": ["unit-001"], "levels": [1], "gate_passed": True},
        "evidence": [],
        "parents_context": [],
        "memory_arm": "none",
    }


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.scratch = TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.scratch.cleanup)
        self.directory = Path(self.scratch.name)
        self.path = self.directory / journal.FILENAME
        self.journal = Journal(self.directory)

    def append(self, op_id="human-01", **changes):
        request = dict(agent="operator", op_id=op_id, kind=journal.HUMAN,
                       payload=example(journal.HUMAN))
        request.update(changes)
        return self.journal.append(**request)

    def test_genesis_contiguous_lamport_and_full_line_link(self):
        self.assertEqual(self.journal.verify(), [])
        self.assertFalse(self.path.exists())
        first = self.append()
        second = self.append("human-02")
        first_line, second_line = self.path.read_bytes().splitlines(keepends=True)
        self.assertEqual(first["prev_sha256"], "0" * 64)
        self.assertEqual([first["lamport"], second["lamport"]], [1, 2])
        self.assertEqual(second["prev_sha256"], digest(first_line))
        self.assertEqual(resign(first), first_line)
        self.assertEqual(resign(second), second_line)
        self.assertEqual(self.journal.verify(), [first, second])

    def test_exact_replay_across_restart_and_agent_scope(self):
        first = self.append()
        before = self.path.read_bytes()
        self.journal = journal.Journal(self.directory)
        self.assertEqual(self.append(evidence=[]), first)
        self.assertEqual(self.path.read_bytes(), before)
        second = self.append(agent="another-agent")
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(second["lamport"], 2)

    def test_mutated_replay_refused(self):
        self.append()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(journal.JournalError, "REQUEST_ID_CONFLICT"):
            self.append(evidence=[{"path": "proof.json", "sha256": "a" * 64}])
        self.assertEqual(self.path.read_bytes(), before)

    def test_human_take_and_return_validate_in_lineage(self):
        returned = dict(example(journal.HUMAN), state="AGENT", epoch_before=2, epoch_after=3)
        events = [self.append(), self.append("human-02", payload=returned)]
        self.assertEqual(self.journal.verify(), events)
        self.assertEqual([event["schema"] for event in events], [journal.EVENT] * 2)
        self.assertEqual([event["payload"]["state"] for event in events], ["HUMAN", "AGENT"])
        before = self.path.read_bytes()
        with self.assertRaises(ValidationError):
            self.append("human-03", payload=dict(returned, by="agent"))
        self.assertEqual(self.path.read_bytes(), before)

    def test_forge_reference_cycle_refused_without_creating_journal(self):
        receipt = example(journal.FORGE)
        with self.assertRaisesRegex(journal.JournalError, "forge lineage.event_hash must be computed"):
            self.append("forge-01", kind=journal.FORGE, payload=receipt)
        self.assertFalse(self.path.exists())

    def test_forge_event_precedes_seal_and_replays_original_reference(self):
        human = self.append()
        receipt = example(journal.FORGE)
        lineage = receipt.pop("lineage")
        reference = self.append("forge-01", kind=journal.FORGE, payload=receipt)
        human_line, forge_line = self.path.read_bytes().splitlines(keepends=True)
        self.assertEqual(reference, {"lamport": 2, "prev_hash": digest(human_line),
                                     "event_hash": digest(forge_line)})
        self.assertNotEqual(reference["event_hash"], digest(forge_line[:-1]))
        forge = json.loads(forge_line)
        self.assertEqual(forge["kind"], journal.FORGE)
        self.assertEqual(forge["payload"], receipt)
        self.assertEqual(self.journal.verify(), [human, forge])
        sealed = dict(receipt, lineage=dict(lineage, **reference))
        validate_contract(journal.FORGE, sealed)

        following = self.append("human-02")
        self.assertEqual(following["prev_sha256"], reference["event_hash"])
        before = self.path.read_bytes()
        self.journal = Journal(self.directory)
        self.assertEqual(self.append("forge-01", kind=journal.FORGE, payload=receipt), reference)
        with self.assertRaisesRegex(journal.JournalError, "REQUEST_ID_CONFLICT"):
            self.append("forge-01", kind=journal.FORGE, payload=receipt,
                        evidence=[{"path": "proof.json", "sha256": "a" * 64}])
        self.assertEqual(self.path.read_bytes(), before)

    def test_event_requires_a_frozen_contract(self):
        with patch.dict(journal._FROZEN, {journal.EVENT: self.directory / "missing-event"}):
            with self.assertRaisesRegex(journal.JournalError, "SCHEMA_GAP: lineage.event/v1"):
                self.append()
            with self.assertRaisesRegex(journal.JournalError, "SCHEMA_GAP: lineage.event/v1"):
                self.journal.verify()
        self.assertFalse(self.path.exists())

    def test_run_requires_a_frozen_contract(self):
        with patch.dict(journal._FROZEN, {journal.RUN: self.directory / "missing-run"}):
            with self.assertRaisesRegex(journal.JournalError, "SCHEMA_GAP: lineage.run/v1"):
                self.append("run-0001", kind=journal.RUN_CLOSE, payload=run_example())
        self.assertFalse(self.path.exists())

    def test_frozen_event_rejects_unknown_stored_kind(self):
        event = self.append()
        damaged = resign(dict(event, kind="unknown"))
        self.path.write_bytes(damaged)
        with self.assertRaisesRegex(journal.JournalError, "event 1: .*unknown"):
            self.journal.verify()
        with self.assertRaisesRegex(journal.JournalError, "event 1: .*unknown"):
            self.append("human-02")
        self.assertEqual(self.path.read_bytes(), damaged)

    def test_run_close_with_frozen_contract(self):
        run = run_example()
        payload = {"record": run, "memory_receipts": [], "gate": {
            "passed": True, "checks": [{"id": "fixture-oracle", "passed": True}],
        }}
        closed = self.append("run-0001", kind=journal.RUN_CLOSE, payload=payload)
        self.assertEqual(self.journal.verify(), [closed])
        self.assertEqual(closed["payload"], payload)
        del run["memory"]
        with self.assertRaises(ValidationError):
            self.append("run-0002", kind=journal.RUN_CLOSE, payload=payload)
        self.assertEqual(self.journal.verify(), [closed])

    def test_legacy_flat_run_close_is_preserved_across_restart(self):
        run = run_example()
        closed = self.append("run-0001", kind=journal.RUN_CLOSE, payload=run)
        before = self.path.read_bytes()
        self.journal = Journal(self.directory)
        self.assertEqual(self.journal.verify(), [closed])
        self.assertEqual(self.append("run-0001", kind=journal.RUN_CLOSE, payload=run), closed)
        self.assertEqual(closed["payload"], run)
        self.assertEqual(self.path.read_bytes(), before)

    def test_three_chains_use_distinct_files_and_records(self):
        broker = self.directory / "events.jsonl"
        memoryweb = self.directory / "receipts.jsonl"
        foreign = {
            broker: b'{"seq":1,"by":"broker"}\n',
            memoryweb: b'{"prev_receipt_sha256":null}\n',
        }
        for path, raw in foreign.items():
            path.write_bytes(raw)
        added = self.append()
        self.assertEqual(self.journal.verify(), [added])
        self.assertEqual({p: p.read_bytes() for p in foreign}, foreign)
        self.path.unlink()
        for path, raw in foreign.items():
            with self.subTest(path=path.name):
                self.path.hardlink_to(path)
                with self.assertRaisesRegex(journal.JournalError, "private regular journal"):
                    self.append()
                self.path.unlink()
                self.path.write_bytes(raw)
                with self.assertRaisesRegex(journal.JournalError, "invalid Lineage event"):
                    self.journal.verify()
                self.path.unlink()

    def test_link_without_lf_is_rejected(self):
        self.append()
        event = self.append("human-02")
        first_line = self.path.read_bytes().splitlines(keepends=True)[0]
        event["prev_sha256"] = digest(first_line.removesuffix(b"\n"))
        self.path.write_bytes(first_line + resign(event))
        with self.assertRaisesRegex(journal.JournalError, "previous line SHA-256 mismatch"):
            self.journal.verify()

    def test_mutant_link_hash_without_lf_is_rejected(self):
        with patch.object(journal, "_digest", side_effect=lambda raw: digest(raw.removesuffix(b"\n"))):
            self.append()
            second = self.append("human-02")
        first_line = self.path.read_bytes().splitlines(keepends=True)[0]
        self.assertEqual(second["prev_sha256"], digest(first_line[:-1]))
        with self.assertRaisesRegex(journal.JournalError, "previous line SHA-256 mismatch"):
            self.journal.verify()

    def test_mutant_lamport_gap_is_rejected(self):
        self.append()
        event = self.append("human-02")
        first_line = self.path.read_bytes().splitlines(keepends=True)[0]
        self.path.write_bytes(first_line + resign(dict(event, lamport=3)))
        with self.assertRaisesRegex(journal.JournalError, "noncontiguous lamport"):
            self.journal.verify()

    def test_mutant_memoryweb_chain_field_is_rejected(self):
        event = self.append()
        event["prev_receipt_sha256"] = event.pop("prev_sha256")
        self.path.write_bytes(resign(event))
        with self.assertRaisesRegex(journal.JournalError, "invalid Lineage event"):
            self.journal.verify()

    def test_torn_tail_blocks_verification_and_append(self):
        self.append()
        torn = self.path.read_bytes().removesuffix(b"\n")
        self.path.write_bytes(torn)
        with self.assertRaisesRegex(journal.JournalError, "JOURNAL_TORN"):
            self.journal.verify()
        with self.assertRaisesRegex(journal.JournalError, "JOURNAL_TORN"):
            self.append("human-02")
        self.assertEqual(self.path.read_bytes(), torn)

    def test_transition_replay_after_process_exits_between_append_and_ack(self):
        self.append()
        child = get_context("fork").Process(target=exit_after_transition_write, args=(self.directory,))
        child.start()
        child.join(timeout=10)
        if child.is_alive():
            child.terminate()
            child.join()
        self.assertEqual(child.exitcode, 79)
        reopened = Journal(self.directory)
        before = self.path.read_bytes()
        human, transition = before.splitlines(keepends=True)
        request = transition_request()
        self.assertEqual(reopened.verify()[-1]["payload"], request["payload"])
        self.assertEqual(reopened.append(**request), {
            "lamport": 2, "prev_hash": digest(human), "event_hash": digest(transition),
        })
        self.assertEqual(self.path.read_bytes(), before)
        request["payload"]["charges"]["resident_bytes"] += 1
        with self.assertRaisesRegex(journal.JournalError, "REQUEST_ID_CONFLICT"):
            reopened.append(**request)
        self.assertEqual(self.path.read_bytes(), before)

    def test_sleep_replay_returns_original_reference_behind_an_interleaved_event(self):
        awake = transition_request()
        self.journal.append(**awake)
        sleep = dict(awake, op_id="transition-sleep", payload=dict(
            awake["payload"], **{"from": "ACTIVE", "to": "ASLEEP"}, fresh_observation_id=None,
            acks=["agent-drain", "demand", "stream", "app"]))
        ref = self.journal.append(**sleep)
        self.append()
        raw = self.path.read_bytes()
        first, second, _ = raw.splitlines(keepends=True)
        self.assertEqual(ref, dict(lamport=2, prev_hash=digest(first), event_hash=digest(second)))
        self.assertEqual(Journal(self.directory).append(**sleep), ref, "sleep replay appended twice")
        self.assertEqual(self.path.read_bytes(), raw)

    def test_mutant_head_only_dedup_duplicates_sleep_on_restart(self):
        read = Journal._read
        def head_only(file):
            records, requests, ids, head = read(file)
            requests = {key: event for key, event in requests.items() if event == records[-1]}
            return records, requests, ids, head

        with patch.object(Journal, "_read", staticmethod(head_only)):
            with self.assertRaisesRegex(AssertionError, "sleep replay appended twice"):
                self.test_sleep_replay_returns_original_reference_behind_an_interleaved_event()

    def test_torn_transition_tail_blocks_replay_and_append(self):
        request = transition_request()
        self.journal.append(**request)
        torn = self.path.read_bytes().removesuffix(b"\n")
        self.path.write_bytes(torn)
        reopened = Journal(self.directory)
        with self.assertRaisesRegex(journal.JournalError, "JOURNAL_TORN"):
            reopened.verify()
        with self.assertRaisesRegex(journal.JournalError, "JOURNAL_TORN"):
            reopened.append(**request)
        self.assertEqual(self.path.read_bytes(), torn)

    def test_rehashed_transition_with_a_substituted_payload_is_refused(self):
        request = transition_request()
        self.journal.append(**request)
        event, = self.journal.verify()
        event["payload"]["charges"]["resident_bytes"] += 1
        changed = resign(event)
        self.path.write_bytes(changed)
        with self.assertRaisesRegex(journal.JournalError, "transition request SHA-256 mismatch"):
            Journal(self.directory).append(**request)
        self.assertEqual(self.path.read_bytes(), changed)

    def test_concurrent_processes_serialize_and_deduplicate(self):
        with ProcessPoolExecutor(max_workers=3) as pool:
            ids = ["human-01", "human-02", "human-01", "human-03"]
            events = list(pool.map(append_human, [self.directory] * len(ids), ids))
        self.assertEqual(events[0], events[2])
        self.assertEqual([event["lamport"] for event in self.journal.verify()], [1, 2, 3])

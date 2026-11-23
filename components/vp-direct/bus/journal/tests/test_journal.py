from concurrent.futures import ProcessPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator, ValidationError

SURFACE = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(SURFACE))

from bus.journal import journal


def example(schema):
    filename = schema.replace("/", "_") + ".valid.json"
    return json.loads((SURFACE / "schemas/tests/examples" / filename).read_bytes())


def append_human(directory, op_id):
    return journal.Journal(directory).append(
        agent="operator", op_id=op_id, kind=journal.HUMAN, payload=example(journal.HUMAN)
    )


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
        self.journal = journal.Journal(self.directory)

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
        with self.assertRaisesRegex(journal.JournalError, "SCHEMA_GAP: forge lineage.event_hash"):
            self.append("forge-01", kind=journal.FORGE, payload=receipt)
        self.assertFalse(self.path.exists())

    def test_run_requires_a_frozen_contract_or_an_explicit_validator(self):
        with patch.object(journal, "validate_contract", side_effect=KeyError(journal.RUN)):
            with self.assertRaisesRegex(journal.JournalError, "SCHEMA_GAP: lineage.run/v1"):
                self.append("run-0001", kind="run.close", payload=run_example())
        self.assertEqual(self.journal.verify(), [])

    def test_run_close_with_frozen_contract(self):
        self.assert_valid_run_close(example(journal.RUN))

    def test_run_close_with_explicit_validator_when_local_schema_is_missing(self):
        upstream = os.environ.get("VP_LINEAGE_RUN_SCHEMA")
        schema = Path(upstream) if upstream else SURFACE / "schemas/lineage.run_v1.schema.json"
        document = json.loads(schema.read_bytes())
        self.assertEqual(document["properties"]["schema"]["const"], journal.RUN)
        validator = Draft202012Validator(document)
        self.journal = journal.Journal(self.directory, run_validator=validator.validate)
        with patch.object(journal, "validate_contract", side_effect=KeyError(journal.RUN)):
            self.assert_valid_run_close(run_example())

    def assert_valid_run_close(self, run):
        closed = self.append("run-0001", kind="run.close", payload=run)
        self.assertEqual(self.journal.verify(), [closed])
        self.assertEqual(closed["payload"], run)
        invalid = dict(run)
        del invalid["memory"]
        with self.assertRaises(ValidationError):
            self.append("run-0002", kind="run.close", payload=invalid)
        self.assertEqual(self.journal.verify(), [closed])

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

    def test_mutant_link_hash_without_lf_is_rejected(self):
        self.append()
        event = self.append("human-02")
        first_line = self.path.read_bytes().splitlines(keepends=True)[0]
        event["prev_sha256"] = digest(first_line.removesuffix(b"\n"))
        self.path.write_bytes(first_line + resign(event))
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

    def test_concurrent_processes_serialize_and_deduplicate(self):
        with ProcessPoolExecutor(max_workers=3) as pool:
            ids = ["human-01", "human-02", "human-01", "human-03"]
            events = list(pool.map(append_human, [self.directory] * len(ids), ids))
        self.assertEqual(events[0], events[2])
        self.assertEqual([event["lamport"] for event in self.journal.verify()], [1, 2, 3])

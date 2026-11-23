"""Synthetic tm1 journal, protocol and mutation checks."""

from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from bus.tm import MicrotaskError, fold, refusal_code, validate_planned_intent
from bus.tm.projection import _Projection
from vendor.tasklight.model import schema


TASK = "t.writer.1"
FIRST = f"{TASK}.m1"
SECOND = f"{TASK}.m2"
RETRY = f"{TASK}.m3"
READER_TASK = "t.reader.1"
READER = f"{READER_TASK}.m1"


def broker(rec, **fields):
    return dict(fields, rec=rec, by="broker", v=1)


def step(mid, **fields):
    return dict(fields, microtask_id=mid, text="Synthetic operation")


def task(*steps, task_id=TASK, agent_id="writer", op="open"):
    return broker("task", op=op, plan=list(steps), task_id=task_id, agent_id=agent_id)


def enqueue(mid=FIRST, event_id="e1", *, after=(), supersedes=None, agent_id="writer"):
    task_id = mid.rsplit(".m", 1)[0]
    tm = dict(step(mid), v=1, task_id=task_id, after=list(after), supersedes=supersedes)
    return broker("enqueue", event_id=event_id, agent_id=agent_id, tm=tm,
                  microtask_id=mid, task_id=task_id)


def close(event_id="e1", verdict="VERIFIED", **fields):
    return broker("close", event_id=event_id, verdict=verdict, **fields)


def base_validator(intent):
    return ["unexpected tm extension"] if "tm" in intent else []


def validate(intent, projection, **kwargs):
    return validate_planned_intent(intent, projection, validate_base=base_validator, **kwargs)


class ProjectionTests(unittest.TestCase):
    def assert_refusal(self, code, records):
        with self.assertRaises(MicrotaskError) as error:
            fold(records)
        self.assertEqual(error.exception.code, code)

    def test_fold_replays_only_broker_records(self):
        journal = [
            task(step(FIRST)), enqueue(),
            broker("turn", agent_id="writer", event_id="e1"),
            close(tick=1),
        ]
        original = deepcopy(journal)
        for end, expected in enumerate(("pending", "queued", "acting", "verified"), 1):
            with self.subTest(expected=expected):
                self.assertEqual(fold(iter(journal[:end])).mt[FIRST]["state"], expected)
        self.assertEqual(fold(journal).snapshot(), fold(deepcopy(journal)).snapshot())
        self.assertEqual(journal, original)
        self.assertEqual(fold([]).snapshot()["microtasks"], {})

    def test_non_broker_record_cannot_change_state(self):
        for spoof in (dict(close(), by="agent"), {"rec": "close", "event_id": "e1", "verdict": "VERIFIED"}):
            with self.subTest(spoof=spoof):
                self.assert_refusal("NO_LEASE", [enqueue(), spoof])

    def test_non_broker_state_change_mutant_is_killed(self):
        rows = [enqueue(), dict(close(), by="agent")]
        with patch.object(_Projection, "feed", schema.Model.feed):
            with self.assertRaises(AssertionError):
                self.assert_refusal("NO_LEASE", rows)
            self.assertEqual(fold(rows).mt[FIRST]["state"], "verified")

    def test_duplicate_microtask_ids_are_refused(self):
        self.assert_refusal("TM_ID_REUSED", [enqueue(), close(), enqueue(event_id="new-event")])
        self.assert_refusal("TM_ID_REUSED", [enqueue(), enqueue(SECOND, "e1")])
        self.assert_refusal("TM_ID_REUSED", [task(step(FIRST), step(FIRST))])

    def test_reused_microtask_id_mutant_is_killed(self):
        class ReuseId(_Projection):
            def _rec_enqueue(self, record, changes):
                existing = self.mt.get(record["microtask_id"])
                if existing is not None:
                    existing.update(state="pending", event_id=None)
                super()._rec_enqueue(record, changes)

            def _check_enqueue(self, record):
                # Remove the port's admission fence as well as the model's reuse guard.
                pass

        rows = [enqueue(), close(), enqueue(event_id="new-event")]
        with patch("bus.tm.projection._Projection", ReuseId):
            with self.assertRaises(AssertionError):
                self.assert_refusal("TM_ID_REUSED", rows)
            self.assertEqual(fold(rows).mt[FIRST]["event_id"], "new-event")

    def test_stamp_mismatch_is_refused(self):
        rows = [task(step(FIRST), step(SECOND)), enqueue(), enqueue(SECOND, "e2")]
        for forged in (
            dict(enqueue(), microtask_id=SECOND),
            close(microtask_id=SECOND),
            close(task_id=READER_TASK),
            broker("turn", agent_id="writer", event_id="e1", microtask_id=SECOND),
            broker("turn", agent_id="writer", microtask_id=SECOND),
            broker("judge", attempt="e1.a1", microtask_id=SECOND),
            broker("txn", phase="begin", attempt="e1.a1", microtask_id=SECOND),
        ):
            with self.subTest(forged=forged):
                self.assert_refusal("TM_ID_MISMATCH", rows + [forged])

    def test_retry_dependency_and_rewind_replay(self):
        journal = [
            task(step(FIRST)), enqueue(),
            task(step(READER, after=[FIRST]), task_id=READER_TASK, agent_id="reader"),
            enqueue(READER, "reader-1", after=[FIRST], agent_id="reader"),
            broker("txn", phase="begin", attempt="e1.a1"),
            broker("txn", phase="rollback", attempt="e1.a1", base_point="p0"),
            close(verdict="ALL_REJECTED"),
        ]
        self.assertEqual(fold(journal).mt[FIRST]["state"], "rolled_back")
        self.assertEqual(fold(journal).mt[READER]["state"], "blocked")
        retry = [
            task(step(RETRY, supersedes=FIRST), op="plan"),
            enqueue(RETRY, "e3", supersedes=FIRST), close("e3", tick=1),
        ]
        rebuilt = fold(journal + retry)
        self.assertEqual(rebuilt.mt[FIRST]["superseded_by"], RETRY)
        self.assertEqual(rebuilt.mt[RETRY]["supersedes"], FIRST)
        self.assertEqual(rebuilt.mt[READER]["state"], "queued")
        self.assertEqual(rebuilt.admissible(READER), (True, None))
        rewound = fold(journal + retry + [broker("control", op="rewind", to="p0", microtasks=[RETRY])])
        self.assertEqual(rewound.mt[RETRY]["state"], "rolled_back")
        self.assertEqual(rewound.mt[READER]["state"], "blocked")

    def test_broker_retry_without_supersedes_is_refused(self):
        journal = [enqueue(), close(verdict="JOB_ERROR"), task(step(RETRY, supersedes=FIRST), op="plan")]
        self.assert_refusal("TM_BAD_SUPERSEDE", journal + [enqueue(RETRY, "e3")])

    def test_legacy_event_join_and_broker_requeues(self):
        legacy = broker("enqueue", agent_id="writer", event_id="e1")
        journal = [legacy, close(verdict="ACTUATOR_BUSY", requeued=True, drops=0),
                   close(verdict="STALE_FRAME", drops=1)]
        result = fold(journal)
        self.assertTrue(result.mt[FIRST]["derived"])
        self.assertEqual(result.mt[FIRST]["state"], "queued")
        self.assertEqual(result.mt[FIRST]["drops"], 1)
        self.assertEqual(fold(journal + [close(verdict="TURN_TIMEOUT", drops=3)]).mt[FIRST]["state"], "blocked")
        turn = broker("turn", agent_id="writer")
        self.assertEqual(fold(journal + [turn]).mt[FIRST]["state"], "acting")

    def test_explicit_turn_event_precedes_legacy_guess(self):
        journal = [enqueue(), enqueue(SECOND, "e2"), broker("turn", agent_id="writer", event_id="e2")]
        result = fold(journal)
        self.assertEqual(result.mt[FIRST]["state"], "queued")
        self.assertEqual(result.mt[SECOND]["state"], "acting")

    def test_frozen_tm_shape_and_unknown_dependency_are_refused(self):
        record = enqueue()
        del record["tm"]["after"]
        self.assert_refusal("TM_BAD_FIELD", [record])
        self.assert_refusal("TM_DEP_UNKNOWN", [enqueue(SECOND, "e2", after=[FIRST])])


class ProtocolTests(unittest.TestCase):
    def test_planned_intent_validates_base_without_tm(self):
        projection = fold([task(step(FIRST))])
        intent = enqueue()
        sent = Mock(side_effect=base_validator)
        self.assertEqual(validate_planned_intent(intent, projection, validate_base=sent), [])
        sent.assert_called_once_with({key: value for key, value in intent.items() if key != "tm"})
        self.assertEqual(projection.mt[FIRST]["state"], "pending")
        self.assertEqual(refusal_code(validate(intent, fold([]))), "PLAN_MISSING")

    def test_microtask_busy_is_per_agent(self):
        journal = [task(step(FIRST), step(SECOND)), enqueue(),
                   task(step(READER), task_id=READER_TASK, agent_id="reader")]
        for extra in ([], [broker("turn", agent_id="writer", event_id="e1")]):
            projection = fold(journal + extra)
            self.assertEqual(refusal_code(validate(enqueue(SECOND, "e2"), projection)), "MICROTASK_BUSY")
            self.assertEqual(validate(enqueue(READER, "reader-1", agent_id="reader"), projection), [])

    def test_protocol_retry_without_supersedes_is_refused(self):
        journal = [enqueue(), close(verdict="JOB_ERROR")]
        declared = fold(journal + [task(step(RETRY, supersedes=FIRST), op="plan")])
        self.assertEqual(refusal_code(validate(enqueue(RETRY, "e3"), declared)), "TM_BAD_SUPERSEDE")
        self.assertEqual(validate(enqueue(RETRY, "e3", supersedes=FIRST), declared, retry_of=FIRST), [])
        undeclared = fold(journal + [task(step(RETRY), op="plan")])
        self.assertEqual(refusal_code(validate(enqueue(RETRY, "e3"), undeclared, retry_of=FIRST)), "TM_BAD_SUPERSEDE")

    def test_protocol_refuses_unmet_dependency_or_reused_event(self):
        projection = fold([task(step(FIRST), step(SECOND, after=[FIRST]))])
        intent = enqueue(SECOND, "e2", after=[FIRST])
        self.assertEqual(refusal_code(validate(intent, projection)), "TM_DEP_UNMET")
        completed = fold([task(step(FIRST), step(SECOND, after=[FIRST])), enqueue(), close()])
        self.assertEqual(refusal_code(validate(enqueue(SECOND, "e1", after=[FIRST]), completed)), "TM_ID_REUSED")

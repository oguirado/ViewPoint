"""Seeded replay oracles over committed units and inert external effects."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from dataclasses import replace
from pathlib import Path
import json
import os
from random import Random
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch

from fixture import (ALIASES, PIXELS, Broker, Journal, LifecycleFixture,
                     canonical_line, digest, example, journal_request, loop_fixture, peer, rows)

from probe_findings import discontinuity

from bus.broker import BrokerError
from bus.journal import JournalError, journal
from perception.demand import DemandError
from perception.lifecycle import LifecycleError
from rewind.machine import Refused


def assert_reference(test, path, reference, position):
    lines = path.read_bytes().splitlines(keepends=True)
    line = lines[position - 1]
    test.assertTrue(line.endswith(b"\n"))
    test.assertEqual(line, canonical_line(json.loads(line)))
    previous = digest(lines[position - 2]) if position > 1 else "0" * 64
    test.assertEqual(json.loads(line)["prev_sha256"], previous)
    test.assertEqual(reference, dict(lamport=position, prev_hash=previous, event_hash=digest(line)))



def expected_cpus():
    """CPU set the replay must run on: VP_REPLAY_CPUS ("a-b" or "a,b,..."), default the timed set 16-19."""
    spec = os.environ.get("VP_REPLAY_CPUS", "16-19")
    cpus = set()
    for part in spec.split(","):
        lo, _, hi = part.strip().partition("-")
        cpus.update(range(int(lo), int(hi or lo) + 1))
    return cpus

class ReplayTests(unittest.TestCase):
    def test_rep_p4_p7_001_publication_discontinuity(self):
        for cut in (None, "before_append", "after_append"):
            with self.subTest(cut=cut):
                self.assertIsNone(discontinuity(cut))

    def scratch(self):
        return Path(self.enterContext(TemporaryDirectory(prefix="rep-", dir="/dev/shm")))

    def lifecycle(self):
        root = self.scratch()
        return LifecycleFixture(root, self.enterContext(ExitStack()))

    def guarded(self, code, call, *args, error=LifecycleError):
        with self.assertRaisesRegex(error, f"^{code}$"):
            call(*args)

    def test_publish_drain_wake_replays_counters_charges_and_delivery(self):
        self.assertEqual(os.sched_getaffinity(0), expected_cpus())
        for seed in range(4):
            with self.subTest(seed=seed):
                self.check_drain_schedule(seed)

    def check_drain_schedule(self, seed):
        fixture = self.lifecycle()
        prior = fixture.initial["fresh_observation_id"]
        entered, release = Barrier(2), Barrier(2)

        def drain():
            entered.wait(timeout=5)
            release.wait(timeout=5)
            return True

        fixture.drain = drain
        with fixture.demand.register(fixture.recorder) as old_handle, ThreadPoolExecutor(1) as pool:
            sleeping = pool.submit(fixture.call, "sleep")
            entered.wait(timeout=5)
            try:
                self.assertEqual(fixture.broker_states(), ["OFF", "WAKING", "ACTIVE"])
                self.guarded("NO_LEASE", fixture.lifecycle.read_frame, prior)
                if not seed & 2:
                    fixture.demand.publish(old_handle, 1, f"recorded-{seed}")
            finally:
                release.wait(timeout=5)
            sleep = sleeping.result(timeout=5)
            expected_queue = 0 if seed & 2 else 1
            self.assertEqual(fixture.demand.snapshot()["queued"]["VERIFIED"], expected_queue)
            self.assertEqual(sleep["retained"], ["recorder"])
            self.assertEqual(sleep["charges"], fixture.initial["charges"])
            proof = fixture.publications[-1][1]
            self.assertEqual(sleep["demand_snapshot_sha256"], digest(canonical_line(proof.demand_snapshot)))

            if seed & 2:
                fixture.demand.retire_owner()
                peer("publisher", fixture.root, seed, 78)
            fixture.reopen()
            fixture.drain = lambda: True
            with fixture.demand.resume_subscription(fixture.recorder) as handle, patch.object(
                    fixture.stream, "wake", wraps=fixture.stream.wake) as acquired:
                self.guarded("unexpected work sequence", fixture.demand.publish, handle, 1,
                             "duplicate", error=DemandError)
                before = fixture.ledger()
                self.assertEqual((before["t"], before["q"], before["flight"]), (1, 1, 0))
                if seed & 1:
                    agent = fixture.call("wake")
                work, external = fixture.lifecycle.wake_for_verified()
                if not seed & 1:
                    held = fixture.ledger()
                    self.assertEqual((held["t"], held["q"]), (1, 1))
                    self.guarded("NO_LEASE", fixture.lifecycle.read_frame, external["fresh_observation_id"])
                    agent = fixture.call("wake")
                self.assertEqual(work.reference, f"recorded-{seed}")
                self.assertEqual(agent["fresh_observation_id"], external["fresh_observation_id"])
                self.assertNotEqual(prior, agent["fresh_observation_id"])
                self.guarded("STALE_FRAME", fixture.lifecycle.read_frame, prior)
                self.assertEqual(fixture.lifecycle.read_frame(agent["fresh_observation_id"]), PIXELS)
                token = fixture.demand.work_token(work)
                external_path = fixture.root / "deliveries.jsonl"
                delivery = dict(reference=work.reference, flight=1)
                with fixture.demand.borrow(work, 97):
                    self.assertEqual(fixture.demand.snapshot()["borrowed_bytes"], 97)
                    self.guarded("delivery unclaimed, borrowed or already settled",
                                 fixture.demand.complete, work, error=DemandError)
                    self.assertEqual(fixture.demand.snapshot()["borrowed_bytes"], 97)
                peer("delivery", fixture.root, seed, 75)
                proof = fixture.publications[-1][1]

                # The delivery happened but acknowledgement has not. Reopen
                # both durable owners while the independent producer survives.
                fixture.reopen(recover_observation=lambda: proof.observation)
                self.assertIsNone(fixture.lifecycle.wake_for_verified())
                self.guarded("HOLD", fixture.lifecycle.reconcile_verified, token, lambda *_: False)
                def seen(candidate, flight):
                    return rows(external_path) == [dict(reference=candidate.reference, flight=flight)]
                self.assertEqual(fixture.lifecycle.reconcile_verified(token, seen), "SETTLED")
                self.assertEqual(fixture.lifecycle.reconcile_verified(token, self.fail), "SETTLED")
                acquired.assert_called_once()

            final = fixture.ledger()
            self.assertEqual({k: final[k] for k in ("t", "r", "d", "i", "q", "a", "flight", "generation")},
                             dict(t=1, r=0, d=0, i=0, q=0, a=0, flight=1, generation=2))
            self.assertEqual((final["work"], final["work_flights"]), ({token: "settled"}, {token: 1}))
            self.assertEqual(rows(external_path), [delivery])
            self.assertEqual(fixture.demand.snapshot()["claimed"], 0)
            self.assertEqual(fixture.demand.snapshot()["borrowed_bytes"], 0)
            self.assertEqual(fixture.call("off")["charges"]["reserved_bytes"], 0)
        self.assertEqual(fixture.broker_states(), ["OFF", "WAKING", "ACTIVE", "ASLEEP", "WAKING",
                                                   "ACTIVE", "OFF"])
        path = fixture.root / "lineage.jsonl"
        before = path.read_bytes()
        entries = Journal(fixture.root).verify()
        self.assertEqual([entry["payload"]["to"] for entry in entries], ["ACTIVE", "ASLEEP", "ACTIVE", "OFF"])
        for i, (receipt, _, ref) in enumerate(fixture.publications, start=1):
            assert_reference(self, path, ref, i)
            self.assertEqual(entries[i - 1]["payload"], receipt)
        self.assertEqual(path.read_bytes(), before)

    def assert_empty_queue_cannot_wake(self, fixture):
        before = fixture.ledger()
        prior_stream = fixture.stream.status()
        broker_path = fixture.root / "broker/events.jsonl"
        lineage = fixture.root / "lineage.jsonl"
        broker_prefix, lineage_prefix = broker_path.read_bytes(), lineage.read_bytes()
        self.assertEqual((before["perception"], before["q"]), ("ASLEEP", 1))
        self.assertEqual(fixture.demand.snapshot()["queued"]["VERIFIED"], 0)

        self.assertIsNone(fixture.lifecycle.wake_for_verified())
        self.assertEqual(fixture.ledger(), before, "absent work changed the durable goal")
        self.assertEqual(fixture.stream.status(), prior_stream)
        self.assertEqual(broker_path.read_bytes(), broker_prefix)
        self.assertEqual(lineage.read_bytes(), lineage_prefix)

    def test_absent_external_work_after_reopen_and_phantom_wake_mutant(self):
        fixture = self.lifecycle()
        fixture.call("sleep")
        fixture.reopen()
        self.assert_empty_queue_cannot_wake(fixture)

        def wake_without_work():
            fixture.call("wake")

        with patch.object(fixture.lifecycle, "wake_for_verified", side_effect=wake_without_work) as mutant:
            with self.assertRaisesRegex(AssertionError, "absent work changed the durable goal"):
                self.assert_empty_queue_cannot_wake(fixture)
        mutant.assert_called_once_with()
        self.assertEqual(fixture.ledger()["perception"], "ACTIVE")

    def test_empty_sleep_charge_mutant_is_rejected_by_durable_progress_gate(self):
        from rewind.machine import transitions

        nominal = self.lifecycle()
        nominal.call("sleep")
        self.assertEqual((nominal.ledger()["t"], nominal.ledger()["q"]), (1, 1))

        fixture = self.lifecycle()
        sleep = transitions.HANDLERS["sleep"]

        def omit_attempt(state, **data):
            before = state["t"]
            result = sleep(state, **data)
            self.assertEqual(state["t"], before + 1)
            state["t"] = before
            return result

        with patch.dict(transitions.HANDLERS, sleep=omit_attempt):
            self.guarded("NON_DESCENDING", fixture.call, "sleep", error=Refused)
        self.assertEqual(fixture.ledger()["t"], 0)
        self.assertEqual(fixture.broker_states(), ["OFF", "WAKING", "ACTIVE"])
        self.assertEqual([row["payload"]["to"] for row in fixture.journal.verify()], ["ACTIVE"])

    def test_wake_refuses_replayed_producer_witness_before_active(self):
        fixture = self.lifecycle()
        retired = fixture.publications[0][1].observation
        fixture.call("sleep")
        original_wake = fixture.stream.wake

        def substitute_retired_witness():
            fresh = original_wake()
            self.assertNotEqual(fresh.observation_id, retired.observation_id)
            return retired

        with patch.object(fixture.stream, "wake", side_effect=substitute_retired_witness) as capture:
            self.guarded("STALE_FRAME", fixture.call, "wake")
            capture.assert_called_once()
        self.assertEqual(fixture.broker_states(), ["OFF", "WAKING", "ACTIVE", "ASLEEP", "WAKING", "ASLEEP"])
        self.assertEqual([row["payload"]["to"] for row in fixture.journal.verify()], ["ACTIVE", "ASLEEP"])
        self.assertEqual((fixture.ledger()["t"], fixture.ledger()["wake_failures"]), (2, 1))
        self.assertEqual(fixture.stream.status()["reserved_bytes"], 0)
        self.guarded("NO_LEASE", fixture.lifecycle.read_frame, retired.observation_id)

    def test_retired_observation_negative_and_admission_mutant(self):
        fixture = self.lifecycle()
        retired = fixture.initial["fresh_observation_id"]
        fixture.call("sleep")
        live = fixture.call("wake")["fresh_observation_id"]
        executed = []
        act = lambda: executed.append("effect")
        self.guarded("STALE_FRAME", fixture.lifecycle.run_agent, "act", retired, act)
        self.assertEqual(executed, [])
        guard = fixture.lifecycle.run_agent

        def discard_observation(kind, _retired, perform):
            return guard(kind, live, perform)

        with patch.object(fixture.lifecycle, "run_agent", side_effect=discard_observation) as mutant:
            with self.assertRaises(AssertionError):
                self.guarded("STALE_FRAME", fixture.lifecycle.run_agent, "act", retired, act)
            self.assertEqual(executed, ["effect"])
            self.assertEqual(mutant.call_args.args[1], retired)
            mutant.assert_called_once()

    def broker_fixture(self, root):
        broker = self.enterContext(Broker(root / "broker"))
        broker.bind("session", example("vp.surface_binding"), ALIASES, lease_state="AGENT")
        return broker

    def assert_alias_exclusion(self, broker, seed):
        first, second = Random(seed).sample(ALIASES, 2)
        broker.try_acquire("owner", first, "inc", 1, "first")
        self.guarded("NO_LEASE", broker.try_acquire, "owner", second, "inc", 1, "second",
                     error=BrokerError)

    def test_alias_double_acquisition_negative_and_mutant(self):
        for seed in range(4):
            with self.subTest(seed=seed):
                self.assert_alias_exclusion(self.broker_fixture(self.scratch()), seed)
        mutant_broker = self.broker_fixture(self.scratch())
        admit = mutant_broker._admit
        def uncanonical(agent, target, incarnation, epoch):
            session, _ = admit(agent, target, incarnation, epoch)
            return session, target

        with patch.object(mutant_broker, "_admit", side_effect=uncanonical) as mutant:
            with self.assertRaises(AssertionError):
                self.assert_alias_exclusion(mutant_broker, 0)
            self.assertEqual(mutant.call_count, 2)
            self.assertNotEqual(mutant.call_args_list[0].args[1], mutant.call_args_list[1].args[1])

    def test_broker_process_death_after_external_send_keeps_request_fenced(self):
        for seed in range(4):
            with self.subTest(seed=seed):
                root = self.scratch()
                self.broker_fixture(root).close()
                peer("broker", root, seed, 77)
                broker = self.enterContext(Broker(root / "broker"))
                other_alias = next(alias for alias in ALIASES if alias != Random(seed).choice(ALIASES))
                lock = broker.lock_status(other_alias)
                self.assertEqual((lock["status"], lock["request_id"]), ("UNKNOWN", "effect"))
                for request, expected in (("effect", "DUPLICATE_EVENT"), ("next", "NO_LEASE")):
                    self.guarded(expected, broker.try_acquire, "owner", other_alias, "inc", 1, request,
                                 error=BrokerError)
                effects = root / "external.jsonl"
                self.assertEqual(rows(effects), [{"request": "effect", "alias": Random(seed).choice(ALIASES)}])
                broker.reconcile(lock["lock_id"], "effect", "VERIFIED", digest(effects.read_bytes()))
                self.guarded("DUPLICATE_EVENT", broker.try_acquire, "owner", other_alias, "inc", 1,
                             "effect", error=BrokerError)
                self.assertEqual(len(rows(effects)), 1)

    def test_journal_restart_before_ack_and_torn_tail(self):
        for seed in range(4):
            with self.subTest(seed=seed):
                root = self.scratch()
                Journal(root).append(**journal_request("prefix"))
                peer("journal", root, seed, 79)
                path = root / "lineage.jsonl"
                clean = path.read_bytes()
                reopened = Journal(root)
                reference = reopened.append(**journal_request(seed))
                assert_reference(self, path, reference, 2)
                self.assertEqual(path.read_bytes(), clean)
                self.assertEqual([row["op_id"] for row in reopened.verify()], ["rep-seed-prefix", f"rep-seed-{seed}"])
                changed = journal_request(seed)
                changed["payload"]["charges"]["resident_bytes"] += 1
                self.guarded("REQUEST_ID_CONFLICT", lambda: reopened.append(**changed), error=JournalError)
                prefix, tail = clean.splitlines(keepends=True)
                torn = prefix + tail[:len(tail) - (1, 2, 5, 13)[seed]]
                path.write_bytes(torn)
                for operation in (Journal(root).verify, lambda: Journal(root).append(**journal_request("later"))):
                    self.guarded("JOURNAL_TORN at event 2", operation, error=JournalError)
                    self.assertEqual(path.read_bytes(), torn)

    def test_journal_reference_without_lf_mutant(self):
        root = self.scratch()
        peer("journal", root, 0, 79)
        path = root / "lineage.jsonl"
        initial = path.read_bytes()
        original = journal._lineage_reference

        def omit_lf(event):
            return dict(original(event), event_hash=digest(canonical_line(event)[:-1]))

        with patch.object(journal, "_lineage_reference", side_effect=omit_lf) as mutant:
            reference = Journal(root).append(**journal_request(0))
            self.assertEqual(mutant.call_args.args[0]["op_id"], "rep-seed-0")
            mutant.assert_called_once()
            with self.assertRaises(AssertionError):
                assert_reference(self, path, reference, 1)
        self.assertEqual(path.read_bytes(), initial)

    def test_checkpoint_substitution_preserves_native_fence_after_reopen(self):
        from rewind import blender_rw1
        from data.locator import ArtifactLocator
        from vendor.rewind import fakes
        from vqa3d_bridge import QualityResult, RenderPredicate

        for seed in range(2):
            with self.subTest(seed=seed):
                root = self.scratch()
                capsule = fakes.Capsule(rt=str(root / "runtime"))
                self.addCleanup(capsule.close)
                directory = root / "artifacts"
                directory.mkdir()
                artifact = directory / "reference"
                artifact.write_bytes(b"admitted input")
                locator = ArtifactLocator({"fixture": directory})
                ref = locator.put(dict(schema="vp.artifact_ref/v1", locator="af://fixture/reference",
                                       sha256=digest(artifact.read_bytes()), bytes=artifact.stat().st_size,
                                       media_type="text/plain", stage="inputs", copy="none"))
                binding = blender_rw1.RuntimeBinding(7, digest(b"runtime"), digest(b"external effects"))

                def attach():
                    return blender_rw1.BlenderRewind(capsule.broker, fakes.FakeOps(),
                        capsule_id="capsule", goal_id="goal", admitted_inputs=(ref,), binding=lambda: binding,
                        compatibility_sha256=binding.compatibility_sha256, resolve_artifact=locator.resolve)

                backend = attach()
                budget = dict(attempts_used=0, attempts_limit=10, rewinds_used=0, rewinds_limit=3)
                marked = backend.mark(by="broker", scene_epoch=1, authority_epoch=7, tick=0, frame=2,
                                      artifact_refs=(ref,), budget=budget, score_checkpoint=lambda blend:
                                      QualityResult(blend, 50, (RenderPredicate("render/frame_000000.png", True),)))
                capsule.restart_broker()
                self.assertEqual(attach().current(), marked.checkpoint)
                journal_path = Path(capsule.broker.p_events)
                verified_journal = journal_path.read_bytes()
                calls = list(capsule.ran)
                with closing(sqlite3.connect(root / "runtime/rw1-acceptance.sqlite3")) as db, db:
                    data, = db.execute("SELECT manifest FROM catalog").fetchone()
                    manifest = json.loads(data)
                    manifest["points"][0]["quality"]["score"] += seed + 1
                    raw = canonical_line(manifest)
                    db.execute("UPDATE catalog SET manifest=?,digest=?", (raw, digest(raw)))
                capsule.restart_broker()
                self.guarded("UNCERTAIN_OUTCOME", attach, error=blender_rw1.RewindError)
                self.assertEqual(capsule.ran, calls)
                self.assertEqual(journal_path.read_bytes(), verified_journal)
                self.assertEqual(marked.receipt["external_effect_frontier"], "UNREVERTED_HOST_NET_JOURNAL")

    def test_microvm_refuses_substituted_checkpoint_and_unfenced_reseed(self):
        from rewind import microvm
        from rewind.microvm.tests.LIVE_microvm_restore import load_fixture

        old, new, phases = load_fixture()
        clean = microvm.replay_restore(old, new, phases)
        self.assertTrue(clean.command_allowed(new))
        self.assertFalse(clean.command_allowed(old))
        self.assertEqual(clean.external_effect_frontier, "UNREVERTED_HOST_NET_JOURNAL")
        for index, evidence in ((1, "rep-substitute"), (2, dict(phases[2].evidence, commands_fenced=False))):
            with self.subTest(index=index):
                changed = list(phases)
                self.assertNotEqual(changed[index].evidence, evidence)
                changed[index] = replace(changed[index], evidence=evidence)
                result = microvm.replay_restore(old, new, changed)
                self.assertIsNotNone(result.refusal)
                self.assertIsNone(result.duration_ns)
                self.assertFalse(result.command_allowed(old))
                self.assertFalse(result.command_allowed(new))

    def test_microvm_unfenced_reseed_mutant_admits_new_authority(self):
        from rewind import microvm
        from rewind.microvm.tests.LIVE_microvm_restore import load_fixture

        old, new, phases = load_fixture()
        phases = list(phases)
        reseed = dict(phases[2].evidence, commands_fenced=False)
        phases[2] = replace(phases[2], evidence=reseed)
        self.assertFalse(microvm.replay_restore(old, new, phases).command_allowed(new))
        validate_reseed = microvm._reseed_matches

        def discard_fence_check(evidence, following):
            return validate_reseed(dict(evidence, commands_fenced=True), following)

        with patch.object(microvm, "_reseed_matches", side_effect=discard_fence_check) as mutant:
            result = microvm.replay_restore(old, new, phases)
            mutant.assert_called_once_with(reseed, new)
            with self.assertRaisesRegex(AssertionError, "unfenced reseed admitted new authority"):
                self.assertFalse(result.command_allowed(new), "unfenced reseed admitted new authority")

    def assert_preserved_external_effects(self, path, expected):
        self.assertEqual(path.read_bytes(), expected, "possible input was resent after owner exit")

    def test_loop_process_restart_preserves_verified_prefix_without_resend(self):
        for seed in range(2):
            with self.subTest(seed=seed):
                root = self.scratch()
                peer("loop", root, seed, 76)
                loop = loop_fixture(root, create=False, witness=bool(seed))
                earlier = json.loads((root / "prefix.json").read_bytes())
                interrupted = loop.goal.replay()
                self.assertEqual((earlier["i"], interrupted["i"], interrupted["phase"],
                                  interrupted["loop"]["claim"]["seq"]), (1, 1, "SCRIPT_RUNNING", 2))
                external = root / "external.jsonl"
                sent = external.read_bytes()
                self.assertEqual([row["seq"] for row in rows(external)], [1, 2])
                self.assertEqual(loop.advance(), "VERIFIED" if seed else "HUMAN_NEEDED")
                result = loop.goal.replay()
                self.assertEqual((result["i"], result["t"], result["r"], result["d"], result["u"]),
                                 (2, 0, 0, 0, 0) if seed else (1, 1, 0, 0, 0))
                self.assertEqual(interrupted["observations"], earlier["observations"])
                self.assertEqual(result["observations"][:len(earlier["observations"])], earlier["observations"])
                for key in ("checkpoint", "authority_epoch", "generation"):
                    self.assertEqual(result[key], earlier[key])
                if seed:
                    self.assertEqual(loop.advance(), "COMPLETE")
                else:
                    self.guarded("NO_LEASE", loop.advance, error=Refused)
                self.assert_preserved_external_effects(external, sent)
                self.assertEqual(loop_fixture(root, create=False).goal.replay(), result)

    def test_loop_reconciliation_resend_mutant(self):
        from loop import replay
        root = self.scratch()
        peer("loop", root, 0, 76)
        loop = loop_fixture(root, create=False, witness=True)
        external = root / "external.jsonl"
        sent = external.read_bytes()
        observe = loop._read_effect

        def resend(reading, workflow, doc, deadline):
            with loop.open_owner(doc) as owner:
                replay.run_one(owner, doc, reading["surface_id"])
            return observe(reading, workflow, doc, deadline)

        with patch.object(loop, "_read_effect", side_effect=resend) as mutant:
            self.assertEqual(loop.advance(), "VERIFIED")
            mutant.assert_called_once()
            self.assertEqual(mutant.call_args.args[0]["loop"]["claim"]["seq"], 2)
        self.assertEqual([row["seq"] for row in rows(external)], [1, 2, 3])
        with self.assertRaisesRegex(AssertionError, "possible input was resent"):
            self.assert_preserved_external_effects(external, sent)

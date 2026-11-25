"""D1-store positive paths and rejecting controls using synthetic evidence."""

from base64 import b64decode
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import errno
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import tempfile
from threading import Barrier
import unittest

from retention_service.contracts.validation import ContractError, ContractValidator
from retention_service.contracts.wire import admission_bytes, record_bytes
from retention_service.store import (
    Actor, Conflict, IntegrityError, Page, PinnedProfiles, Policy, RangeError, StorageFailure,
    Store, Unavailable,
)
from retention_service.store.files import digest

ROOT = Path(__file__).resolve().parents[2]
TENANT = "tenant/fixture"
AUTHORITY = Actor(TENANT, "fixture/authority")
RUNNER = Actor(TENANT, "host/launcher")
INSPECTOR = Actor(TENANT, "fixture/inspector")
ADMIN = Actor(TENANT, "fixture/admin")
RECOVER = Actor(TENANT, "fixture/recovery")
START_PROOF = {"stage": "witnessed"}
CLAIM_PROOF = {"custody": "witnessed"}
FINISH_PROOF = {"capture": "original", "retirement": "original", "no_spawn": "independent"}
PROFILE_BODY = b'{ "schema": "fixture.profile/v1", "result": "ok" }\n'
PROFILE_SCHEMA = record_bytes({
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["schema", "result"],
    "properties": {"schema": {"const": "fixture.profile/v1"}, "result": {"const": "ok"}},
})
GRANTS = {
    AUTHORITY: {"admit"}, RUNNER: {"start", "claim", "upload", "finish"},
    INSPECTOR: {"inspect"}, ADMIN: {"hold"}, RECOVER: {"recover"},
    Actor("fixture/other", INSPECTOR.principal): {"inspect"},
}


def vector(name):
    with (ROOT / "retention_service/contracts/vectors.json").open() as stream:
        items = json.load(stream)
    return json.loads(b64decode(next(v["data"] for v in items if v["name"] == name)))


class Fixture:
    def __init__(self):
        self.validator = ContractValidator(Path(os.environ.get("RETENTION_FROZEN_DIR", ROOT / "retention_service/contracts/frozen")))
        self.profile = PinnedProfiles(
            {("fixture.profile/v1", digest(PROFILE_SCHEMA)): PROFILE_SCHEMA},
            lambda admission, profile, terminal: admission["attempt_id"] == terminal["attempt_id"],
        )
        self.now = datetime(2026, 11, 24, tzinfo=timezone.utc)
        self.denied = set()
        self.denied_side = set()
        self.denied_scope = set()
        self.denied_request = set()
        self.queries = []
        self.policy = Policy("fixture/policy")

    def authorize(self, actor, verb, attempt, admission):
        return ((actor, verb) not in self.denied and verb in GRANTS.get(actor, set())
                and (actor, verb, attempt) not in self.denied_scope
                and (admission is None
                     or (actor, verb, admission["request_id"]) not in self.denied_request))

    def authorize_source(self, actor, destination, source, side, source_admission=None):
        self.queries.append((actor.principal, source, side))
        return (actor in {AUTHORITY, RUNNER} and (side not in self.denied_side)
                and self.authorize(RUNNER, "finish", destination["attempt_id"], destination)
                and self.authorize(INSPECTOR, "inspect", source, source_admission))

    @staticmethod
    def witness(kind, actor, admission, record, proof):
        original = {"start": START_PROOF, "claim": CLAIM_PROOF, "finish": FINISH_PROOF}[kind]
        if kind == "finish":
            return (isinstance(proof, dict) and set(proof) == set(original)
                    and (record["state"] != "aborted_before_spawn" or proof.get("no_spawn") == "independent"))
        return original == proof and actor.principal == admission["executor_id"]

    def validate_profile(self, admission, value, terminal):
        return self.profile(admission, value, terminal)

    def store(self, path, **options):
        return Store(path, self.validator, self.policy, authorize=self.authorize,
                     authorize_source=self.authorize_source, verify_witness=self.witness,
                     validate_profile=self.validate_profile, clock=lambda: self.now, **options)

    def admission(self, attempt_id, *, request_id=None, executor=RUNNER.principal):
        start = vector("external_launch_started-canonical")
        start.update(attempt_id=attempt_id, executor_id=executor)
        if request_id is not None:
            start["external_process"]["request_id"] = request_id
        raw = record_bytes(start)
        admission = dict(**start["external_process"], operation="replay", attempt_id=attempt_id,
                         executor_id=start["executor_id"], source_url=start["slot"]["source_url"],
                         profile_schema="fixture.profile/v1", profile_schema_sha256=digest(PROFILE_SCHEMA))
        self.seal(admission)
        return admission, raw

    def seal(self, admission):
        admission["admission_sha256"] = digest(admission_bytes(admission, self.validator))

    def start(self, store, attempt_id, *, claim=True, request_id=None):
        admission, raw = self.admission(attempt_id, request_id=request_id)
        store.admit(AUTHORITY, admission)
        ack, _ = store.start(RUNNER, attempt_id, raw, content_length=len(raw),
                             sha256=digest(raw), evidence=START_PROOF)
        if claim:
            store.claim(RUNNER, attempt_id, ack, evidence=CLAIM_PROOF)
        return admission, ack

    @staticmethod
    def terminal(ack, artifact=PROFILE_BODY, *, indexed=False, media=None, timeline=None,
                 request_id=None):
        terminal = vector("external_launch_observed-canonical")
        terminal["attempt_id"] = ack["attempt_id"]
        terminal["start_sha256"] = ack["start_sha256"]
        terminal["execution"]["attempt_id"] = ack["attempt_id"]
        if request_id is not None:
            terminal["execution"]["external_process"]["request_id"] = request_id
        execution_sha = digest(record_bytes(terminal["execution"]))
        terminal["cleanup"]["execution_sha256"] = execution_sha
        terminal["artifact"].update(bytes=len(artifact), sha256=digest(artifact),
                                    execution_sha256=execution_sha,
                                    content_kind="object_index" if indexed else "inline_payload")
        if media is not None:
            terminal["media"] = media
        if timeline is not None:
            terminal["timeline"] = timeline
        return record_bytes(terminal)

    def finish(self, store, ack, artifact=PROFILE_BODY, *, terminal=None, proof=FINISH_PROOF):
        return store.finish(RUNNER, ack["attempt_id"], terminal or self.terminal(ack, artifact),
                            artifact, evidence=proof)

    @staticmethod
    def upload(store, attempt, name, data, *, sha=None):
        return store.upload(RUNNER, attempt, name, BytesIO(data), content_length=len(data),
                            sha256=sha if sha is not None else digest(data))

    @staticmethod
    def index(admission, entries, **optional):
        keys = ("request_id", "request_sha256", "attempt_id", "operation",
                "admission_sha256", "profile_schema", "profile_schema_sha256")
        return record_bytes(dict({key: admission[key] for key in keys},
                                 schema="rea.retention_index/v1", profile_sha256=digest(PROFILE_BODY),
                                 objects=entries, **optional))

    def compare_admission(self, comparison, source_ids, *, executor=RUNNER.principal):
        start = vector("host_compare_started-canonical")
        start.update(attempt_id=comparison, executor_id=executor,
                     reference_attempt_id=source_ids["reference"],
                     candidate_attempt_id=source_ids["candidate"])
        admission = {key: start[key] for key in ("attempt_id", "executor_id", "request_id",
                     "request_sha256", "operation", "reference_attempt_id", "candidate_attempt_id")}
        admission.update(profile_schema="fixture.profile/v1", profile_schema_sha256=digest(PROFILE_SCHEMA))
        self.seal(admission)
        return admission, start

    @staticmethod
    def compare_terminal(ack, index, **bindings):
        return dict(schema=vector("host_compare_started-canonical")["schema"],
                    execution_kind="external_process",
                    attempt_id=ack["attempt_id"], state="observed", operation="compare",
                    start_sha256=ack["start_sha256"],
                    observation=dict(outside_capsule=True, perception_level="none",
                                     producer=dict(kind="host_level", id="fixture", process_bound=False)),
                    verdict=dict(result="match", comparator_id="fixture", version="1",
                                 contrast_ids=[], negative_control_ids=[],
                                 oracle_independence="independent",
                                 policy=dict(id="fixture/policy", version="1", sha256="a" * 64),
                                 independent_qualification=False),
                    artifact=dict(start_sha256=ack["start_sha256"], observer_id="observer/capture",
                                  observation_id="capture", sha256=digest(index), bytes=len(index),
                                  content_kind="object_index"),
                    cleanup=dict(start_sha256=ack["start_sha256"], observer_id="observer/cleanup",
                                 observation_id="cleanup", state="unknown"),
                    **bindings)


def recording_binding(sha):
    return dict(recording_sha256=sha, container="fixture", codec="fixture", clock_source="pts",
                frames=[], frames_unavailable_reason="fixture")


def empty_timeline():
    return dict(clock_source="monotonic", origin="first_input", canonicalization="rea.timeline_digest/v1",
                timeline_sha256=digest(b"[]"), events=[])


class StoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.attempt = "2" * 32

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.fix = Fixture()
        self.store = self.fix.store(self.root)
        self.addCleanup(lambda: self.store.close())

    def reopen(self):
        self.store.close()
        self.store = self.fix.store(self.root)

    def path(self, attempt=None):
        return self.root / "tenants" / digest(TENANT.encode()) / "attempts" / (attempt or self.attempt)

    def export(self, limit=2):
        pages = []
        page = self.store.recovery_page(RECOVER, limit=limit)
        pages.append(page)
        while page["next"] is not None:
            page = self.store.recovery_page(RECOVER, limit=limit, cursor=page["next"])
            pages.append(page)
        raw = self.store.recovery_item(RECOVER, page["manifest_item"]).data
        return raw, pages

    def cache(self, pages):
        raw_items = {}
        for page in pages:
            for item in page["items"]:
                token = item["item"]
                raw = self.store.recovery_item(RECOVER, token).data
                self.assertEqual(digest(raw), item["sha256"])
                raw_items[token] = raw
        return raw_items

    def target(self):
        path = self.root.parent / (self.root.name + "-restore")
        path.mkdir(mode=0o700)
        self.addCleanup(shutil.rmtree, path)
        return path

    def test_ready_does_not_create_tenant_or_allow_denied_actor(self):
        self.assertTrue(self.store.ready(RUNNER, "start"))
        self.assertEqual(list((self.root / "tenants").iterdir()), [])
        with self.assertRaises(Unavailable):
            self.store.ready(RUNNER, "inspect")

    def test_store_root_exclusive_and_private(self):
        with self.assertRaises(BlockingIOError):
            self.fix.store(self.root)
        self.store.close()
        self.root.chmod(0o755)
        with self.assertRaises(IntegrityError):
            self.fix.store(self.root)

    def test_admission_unknown_claim_and_reopen(self):
        admission, raw = self.fix.admission(self.attempt)
        self.assertTrue(self.store.admit(AUTHORITY, admission)[1])
        self.assertFalse(self.store.admit(AUTHORITY, admission)[1])
        with self.assertRaises(Unavailable):
            self.store.inspect(INSPECTOR, self.attempt)
        changed = dict(admission, provider_revision="substituted")
        self.fix.seal(changed)
        with self.assertRaises(Conflict):
            self.store.admit(AUTHORITY, changed)
        with self.assertRaises(Unavailable):
            self.store.admit(RUNNER, changed)
        bad = json.loads(raw)
        bad["external_process"]["request_sha256"] = "1" * 64
        bad = record_bytes(bad)
        with self.assertRaisesRegex(ContractError, "admission pins"):
            self.store.start(RUNNER, self.attempt, bad, content_length=len(bad), sha256=digest(bad),
                             evidence=START_PROOF)
        ack, created = self.store.start(RUNNER, self.attempt, raw, content_length=len(raw),
                                        sha256=digest(raw), evidence=START_PROOF)
        self.assertTrue(created)
        self.assertEqual(self.store.inspect(INSPECTOR, self.attempt)["launch_claimed"], False)
        self.assertEqual(self.store.claim(RUNNER, self.attempt, ack, evidence=CLAIM_PROOF), ack)
        self.reopen()
        result = self.store.inspect(INSPECTOR, self.attempt)
        self.fix.validator.validate("Inspection", result)
        self.assertEqual((result["state"], result["terminal"], result["launch_claimed"]),
                         ("unknown", None, True))
        self.assertEqual(result["start_sha256"], digest(raw))
        with self.assertRaises(Conflict):
            self.store.claim(RUNNER, self.attempt, ack, evidence=CLAIM_PROOF)

    def test_exact_finish_and_two_independent_reference_types(self):
        _, ack = self.fix.start(self.store, self.attempt)
        result, new = self.fix.finish(self.store, ack)
        self.assertTrue(new)
        inspection = self.store.inspect(INSPECTOR, self.attempt)
        self.fix.validator.validate("Inspection", inspection)
        self.assertEqual(self.store.artifact(INSPECTOR, self.attempt, inspection["artifact_ref"]),
                         PROFILE_BODY)
        self.reopen()
        self.assertEqual(self.fix.finish(self.store, ack), (result, False))
        self.assertEqual(self.store.receipt(INSPECTOR, inspection["receipt_ref"]), inspection)
        with self.assertRaises(ContractError):
            self.store.receipt(INSPECTOR, inspection["artifact_ref"])
        with self.assertRaises(Conflict):
            self.fix.finish(self.store, ack, PROFILE_BODY + b" ")
        with self.assertRaises(Conflict):
            self.fix.finish(self.store, ack, proof=dict(FINISH_PROOF, capture="changed"))
        self.assertEqual(list(self.path().glob("end.json")), [self.path() / "end.json"])

    def test_conflicting_finishes_serialize(self):
        _, ack = self.fix.start(self.store, self.attempt)
        barrier = Barrier(2)

        def compete(artifact):
            barrier.wait()
            try:
                return self.fix.finish(self.store, ack, artifact)
            except Conflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as runner:
            results = list(runner.map(compete, (PROFILE_BODY, PROFILE_BODY + b" ")))
        self.assertEqual(results.count("conflict"), 1)
        winner = self.store.inspect(INSPECTOR, self.attempt)
        self.assertIn(self.store.artifact(INSPECTOR, self.attempt, winner["artifact_ref"]),
                      [PROFILE_BODY, PROFILE_BODY + b" "])

    def test_observed_and_aborted_refuse_unproven_witnesses(self):
        _, ack = self.fix.start(self.store, self.attempt, claim=False)
        with self.assertRaisesRegex(ContractError, "durable claim"):
            self.fix.finish(self.store, ack)
        terminal = vector("external_launch_aborted_before_spawn-canonical")
        terminal["start_sha256"] = ack["start_sha256"]
        terminal["attempt_id"] = ack["attempt_id"]
        terminal["artifact"].update(bytes=len(PROFILE_BODY), sha256=digest(PROFILE_BODY),
                                    start_sha256=ack["start_sha256"])
        terminal["cleanup"]["start_sha256"] = ack["start_sha256"]
        with self.assertRaisesRegex(ContractError, "finish evidence"):
            self.fix.finish(self.store, ack, terminal=record_bytes(terminal),
                            proof=dict(FINISH_PROOF, no_spawn="unobserved"))
        self.assertTrue(self.fix.finish(self.store, ack, terminal=record_bytes(terminal))[1])

    def test_indexed_bulk_paging_stable_retry_and_denial(self):
        admission, ack = self.fix.start(self.store, self.attempt)
        media = bytes(range(256)) * 5000
        bulk, created = self.fix.upload(self.store, self.attempt, "bulk", media)
        self.assertTrue(created)
        self.reopen()
        self.assertEqual(self.fix.upload(self.store, self.attempt, "bulk", media), (bulk, False))
        with self.assertRaises(Conflict):
            self.fix.upload(self.store, self.attempt, "bulk", media + b"x")
        p, _ = self.fix.upload(self.store, self.attempt, "profile", PROFILE_BODY)
        extra, _ = self.fix.upload(self.store, self.attempt, "extra", b"not indexed")
        artifact = self.fix.index(admission, [
            dict(p, role="rea_profile"), dict(bulk, role="recording", side="candidate")])
        terminal = self.fix.terminal(ack, artifact, indexed=True,
                                     media={"candidate": recording_binding(bulk["sha256"])})
        self.fix.finish(self.store, ack, artifact, terminal=terminal)
        self.assertEqual(self.fix.upload(self.store, self.attempt, "bulk", media), (bulk, False))
        whole = b"".join(self.store.object(INSPECTOR, self.attempt, bulk["object_ref"], offset=o).data
                         for o in (0, 1 << 20))
        self.assertEqual(whole, media)
        with self.assertRaises(Conflict):
            self.fix.upload(self.store, self.attempt, "late", b"bad")
        with self.assertRaises(Unavailable):
            self.store.object(INSPECTOR, self.attempt, extra["object_ref"], offset=len(media))
        with self.assertRaises(RangeError):
            self.store.object(INSPECTOR, self.attempt, bulk["object_ref"], offset=len(media))
        with self.assertRaises(Unavailable):
            self.store.object(Actor("fixture/other", INSPECTOR.principal), self.attempt,
                              bulk["object_ref"], offset=len(media))

    def test_index_rejects_foreign_objects_wrong_pin_and_side(self):
        admission, ack = self.fix.start(self.store, self.attempt)
        other, _ = self.fix.start(self.store, "f" * 32)
        foreign, _ = self.fix.upload(self.store, other["attempt_id"], "profile", PROFILE_BODY)
        p, _ = self.fix.upload(self.store, self.attempt, "profile", PROFILE_BODY)
        side, _ = self.fix.upload(self.store, self.attempt, "events", b"[]")
        variants = [
            self.fix.index(admission, [dict(foreign, role="rea_profile")]),
            self.fix.index(admission | {"profile_schema_sha256": "1" * 64},
                           [dict(p, role="rea_profile")]),
            self.fix.index(admission, [dict(p, role="rea_profile"),
                                      dict(side, role="timeline_events", side="reference")]),
        ]
        timeline = {"candidate": {"clock_source": "monotonic", "origin": "first_input",
                                 "canonicalization": "rea.timeline_digest/v1",
                                 "timeline_sha256": digest(b"[]"),
                                 "events_object_ref": side["object_ref"]}}
        for index, artifact in enumerate(variants):
            with self.subTest(index=index), self.assertRaises(ContractError):
                terminal = self.fix.terminal(ack, artifact, indexed=True, timeline=timeline)
                self.fix.finish(self.store, ack, artifact, terminal=terminal)
        self.assertEqual(self.store.inspect(INSPECTOR, self.attempt)["state"], "unknown")

    def test_index_accepts_optional_sides_for_schema_allowed_roles(self):
        admission, ack = self.fix.start(self.store, self.attempt)
        profile, _ = self.fix.upload(self.store, self.attempt, "profile", PROFILE_BODY)
        entries = [dict(profile, role="rea_profile")]
        for role in ("input_tape", "comparison", "observation", "provider_result", "cleanup"):
            meta, _ = self.fix.upload(self.store, self.attempt, role, role.encode())
            entries.append(dict(meta, role=role, side="candidate"))
        index = self.fix.index(admission, entries)
        terminal = self.fix.terminal(ack, index, indexed=True)
        self.assertTrue(self.fix.finish(self.store, ack, index, terminal=terminal)[1])
        self.reopen()
        self.assertEqual(self.store.inspect(INSPECTOR, self.attempt)["state"], "committed")

    def test_index_rejects_unreferenced_and_duplicate_sided_roles(self):
        admission, ack = self.fix.start(self.store, self.attempt)
        profile, _ = self.fix.upload(self.store, self.attempt, "profile", PROFILE_BODY)
        first, _ = self.fix.upload(self.store, self.attempt, "first", b"[]")
        second, _ = self.fix.upload(self.store, self.attempt, "second", b"[]")
        invalid, _ = self.fix.upload(self.store, self.attempt, "invalid", b"not events")
        self.assertNotEqual(first["object_ref"], second["object_ref"])
        for role, expected_binding in (
            ("timeline_events", {"timeline": {"candidate": empty_timeline()}}),
            ("recording", {"media": {"candidate": recording_binding(first["sha256"])}}),
        ):
            for variant, metas, binding in (
                ("orphaned", [invalid], {}),
                ("orphaned duplicate", [first, second], {}),
                ("bound duplicate", [first, second], expected_binding),
            ):
                with self.subTest(role=role, variant=variant):
                    index = self.fix.index(admission, [dict(profile, role="rea_profile"), *(
                        dict(meta, role=role, side="candidate") for meta in metas)])
                    self.fix.validator.validate_index(json.loads(index))
                    terminal = self.fix.terminal(ack, index, indexed=True, **binding)
                    with self.assertRaisesRegex(ContractError, "unique matching terminal side"):
                        self.fix.finish(self.store, ack, index, terminal=terminal)
        self.assertEqual(self.store.inspect(INSPECTOR, self.attempt)["state"], "unknown")

    def test_launch_media_requires_uploaded_recording(self):
        admission, ack = self.fix.start(self.store, self.attempt)
        media = {"candidate": recording_binding(digest(b"recording"))}
        with self.assertRaisesRegex(ContractError, "recording bytes require an object index"):
            terminal = self.fix.terminal(ack, media=media)
            self.fix.finish(self.store, ack, terminal=terminal)
        profile, _ = self.fix.upload(self.store, self.attempt, "profile", PROFILE_BODY)
        index = self.fix.index(admission, [dict(profile, role="rea_profile")])
        terminal = self.fix.terminal(ack, index, indexed=True, media=media)
        with self.assertRaisesRegex(ContractError, "terminal media requires matching recording bytes"):
            self.fix.finish(self.store, ack, index, terminal=terminal)

    def test_index_episode_fields_and_reconciliation_bindings(self):
        admission, raw = self.fix.admission(self.attempt)
        start = json.loads(raw)
        start.update(episode_id="fixture/episode", parent_episode_id="fixture/parent")
        raw = record_bytes(start)
        self.store.admit(AUTHORITY, admission)
        ack, _ = self.store.start(RUNNER, self.attempt, raw, content_length=len(raw),
                                  sha256=digest(raw), evidence=START_PROOF)
        self.store.claim(RUNNER, self.attempt, ack, evidence=CLAIM_PROOF)
        profile, _ = self.fix.upload(self.store, self.attempt, "profile", PROFILE_BODY)
        fields = dict(episode_id=start["episode_id"], parent_episode_id=start["parent_episode_id"])
        for change in (dict(episode_id="fixture/substitute"),
                       dict(parent_episode_id="fixture/substitute"),
                       dict(reconciles_attempt_id="a" * 32)):
            with self.subTest(change=change):
                index = self.fix.index(admission, [dict(profile, role="rea_profile")],
                                       **(fields | change))
                terminal = self.fix.terminal(ack, index, indexed=True)
                with self.assertRaisesRegex(ContractError, "index changed|reconciliation"):
                    self.fix.finish(self.store, ack, index, terminal=terminal)
        index = self.fix.index(admission, [dict(profile, role="rea_profile")], **fields)
        other_admission, other_ack = self.fix.start(self.store, "9" * 32)
        other_profile, _ = self.fix.upload(self.store, other_ack["attempt_id"], "profile", PROFILE_BODY)
        other_index = self.fix.index(other_admission, [dict(other_profile, role="rea_profile")], **fields)
        other_terminal = json.loads(self.fix.terminal(other_ack, other_index, indexed=True))
        other_terminal["episode_id"] = "fixture/terminal-episode"
        with self.assertRaisesRegex(ContractError, "index changed episode_id"):
            self.fix.finish(self.store, other_ack, other_index, terminal=record_bytes(other_terminal))
        self.assertTrue(self.fix.finish(self.store, ack, index,
                                       terminal=self.fix.terminal(ack, index, indexed=True))[1])

    def test_inline_schema_and_upload_count_hash_refuse_before_publication(self):
        _, ack = self.fix.start(self.store, self.attempt)
        with self.assertRaisesRegex(ContractError, "schema assertion"):
            self.fix.finish(self.store, ack, b'{"result":"ok"}')
        with self.assertRaises(ContractError):
            self.fix.upload(self.store, self.attempt, "bulk", b"abc", sha="0" * 64)
        with self.assertRaises(ContractError):
            self.store.upload(RUNNER, self.attempt, "bulk", BytesIO(b"a"), content_length=2,
                              sha256=digest(b"a"))
        self.assertEqual(list((self.path() / "uploads").iterdir()), [])
        self.assertFalse((self.path() / "end.json").exists())

    def test_claim_race_consumes_exactly_once(self):
        _, ack = self.fix.start(self.store, self.attempt, claim=False)
        barrier = Barrier(2)

        def claim(_):
            barrier.wait()
            try:
                return self.store.claim(RUNNER, self.attempt, ack, evidence=CLAIM_PROOF)
            except Conflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as runner:
            self.assertCountEqual(runner.map(claim, range(2)), [ack, "conflict"])

    def test_corrupt_end_dependency_is_integrity_failure_across_restart(self):
        _, ack = self.fix.start(self.store, self.attempt)
        self.fix.finish(self.store, ack)
        result = self.store.inspect(INSPECTOR, self.attempt)
        (self.path() / "artifact/bytes").write_bytes(b"changed")
        self.reopen()
        with self.assertRaises(IntegrityError):
            self.store.inspect(INSPECTOR, self.attempt)
        with self.assertRaises(IntegrityError):
            self.store.receipt(INSPECTOR, result["receipt_ref"])
        raw, _ = self.export()
        self.assertEqual(json.loads(raw)["attempts"][0]["disposition"], "unknown")

    def test_orphan_and_interrupted_dispositions_preserve_original_bytes(self):
        admission, _ = self.fix.admission("a" * 32)
        self.store.admit(AUTHORITY, admission)
        orphan = self.path("a" * 32) / "staging/private"
        orphan.touch(mode=0o600)
        orphan.write_bytes(b"isolated orphan")
        admission, ack = self.fix.start(self.store, self.attempt)
        self.fix.finish(self.store, ack)
        (self.path() / "end.json").unlink()
        self.reopen()
        with self.assertRaises(Unavailable):
            self.store.inspect(INSPECTOR, "a" * 32)
        self.assertEqual(self.store.inspect(INSPECTOR, self.attempt)["state"], "unknown")
        raw, _ = self.export()
        states = {item["attempt_id"]: item["disposition"] for item in json.loads(raw)["attempts"]}
        self.assertEqual(states, {self.attempt: "terminal_commit_interrupted", "a" * 32: "orphaned"})
        with self.assertRaises(Conflict):
            self.fix.finish(self.store, ack, PROFILE_BODY + b" ")
        self.assertTrue(self.fix.finish(self.store, ack)[1])
        self.assertEqual(orphan.read_bytes(), b"isolated orphan")

    def test_hold_release_and_deletion_fence_old_export(self):
        _, ack = self.fix.start(self.store, self.attempt)
        self.fix.finish(self.store, ack)
        before = self.store.inspect(INSPECTOR, self.attempt)
        self.assertTrue(self.store.hold(ADMIN, self.attempt, "hold-one", enable=True))
        self.assertTrue(self.store.hold(ADMIN, self.attempt, "hold-two", enable=True))
        self.assertFalse(self.store.hold(ADMIN, self.attempt, "hold-two", enable=True))
        self.fix.now += timedelta(days=31)
        self.assertEqual(self.store.sweep(), 0)
        self.store.hold(ADMIN, self.attempt, "hold-one", enable=False)
        self.assertEqual(self.store.sweep(), 0)
        raw, pages = self.export()
        cache = self.cache(pages)
        token = pages[0]["manifest_item"]
        self.store.hold(ADMIN, self.attempt, "hold-two", enable=False)
        eligible = self.fix.now
        self.assertEqual(self.store.sweep(), 1)
        self.assertFalse(self.path().exists())
        with self.assertRaises(Unavailable):
            self.store.recovery_item(RECOVER, token)
        with self.assertRaises(Unavailable):
            self.store.hold(ADMIN, self.attempt, "hold-three", enable=True)
        with self.assertRaises(Unavailable):
            self.store.admit(AUTHORITY, before["admission"])
        raw_fence, _ = self.export()
        manifest = json.loads(raw_fence)
        self.assertEqual(manifest["deletion_high_water"], 1)
        tombstone = manifest["items"][0]["item"]
        tomb = json.loads(self.store.recovery_item(RECOVER, tombstone).data)
        self.assertEqual(datetime.fromisoformat(tomb["eligible_at"]), eligible)

        def fetch(item, offset, limit):
            data = cache[item]
            return Page(item, len(data), digest(data), offset, data[offset:offset + limit])

        with self.fix.store(self.target()) as target:
            target.restore(RECOVER, raw, pages, fetch, manifest_sha256=digest(raw), authority=self.store)
            with self.assertRaises(Unavailable):
                target.inspect(INSPECTOR, self.attempt)
            with self.assertRaises(Unavailable):
                target.receipt(INSPECTOR, before["receipt_ref"])
            self.assertEqual(target.sweep(), 0)

    def test_paged_restore_compares_items_refs_holds_and_booleans(self):
        admission, ack = self.fix.start(self.store, self.attempt)
        p, _ = self.fix.upload(self.store, self.attempt, "profile", PROFILE_BODY)
        z, _ = self.fix.upload(self.store, self.attempt, "zero", b"")
        big = b"fixture-recording" * 70_000
        recording, _ = self.fix.upload(self.store, self.attempt, "recording", big)
        artifact = self.fix.index(admission, [dict(p, role="rea_profile"), dict(z, role="input_tape"),
                                  dict(recording, role="recording", side="candidate")])
        terminal = self.fix.terminal(ack, artifact, indexed=True,
                                     media={"candidate": recording_binding(recording["sha256"])})
        finish_ack, _ = self.fix.finish(self.store, ack, artifact, terminal=terminal)
        self.store.hold(ADMIN, self.attempt, "hold-restore", enable=True)
        before = self.store.inspect(INSPECTOR, self.attempt)
        raw, pages = self.export(limit=1)
        path = self.target()

        def fetch(item, offset, limit):
            return self.store.recovery_item(RECOVER, item, offset=offset, limit=limit)

        with self.fix.store(path) as target:
            with self.assertRaisesRegex(ContractError, "coverage"):
                target.restore(RECOVER, raw, pages[:2] + pages[3:], fetch,
                               manifest_sha256=digest(raw), authority=self.store)
            target.restore(RECOVER, raw, pages, fetch, manifest_sha256=digest(raw), authority=self.store)
        with self.fix.store(path) as target:
            self.assertEqual(target.receipt(INSPECTOR, before["receipt_ref"]), before)
            self.assertEqual(target.artifact(INSPECTOR, self.attempt, before["artifact_ref"]), artifact)
            self.assertEqual(target.object(INSPECTOR, self.attempt, z["object_ref"]).data, b"")
            self.assertEqual(target.object(INSPECTOR, self.attempt, recording["object_ref"],
                                           offset=1 << 20).data, big[1 << 20:])
            self.assertFalse(target.hold(ADMIN, self.attempt, "hold-restore", enable=True))
            repeated = self.fix.finish(target, ack, artifact, terminal=terminal)
            self.assertEqual(repeated, (finish_ack, False))

    def test_snapshot_expiry_acl_and_restore_rejects_changed_bindings(self):
        _, ack = self.fix.start(self.store, self.attempt, claim=False)
        raw, pages = self.export(limit=1)
        root, cursor = pages[0]["manifest_item"], pages[0]["next"]
        self.fix.denied.add((RECOVER, "recover"))
        with self.assertRaises(Unavailable):
            self.store.recovery_page(RECOVER, limit=1, cursor=cursor)
        with self.assertRaises(Unavailable):
            self.store.recovery_item(RECOVER, root)
        self.fix.denied.clear()
        self.fix.now += timedelta(minutes=15)
        with self.assertRaises(Unavailable):
            self.store.recovery_item(RECOVER, root)
        self.assertEqual(self.store.inspect(INSPECTOR, self.attempt)["state"], "unknown")
        self.assertEqual(self.store.inspect(INSPECTOR, self.attempt)["start_sha256"], ack["start_sha256"])
        with self.fix.store(self.target()) as target:
            with self.assertRaisesRegex(ContractError, "authenticated root"):
                target.restore(RECOVER, raw + b" ", pages, None,
                               manifest_sha256=digest(raw), authority=self.store)

    def test_pinned_profile_does_not_load_changed_or_remote_schema(self):
        approve = lambda *_: True
        with self.assertRaisesRegex(ContractError, "digest mismatch"):
            PinnedProfiles({("fixture.profile/v1", digest(PROFILE_SCHEMA)): PROFILE_SCHEMA + b" "},
                           approve)
        schema = record_bytes({"$schema": "https://json-schema.org/draft/2020-12/schema",
                               "$ref": "https://example.org/fixture/missing"})
        with self.assertRaisesRegex(ContractError, "unresolved"):
            PinnedProfiles({("fixture.profile/v1", digest(schema)): schema}, approve)

    def test_timeline_uses_canonical_event_bytes_and_exact_tick_order(self):
        admission, ack = self.fix.start(self.store, self.attempt)
        profile, _ = self.fix.upload(self.store, self.attempt, "profile", PROFILE_BODY)
        event = dict(event_id="event-a", kind="click", input_digest="b" * 64, t_ms=2, t_ticks=2)
        correct = json.dumps([event], sort_keys=True, separators=(",", ":")).encode()
        bad_data = {
            "wrong-ticks": correct.replace(b'"t_ticks":2', b'"t_ticks":1'),
            "bad-fields": b'[{ "bad":true }]',
            "ordered-jcs": correct,
        }
        for name, data in bad_data.items():
            obj, _ = self.fix.upload(self.store, self.attempt, name, data)
            index = self.fix.index(admission, [dict(profile, role="rea_profile"),
                                      dict(obj, role="timeline_events", side="candidate")])
            timeline = {"candidate": {"clock_source": "monotonic", "origin": "first_input",
                                     "canonicalization": "rea.timeline_digest/v1",
                                     "timeline_sha256": obj["sha256"],
                                     "events_object_ref": obj["object_ref"],
                                     "time_base": {"num": 1, "den": 1000}}}
            terminal = self.fix.terminal(ack, index, indexed=True, timeline=timeline)
            if name == "ordered-jcs":
                self.assertTrue(self.fix.finish(self.store, ack, index, terminal=terminal)[1])
            else:
                with self.assertRaises(ContractError):
                    self.fix.finish(self.store, ack, index, terminal=terminal)

    def test_source_grants_side_bindings_and_lineage(self):
        comparison = "3" * 32
        reference_media, candidate_media = b"reference bytes", b"candidate bytes"
        source_ids = {"reference": "1" * 32, "candidate": "2" * 32}
        source_meta = {}
        candidate_timeline = empty_timeline()
        source_frames = []
        for side, data in zip(source_ids, (reference_media, candidate_media)):
            source_id = source_ids[side]
            admission, ack = self.fix.start(self.store, source_id)
            p, _ = self.fix.upload(self.store, source_id, "profile", PROFILE_BODY)
            recording, _ = self.fix.upload(self.store, source_id, "recording", data)
            source_meta[side] = recording
            entries = [dict(p, role="rea_profile"), dict(recording, role="recording", side=side)]
            if side == "candidate":
                for name in ("frame1", "frame2"):
                    frame, _ = self.fix.upload(self.store, source_id, name, b"identical frame")
                    source_frames.append(frame["object_ref"])
                    entries.append(dict(frame, role="frame", side=side))
            index = self.fix.index(admission, entries)
            timeline = {side: candidate_timeline} if side == "candidate" else None
            terminal = self.fix.terminal(ack, index, indexed=True, timeline=timeline,
                                         media={side: recording_binding(recording["sha256"])})
            self.fix.finish(self.store, ack, index, terminal=terminal)

        admission, start = self.fix.compare_admission(comparison, source_ids)
        self.fix.denied_side.add("candidate")
        with self.assertRaises(Unavailable):
            self.store.admit(AUTHORITY, admission)
        self.fix.denied_side.clear()
        self.assertTrue(self.store.admit(AUTHORITY, admission)[1])
        swapped = dict(start, reference_attempt_id=source_ids["candidate"],
                       candidate_attempt_id=source_ids["reference"])
        raw = record_bytes(swapped)
        with self.assertRaisesRegex(ContractError, "ordered comparison"):
            self.store.start(RUNNER, comparison, raw, content_length=len(raw), sha256=digest(raw),
                             evidence=START_PROOF)
        raw = record_bytes(start)
        ack, _ = self.store.start(RUNNER, comparison, raw, content_length=len(raw), sha256=digest(raw),
                                  evidence=START_PROOF)
        with self.assertRaises(Conflict):
            self.store.claim(RUNNER, comparison, ack, evidence=CLAIM_PROOF)
        p, _ = self.fix.upload(self.store, comparison, "profile", PROFILE_BODY)
        c, _ = self.fix.upload(self.store, comparison, "cand", candidate_media)
        frame, _ = self.fix.upload(self.store, comparison, "frame", b"identical frame")
        entries = [dict(p, role="rea_profile"), dict(c, role="recording", side="candidate"),
                   dict(frame, role="frame", side="candidate")]
        index = self.fix.index(admission, entries)
        media = {side: recording_binding(meta["sha256"]) for side, meta in source_meta.items()}
        terminal = self.fix.compare_terminal(ack, index, media=media,
                                             timeline={"candidate": candidate_timeline})
        raw_terminal = record_bytes(terminal)
        for side in source_ids:
            with self.subTest(side=side):
                revoked = (INSPECTOR, "inspect", source_ids[side])
                self.fix.denied_scope.add(revoked)
                with self.assertRaises(Unavailable):
                    self.fix.finish(self.store, ack, index, terminal=raw_terminal)
                self.fix.denied_scope.remove(revoked)
        swapped_index = self.fix.index(admission, [dict(p, role="rea_profile"),
                                        dict(c, role="recording", side="reference")])
        swapped_terminal = self.fix.compare_terminal(ack, swapped_index,
                                                     media={"reference": media["candidate"]})
        with self.assertRaisesRegex(ContractError, "media changed the admitted source"):
            self.fix.finish(self.store, ack, swapped_index, terminal=record_bytes(swapped_terminal))
        wrong_clock_origin = dict(terminal, timeline={
            "candidate": dict(candidate_timeline, origin="recording_start")})
        with self.assertRaisesRegex(ContractError, "timeline changed"):
            self.fix.finish(self.store, ack, index, terminal=record_bytes(wrong_clock_origin))
        first, created = self.fix.finish(self.store, ack, index, terminal=raw_terminal)
        self.assertTrue(created)
        self.reopen()
        self.assertEqual(self.fix.finish(self.store, ack, index, terminal=raw_terminal),
                         (first, False))
        raw_manifest, pages = self.export()
        mapping = next(item for item in json.loads(raw_manifest)["attempts"]
                       if item["attempt_id"] == comparison)
        retained = [entry["source_object_ref"] for entry in mapping["source_lineage"]
                    if entry["destination_object_ref"] is None and entry["source_object_ref"] is not None]
        self.assertEqual(retained, [source_meta["reference"]["object_ref"]])
        copied = {entry["destination_object_ref"]: entry["source_object_ref"]
                  for entry in mapping["source_lineage"] if entry["destination_object_ref"] is not None}
        self.assertEqual(copied, {c["object_ref"]: source_meta["candidate"]["object_ref"],
                                  frame["object_ref"]: min(source_frames)})

        def fetch(item, offset, limit):
            return self.store.recovery_item(RECOVER, item, offset=offset, limit=limit)

        with self.fix.store(self.target()) as restored:
            restored.restore(RECOVER, raw_manifest, pages, fetch,
                             manifest_sha256=digest(raw_manifest), authority=self.store)
            self.assertEqual(self.fix.finish(restored, ack, index, terminal=raw_terminal),
                             (first, False))
        self.fix.denied_side.add("reference")
        with self.assertRaises(Unavailable):
            self.fix.finish(self.store, ack, index, terminal=raw_terminal)
        self.assertEqual(self.store.inspect(INSPECTOR, comparison)["state"], "committed")

    def test_attempt_scope_is_rechecked_on_snapshot_root_and_cursor(self):
        self.fix.start(self.store, self.attempt)
        _, pages = self.export(limit=1)
        self.fix.denied_scope.add((RECOVER, "recover", self.attempt))
        with self.assertRaises(Unavailable):
            self.store.recovery_page(RECOVER, limit=1, cursor=pages[0]["next"])
        with self.assertRaises(Unavailable):
            self.store.recovery_item(RECOVER, pages[0]["manifest_item"])
        with self.assertRaises(Unavailable):
            self.store.recovery_page(RECOVER, limit=1)

    def test_request_scope_rechecked_for_snapshot_cursor_manifest_and_item(self):
        admission, _ = self.fix.start(self.store, self.attempt)
        _, pages = self.export(limit=1)
        item = pages[1]["items"][0]["item"]
        self.fix.denied_request.add((RECOVER, "recover", admission["request_id"]))
        with self.subTest(route="cursor"), self.assertRaises(Unavailable):
            self.store.recovery_page(RECOVER, limit=1, cursor=pages[0]["next"])
        with self.subTest(route="manifest"), self.assertRaises(Unavailable):
            self.store.recovery_item(RECOVER, pages[0]["manifest_item"])
        with self.subTest(route="item"), self.assertRaises(Unavailable):
            self.store.recovery_item(RECOVER, item)

    def test_restore_rechecks_request_scope_before_fetching_attempt_payload(self):
        admission, ack = self.fix.start(self.store, self.attempt)
        self.fix.finish(self.store, ack)
        raw, pages = self.export()
        export = self.cache(pages)
        fetched = []
        kinds = {item["item"]: item["kind"] for page in pages for item in page["items"]}

        def fetch(token, offset, limit):
            fetched.append(kinds[token])
            data = export[token]
            return Page(token, len(data), digest(data), offset, data[offset:offset + limit])

        self.fix.denied_request.add((RECOVER, "recover", admission["request_id"]))
        with self.fix.store(self.target()) as restored:
            with self.assertRaises(Unavailable):
                restored.restore(RECOVER, raw, pages, fetch,
                                 manifest_sha256=digest(raw), authority=self.store)
        self.assertEqual(fetched, ["admission"])

    def ready_comparison(self, *, timeline=None):
        source_ids = {"reference": "1" * 32, "candidate": "2" * 32}
        sources = {}
        for side, source_id in source_ids.items():
            request_id = f"fixture/compare-source/{side}"
            source, ack = self.fix.start(self.store, source_id, request_id=request_id)
            terminal = self.fix.terminal(ack, timeline={side: timeline} if timeline else None,
                                         request_id=request_id)
            self.fix.finish(self.store, ack, terminal=terminal)
            sources[side] = source
        admission, start = self.fix.compare_admission("3" * 32, source_ids)
        return sources, admission, record_bytes(start)

    def start_comparison(self, admission, raw):
        attempt = admission["attempt_id"]
        self.store.admit(AUTHORITY, admission)
        ack, _ = self.store.start(RUNNER, attempt, raw, content_length=len(raw),
                                  sha256=digest(raw), evidence=START_PROOF)
        profile, _ = self.fix.upload(self.store, attempt, "profile", PROFILE_BODY)
        return ack, dict(profile, role="rea_profile")

    def test_comparison_rechecks_source_request_scope_at_admission_and_finish(self):
        sources, admission, raw = self.ready_comparison()
        denied = {side: (INSPECTOR, "inspect", source["request_id"]) for side, source in sources.items()}
        self.fix.denied_request.add(denied["candidate"])
        with self.subTest(route="admit"), self.assertRaises(Unavailable):
            self.store.admit(AUTHORITY, admission)
        self.fix.denied_request.clear()
        ack, profile = self.start_comparison(admission, raw)
        index = self.fix.index(admission, [profile])
        terminal = record_bytes(self.fix.compare_terminal(ack, index))
        for side, grant in denied.items():
            self.fix.denied_request.add(grant)
            with self.subTest(route="finish", side=side), self.assertRaises(Unavailable):
                self.fix.finish(self.store, ack, index, terminal=terminal)
            self.fix.denied_request.clear()
        self.assertTrue(self.fix.finish(self.store, ack, index, terminal=terminal)[1])
        self.fix.denied_request.add(denied["reference"])
        with self.subTest(route="finish retry"), self.assertRaises(Unavailable):
            self.fix.finish(self.store, ack, index, terminal=terminal)

    def test_comparison_offloads_verified_inline_timeline_and_owns_sided_objects(self):
        inline = empty_timeline()
        _, admission, raw = self.ready_comparison(timeline=inline)
        ack, profile = self.start_comparison(admission, raw)
        attempt = ack["attempt_id"]
        events, _ = self.fix.upload(self.store, attempt, "events", b"[]")
        frame, _ = self.fix.upload(self.store, attempt, "frame", b"comparison frame")
        entries = [profile, dict(events, role="timeline_events", side="candidate"),
                   dict(frame, role="frame", side="reference")]
        timeline_side = dict(inline, events_object_ref=events["object_ref"])
        del timeline_side["events"]
        timeline = {"reference": inline, "candidate": timeline_side}
        index = self.fix.index(admission, entries)
        wrong_timeline = dict(timeline, candidate=dict(timeline_side, origin="recording_start"))
        terminal = self.fix.compare_terminal(ack, index, timeline=wrong_timeline)
        with self.assertRaisesRegex(ContractError, "timeline changed the admitted source"):
            self.fix.finish(self.store, ack, index, terminal=record_bytes(terminal))
        terminal = self.fix.compare_terminal(ack, index, timeline=timeline,
                                             media={"reference": recording_binding(digest(b"fake"))})
        with self.assertRaises(ContractError):
            self.fix.finish(self.store, ack, index, terminal=record_bytes(terminal))
        terminal = record_bytes(self.fix.compare_terminal(ack, index, timeline=timeline))
        first, created = self.fix.finish(self.store, ack, index, terminal=terminal)
        self.assertTrue(created)
        self.reopen()
        self.assertEqual(self.fix.finish(self.store, ack, index, terminal=terminal), (first, False))
        raw_manifest, _ = self.export()
        lineage = next(mapping["source_lineage"] for mapping in json.loads(raw_manifest)["attempts"]
                       if mapping["attempt_id"] == attempt)
        by_destination = {row["destination_object_ref"]: row for row in lineage
                          if row["destination_object_ref"] is not None}
        self.assertEqual(set(by_destination), {events["object_ref"], frame["object_ref"]})
        for obj in (events, frame):
            row = by_destination[obj["object_ref"]]
            self.assertIsNone(row["source_object_ref"])
            self.assertEqual(row["source_sha256"], obj["sha256"])

    def test_index_policy_matches_compare_verdict(self):
        _, admission, raw = self.ready_comparison()
        ack, profile = self.start_comparison(admission, raw)
        index = self.fix.index(admission, [profile], policy_sha256="b" * 64)
        terminal = record_bytes(self.fix.compare_terminal(ack, index))
        with self.assertRaisesRegex(ContractError, "index changed the verdict policy"):
            self.fix.finish(self.store, ack, index, terminal=terminal)
        index = self.fix.index(admission, [profile], policy_sha256="a" * 64)
        terminal = record_bytes(self.fix.compare_terminal(ack, index))
        self.assertTrue(self.fix.finish(self.store, ack, index, terminal=terminal)[1])

    def test_truncated_staged_intent_is_inert_during_claim_upload_and_finish_retry(self):
        _, ack = self.fix.start(self.store, self.attempt, claim=False)
        residue = self.path() / "staging" / (".commit-intent.json." + "c" * 32)
        residue.write_bytes(b'{"terminal_b64":')
        residue.chmod(0o600)
        self.reopen()
        self.store.claim(RUNNER, self.attempt, ack, evidence=CLAIM_PROOF)
        self.fix.upload(self.store, self.attempt, "late", b"unsealed")
        page = self.store.recovery_page(RECOVER, limit=20)
        staged = [item for item in page["items"] if item["kind"] == "staging"
                  and item["sha256"] == digest(residue.read_bytes())]
        self.assertEqual(len(staged), 1)
        self.assertEqual(staged[0]["disposition"], "unknown")
        self.assertEqual(self.store.recovery_item(RECOVER, staged[0]["item"]).data,
                         residue.read_bytes())
        self.assertFalse(any(item["kind"] == "commit_intent" for item in page["items"]))
        first, created = self.fix.finish(self.store, ack)
        self.assertTrue(created)
        self.reopen()
        self.assertTrue(residue.exists())
        self.assertEqual(self.fix.finish(self.store, ack), (first, False))

    def test_exact_finish_retries_after_truncated_unpublished_intent(self):
        _, ack = self.fix.start(self.store, self.attempt)

        def interrupt(label, phase):
            if (label, phase) == ("intent", "before_file_sync"):
                raise OSError(errno.ENOSPC, "injected intent sync failure")

        self.store.close()
        with self.fix.store(self.root, fault=interrupt) as failing:
            with self.assertRaises(StorageFailure):
                self.fix.finish(failing, ack)
        residue, = (self.path() / "staging").glob(".commit-intent.json.*")
        residue.write_bytes(b'{"terminal_b64":')
        self.reopen()
        first, created = self.fix.finish(self.store, ack)
        self.assertTrue(created)
        self.assertEqual(self.fix.finish(self.store, ack), (first, False))

    def test_missing_newest_tombstone_fails_even_when_earlier_sequence_is_complete(self):
        _, ack = self.fix.start(self.store, self.attempt)
        self.fix.finish(self.store, ack)
        self.fix.now += timedelta(days=31)
        self.assertEqual(self.store.sweep(), 1)
        self.store.close()
        (self.path().parent.parent / "tombstones" / self.attempt).unlink()
        with self.assertRaisesRegex(IntegrityError, "high-water"):
            self.fix.store(self.root)

    def test_hold_and_deletion_serialize_to_one_eligible_result(self):
        _, ack = self.fix.start(self.store, self.attempt)
        self.fix.finish(self.store, ack)
        self.fix.now += timedelta(days=31)
        barrier = Barrier(2)

        def place_hold():
            barrier.wait()
            try:
                return self.store.hold(ADMIN, self.attempt, "hold-racing", enable=True)
            except Unavailable:
                return False

        def sweep():
            barrier.wait()
            return self.store.sweep()

        with ThreadPoolExecutor(max_workers=2) as pool:
            hold_future = pool.submit(place_hold)
            deletion_future = pool.submit(sweep)
            held, deleted = hold_future.result(), deletion_future.result()
        self.assertIn((held, deleted), [(True, 0), (False, 1)])
        if held:
            self.assertEqual(self.store.inspect(INSPECTOR, self.attempt)["state"], "committed")
        else:
            with self.assertRaises(Unavailable):
                self.store.inspect(INSPECTOR, self.attempt)

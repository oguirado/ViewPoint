"""CPU-only tests of the host TRELLIS pilot authorization boundary."""

from dataclasses import replace
import hashlib
import multiprocessing
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import UUID

from asset_factory import core, proc
from tools.qualification import permit
from tools.qualification.gpu import SCOPE_LIMITS
from tools.qualification.manifest import Refusal

ROOT = Path(__file__).resolve().parents[1]


def encoded(value):
    return core.canonical_json(value).encode("utf-8")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def contend(barrier, result, raw, pin, launch, state):
    try:
        barrier.wait(timeout=10)
        permit.consume_permit(raw, trusted_permit_sha256=pin, launch=launch, state_dir=state)
    except Refusal as exc:
        result.send((exc.rc, exc.code))
    else:
        result.send((0, "consumed"))
    finally:
        result.close()


class TrellisRunPermit(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        self.contract = (ROOT / "providers/trellis2/qualification/result.schema.json").read_bytes()
        self.request = {
            "image": "/pilot/derivative.png", "matte_receipt": "/pilot/matte.json",
            "permit": "/pilot/native-permit.json", "model_root": "/pilot/model",
            "dino_root": "/pilot/dino", "decoder_root": "/pilot/decoder",
            "pipeline_type": "512", "seed": 42,
        }
        self.expires = time.time_ns() + 60_000_000_000
        self.launch = self.launch_for(self.request)
        self.permit = permit.prepare_permit(self.launch, expires_unix_ns=self.expires)

    def launch_for(self, request, *, verdict="PASS", raw_request=None):
        raw_request = raw_request if raw_request is not None else encoded(request)
        subject = {
            "schema": "af.qualification-candidate/v1", "provider": "trellis2",
            "code": {"revision": "b" * 40, "entrypoint": {}, "files": []},
            "weights": [], "conditioners": [], "dependencies": [],
            "runtime": {"cuda": True},
            "request": {"path": "/pilot/request.json", "sha256": digest(raw_request)},
            "input_schema": {},
            "output_schema": {"path": "/pilot/result.schema.json", "sha256": digest(self.contract)},
            "outputs": ["raw.glb", "model-native.npz", "result.json", "cuda-peaks.json"],
        }
        rights = encoded({
            "schema": "af.qualification-rights/v1", "provider": "trellis2",
            "jurisdiction": "EU", "commercial_use": True, "verdict": verdict,
            "subject_sha256": core.sha256_json(subject),
        })
        candidate = {**subject, "rights": {"path": "/pilot/rights.json", "sha256": digest(rights)}}
        return permit.PilotLaunch(
            provider_revision="a" * 40, candidate=candidate, request_bytes=raw_request,
            rights_bytes=rights, derivative_sha256=digest(b"matte derivative"),
            output_contract_bytes=self.contract, memory_limits=proc.MemoryLimits(**SCOPE_LIMITS),
        )

    def consume(self, record=None, launch=None, pin=None, state=None):
        raw = encoded(self.permit if record is None else record)
        return permit.consume_permit(
            raw, trusted_permit_sha256=digest(raw) if pin is None else pin,
            launch=self.launch if launch is None else launch,
            state_dir=self.state if state is None else state,
        )

    def refused(self, rc, code, **kwargs):
        with self.assertRaises(Refusal) as context:
            self.consume(**kwargs)
        self.assertEqual((context.exception.rc, context.exception.code), (rc, code))

    def test_schema_and_durable_consumption(self):
        schema = core.load_json(ROOT / "schemas/af.run-permit.v1.schema.json", max_bytes=8192)
        core.validate(self.permit, schema)
        self.assertEqual(UUID(hex=self.permit["permit_id"]).version, 4)
        self.assertEqual(self.permit["resource_scope"], SCOPE_LIMITS)
        self.assertEqual(self.permit["issuer_sha256"],
                         "77a1c9d9938f41fffe874882a1081a590c2d22806f4cc77366a7a9927049630c")
        receipt = self.consume()
        self.assertEqual(receipt["permit_sha256"], digest(encoded(self.permit)))
        self.assertEqual(receipt["permit_id"], self.permit["permit_id"])
        self.assertLess(receipt["consumed_unix_ns"], self.expires)
        marker, = self.state.iterdir()
        self.assertEqual(marker.read_bytes(), encoded(receipt))
        self.assertEqual(marker.stat().st_mode & 0o777, 0o600)

    def test_replay_after_failed_work_requires_fresh_permit(self):
        first = self.consume()
        with self.assertRaisesRegex(RuntimeError, "synthetic model failure"):
            raise RuntimeError("synthetic model failure")
        self.refused(5, "permit_already_consumed")
        same_id = {**self.permit, "expires_unix_ns": self.expires + 1}
        self.refused(5, "permit_already_consumed", record=same_id)
        self.permit = permit.prepare_permit(self.launch, expires_unix_ns=self.expires)
        second = self.consume()
        self.assertNotEqual(first["permit_id"], second["permit_id"])
        self.assertEqual(len(list(self.state.iterdir())), 2)

    def test_request_seed_derivative_and_provider_mismatch(self):
        requests = [
            ("request", self.launch_for({**self.request, "matte_receipt": "/pilot/other-matte.json"})),
            ("bytes", self.launch_for(self.request, raw_request=encoded(self.request) + b"\n")),
            ("seed", self.launch_for({**self.request, "seed": 43})),
            ("derivative", replace(self.launch, derivative_sha256=digest(b"other derivative"))),
            ("revision", replace(self.launch, provider_revision="c" * 40)),
        ]
        for name, launch in requests:
            with self.subTest(name=name):
                self.refused(4, "permit_binding_stale", launch=launch)
        self.assertEqual(list(self.state.iterdir()), [])
        self.consume()

    def test_stale_candidate_rights_request_and_output_bytes(self):
        cases = [
            ("rights", replace(self.launch, rights_bytes=self.launch.rights_bytes + b"\n"),
             "permit_rights_bytes_stale"),
            ("request", replace(self.launch, request_bytes=self.launch.request_bytes + b"\n"),
             "permit_request_bytes_stale"),
            ("output", replace(self.launch, output_contract_bytes=self.contract + b"\n"),
             "permit_output_contract_stale"),
            ("subject", replace(self.launch, candidate={**self.launch.candidate, "weights": [{}]}),
             "rights_subject_mismatch"),
        ]
        for name, launch, code in cases:
            with self.subTest(name=name):
                self.refused(4, code, launch=launch)
        self.assertEqual(list(self.state.iterdir()), [])

    def test_legacy_unproven_declaration_does_not_block_permit(self):
        launch = self.launch_for(self.request, verdict="CANNOT_SEE")
        record = permit.prepare_permit(launch, expires_unix_ns=self.expires)
        self.assertEqual(record["rights_sha256"], digest(launch.rights_bytes))
        self.consume(record=record, launch=launch)

    def test_permit_works_without_any_grant_record(self):
        candidate = {key: value for key, value in self.launch.candidate.items() if key != "rights"}
        candidate["licence_metadata"] = {"licence": "AUSENTE", "commercial_use": False}
        launch = replace(self.launch, candidate=candidate, rights_bytes=None)
        record = permit.prepare_permit(launch, expires_unix_ns=self.expires)
        self.assertNotIn("rights_sha256", record)
        self.assertEqual(record["candidate_sha256"], core.sha256_json(candidate))
        self.consume(record=record, launch=launch)

    def test_unpinned_legacy_declaration_is_stale(self):
        candidate = {key: value for key, value in self.launch.candidate.items() if key != "rights"}
        launch = replace(self.launch, candidate=candidate)
        with self.assertRaisesRegex(Refusal, "^permit_rights_bytes_stale$"):
            permit.prepare_permit(launch, expires_unix_ns=self.expires)

    def test_permit_cannot_claim_an_unbound_legacy_declaration(self):
        candidate = {key: value for key, value in self.launch.candidate.items() if key != "rights"}
        launch = replace(self.launch, candidate=candidate, rights_bytes=None)
        record = permit.prepare_permit(launch, expires_unix_ns=self.expires)
        record["rights_sha256"] = digest(b"unbound")
        self.refused(4, "permit_binding_stale", record=record, launch=launch)

    def test_permit_encoding_and_fields_are_closed(self):
        malformed = [
            encoded(self.permit) + b"\n",
            b'{"schema":"af.run-permit/v1",' + encoded(self.permit)[1:],
            encoded({**self.permit, "permit_id": "../outside"}),
            encoded({**self.permit, "unused": True}),
        ]
        for raw in malformed:
            with self.subTest(raw=raw[:40]), self.assertRaises(Refusal) as context:
                permit.consume_permit(raw, trusted_permit_sha256=digest(raw),
                                      launch=self.launch, state_dir=self.state)
            self.assertEqual((context.exception.rc, context.exception.code), (2, "run_permit_invalid"))
        self.assertEqual(list(self.state.iterdir()), [])

    def test_expired_permit_never_reserves(self):
        with patch.object(permit.time, "time_ns", return_value=self.expires):
            self.refused(5, "permit_expired")
        self.assertEqual(list(self.state.iterdir()), [])

    def test_expiry_during_durable_write_keeps_reservation(self):
        with patch.object(permit.time, "time_ns", side_effect=[self.expires - 1, self.expires]):
            self.refused(5, "permit_expired")
        self.assertEqual(len(list(self.state.iterdir())), 1)
        self.refused(5, "permit_already_consumed")

    def test_independent_pin_and_issuer_are_required(self):
        self.refused(4, "permit_identity_stale", pin="a" * 64)
        self.refused(2, "permit_pin_required", pin="")
        self.refused(2, "run_permit_invalid", record={**self.permit, "issuer_sha256": "a" * 64})
        self.assertEqual(list(self.state.iterdir()), [])

    def test_geometry_and_scope_are_required(self):
        self.refused(5, "trellis_geometry_required",
                     launch=self.launch_for({**self.request, "pipeline_type": "1024"}))
        limits = proc.MemoryLimits(6 * 1024**3, 8 * 1024**3)
        self.refused(5, "trellis_pilot_scope_required",
                     launch=replace(self.launch, memory_limits=limits))
        self.assertEqual(list(self.state.iterdir()), [])

    def test_concurrent_consumption_in_separate_processes(self):
        context = multiprocessing.get_context("fork")
        barrier = context.Barrier(2)
        raw = encoded(self.permit)
        results, children = [], []
        for _ in range(2):
            reader, writer = context.Pipe(duplex=False)
            child = context.Process(target=contend,
                                    args=(barrier, writer, raw, digest(raw), self.launch, self.state))
            child.start()
            writer.close()
            self.addCleanup(reader.close)
            results.append(reader)
            children.append(child)
        for child in children:
            child.join(timeout=15)
            self.assertEqual(child.exitcode, 0)
            child.close()
        self.assertTrue(all(reader.poll() for reader in results))
        self.assertCountEqual([reader.recv() for reader in results],
                              [(0, "consumed"), (5, "permit_already_consumed")])
        self.assertEqual(len(list(self.state.iterdir())), 1)

    def test_unsafe_or_missing_ledger_is_refused(self):
        subdir = self.state / "public"
        subdir.mkdir(mode=0o755)
        linked = self.state / "link"
        linked.symlink_to(self.state, target_is_directory=True)
        for state in (subdir, linked, self.state / "missing"):
            with self.subTest(state=state.name):
                self.refused(3, "permit_ledger_unavailable", state=state)
        self.assertEqual(list(subdir.iterdir()), [])
        self.assertEqual(set(self.state.iterdir()), {subdir, linked})

    def test_failed_ledger_sync_keeps_reservation_and_refuses_launch(self):
        with patch.object(permit.os, "fsync", side_effect=OSError("synthetic storage failure")):
            self.refused(3, "permit_ledger_unavailable")
        self.assertEqual(len(list(self.state.iterdir())), 1)
        self.refused(5, "permit_already_consumed")

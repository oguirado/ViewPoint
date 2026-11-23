"""D4 lease, admission and runtime evidence oracles."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from functools import partial
from hashlib import sha256
import json
from pathlib import Path
import sys
from threading import Event
from types import ModuleType
import unittest
from unittest.mock import Mock

DIRECT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(DIRECT))

from admission import AdmissionGate, AdmissionPlan, UsageSnapshot
from data.abi import BlockRuntimeAdapter
from data import residency
from schemas.validation import ValidationError, validate_contract


HOLDER = "asset-worker"
LEASE = "vp.model_lease/v1"


def _digest(text):
    return sha256(text.encode()).hexdigest()


def _capability(name, *, device="gpu", state="runnable", territory_ok=True, digest=None):
    digest = digest or _digest(name)
    path = f"fixture/{digest}.json"
    pin = (name, digest, path, _digest(path))
    entry = json.loads((DIRECT / "schemas/tests/examples/vp.capability_v1.valid.json").read_bytes())
    entry.update(tool="vp_factory_call", json_op="vp_factory_call", cli="factory",
                 origin="factory", supported=False)
    entry["provider"] = {"kind": "local_model", "model": {
        "name": name,
        "role": "Synthetic heavy model",
        "weights": {"format": "safetensors", "bytes_on_disk": 1,
                    "tree_sha256": digest, "locator": "af://fixture/weights"},
        "device": device,
        "deps": [],
        "license": {"id": "fixture", "territory_ok": territory_ok},
        "qualification": {"state": state, "receipt": {"path": path, "sha256": pin[-1]},
                          "reason": "Synthetic qualification"},
        "measured": [],
        "residency": {"mode": "cold_process", "lease": "gpu_exclusive" if device == "gpu" else "none",
                      "resident_bytes": None},
    }}
    return entry, pin


class RuntimeFixture:
    """Synthetic transport and separately observed transition receipts."""

    def __init__(self):
        self.transitions = []
        self.observed = {}
        self.reserved = None
        self.return_evidence = True
        self.block_load = None

    @contextmanager
    def reserve(self, digest):
        self.reserved = digest
        try:
            yield {
                "schema": "vp.admission/v1", "surface_id": "fixture", "state": "ACTIVE",
                "backend": "fixture", "admitted": True, "surface_count": 1,
                "resident_bytes": 50, "retained_bytes": 0, "old_ring_bytes": 0,
                "new_ring_bytes": 0, "reserved_peak_bytes": 0, "available_bytes": 300,
                "margin_percent": 30, "gpu_dependent": True, "gpu_accounted": True,
            }
        finally:
            self.reserved = None

    def transition(self, nonce, lease):
        if lease["state"] == "held":
            if self.reserved != lease["model_sha256"]:
                raise AssertionError("Weight allocation ran outside its P6 reservation")
            if self.block_load is not None:
                self.block_load()
        self.transitions.append(lease)
        evidence = object()
        self.observed[evidence] = nonce, dict(lease)
        return evidence if self.return_evidence else None

    def verify(self, nonce, lease, evidence):
        return self.observed.get(evidence) == (nonce, lease)


class ResidencyTests(unittest.TestCase):
    def setUp(self):
        self.fixtures = [
            _capability("TRELLIS.2-4B"),
            _capability("Second fixture model"),
            _capability("CPU fixture model", device="cpu"),
            _capability("Hunyuan3D-2.1", state="excluded", territory_ok=False),
            _capability("Excluded by qualification", state="excluded"),
            _capability("Excluded by territory", territory_ok=False),
            _capability("Ｈunyuan 3D 2.1"),
        ]
        (self.first, self.second, self.cpu, self.hunyuan, self.excluded,
         self.territory, self.hunyuan_alias) = [pin[1] for _, pin in self.fixtures]
        self.runtime = RuntimeFixture()

    def client(self, *, module=residency, configured=True, **ports):
        entries, pins = zip(*self.fixtures)
        if configured:
            defaults = dict(reserve_gpu=self.runtime.reserve, runtime_transition=self.runtime.transition,
                            verify_runtime_receipt=self.runtime.verify)
            defaults.update(ports)
            ports = defaults
        return module.ResidencyClient(entries, verified_qualifications=pins, **ports)

    @staticmethod
    def call(client, op, digest, *, device="gpu", holder=HOLDER, dry_run=False):
        args = dict(op=op, model_sha256=digest, holder=holder, device=device)
        return client.handle(args, holder=holder, dry_run=dry_run)

    def assert_refused(self, code, action, *args, **kwargs):
        with self.assertRaises(residency.ResidencyError) as caught:
            action(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def assert_cold_fallback(self, client):
        self.assertEqual(client.mode, "cold_process")
        data = BlockRuntimeAdapter(partial(client.handle, holder=HOLDER))
        self.assertFalse(data.start_probe(lambda: None, expected_version="fixture-v1").result(5))
        key = {"model_sha256": self.cpu, "holder": HOLDER, "device": "cpu"}
        route = lambda op: data.dispatch(dict(key, op=op))
        lease = route("lease_acquire")
        validate_contract(LEASE, lease.result)
        self.assertEqual((lease.copy, lease.result["state"]), ("file", "held"))
        self.assertEqual(data.dispatch({"op": "status"}).result, {"copy": "file", "leases": [lease.result]})
        self.assertEqual(route("lease_release").result["state"], "released")
        self.assertEqual(data.dispatch({"op": "status"}).result["leases"], [])
        self.assertEqual(client.mode, "cold_process")
        self.assert_refused("NO_LEASE", self.call, client, "lease_acquire", self.first)

    def test_d1_fallback_reports_cold_process_and_handles_cpu_lease(self):
        self.assert_cold_fallback(self.client(configured=False))

    def assert_second_refused(self, client):
        self.assertEqual(self.call(client, "lease_acquire", self.first)["state"], "held")
        self.assertEqual(client.mode, "resident")
        self.assert_refused("BUDGET_EXCEEDED", self.call, client, "lease_acquire", self.second)
        self.assertEqual([lease["model_sha256"] for lease in self.runtime.transitions], [self.first])

    def test_second_heavy_gpu_lease_refused(self):
        self.assert_second_refused(self.client())

    def assert_explicit_eviction(self, client):
        self.call(client, "lease_acquire", self.first)
        self.assert_refused("NO_LEASE", client.evict, self.first, holder=HOLDER)
        released = self.call(client, "lease_release", self.first)
        self.assertEqual(released["state"], "released")
        self.assertEqual(client.mode, "resident")
        self.assert_refused("BUDGET_EXCEEDED", self.call, client, "lease_acquire", self.second)
        self.assert_refused("NO_LEASE", client.evict, self.second, holder=HOLDER)
        self.assert_refused("NO_LEASE", client.evict, self.first, holder="other")
        evicted = client.evict(self.first, holder=HOLDER)
        validate_contract(LEASE, evicted)
        self.assertEqual((evicted["state"], evicted["model_sha256"]), ("evicted", self.first))
        self.assertEqual(client.mode, "cold_process")
        self.assertEqual(self.call(client, "lease_acquire", self.second)["state"], "held")
        self.assertEqual([(lease["model_sha256"], lease["state"]) for lease in self.runtime.transitions],
                         [(self.first, "held"), (self.first, "released"), (self.first, "evicted"),
                          (self.second, "held")])

    def test_eviction_is_explicit_and_content_addressed(self):
        self.assert_explicit_eviction(self.client())

    def test_reacquire_same_released_model_requires_fresh_evidence(self):
        client = self.client()
        self.call(client, "lease_acquire", self.first)
        evidence = next(iter(self.runtime.observed))
        self.call(client, "lease_release", self.first)
        client_before = len(self.runtime.transitions)
        self.assert_refused("BUDGET_EXCEEDED", self.call, client, "lease_acquire",
                            self.first, holder="other")
        self.assertEqual(len(self.runtime.transitions), client_before)
        self.call(client, "lease_acquire", self.first)
        self.assertEqual(len(self.runtime.transitions), client_before + 1)
        self.assertFalse(self.runtime.verify(*next(reversed(self.runtime.observed.values())), evidence))

    def assert_excluded_refused(self, client):
        for digest in (self.hunyuan_alias, self.hunyuan, self.excluded, self.territory):
            self.assert_refused("NO_LEASE", self.call, client, "lease_acquire", digest)
        self.assertEqual(self.runtime.transitions, [])

    def test_excluded_or_territory_denied_model_never_gets_lease(self):
        self.fixtures.append(_capability("Alternate name", digest=self.hunyuan))
        self.assert_excluded_refused(self.client())

    def test_model_qualification_and_device_require_independent_exact_pin(self):
        entry, pin = self.fixtures[0]
        client = residency.ResidencyClient([entry], verified_qualifications=[(*pin[:-1], _digest("other"))])
        self.assert_refused("E_MODEL_NOT_CONFIGURED", self.call, client, "lease_acquire", self.first)
        self.assert_refused("E_MODEL_NOT_CONFIGURED", self.call, self.client(), "lease_acquire",
                            self.first, device="cpu")
        self.assertEqual(self.runtime.transitions, [])

    def test_real_p6_refusal_fences_current_gpu_path(self):
        gate = AdmissionGate(lambda: UsageSnapshot({}), scopes=[("fixture", "/fixture.scope")],
                             read_memory=lambda: (1000, 500),
                             inventory=lambda _: {"complete": True, "charged_bytes": 100})
        plan = AdmissionPlan("fixture", "ACTIVE", "fixture", True)
        with gate.reserve(plan) as decision:
            self.assertFalse(decision["gpu_accounted"])
            self.assertFalse(decision["admitted"])
        client = self.client(reserve_gpu=lambda _: gate.reserve(plan))
        self.assert_refused("NO_LEASE", self.call, client, "lease_acquire", self.first)
        self.assertEqual(client.mode, "cold_process")
        self.assertEqual(self.runtime.transitions, [])

    def test_runtime_without_verifier_and_absent_accounting_refuse(self):
        for ports in ({"reserve_gpu": None}, {"verify_runtime_receipt": None}):
            with self.subTest(ports=ports):
                client = self.client(**ports)
                self.assert_refused("NO_LEASE", self.call, client, "lease_acquire", self.first)
                self.assertEqual(client.mode, "cold_process")
        self.assertEqual(self.runtime.transitions, [])

    def test_cpu_admission_cannot_authorize_gpu_residency(self):
        @contextmanager
        def reserve_cpu(digest):
            with self.runtime.reserve(digest) as admission:
                yield dict(admission, gpu_dependent=False, gpu_accounted=False)

        client = self.client(reserve_gpu=reserve_cpu)
        self.assert_refused("NO_LEASE", self.call, client, "lease_acquire", self.first)
        self.assertEqual(self.runtime.transitions, [])

    def assert_synthetic_receipt_refused(self, client):
        self.assert_refused("UNCERTAIN_OUTCOME", self.call, client, "lease_acquire", self.first)
        self.assert_refused("UNCERTAIN_OUTCOME", lambda: client.mode)
        self.assert_refused("UNCERTAIN_OUTCOME", self.call, client, "lease_acquire", self.second)

    def test_synthesized_but_schema_valid_lease_does_not_prove_residency(self):
        def echo(_, lease):
            validate_contract(LEASE, lease)
            return lease

        client = self.client(runtime_transition=echo, verify_runtime_receipt=lambda *_: False)
        self.assert_synthetic_receipt_refused(client)

    def test_missing_runtime_receipt_fences_until_readback_without_resend(self):
        self.runtime.return_evidence = False
        client = self.client()
        self.assert_synthetic_receipt_refused(client)
        self.assert_refused("UNCERTAIN_OUTCOME", self.call, client, "lease_acquire", self.first)
        self.assert_refused("NO_LEASE", client.reconcile, object(), holder="other")
        self.assert_refused("UNCERTAIN_OUTCOME", client.reconcile, object(), holder=HOLDER)
        self.assertEqual(self.call(client, "lease_acquire", self.cpu, device="cpu")["state"], "held")
        self.assertEqual(self.call(client, "lease_release", self.cpu, device="cpu")["state"], "released")
        evidence = next(iter(self.runtime.observed))
        self.assertEqual(client.reconcile(evidence, holder=HOLDER)["state"], "held")
        self.assertEqual(client.mode, "resident")
        self.assertEqual(len(self.runtime.transitions), 1)

    def test_stale_evidence_cannot_settle_eviction_or_free_gpu(self):
        client = self.client()
        self.call(client, "lease_acquire", self.first)
        stale = next(iter(self.runtime.observed))
        self.call(client, "lease_release", self.first)
        self.runtime.return_evidence = False
        self.assert_refused("UNCERTAIN_OUTCOME", client.evict, self.first, holder=HOLDER)
        self.assert_refused("UNCERTAIN_OUTCOME", client.reconcile, stale, holder=HOLDER)
        self.assert_refused("UNCERTAIN_OUTCOME", client.evict, self.first, holder=HOLDER)
        self.assert_refused("UNCERTAIN_OUTCOME", self.call, client, "lease_acquire", self.second)
        self.assertEqual(len(self.runtime.transitions), 3)
        evidence = next(reversed(self.runtime.observed))
        self.assertEqual(client.reconcile(evidence, holder=HOLDER)["state"], "evicted")
        self.assertEqual(client.mode, "cold_process")

    def test_transport_error_leaves_gpu_fenced_and_is_not_retried(self):
        transition = Mock(side_effect=OSError("lost response"))
        client = self.client(runtime_transition=transition)
        self.assert_synthetic_receipt_refused(client)
        transition.assert_called_once()

    def test_concurrent_heavy_requests_linearize_at_gpu_slot(self):
        entered, resume, second_started = Event(), Event(), Event()

        def block():
            entered.set()
            if not resume.wait(5):
                raise AssertionError("runtime barrier timed out")

        client = self.client()
        self.runtime.block_load = block

        def second_request():
            second_started.set()
            self.call(client, "lease_acquire", self.second)

        with ThreadPoolExecutor() as pool:
            first = pool.submit(self.call, client, "lease_acquire", self.first)
            try:
                self.assertTrue(entered.wait(5))
                second = pool.submit(second_request)
                self.assertTrue(second_started.wait(5))
            finally:
                resume.set()
            self.assertEqual(first.result(5)["state"], "held")
            self.assert_refused("BUDGET_EXCEEDED", second.result, 5)
        self.assertEqual(len(self.runtime.transitions), 1)

    def test_dry_run_never_dispatches_or_changes_custody(self):
        client = self.client()
        self.call(client, "lease_acquire", self.first, dry_run=True)
        self.assertEqual(client.mode, "cold_process")
        self.assertEqual(self.runtime.transitions, [])
        self.call(client, "lease_acquire", self.first)
        self.call(client, "lease_release", self.first, dry_run=True)
        status = client.handle({"op": "status"}, holder=HOLDER)
        self.assertEqual(status["leases"][0]["state"], "held")
        self.call(client, "lease_release", self.first)
        client.evict(self.first, holder=HOLDER, dry_run=True)
        self.assertEqual(client.mode, "resident")
        self.assertEqual(len(self.runtime.transitions), 2)

    def test_invalid_input_and_foreign_holder_are_refused_before_dispatch(self):
        client = self.client()
        args = dict(op="lease_acquire", model_sha256=self.first, holder=HOLDER, device="gpu")
        self.assert_refused("NO_LEASE", client.handle, args, holder="other")
        with self.assertRaises(ValidationError):
            client.handle(dict(args, model_sha256="invalid"), holder=HOLDER)
        self.assertEqual(self.runtime.transitions, [])

    def assert_mutant_killed(self, original, replacement, oracle, **ports):
        source = Path(residency.__file__).read_text()
        self.assertEqual(source.count(original), 1, "mutant missing or ambiguous")
        mutated = source.replace(original, replacement, 1)
        module = ModuleType("mutated_residency")
        exec(compile(mutated, "<D4 mutant>", "exec"), module.__dict__)
        # Keep exception identity so an oracle cannot pass on the wrong exception type.
        module.ResidencyError = residency.ResidencyError
        with self.assertRaises(AssertionError):
            oracle(self.client(module=module, **ports))

    def test_mutant_without_gpu_slot_check(self):
        self.assert_mutant_killed(
            'if self._gpu is not None and (', 'if False and (', self.assert_second_refused)

    def test_mutant_release_implicitly_evicts(self):
        self.assert_mutant_killed(
            'record["state"] == "evicted" else', 'record["state"] != "held" else',
            self.assert_explicit_eviction)

    def test_mutant_claims_residency_without_runtime(self):
        self.assert_mutant_killed(
            'return "resident" if self._gpu is not None else "cold_process"',
            'return "resident"', self.assert_cold_fallback, configured=False)

    def test_mutant_ignores_model_exclusion(self):
        self.assert_mutant_killed(
            'if digest in self._excluded:', 'if False:', self.assert_excluded_refused)

    def test_mutant_accepts_unverified_runtime_receipt(self):
        self.assert_mutant_killed(
            'if not confirmed:', 'if False:', self.assert_synthetic_receipt_refused,
            runtime_transition=lambda _, lease: lease, verify_runtime_receipt=lambda *_: False)

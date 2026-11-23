"""Lease custody for the optional ViewPoint model runtime."""

from threading import Lock
from unicodedata import normalize
from uuid import uuid4

from schemas.validation import validate_contract


class ResidencyError(ValueError):
    """A lease was refused or its runtime outcome needs reconciliation."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _lease(model_sha256, holder, device, state):
    record = {
        "schema": "vp.model_lease/v1",
        "model_sha256": model_sha256,
        "holder": holder,
        "device": device,
        "state": state,
        "evict_policy": "explicit",
        "expires_on": "release",
    }
    validate_contract("vp.model_lease/v1", record)
    return record


def _model_policy(capabilities, verified_qualifications):
    pins = frozenset(verified_qualifications)
    qualified = set()
    excluded = set()
    for entry in capabilities:
        validate_contract("vp.capability/v1", entry)
        model = entry.get("provider", {}).get("model")
        if model is None:
            continue
        digest = model["weights"]["tree_sha256"]
        name = model["name"]
        model_key = "".join(c for c in normalize("NFKC", name).casefold() if c.isalnum())
        qualification = model["qualification"]
        if (qualification["state"] == "excluded" or not model["license"]["territory_ok"]
                or model_key == "hunyuan3d21"):
            excluded.add(digest)
        receipt = qualification["receipt"]
        if (qualification["state"] == "runnable" and receipt is not None
                and (name, digest, receipt["path"], receipt["sha256"]) in pins):
            qualified.add((digest, model["device"]))
    return qualified, excluded


class ResidencyClient:
    """One client per lease owner, shared across the owner's callers.

    Pass effective capability entries and independent qualification pins in
    M1's (name, weight digest, receipt path, receipt digest) format.

    The optional ports are provided by the runtime owner. reserve_gpu(digest)
    must hold a fresh P6 reservation for that digest through allocation.
    runtime_transition(nonce, lease) must execute under the runtime's exclusive
    GPU authority, including across processes and restarts. A released GPU
    lease retains the model. Eviction requires the holder to release first.
    verify_runtime_receipt(nonce, lease, evidence) must independently verify
    this exact transition, including residency for held/released and teardown
    for evicted. A probe or a lease record alone cannot serve as evidence.
    These Python ports require an independently qualified runtime binding.
    """

    def __init__(self, capabilities, *, verified_qualifications=(), reserve_gpu=None,
                 runtime_transition=None, verify_runtime_receipt=None):
        self._qualified, self._excluded = _model_policy(capabilities, verified_qualifications)
        self._reserve_gpu = reserve_gpu
        self._runtime_transition = runtime_transition
        self._verify_runtime_receipt = verify_runtime_receipt
        self._cpu = {}
        self._gpu = None
        self._pending = None
        self._lock = Lock()

    @property
    def mode(self):
        """Report the current residency mode to the local owner."""
        with self._lock:
            self._require_settled()
            return "resident" if self._gpu is not None else "cold_process"

    def handle(self, args, *, holder, dry_run=False):
        """Handle the frozen lease operations with an authenticated holder.

        Bind holder at the caller boundary (for example, in D1's file
        dispatcher). A dry run previews the result without creating authority.
        """
        validate_contract("vp_data_call.args/v1", args)
        op = args["op"]
        if op not in ("status", "lease_acquire", "lease_release"):
            raise ValueError("D4 handles lease operations and status")
        if not isinstance(holder, str) or not holder.strip():
            raise ResidencyError("NO_LEASE", "An authenticated holder is required")
        if op != "status" and args["holder"] != holder:
            raise ResidencyError("NO_LEASE", "The request holder does not match the caller")

        with self._lock:
            if op == "status":
                self._require_settled()
                gpu = self._gpu if self._gpu is not None and self._gpu["holder"] == holder else None
                leases = [record.copy() for _, record in sorted(self._cpu.items())
                          if record["holder"] == holder]
                if gpu is not None:
                    leases.append(gpu.copy())
                result = {"copy": "none" if gpu is not None else "file", "leases": leases}
            else:
                digest, device = args["model_sha256"], args["device"]
                if device == "gpu":
                    self._require_settled()
                if op == "lease_acquire":
                    result = self._acquire(digest, holder, device, dry_run)
                else:
                    result = self._release(digest, holder, device, dry_run)
            validate_contract(f"vp_data_call.{op}.result/v1", result)
            return result

    def _acquire(self, digest, holder, device, dry_run):
        if digest in self._excluded:
            raise ResidencyError("NO_LEASE", "Model is excluded")
        if (digest, device) not in self._qualified:
            raise ResidencyError("E_MODEL_NOT_CONFIGURED", "Model and device lack verified qualification")

        record = _lease(digest, holder, device, "held")
        if device == "cpu":
            if not dry_run:
                self._cpu[digest, holder] = record.copy()
            return record

        if self._gpu is not None and (
            self._gpu["state"] != "released"
            or (self._gpu["model_sha256"], self._gpu["holder"]) != (digest, holder)
        ):
            raise ResidencyError("BUDGET_EXCEEDED", "The exclusive GPU slot is occupied")
        if not all(callable(port) for port in (
            self._reserve_gpu, self._runtime_transition, self._verify_runtime_receipt
        )):
            raise ResidencyError("NO_LEASE", "GPU residency requires P6 and a qualified runtime")
        with self._reserve_gpu(digest) as admission:
            if admission is None:
                raise ResidencyError("NO_LEASE", "GPU admission evidence is absent")
            validate_contract("vp.admission/v1", admission)
            if not (admission["admitted"] and admission["gpu_dependent"] and admission["gpu_accounted"]):
                raise ResidencyError("NO_LEASE", "P6 has not admitted this GPU allocation")
            if not dry_run:
                self._apply_runtime_transition(record)
        return record

    def _release(self, digest, holder, device, dry_run):
        record = _lease(digest, holder, device, "released")
        if device == "cpu":
            key = digest, holder
            if key not in self._cpu:
                raise ResidencyError("NO_LEASE", "No matching held CPU lease")
            if not dry_run:
                del self._cpu[key]
        else:
            self._require_gpu(record, "held")
            if not dry_run:
                self._apply_runtime_transition(record)
        return record

    def evict(self, model_sha256, *, holder, dry_run=False):
        """Explicitly evict the holder's released model by content digest."""
        record = _lease(model_sha256, holder, "gpu", "evicted")
        with self._lock:
            self._require_settled()
            self._require_gpu(record, "released")
            if not dry_run:
                self._apply_runtime_transition(record)
            return record

    def _require_gpu(self, record, state):
        expected = dict(record, state=state)
        if self._gpu != expected:
            raise ResidencyError("NO_LEASE", f"No matching {state} GPU lease")

    def _require_settled(self):
        if self._pending is not None:
            raise ResidencyError("UNCERTAIN_OUTCOME", "A GPU transition needs independent readback")

    def _apply_runtime_transition(self, record):
        nonce = uuid4().hex
        self._pending = nonce, record.copy()
        try:
            evidence = self._runtime_transition(nonce, record.copy())
        except Exception as error:
            raise ResidencyError("UNCERTAIN_OUTCOME", "Runtime transition outcome is unknown") from error
        self._confirm_transition(evidence)

    def reconcile(self, evidence, *, holder):
        """Resolve a fenced transition using verified readback of its outcome."""
        with self._lock:
            if self._pending is None or self._pending[1]["holder"] != holder:
                raise ResidencyError("NO_LEASE", "No matching pending GPU transition")
            return self._confirm_transition(evidence)

    def _confirm_transition(self, evidence):
        nonce, record = self._pending
        try:
            confirmed = (evidence is not None and type(evidence) is not bool
                         and self._verify_runtime_receipt(nonce, record.copy(), evidence) is True)
        except Exception as error:
            raise ResidencyError("UNCERTAIN_OUTCOME", "Runtime evidence could not be verified") from error
        if not confirmed:
            raise ResidencyError("UNCERTAIN_OUTCOME", "Runtime receipt is absent or unverified")
        self._gpu = None if record["state"] == "evicted" else record.copy()
        self._pending = None
        return record.copy()

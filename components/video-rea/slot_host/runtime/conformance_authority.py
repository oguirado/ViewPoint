# SPDX-License-Identifier: AGPL-3.0-only
"""Default-on conformance predicate with host-created fixture receipts.

A live call is enabled by default exactly when all of the following hold:

* the active signed baseline covers the selected slot and operation,
* MAIN has enrolled this binding,
* every mandatory host gate passes (profile, grants, evidence binding
  already enforced by ``binding.verify_binding``),
* the current installation has a verified passing conformance receipt
  for this exact operation/profile,
* Tools can durably retain and resolve its evidence,
* no applicable restriction or revocation denies it.

The predicate is evaluated on every dispatch.  Any missing, stale,
mismatched or unverified prerequisite denies (fail closed).  When the
predicate denies, the live gate stays at ``LIVE_EXECUTION_PARKED`` and
no process is spawned.

Fixture receipts qualify only the host fixture path.  A receipt earned
by a different fixture executable cannot qualify that installation: the
receipt binds ``slot_id``, ``operation_id``, ``implementation_sha256``,
``manifest_digest``, ``conformance_kit_sha256`` and
``registry_generation`` to the exact binding.  A bare ``passed`` string
without a matching receipt never authorizes.

The unconfigured MAIN enrolment check denies (``owner-pending: MAIN``).
The host can configure separately pinned MAIN and Carbon witnesses.
Tools retention remains an owner seam (``owner-pending: T01``).
"""

from __future__ import annotations

import dataclasses
import datetime
import re

from .binding import _EVIDENCE_PINS, _V03_REF, _V03_SHA
from .main_enrolment import MainEnrolmentCheck
from .policy_activation import PolicyActivationRejected, SignedPolicy, activation_lock
from .policy_signature import PolicySignatureRejected
from .retention import RetentionClients

__all__ = (
    "ConformanceDecision",
    "ConformanceReceipt",
    "ConformanceLiveAuthority",
    "ConformanceRejected",
    "ReceiptStore",
    "MAIN_ENROLMENT_DENY_REASON",
    "TOOLS_RETENTION_DENY_REASON",
    "main_enrolment_available",
    "tools_retention_available",
    "create_fixture_receipt",
    "create_fixture_receipt_for_binding",
    "is_receipt_fresh",
    "receipt_matches_binding",
    "policy_covers",
    "evaluate_default_on",
    "is_live_enabled",
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SLOT_ID_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
_OP_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")

MAIN_ENROLMENT_DENY_REASON = "main_not_enrolled: owner-pending: MAIN"
TOOLS_RETENTION_DENY_REASON = "tools_retention_unavailable: owner-pending: T01"


class ConformanceRejected(ValueError):
    """Raised when a receipt or policy is malformed (fail closed)."""

    def __init__(self, reason: str, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclasses.dataclass(frozen=True, kw_only=True)
class ConformanceReceipt:
    """Host-created conformance receipt for one installation/operation.

    The receipt is created only by host code after a bounded fixture run.
    It binds the exact executable object, manifest and kit.  All fields
    are validated before any live launch; a receipt from a different
    executable or a stale receipt never authorizes.
    """

    receipt_id: str
    slot_id: str
    operation_id: str
    implementation_sha256: str
    manifest_digest: str
    conformance_kit_sha256: str
    registry_generation: int
    issued_at: datetime.datetime
    expires_at: datetime.datetime
    outcome: str = "passed"
    provider_revision: str | None = None
    adapter_sha256: str | None = None
    installation_ref: str | None = None


@dataclasses.dataclass(frozen=True, kw_only=True)
class ConformanceDecision:
    allowed: bool
    reason: str


@dataclasses.dataclass
class ReceiptStore:
    """Host-owned in-memory receipt store keyed by binding identity."""

    _by_key: dict[tuple[str, str, str, str | None], ConformanceReceipt] = dataclasses.field(default_factory=dict)

    def put(self, receipt: ConformanceReceipt) -> ConformanceReceipt:
        key = (receipt.slot_id, receipt.operation_id, receipt.implementation_sha256, receipt.installation_ref)
        self._by_key[key] = receipt
        return receipt

    def get(self, *, slot_id: str, operation_id: str, implementation_sha256: str, installation_ref=None) -> ConformanceReceipt | None:
        return self._by_key.get((slot_id, operation_id, implementation_sha256, installation_ref))

    def get_for_binding(self, binding, operation_id: str) -> ConformanceReceipt | None:
        try:
            slot = getattr(binding, "slot_id", None)
            impl = getattr(binding, "implementation_sha256", None)
        except Exception:
            return None
        if not isinstance(slot, str) or not isinstance(impl, str) or not isinstance(operation_id, str):
            return None
        return self.get(slot_id=slot, operation_id=operation_id, implementation_sha256=impl,
                        installation_ref=getattr(binding, "installation_ref", None))

    def clear(self) -> None:
        self._by_key.clear()


def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def _coerce_dt(value) -> datetime.datetime | None:
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            return None
        try:
            return value.astimezone(datetime.timezone.utc).replace(microsecond=0)
        except Exception:
            return None
    if isinstance(value, str):
        try:
            # Accept only Zulu seconds form YYYY-MM-DDTHH:MM:SSZ
            if not re.fullmatch(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$", value):
                return None
            parsed = datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
            return parsed.replace(tzinfo=datetime.timezone.utc)
        except Exception:
            return None
    return None


def _coerce_now(now) -> datetime.datetime:
    dt = _coerce_dt(now) if now is not None else _now_utc()
    if dt is None and now is not None:
        raise ConformanceRejected("now_invalid", "now must be timezone-aware datetime or Zulu string")
    return dt


def _is_sha256(value) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None and value != "0" * 64


# --------------------------------------------------------------------------
# Owner seams: default deny (AUSENTE until owners deliver)
# --------------------------------------------------------------------------

def main_enrolment_available(*, binding, operation_id: str, policy: dict | None = None) -> bool:
    """Default MAIN check when the host has configured no enrolment source."""
    _ = binding, operation_id, policy
    return False


def tools_retention_available(*, binding, operation_id: str, policy: dict | None = None,
                              retention=None) -> bool:
    """Check current authenticated readiness without opening an attempt."""
    return (
        isinstance(retention, RetentionClients) and binding.operation_id == operation_id
        and _EVIDENCE_PINS.get(binding.shared_schema_ref) == (_V03_REF, _V03_SHA)
        and binding.shared_schema_sha256 == _V03_SHA
        and retention.matches_binding(binding)
        and retention.ready(record_pin=_V03_SHA, operation_id=operation_id)
    )


# --------------------------------------------------------------------------
# Receipt validation
# --------------------------------------------------------------------------

def validate_receipt(receipt: ConformanceReceipt | dict) -> ConformanceReceipt:
    """Validate receipt shape; raise ConformanceRejected on malformed."""
    if isinstance(receipt, dict):
        try:
            issued = _coerce_dt(receipt.get("issued_at"))
            expires = _coerce_dt(receipt.get("expires_at"))
            receipt = ConformanceReceipt(
                receipt_id=str(receipt.get("receipt_id", "")),
                slot_id=str(receipt.get("slot_id", "")),
                operation_id=str(receipt.get("operation_id", "")),
                implementation_sha256=str(receipt.get("implementation_sha256", "")),
                manifest_digest=str(receipt.get("manifest_digest", "")),
                conformance_kit_sha256=str(receipt.get("conformance_kit_sha256", "")),
                registry_generation=int(receipt.get("registry_generation", 0)) if type(receipt.get("registry_generation")) is int else 0,
                issued_at=issued,
                expires_at=expires,
                outcome=str(receipt.get("outcome", "")),
                provider_revision=receipt.get("provider_revision"),
                adapter_sha256=receipt.get("adapter_sha256"),
                installation_ref=receipt.get("installation_ref"),
            )
        except Exception as exc:
            raise ConformanceRejected("receipt_invalid", f"cannot materialize receipt: {exc}") from exc
    if not isinstance(receipt, ConformanceReceipt):
        raise ConformanceRejected("receipt_invalid", "receipt must be a ConformanceReceipt or dict")
    if not isinstance(receipt.receipt_id, str) or not receipt.receipt_id.strip():
        raise ConformanceRejected("receipt_invalid", "receipt_id must be nonempty")
    if not isinstance(receipt.slot_id, str) or _SLOT_ID_RE.fullmatch(receipt.slot_id) is None:
        raise ConformanceRejected("receipt_invalid", "slot_id invalid")
    if not isinstance(receipt.operation_id, str) or _OP_ID_RE.fullmatch(receipt.operation_id) is None:
        raise ConformanceRejected("receipt_invalid", "operation_id invalid")
    if not _is_sha256(receipt.implementation_sha256):
        raise ConformanceRejected("receipt_invalid", "implementation_sha256 must be SHA-256 hex")
    if not _is_sha256(receipt.manifest_digest):
        raise ConformanceRejected("receipt_invalid", "manifest_digest must be SHA-256 hex")
    if not _is_sha256(receipt.conformance_kit_sha256):
        raise ConformanceRejected("receipt_invalid", "conformance_kit_sha256 must be SHA-256 hex")
    if type(receipt.registry_generation) is not int or receipt.registry_generation < 1 or receipt.registry_generation > (1 << 53) - 1:
        raise ConformanceRejected("receipt_invalid", "registry_generation out of bounds")
    if not isinstance(receipt.issued_at, datetime.datetime) or not isinstance(receipt.expires_at, datetime.datetime):
        raise ConformanceRejected("receipt_invalid", "issued_at/expires_at must be datetimes")
    issued = _coerce_dt(receipt.issued_at)
    expires = _coerce_dt(receipt.expires_at)
    if issued is None or expires is None:
        raise ConformanceRejected("receipt_invalid", "issued_at/expires_at must be timezone-aware")
    if not issued < expires:
        raise ConformanceRejected("receipt_invalid", "issued_at must be before expires_at")
    # Normalize to UTC without microseconds for consistent checks.
    receipt = dataclasses.replace(receipt, issued_at=issued, expires_at=expires)
    if receipt.outcome != "passed":
        raise ConformanceRejected("receipt_not_passed", f"outcome {receipt.outcome!r} is not passed")
    if receipt.provider_revision is not None and (not isinstance(receipt.provider_revision, str) or not receipt.provider_revision.strip()):
        raise ConformanceRejected("receipt_invalid", "provider_revision invalid")
    if receipt.adapter_sha256 is not None and not _is_sha256(receipt.adapter_sha256):
        raise ConformanceRejected("receipt_invalid", "adapter_sha256 must be SHA-256 hex")
    if receipt.installation_ref is not None and (
        type(receipt.installation_ref) is not str or not receipt.installation_ref.strip()
    ):
        raise ConformanceRejected("receipt_invalid", "installation_ref invalid")
    return receipt


def is_receipt_fresh(receipt: ConformanceReceipt, now: datetime.datetime) -> bool:
    """True only when issued_at <= now < expires_at."""
    try:
        receipt = validate_receipt(receipt)
    except ConformanceRejected:
        return False
    moment = _coerce_dt(now)
    if moment is None:
        return False
    return receipt.issued_at <= moment < receipt.expires_at


def receipt_matches_binding(receipt: ConformanceReceipt, binding, operation_id: str) -> bool:
    """Exact binding match: slot, operation, implementation, manifest, optional pins."""
    try:
        receipt = validate_receipt(receipt)
    except ConformanceRejected:
        return False
    if not isinstance(operation_id, str):
        return False
    if receipt.operation_id != operation_id:
        return False
    try:
        slot = getattr(binding, "slot_id", None)
        impl = getattr(binding, "implementation_sha256", None)
        manifest_digest = getattr(binding, "manifest_digest", None)
    except Exception:
        return False
    if slot != receipt.slot_id:
        return False
    if impl != receipt.implementation_sha256:
        return False
    if not _is_sha256(manifest_digest) or manifest_digest != receipt.manifest_digest:
        return False
    return all(
        getattr(binding, name, None) == pinned
        for name, pinned in (
            ("provider_revision", receipt.provider_revision),
            ("adapter_sha256", receipt.adapter_sha256),
            ("installation_ref", receipt.installation_ref),
        )
        if pinned is not None or name == "installation_ref"
    )


_PERCEPTION_LEVELS = ("none", "cdp_atspi", "vision")


def _within_perception(binding, ceiling):
    levels = (getattr(binding, "perception_required", None), *getattr(binding, "perception_optional", ()))
    return ceiling in _PERCEPTION_LEVELS and all(
        level in _PERCEPTION_LEVELS and _PERCEPTION_LEVELS.index(level) <= _PERCEPTION_LEVELS.index(ceiling)
        for level in levels
    )


def _restriction_met(item, binding):
    kind = item["kind"]
    if kind == "lower_perception_ceiling":
        return _within_perception(binding, item["perception_ceiling"])
    if kind == "limit_timeout":
        return getattr(binding, "timeout_s", float("inf")) <= item["timeout_s"]
    if kind == "limit_output_bytes":
        return getattr(binding, "max_output_bytes", float("inf")) <= item["max_output_bytes"]
    if kind == "remove_grant":
        grants = getattr(binding, "grants", None)
        return grants is not None and item["grant_id"] not in grants
    if kind == "remove_env":
        env = getattr(binding, "env_allowlist", None)
        return env is not None and item["env_name"] not in env
    # Disable and preview_only always deny a live grant. The remaining
    # restrictions need authenticated approval, a principal or a signed
    # qualification deadline. The offline binding provides none of these.
    return False


def policy_covers(policy: dict | None, binding, operation_id: str) -> tuple[bool, str]:
    """Check signed baseline scope coverage plus revocation/restriction denies."""
    if not isinstance(policy, dict):
        return False, "policy_missing"
    baseline = policy.get("baseline")
    if not isinstance(baseline, dict):
        return False, "policy_invalid: baseline must be an object"
    if baseline.get("mode") != "live_after_conformance":
        return False, "policy_invalid: baseline.mode must be live_after_conformance"
    if getattr(binding, "execution_profile", None) not in baseline.get("allowed_profiles", ()):
        return False, "policy_baseline_mismatch: execution profile"
    transport = getattr(binding, "transport_kind", None)
    operation_kind = getattr(binding, "operation_binding", {}).get("kind")
    if not {transport, operation_kind} <= set(baseline.get("allowed_transports", ())):
        return False, "policy_baseline_mismatch: transport or operation binding"
    if not set(baseline.get("required_grants", ())) <= set(getattr(binding, "grants", ())):
        return False, "policy_baseline_mismatch: required grant"
    if not _within_perception(binding, baseline.get("perception_ceiling")):
        return False, "policy_baseline_mismatch: perception ceiling"
    evidence_pin = _EVIDENCE_PINS.get(getattr(binding, "shared_schema_ref", None))
    if (
        evidence_pin is None
        or evidence_pin != (baseline.get("shared_evidence_ref"), baseline.get("shared_evidence_sha256"))
        or getattr(binding, "shared_schema_sha256", None) != evidence_pin[1]
    ):
        return False, "policy_baseline_mismatch: shared evidence pin"
    if not _is_sha256(baseline.get("conformance_kit_sha256")):
        return False, "policy_invalid: conformance_kit_sha256 must be SHA-256 hex"
    reg = baseline.get("registry_generation")
    if type(reg) is not int or not 1 <= reg <= (1 << 53) - 1:
        return False, "policy_invalid: registry_generation out of bounds"
    gen = policy.get("generation")
    revocation = policy.get("revocation")
    if type(gen) is not int or gen < 1 or not isinstance(revocation, dict):
        return False, "policy_invalid: generation or revocation"
    slot = getattr(binding, "slot_id", None)
    impl = getattr(binding, "implementation_sha256", None)
    md = getattr(binding, "manifest_digest", None)
    installation = getattr(binding, "installation_ref", None)
    if not isinstance(slot, str) or not slot or not _is_sha256(impl) or not _is_sha256(md):
        return False, "binding_invalid: missing slot, executable or manifest pin"
    if revocation.get("installation_refs") and not installation:
        return False, "binding_invalid: missing installation ref for revocation"
    for selector, value in (
        ("slot_ids", slot), ("operation_ids", operation_id),
        ("implementation_sha256s", impl), ("installation_refs", installation),
        ("revoked_generations", gen),
    ):
        if value in revocation.get(selector, ()):
            return False, f"policy_revoked: {selector} includes {value!r}"
    restrictions = policy.get("restrictions")
    if type(restrictions) is not list:
        return False, "policy_invalid: restrictions must be a list"
    for item in restrictions:
        if type(item) is not dict or not {"kind", "target"} <= item.keys():
            return False, "policy_invalid: restriction is missing kind or target"
        if item["target"] in (slot, operation_id, installation) and not _restriction_met(item, binding):
            return False, f"policy_restricted: {item['kind']} on {item['target']!r}"
    # Scope coverage.
    scope = policy.get("scope")
    if not isinstance(scope, list):
        return False, "policy_invalid: scope must be a list"
    covered = False
    for rule in scope:
        if not isinstance(rule, dict):
            continue
        has_slot = "slot_id" in rule
        has_ns = "namespace" in rule
        if has_slot == has_ns:
            return False, "policy_invalid: scope rule needs exactly one of slot_id/namespace"
        slot_match = False
        if has_slot:
            if rule.get("slot_id") == slot:
                slot_match = True
        else:
            ns = rule.get("namespace")
            if isinstance(ns, str) and (slot == ns or slot.startswith(ns + "-")):
                slot_match = True
        if not slot_match:
            continue
        # Optional narrowing selectors.
        if "implementation_sha256" in rule and rule.get("implementation_sha256") != impl:
            continue
        if any(
            name in rule and (not current or rule[name] != current)
            for name, current in (
                ("manifest_sha256", md),
                ("provider_revision", getattr(binding, "provider_revision", None)),
                ("adapter_sha256", getattr(binding, "adapter_sha256", None)),
                ("installation_ref", installation),
            )
        ):
            continue
        ops = rule.get("operation_ids")
        if ops is not None:
            if not isinstance(ops, list) or operation_id not in ops:
                continue
        covered = True
        break
    if not covered:
        return False, "scope_not_covered: no scope rule matches slot/operation"
    return True, "covered"


def evaluate_default_on(
    *,
    binding,
    operation_id: str,
    policy: SignedPolicy | None,
    receipt: ConformanceReceipt | dict | None,
    enrolment_check=None,
    retention_check=None,
    retention=None,
    now=None,
    activation_state: dict | None = None,
) -> ConformanceDecision:
    """Evaluate the default-on predicate (fail closed).

    Every check must pass for ``allowed=True``.  Any exception, missing
    prerequisite or stale value denies with a specific reason.  The
    function never raises for ordinary denial; callers inspect
    ``decision.allowed``.

    Prerequisites (all required):
      * signed baseline covers slot/operation and is not revoked/restricted
      * fixture receipt exists, outcome passed, fresh, and matches the exact
        binding (slot, operation, implementation, manifest, kit, registry)
      * MAIN enrolment check returns strictly True
      * Tools retention check returns strictly True
      * signed envelope agrees with the committed activation at ``now``
    """
    try:
        if not isinstance(operation_id, str) or not operation_id:
            return ConformanceDecision(allowed=False, reason="operation_invalid")
        if binding is None:
            return ConformanceDecision(allowed=False, reason="binding_missing")
        try:
            binding_op = getattr(binding, "operation_id", None)
        except Exception:
            binding_op = None
        if binding_op != operation_id:
            return ConformanceDecision(allowed=False, reason=f"operation_mismatch: binding {binding_op!r} != requested {operation_id!r}")
        moment = _coerce_now(now) if now is not None else _now_utc()

        if type(policy) is not SignedPolicy or activation_state is not None:
            return ConformanceDecision(allowed=False, reason="activation_required: signed policy and committed state required")
        try:
            policy = policy.read(moment)
        except (PolicyActivationRejected, PolicySignatureRejected) as exc:
            return ConformanceDecision(allowed=False, reason=f"{exc.reason}: {exc.detail}")

        ok, why = policy_covers(policy, binding, operation_id)
        if not ok:
            return ConformanceDecision(allowed=False, reason=why)
        baseline = policy["baseline"]
        kit = baseline["conformance_kit_sha256"]
        reg_gen = baseline["registry_generation"]

        # 2. Receipt: presence, shape, freshness, match.
        if receipt is None:
            return ConformanceDecision(allowed=False, reason="receipt_missing")
        try:
            validated = validate_receipt(receipt)
        except ConformanceRejected as exc:
            return ConformanceDecision(allowed=False, reason=f"{exc.reason}: {exc.detail}")
        # Freshness.
        if not (validated.issued_at <= moment < validated.expires_at):
            if moment < validated.issued_at:
                return ConformanceDecision(allowed=False, reason="receipt_not_yet_valid")
            return ConformanceDecision(allowed=False, reason="receipt_expired")
        # Exact binding match.
        if not getattr(binding, "installation_ref", None) or not receipt_matches_binding(validated, binding, operation_id):
            return ConformanceDecision(allowed=False, reason="receipt_mismatch: receipt does not match binding identity")
        # Kit pin.
        if validated.conformance_kit_sha256 != kit:
            return ConformanceDecision(allowed=False, reason="receipt_kit_mismatch")
        # Registry generation pin.
        if validated.registry_generation != reg_gen:
            return ConformanceDecision(allowed=False, reason="receipt_registry_mismatch")
        # Revocation also applies to the receipt's own generation pin: if the
        # policy's registry generation is revoked, deny (already covered via
        # policy generation revoked, but registry generation revoked is checked
        # through the same revocation list if present).
        revocation = policy.get("revocation", {}) if isinstance(policy, dict) else {}
        if isinstance(revocation, dict):
            revoked_gens = revocation.get("revoked_generations", [])
            if isinstance(revoked_gens, list) and validated.registry_generation in revoked_gens:
                return ConformanceDecision(allowed=False, reason="registry_revoked")

        # 3. MAIN enrolment seam (strict True only).
        if enrolment_check is None:
            return ConformanceDecision(allowed=False, reason=MAIN_ENROLMENT_DENY_REASON)
        try:
            if type(enrolment_check) is MainEnrolmentCheck:
                enrolled = enrolment_check(binding=binding, operation_id=operation_id, policy=policy, now=moment)
            else:
                try:
                    enrolled = enrolment_check(binding=binding, operation_id=operation_id, policy=policy)
                except TypeError:
                    enrolled = enrolment_check(binding, operation_id)
        except Exception:
            return ConformanceDecision(allowed=False, reason=MAIN_ENROLMENT_DENY_REASON)
        if enrolled is not True:
            return ConformanceDecision(allowed=False, reason=MAIN_ENROLMENT_DENY_REASON)

        # 4. Tools readiness. The legacy predicate is used by offline fixtures.
        if retention is not None or getattr(binding, "retention_required", False):
            retained = tools_retention_available(
                binding=binding, operation_id=operation_id, policy=policy, retention=retention,
            )
        elif retention_check is None:
            retained = False
        else:
            try:
                retained = retention_check(binding=binding, operation_id=operation_id, policy=policy)
            except TypeError:
                try:
                    retained = retention_check(binding, operation_id)
                except Exception:
                    retained = False
            except Exception:
                retained = False
        if retained is not True:
            return ConformanceDecision(allowed=False, reason=TOOLS_RETENTION_DENY_REASON)

        return ConformanceDecision(allowed=True, reason="allowed")
    except Exception:
        # Fail closed on any unexpected error.
        return ConformanceDecision(allowed=False, reason="predicate_error")


def is_live_enabled(
    *,
    binding,
    operation_id: str,
    policy: SignedPolicy | None,
    receipt: ConformanceReceipt | dict | None,
    enrolment_check=None,
    retention_check=None,
    retention=None,
    now=None,
    activation_state: dict | None = None,
) -> bool:
    """Strict predicate: True only when every prerequisite passes."""
    try:
        decision = evaluate_default_on(
            binding=binding,
            operation_id=operation_id,
            policy=policy,
            receipt=receipt,
            enrolment_check=enrolment_check,
            retention_check=retention_check,
            retention=retention,
            now=now,
            activation_state=activation_state,
        )
        return decision.allowed is True
    except Exception:
        return False


def create_fixture_receipt(
    *,
    slot_id: str,
    operation_id: str,
    implementation_sha256: str,
    manifest_digest: str,
    conformance_kit_sha256: str,
    registry_generation: int,
    now=None,
    valid_for_seconds: int = 3600,
    outcome: str = "passed",
    provider_revision: str | None = None,
    adapter_sha256: str | None = None,
    receipt_id: str | None = None,
    installation_ref: str | None = None,
) -> ConformanceReceipt:
    """Create a host-created fixture receipt (test helper; host-only)."""
    moment = _coerce_now(now) if now is not None else _now_utc()
    issued = moment
    expires = moment + datetime.timedelta(seconds=int(valid_for_seconds))
    if receipt_id is None:
        # Deterministic id from binding identity.
        import hashlib as _hashlib
        seed = f"{slot_id}:{operation_id}:{implementation_sha256}:{manifest_digest}:{installation_ref}:{moment.isoformat()}"
        receipt_id = "receipt-" + _hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
    return ConformanceReceipt(
        receipt_id=str(receipt_id),
        slot_id=str(slot_id),
        operation_id=str(operation_id),
        implementation_sha256=str(implementation_sha256),
        manifest_digest=str(manifest_digest),
        conformance_kit_sha256=str(conformance_kit_sha256),
        registry_generation=int(registry_generation),
        issued_at=issued,
        expires_at=expires,
        outcome=str(outcome),
        provider_revision=provider_revision,
        adapter_sha256=adapter_sha256,
        installation_ref=installation_ref,
    )


def create_fixture_receipt_for_binding(
    binding,
    operation_id: str,
    *,
    conformance_kit_sha256: str,
    registry_generation: int,
    now=None,
    valid_for_seconds: int = 3600,
    outcome: str = "passed",
) -> ConformanceReceipt:
    """Create a fixture receipt directly from a Binding (host-only helper)."""
    try:
        slot = getattr(binding, "slot_id", None)
        impl = getattr(binding, "implementation_sha256", None)
        manifest_digest = getattr(binding, "manifest_digest", None)
        provider_rev = getattr(binding, "provider_revision", None)
        adapter = getattr(binding, "adapter_sha256", None)
    except Exception as exc:
        raise ConformanceRejected("binding_invalid", f"cannot read binding identity: {exc}") from exc
    if not isinstance(slot, str) or not slot:
        raise ConformanceRejected("binding_invalid", "binding missing slot_id")
    if not _is_sha256(impl):
        raise ConformanceRejected("binding_invalid", "binding implementation_sha256 invalid")
    if not _is_sha256(manifest_digest):
        raise ConformanceRejected("binding_invalid", "binding manifest_digest invalid")
    return create_fixture_receipt(
        slot_id=slot,
        operation_id=operation_id,
        implementation_sha256=impl,
        manifest_digest=manifest_digest,
        conformance_kit_sha256=conformance_kit_sha256,
        registry_generation=registry_generation,
        now=now,
        valid_for_seconds=valid_for_seconds,
        outcome=outcome,
        provider_revision=provider_rev if isinstance(provider_rev, str) and provider_rev else None,
        adapter_sha256=adapter if isinstance(adapter, str) and _is_sha256(adapter) else None,
        installation_ref=getattr(binding, "installation_ref", None),
    )


class ConformanceLiveAuthority:
    """LiveAuthority implementation backed by the default-on predicate.

    Preserve the landed ``verify(*, binding, operation_id, request_id,
    attempt_id) -> bool`` protocol.  Host composition constructs one
    instance with the verified policy, host-owned receipt store and
    injected enrolment/retention checks.  The verifier returns strictly
    ``True`` only when :func:`evaluate_default_on` allows; any
    exception, non-True result or stale prerequisite returns ``False``.
    ``request_id``/``attempt_id`` are bound for audit but never
    influence the decision beyond being forwarded to the seams.
    """

    def __init__(
        self,
        *,
        policy: SignedPolicy | None = None,
        receipt_store: ReceiptStore | None = None,
        receipts: list[ConformanceReceipt] | tuple[ConformanceReceipt, ...] | None = None,
        enrolment_check=None,
        retention_check=None,
        retention=None,
        activation_state: dict | None = None,
        now=None,
    ):
        self._policy = policy
        self._store = receipt_store if receipt_store is not None else ReceiptStore()
        if receipts is not None:
            for r in receipts:
                try:
                    self._store.put(r)
                except Exception:
                    pass
        self._enrolment_check = enrolment_check if enrolment_check is not None else main_enrolment_available
        self._retention_check = retention_check
        self._retention = retention
        self._activation_state = activation_state
        self._now = now
        self.last_reason = ""

    @property
    def requires_retention_handoff(self) -> bool:
        return self._retention is not None

    @property
    def receipt_store(self) -> ReceiptStore:
        return self._store

    def qualify_fixture(self, binding, operation_id: str, **kwargs) -> ConformanceReceipt:
        """Host-only helper: create and store a fixture receipt for testing."""
        receipt = create_fixture_receipt_for_binding(binding, operation_id, **kwargs)
        self._store.put(receipt)
        return receipt

    def admission_lock(self):
        return activation_lock(self._policy.state_path)

    def verify(self, *, binding, operation_id: str, request_id: str, attempt_id: str) -> bool:
        _ = request_id, attempt_id
        if self._enrolment_check is main_enrolment_available:
            self.last_reason = MAIN_ENROLMENT_DENY_REASON
            return False
        try:
            receipt = self._store.get_for_binding(binding, operation_id)
            # Resolve the clock again for every admission and disclosure.
            now = self._now() if callable(self._now) else self._now
            decision = evaluate_default_on(
                binding=binding,
                operation_id=operation_id,
                policy=self._policy,
                receipt=receipt,
                enrolment_check=self._enrolment_check,
                retention_check=self._retention_check,
                retention=self._retention,
                now=now,
                activation_state=self._activation_state,
            )
            self.last_reason = decision.reason
            return decision.allowed is True
        except Exception:
            self.last_reason = "predicate_error"
            return False

# SPDX-License-Identifier: AGPL-3.0-only
"""Fail-closed gate for external execution and host-owned disposable fixtures."""

import threading
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Protocol

from .executable_custody import measure_verified_executable

LIVE_EXECUTION_PARKED = "LIVE_EXECUTION_PARKED"


class LiveAuthority(Protocol):
    """Authenticated host authority capable of opening the live gate."""

    def verify(
        self,
        *,
        binding,
        operation_id: str,
        request_id: str,
        attempt_id: str,
    ) -> bool:
        """Return True only when host policy authorizes this live launch."""
        ...


class DenyLiveAuthority:
    """Default authority: always denies. No production authority exists."""

    def verify(
        self,
        *,
        binding,
        operation_id: str,
        request_id: str,
        attempt_id: str,
    ) -> bool:
        return False


_DEFAULT_AUTHORITY = DenyLiveAuthority()


class _FixtureLiveAuthority:
    """Host-created capability for one exact fixture object and request."""

    def __init__(self, binding, *, request_id: str, attempt_id: str):
        self._path = Path(binding.executable_path).resolve(strict=True)
        self._digest = binding.implementation_sha256
        self._operation_id = binding.operation_id
        if self._path != Path(binding.executable_path) or binding.observed_sha256 != self._digest:
            raise ValueError("fixture requires a canonical, measured executable")
        if not all(type(value) is str and value for value in (self._operation_id, request_id, attempt_id)):
            raise ValueError("fixture requires operation and request identity")
        self._request_id = request_id
        self._attempt_id = attempt_id
        self._identity = self._object_identity()
        self._lock = threading.RLock()
        self._revoked = False

    def _object_identity(self):
        _, identity = measure_verified_executable(self._path, self._digest)
        return identity

    def attempt_for(self, request_id):
        return self._attempt_id if request_id == self._request_id else None

    @property
    def source_identity(self):
        return self._identity

    def revoke(self):
        with self._lock:
            self._revoked = True

    def verify(self, *, binding, operation_id, request_id, attempt_id) -> bool:
        with self._lock:
            return (
                not self._revoked
                and (operation_id, request_id, attempt_id)
                == (self._operation_id, self._request_id, self._attempt_id)
                and binding.operation_id == operation_id
                and Path(binding.executable_path) == self._path
                and binding.implementation_sha256 == binding.observed_sha256 == self._digest
                and self._object_identity() == self._identity
            )


def make_conformance_authority(
    *, policy=None, receipt_store=None, receipts=None, enrolment_check=None,
    retention_check=None, retention=None, activation_state=None, now=None,
    enrolment_directory=None, enrolment_attestation_ref=None, enrolment_carbon_read_ref=None,
):
    """Build the verifier with host-provisioned enrolment and signed policy inputs."""
    from .conformance_authority import ConformanceLiveAuthority
    from .main_enrolment import MainEnrolmentCheck

    if any(value is not None for value in (
        enrolment_directory, enrolment_attestation_ref, enrolment_carbon_read_ref,
    )):
        if enrolment_check is not None:
            raise ValueError("configure one MAIN enrolment check")
        enrolment_check = MainEnrolmentCheck(
            directory=enrolment_directory, attestation_ref=enrolment_attestation_ref,
            carbon_read_ref=enrolment_carbon_read_ref,
        )

    return ConformanceLiveAuthority(
        policy=policy, receipt_store=receipt_store, receipts=receipts,
        enrolment_check=enrolment_check, retention_check=retention_check, retention=retention,
        activation_state=activation_state, now=now,
    )


def authorize_live(
    binding, executable_path, *, live_authority=None, operation_id: str,
    request_id: str, attempt_id: str | None, readiness_only: bool = False,
) -> bool:
    """Ask the supplied host verifier for a strict positive on this exact call."""
    authority = live_authority if live_authority is not None else _DEFAULT_AUTHORITY
    try:
        # Executing a retention-backed binding requires a verified StartAck
        # and one-shot claim at the runner's final launch boundary.
        if not readiness_only and (
            getattr(binding, "retention_required", False)
            or getattr(authority, "requires_retention_handoff", False) is True
        ):
            return False
        return (
            Path(executable_path) == Path(binding.executable_path)
            and authority.verify(
                binding=binding, operation_id=operation_id, request_id=request_id,
                attempt_id=attempt_id,
            ) is True
        )
    except Exception:
        return False


@contextmanager
def live_admission(binding, *, live_authority, operation_id, request_id, attempt_id, readiness_only=False):
    """Hold revocation exclusion across final verification and the protected action."""
    from .conformance_authority import ConformanceLiveAuthority

    with ExitStack() as stack:
        try:
            if type(live_authority) is ConformanceLiveAuthority:
                stack.enter_context(live_authority.admission_lock())
            elif type(live_authority) is _FixtureLiveAuthority:
                stack.enter_context(live_authority._lock)
        except Exception:
            yield False
            return
        yield authorize_live(
            binding, binding.executable_path, live_authority=live_authority,
            operation_id=operation_id, request_id=request_id, attempt_id=attempt_id,
            readiness_only=readiness_only,
        )

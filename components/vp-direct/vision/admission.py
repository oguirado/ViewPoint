"""Lease host vision work through the shared ViewPoint Tools helper."""

import json
import os
from pathlib import Path
import subprocess
import sys


class VisionAdmissionError(RuntimeError):
    """The shared pool did not grant or settle the lease."""


class PooledPerceiver:
    """Provide one engine job per lease, including persistent worker teardown."""

    def __init__(self, factory, helper=None):
        self._factory = factory
        self._helper = helper
        self._unsettled = False
        self.last_c2_payload = self.last_transport = self.last_vserve_error = None

    def _call(self, command, *arguments, timeout, status):
        helper = Path(self._helper or os.environ.get("VP_VISION_POOL_HELPER", ""))
        if not helper.is_absolute() or not helper.is_file():
            raise VisionAdmissionError("shared vision pool helper unavailable")
        try:
            result = subprocess.run(
                [sys.executable, "-IB", str(helper), command, "--front", "MAIN",
                 "--pid", str(os.getpid()), *arguments],
                check=False, capture_output=True, timeout=timeout,
            )
            receipt = json.loads(result.stdout)
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            raise VisionAdmissionError("shared vision pool call failed") from exc
        if result.returncode or not isinstance(receipt, dict) or receipt.get("status") != status:
            raise VisionAdmissionError("shared vision pool refused or timed out")
        return receipt

    def perceive(self, frame_ref, frame, *, prev):
        if self._unsettled:
            raise VisionAdmissionError("vision lease settlement unresolved")
        receipt = self._call("acquire", timeout=65, status="granted")
        lease = receipt.get("lease_id")
        self._unsettled = True
        if (not isinstance(lease, str) or not lease or receipt.get("pid") != os.getpid()
                or receipt.get("front") != "MAIN"):
            raise VisionAdmissionError("shared vision pool grant does not match this job")
        provider = None
        try:
            provider = self._factory()
            vision = provider.perceive(frame_ref, frame, prev=prev)
            self.last_c2_payload = provider.last_c2_payload
            self.last_transport = provider.last_transport
            self.last_vserve_error = provider.last_vserve_error
            return vision
        finally:
            if provider is not None:
                child = provider._vserve_proc
                provider.close()
                if child is not None and child.poll() is None:
                    raise VisionAdmissionError("vision engine teardown unresolved")
            self._call("release", "--lease-id", lease, timeout=5, status="released")
            self._unsettled = False

    def close(self):
        if self._unsettled:
            raise VisionAdmissionError("vision lease settlement unresolved")

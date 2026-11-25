# SPDX-License-Identifier: AGPL-3.0-only
"""Host-side perception policy for capsule-bound adapters.

The host supplies the admitted target, its ceiling and trusted adapters. A probe
returns None only when its evidence cannot answer the question. Adapter failure
propagates. The supervisor owns the pool ticket and keeps it while capture and
observation are in flight. The router never releases it.
"""

from dataclasses import dataclass
from hashlib import sha256
from hmac import compare_digest
import os
from pathlib import Path
import secrets
import time
from typing import Callable


LEVELS = ("off", "api", "accessibility", "vision")


class PerceptionRefused(RuntimeError):
    """A policy or identity refusal suitable for a closed host reply."""


@dataclass(frozen=True)
class Target:
    capsule_id: str
    incarnation: str
    view_id: str


@dataclass(frozen=True)
class Scope:
    target: Target
    front: str
    ceiling: str


@dataclass(frozen=True)
class Answer:
    target: Target
    value: object


@dataclass(frozen=True)
class CaptureRequest:
    target: Target
    nonce: str
    issued_ns: int


@dataclass(frozen=True)
class FrameIdentity:
    target: Target
    nonce: str
    capture_ns: int
    occurrence: int


@dataclass(frozen=True)
class Frame:
    identity: FrameIdentity
    raster: bytes


@dataclass(frozen=True)
class Observation:
    identity: FrameIdentity
    raster_sha256: str


Probe = Callable[[Target], Answer | None]


@dataclass(frozen=True)
class Probes:
    structured: Probe | None = None
    cdp: Probe | None = None
    at_spi: Probe | None = None


@dataclass(frozen=True)
class Vision:
    pool: object
    # Raw ticket stays with the supervisor. It is never placed in a route result.
    lease_id: str
    capture: Callable[[CaptureRequest], Frame]
    observe: Callable[[CaptureRequest, bytes], Observation]


def _verify_lease(front, vision):
    if vision is None or not vision.lease_id:
        raise PerceptionRefused("VISION_LEASE_REQUIRED")
    pid = os.getpid()
    proc_stat = Path("/proc/self/stat").read_text(encoding="utf-8")
    starttime = int(proc_stat.rsplit(")", 1)[1].split()[19])
    # Pool.status() redacts the token by design. Verify the complete grant
    # under the helper's lock, which is free during foreground adapter calls.
    with vision.pool.transaction() as state:
        lease = state["lease"]
        if (
            lease is None
            or lease["front"] != front
            or lease["pid"] != pid
            or lease["starttime"] != starttime
            or not compare_digest(lease["lease_id"], vision.lease_id)
            or not time.monotonic() < lease["expires_at_monotonic"]
        ):
            raise PerceptionRefused("VISION_LEASE_REQUIRED")


def _verify_frame(identity, request):
    if identity.target != request.target:
        raise PerceptionRefused("WRONG_CAPSULE_OR_VIEW")
    if (
        identity.nonce != request.nonce
        or type(identity.capture_ns) is not int
        or not request.issued_ns <= identity.capture_ns <= time.monotonic_ns()
        or type(identity.occurrence) is not int
        or identity.occurrence < 0
    ):
        raise PerceptionRefused("STALE_FRAME")


def _vision_result(scope, vision):
    _verify_lease(scope.front, vision)
    request = CaptureRequest(scope.target, secrets.token_hex(16), time.monotonic_ns())
    frame = vision.capture(request)
    _verify_lease(scope.front, vision)
    _verify_frame(frame.identity, request)
    if type(frame.raster) is not bytes or not frame.raster:
        raise PerceptionRefused("INVALID_RASTER")
    digest = sha256(frame.raster).hexdigest()
    observed = vision.observe(request, frame.raster)
    _verify_lease(scope.front, vision)
    _verify_frame(observed.identity, request)
    if observed.identity != frame.identity or observed.raster_sha256 != digest:
        raise PerceptionRefused("FRAME_OBSERVER_MISMATCH")
    return {
        "level": "vision",
        "source": "independent_observer",
        "frame": {
            "capsule_id": scope.target.capsule_id,
            "incarnation": scope.target.incarnation,
            "view_id": scope.target.view_id,
            "capture_nonce": request.nonce,
            "capture_monotonic_ns": frame.identity.capture_ns,
            "occurrence": frame.identity.occurrence,
            "raster_sha256": digest,
        },
        "lease_id_sha256": sha256(vision.lease_id.encode("ascii")).hexdigest(),
    }


def route(scope, requested_level, *, nonvisual, probes, vision=None):
    """Select the first sufficient source under the task and capsule ceilings.

    Use off for a command-only task. A nonvisual query can use structured state
    only. CDP precedes AT-SPI when both are available for the admitted target.
    """
    limit = min(LEVELS.index(requested_level), LEVELS.index(scope.ceiling))
    if limit == LEVELS.index("off"):
        if requested_level == "off":
            return {"level": "off", "source": "command"}
        raise PerceptionRefused("PERCEPTION_CEILING")
    if nonvisual:
        limit = min(limit, LEVELS.index("api"))

    for level, source, probe in (
        ("api", "structured_state", probes.structured),
        ("accessibility", "cdp", probes.cdp),
        ("accessibility", "at_spi", probes.at_spi),
    ):
        if LEVELS.index(level) > limit:
            break
        if probe is None:
            continue
        answer = probe(scope.target)
        if answer is None:
            continue
        if answer.target != scope.target:
            raise PerceptionRefused("WRONG_CAPSULE_OR_VIEW")
        return {"level": level, "source": source, "value": answer.value}

    if limit == LEVELS.index("vision"):
        return _vision_result(scope, vision)
    raise PerceptionRefused("PERCEPTION_CEILING")

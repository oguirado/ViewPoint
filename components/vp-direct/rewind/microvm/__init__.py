"""Offline replay of admitted MicroVM restore evidence."""

import json
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Sequence

from jsonschema import Draft202012Validator


_CONTRACTS = Path(__file__).resolve().parents[4] / "contracts" / "v0.2"
_PHASES = ("request", "native_load", "reseed", "app_ready")
_MAX_COUNTER = (1 << 53) - 1


@lru_cache(maxsize=None)
def _contract(name: str) -> Draft202012Validator:
    with (_CONTRACTS / (name + ".schema.json")).open(encoding="utf-8") as stream:
        return Draft202012Validator(json.load(stream))


@dataclass(frozen=True)
class Binding:
    owner_id: str
    workload_id: str
    reservation_id: str
    generation: int
    authority_epoch: int
    session_id: str

    def __post_init__(self):
        for identity in (self.owner_id, self.workload_id, self.reservation_id, self.session_id):
            if type(identity) is not str or not identity:
                raise ValueError("missing binding identity")
        for counter in (self.generation, self.authority_epoch):
            if type(counter) is not int or not 1 <= counter <= _MAX_COUNTER:
                raise ValueError("invalid binding counter")


@dataclass(frozen=True)
class Phase:
    name: str
    request_id: str
    evidence: object
    mono_ns: int | None = None


@dataclass(frozen=True)
class ReplayResult:
    binding: Binding | None
    duration_ns: int | None
    refusal: str | None
    external_effect_frontier: str = field(default="UNREVERTED_HOST_NET_JOURNAL", init=False)

    def command_allowed(self, reference: Binding) -> bool:
        """Report the replayed gate for an exact current binding."""
        return self.binding is not None and self.binding == reference


def _renews(previous: Binding, following: Binding) -> bool:
    return (
        following.owner_id == previous.owner_id
        and following.workload_id == previous.workload_id
        and following.generation > previous.generation
        and following.authority_epoch > previous.authority_epoch
        and following.session_id != previous.session_id
        and following.reservation_id != previous.reservation_id
    )


def _reseed_matches(reply, following: Binding) -> bool:
    return (
        _contract("rewind_guest_reset").is_valid(reply)
        and reply["op"] == "reseeded"
        and type(reply["counter"]) is int
        and reply["session_id"] == following.session_id
        and type(reply["command_generation"]) is int
        and reply["command_generation"] == following.generation
        and type(reply["authority_epoch"]) is int
        and reply["authority_epoch"] == following.authority_epoch
    )


def _ready_matches(status, following: Binding, counter: int, point_id: str) -> bool:
    if not isinstance(status, dict):
        return False
    binding = status.get("binding")
    return (
        status.get("phase") == "ready"
        and status.get("ownership") == "held"
        and isinstance(binding, dict)
        and type(binding.get("generation")) is int
        and type(binding.get("authority_epoch")) is int
        and binding == asdict(following)
        and type(status.get("counter")) is int
        and status["counter"] == counter
        and isinstance(status.get("points"), dict)
        and point_id in status["points"]
    )


def _duration(phases: Sequence[Phase]) -> int | None:
    stamps = [phase.mono_ns for phase in phases]
    if any(type(stamp) is not int or stamp < 0 for stamp in stamps):
        return None
    if stamps != sorted(stamps):
        return None
    return stamps[-1] - stamps[0]


def replay_restore(previous: Binding, following: Binding, phases: Sequence[Phase]) -> ReplayResult:
    """Fold a single restore trace against the caller's admitted authority.

    Evidence is supplied by the local fixture. The load carries the selected
    point ID, the reseed carries the reset reply, and app_ready carries the
    controller's returned status. Timing uses a single monotonic timebase.
    """
    if tuple(phase.name for phase in phases) != _PHASES:
        return ReplayResult(None, None, "incomplete or out-of-order restore phases")
    request_phase, load_phase, reseed_phase, ready_phase = phases
    request = request_phase.evidence
    if not _contract("rewind_request").is_valid(request) or request["op"] != "restore":
        return ReplayResult(None, None, "invalid restore request")
    if any(phase.request_id != request["request_id"] for phase in phases):
        return ReplayResult(None, None, "restore request identity mismatch")
    params = request["params"]
    if (params["workload_id"] != previous.workload_id
            or type(params["generation"]) is not int
            or params["generation"] != previous.generation
            or type(params["authority_epoch"]) is not int
            or params["authority_epoch"] != previous.authority_epoch
            or not _renews(previous, following)):
        return ReplayResult(None, None, "restore authority mismatch")
    if load_phase.evidence != params["point_id"]:
        return ReplayResult(None, None, "native load point mismatch")
    reseed = reseed_phase.evidence
    if not _reseed_matches(reseed, following):
        return ReplayResult(None, None, "unproven reseed or command fence")
    if not _ready_matches(ready_phase.evidence, following, reseed["counter"], params["point_id"]):
        return ReplayResult(None, None, "controller readiness mismatch")
    return ReplayResult(following, _duration(phases), None)

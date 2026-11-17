"""Vision evidence and the channel's declared pre-dispatch fallback."""

from collections.abc import Mapping
from dataclasses import dataclass
import re

from metaviewer_agents.perception.base import PerceptionError, frame_dims
from metaviewer_agents.perception.vcore import _parse_c2_payload
from schemas.validation import validate_contract


_CHANNEL_REQUEST = "vp.channel.req/v1"
_CHANNEL_REPLY = "vp.channel.reply/v1"
_PRE_SEND_RISKS = frozenset({"observar", "leer", "preparar"})
_SEMANTIC_BACKENDS = frozenset({"cdp", "atspi"})


@dataclass(frozen=True)
class VerifiedDescription:
    """Frame-bound change evidence. Regions contain (x, y, width, height)."""

    frame_sha256: str
    width: int
    height: int
    changed: bool | None
    regions: tuple[tuple[int, int, int, int], ...]


def verify_description(payload: bytes, *, frame_ref: Mapping,
                       previous_available: bool) -> VerifiedDescription:
    """Verify a ve.cu_describe/v1 result against an independently bound RGB24 frame."""
    width, height = frame_dims(frame_ref)
    expected_digest = frame_ref.get("sha256")
    if not isinstance(expected_digest, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_digest):
        raise PerceptionError("bad_shape", "Frame reference lacks an RGB24 digest")
    if type(previous_available) is not bool or type(payload) is not bytes:
        raise PerceptionError("bad_shape", "Description bytes and comparison state are required")

    parsed = _parse_c2_payload(payload, width=width, height=height,
                               prev_given=previous_available, marks_out=None, ocr_requested=False)
    if parsed["frame_sha256"] != expected_digest:
        raise PerceptionError("identity_mismatch", "Description belongs to a different frame")
    changed = parsed["changed"]
    if "changed" not in parsed["validated_payload"] or bool(changed) != bool(parsed["regions"]):
        raise PerceptionError("bad_shape", "Description changes disagree with its regions")

    # The engine's region score is a share of changed tiles.
    regions = tuple((row["x"], row["y"], row["w"], row["h"]) for row in parsed["regions"])
    return VerifiedDescription(expected_digest, width, height, changed, regions)


@dataclass(frozen=True)
class BoundDescription:
    """Current frame and producer result supplied by the channel authority."""

    session_id: str
    incarnation: str
    authority_epoch: int
    observation_id: str
    frame_ref: Mapping
    payload: bytes
    previous_available: bool


def _reply(code, *, ok=False, fallback_from=None):
    reply = {"schema": _CHANNEL_REPLY, "ok": ok, "code": code}
    if fallback_from is not None:
        reply["fallback_from"] = fallback_from
    return reply


class VisionFallback:
    """Try a configured (semantic backend, refusal code) route at most once.

    The trusted channel core supplies the locked target, the request as routed
    to the primary backend, its failure, and the current session and observation.
    The injected executor must revalidate live authority and geometry at Core
    admission and return its vp.channel.reply/v1 receipt.
    """

    def __init__(self, *, routes, dispatch):
        self._routes = frozenset(routes)
        self._dispatch = dispatch

    def run(self, request, *, session, observation: BoundDescription, point,
            primary_code: str, primary_sent: bool | None):
        validate_contract(_CHANNEL_REQUEST, request)
        validate_contract("vp.session/v1", session)
        if not isinstance(primary_code, str) or not primary_code or primary_code == "OK":
            raise ValueError("Expected a primary backend refusal code")

        if primary_sent is not False or primary_code in {"INPUT_UNCERTAIN", "UNCERTAIN_OUTCOME"}:
            return _reply("UNCERTAIN_OUTCOME")
        if primary_code in {"identity_mismatch", "NO_LEASE"}:
            return _reply(primary_code)

        backend = request["backend"]
        if (request["op"] != "act" or backend not in _SEMANTIC_BACKENDS
                or (backend, primary_code) not in self._routes):
            return _reply(primary_code)
        if request["risk_class"] not in _PRE_SEND_RISKS:
            return _reply("FALLBACK_FORBIDDEN_IRREVERSIBLE")
        lease = session["lease"]
        if (session["state"] != "AGENT" or lease["holder"] is None
                or request["authority_epoch"] != lease["authority_epoch"]):
            return _reply("NO_LEASE")
        for field in ("session_id", "capsule_id", "incarnation"):
            if request[field] != session[field]:
                return _reply("identity_mismatch")
        target_id = request["target"].get("cdp_target_id")
        if target_id is not None and target_id != session.get("cdp_target_id"):
            return _reply("identity_mismatch")
        display = session["display"]
        if not re.fullmatch(r"xpra:[0-9]+", display):
            return _reply("DISPLAY_UNSUPPORTED")

        for field in ("session_id", "incarnation", "authority_epoch", "observation_id"):
            if getattr(observation, field) != request[field]:
                return _reply("STALE_FRAME")
        try:
            verified = verify_description(observation.payload, frame_ref=observation.frame_ref,
                                          previous_available=observation.previous_available)
        except PerceptionError as error:
            return _reply(error.code)
        if (type(point) is not tuple or len(point) != 2 or any(type(v) is not int for v in point)
                or not (0 <= point[0] < verified.width and 0 <= point[1] < verified.height)):
            return _reply("bad_shape")

        if request["dry_run"]:
            return _reply("OK", ok=True, fallback_from=backend)
        try:
            reply = self._dispatch(display, request, point, verified)
            validate_contract(_CHANNEL_REPLY, reply)
        except Exception:
            # After a possible send, a missing or malformed receipt proves no outcome.
            return _reply("UNCERTAIN_OUTCOME", fallback_from=backend)
        return dict(reply, fallback_from=backend)

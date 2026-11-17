"""Associate an existing foreign window with qualified CDP and AT-SPI evidence."""

from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import re
from threading import Lock
from typing import Protocol, Sequence

from jsonschema import Draft202012Validator, RefResolver, ValidationError


class AttachRefused(ValueError):
    def __init__(self, code: str, reason: str):
        self.code = code
        super().__init__(reason)


class _LocalResolver(RefResolver):
    def resolve_remote(self, uri):
        raise ValueError(f"Unregistered frozen schema: {uri}")


def _validators():
    root = Path(__file__).resolve().parents[1] / "schemas"
    workspace = json.loads((root / "vp_workspace_call.args_v1.schema.json").read_text())
    binding = json.loads((root / "vp.surface_binding_v1.schema.json").read_text())
    definitions = workspace["$defs"]
    return {
        key: Draft202012Validator(
            definitions[key],
            resolver=_LocalResolver.from_schema(
                definitions[key], store={binding["$id"]: binding},
            ),
        )
        for key in ("attach.args", "attach.result", "detach.args", "detach.result")
    }


@dataclass(frozen=True)
class EvidenceRef:
    path: str
    line_start: int
    line_end: int
    sha256: str

    def overlaps(self, other: "EvidenceRef") -> bool:
        return self.path == other.path and max(self.line_start, other.line_start) <= min(
            self.line_end, other.line_end,
        )


@dataclass(frozen=True)
class WindowIdentity:
    display: str
    xid: int
    pid: int
    witness: str


@dataclass(frozen=True)
class CdpWitness:
    evidence: EvidenceRef
    window: WindowIdentity
    endpoint: str
    target_id: str


@dataclass(frozen=True)
class AtspiWitness:
    evidence: EvidenceRef
    window: WindowIdentity
    bus_name: str
    object_path: str


class WitnessSource(Protocol):
    """Trusted, read-only observations for one host display.

    Each channel must independently correlate its live object to the full
    native window identity. It verifies the evidence coordinates and digest
    from its own records. Request paths are references and never opened here.
    """

    def current_window(self, xid: int) -> WindowIdentity | None: ...

    def cdp_targets(self, window: WindowIdentity) -> Sequence[CdpWitness]: ...

    def atspi_objects(self, window: WindowIdentity) -> Sequence[AtspiWitness]: ...


@dataclass(frozen=True)
class Association:
    window: WindowIdentity
    cdp: CdpWitness
    atspi: AtspiWitness

    def aliases(self):
        return (
            (self.window.display, self.window.xid),
            (self.cdp.endpoint, self.cdp.target_id),
            (self.atspi.bus_name, self.atspi.object_path),
        )


def _xid(raw: str) -> int:
    if re.fullmatch(r"0x[0-9a-fA-F]{1,8}", raw):
        value = int(raw, 16)
    elif re.fullmatch(r"[1-9][0-9]{0,9}", raw):
        value = int(raw, 10)
    else:
        value = 0
    if not 0 < value <= 0xffffffff:
        raise AttachRefused("-32602", "Invalid native window XID")
    return value


def _select_witness(candidates, references, window):
    cited = [candidate for candidate in candidates if candidate.evidence in references]
    if len(cited) != 1 or cited[0].window != window:
        raise AttachRefused("identity_mismatch", "No unique cited witness for this incarnation")
    return cited[0]


class AttachmentService:
    """Hold local associations after upstream admission and session provisioning.

    Callers provision a fresh session ID and its broker-issued surface binding.
    This table records correlation only. The caller revalidates authority and
    freshness at each effect boundary. Detach retires only the local record.
    """

    def __init__(self, source: WitnessSource, *, display: str):
        if not display:
            raise ValueError("A provisioned display is required")
        self._source = source
        self._display = display
        self._schemas = _validators()
        self._lock = Lock()
        self._active: dict[str, tuple[str, Association]] = {}

    def _validate(self, key, record):
        try:
            with self._lock:
                self._schemas[key].validate(record)
        except ValidationError as error:
            raise AttachRefused("-32602", f"Invalid frozen workspace {key}") from error

    def current_window(self, xid: int) -> WindowIdentity | None:
        return self._source.current_window(xid)

    def _associate(self, args) -> Association:
        xid = _xid(args["xid"])
        references = {EvidenceRef(**record) for record in args["evidence"]}
        if any(ref.line_end < ref.line_start for ref in references):
            raise AttachRefused("-32602", "Reversed evidence coordinates")

        window = self.current_window(xid)
        if window is None:
            raise AttachRefused("host_window_unavailable", "Window is absent")
        if (window.display != self._display or window.xid != xid or window.pid <= 0
                or window.witness != args["witness"]):
            raise AttachRefused("identity_mismatch", "Native window witness changed")

        cdp = _select_witness(self._source.cdp_targets(window), references, window)
        atspi = _select_witness(self._source.atspi_objects(window), references, window)
        if (not cdp.endpoint or not cdp.target_id
                or not re.fullmatch(r":[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+", atspi.bus_name)
                or not re.fullmatch(r"/(?:[A-Za-z0-9_]+(?:/[A-Za-z0-9_]+)*)?", atspi.object_path)):
            raise AttachRefused("identity_mismatch", "Incomplete channel identity")
        if (cdp.evidence.sha256 == atspi.evidence.sha256
                or cdp.evidence.overlaps(atspi.evidence)):
            raise AttachRefused("identity_mismatch", "Channel witnesses share evidence")
        return Association(window, cdp, atspi)

    def attach(self, args: dict, *, session_id: str, binding: dict) -> dict:
        args = deepcopy(args)
        result = {"session_id": session_id, "binding": deepcopy(binding)}
        self._validate("attach.args", args)
        self._validate("attach.result", result)
        association = self._associate(args)
        surface_id = result["binding"]["surface_id"]
        aliases = association.aliases()
        with self._lock:
            if self.current_window(association.window.xid) != association.window:
                raise AttachRefused("identity_mismatch", "Window changed during association")
            if session_id in self._active or any(
                old_surface == surface_id or any(
                    old == new for old, new in zip(active.aliases(), aliases)
                )
                for old_surface, active in self._active.values()
            ):
                raise AttachRefused("identity_mismatch", "Association is already bound")
            self._active[session_id] = surface_id, association
        return result

    def association(self, session_id: str) -> Association | None:
        with self._lock:
            row = self._active.get(session_id)
            return row[1] if row is not None else None

    def detach(self, args: dict) -> dict:
        args = deepcopy(args)
        self._validate("detach.args", args)
        result = {"detached": True, "closed_foreign": False}
        self._validate("detach.result", result)
        with self._lock:
            if self._active.pop(args["session_id"], None) is None:
                raise AttachRefused("host_window_unavailable", "No such local association")
        return result

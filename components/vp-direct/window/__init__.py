"""S3 presentation verbs for registered windows."""

import json
from dataclasses import dataclass
from pathlib import Path
import re
from threading import Lock
from time import monotonic, sleep
from typing import Protocol

from jsonschema import Draft202012Validator


_VP_DIRECT = Path(__file__).resolve().parents[1]
_UNAVAILABLE = "host_window_unavailable"
_UNCERTAIN = "UNCERTAIN_OUTCOME"


class WindowRefused(RuntimeError):
    def __init__(self, code, reason):
        super().__init__(reason)
        self.code = code


def _schema(filename):
    return Draft202012Validator(
        json.loads((_VP_DIRECT / "schemas" / filename).read_text(encoding="utf-8"))
    )


def _xid(value):
    if type(value) is str and re.fullmatch(r"(?:0[xX][0-9a-fA-F]+|[0-9]+)", value):
        digits = value[2:] if value[:2].lower() == "0x" else value
        if len(digits) <= 10:
            value = int(digits, 16 if value[:2].lower() == "0x" else 10)
    if type(value) is not int or not 0 < value <= 0xffffffff:
        raise WindowRefused(_UNAVAILABLE, "invalid XID")
    return value


@dataclass(frozen=True)
class WindowBinding:
    xid: int
    pid: int
    witness: str
    incarnation: str
    display: str


@dataclass(frozen=True)
class DesktopState:
    workspace: int
    workspace_count: int
    active_xid: int
    cursor: tuple[int, int]


class PresentationBackend(Protocol):
    def desktop(self) -> DesktopState: ...
    def verify_window(self, binding: WindowBinding) -> None: ...
    def workspace(self, binding: WindowBinding) -> int: ...
    def move(self, binding: WindowBinding, workspace: int) -> None: ...


class WindowController:
    """Read a session by ID for each action. The registry remains its sole writer."""

    def __init__(self, get_session, backends, *, receipt=None):
        if receipt is None:
            receipt = json.loads(
                (_VP_DIRECT / "config" / "wl0.json").read_text(encoding="utf-8")
            )
        if not isinstance(receipt, dict) or receipt.get("schema") != "vp.l0_receipt/v1":
            raise WindowRefused(_UNAVAILABLE, "W-L0 receipt is unavailable")
        steps = receipt.get("steps")
        step = steps.get("L0-3") if isinstance(steps, dict) else None
        if not isinstance(step, dict):
            raise WindowRefused(_UNAVAILABLE, "L0-3 is unavailable")
        name = step.get("backend")
        if step.get("status") != "PASS" or name not in ("wmctrl", "xpra"):
            raise WindowRefused(_UNAVAILABLE, "L0-3 has no qualified workspace backend")
        self._backend = backends.get(name)
        if self._backend is None:
            raise WindowRefused(_UNAVAILABLE, "the qualified workspace backend is unavailable")
        self._backend_name = name
        self._agent_workspace = step.get("agent_workspace")
        if type(self._agent_workspace) is not int or self._agent_workspace < 0:
            raise WindowRefused(_UNAVAILABLE, "L0-3 agent workspace is unavailable")
        self._get_session = get_session
        self._session_schema = _schema("vp.session_v1.schema.json")
        self._reply_schema = _schema("vp.window.reply_v1.schema.json")
        self._lock = Lock()

    def vp_window_summon(self, session_id):
        return self._present(session_id, dismiss=False)

    def vp_window_dismiss(self, session_id):
        return self._present(session_id, dismiss=True)

    def _registered(self, session_id):
        if not isinstance(session_id, str) or not session_id:
            raise WindowRefused(_UNAVAILABLE, "missing registry session ID")
        try:
            row = self._get_session(session_id)
        except KeyError:
            row = None
        if (not isinstance(row, dict) or row.get("session_id") != session_id
                or not self._session_schema.is_valid(row) or row["state"] == "CLOSED"
                or "window" not in row or "incarnation" not in row):
            raise WindowRefused(_UNAVAILABLE, "no live registered window for this session")
        window = row["window"]
        binding = WindowBinding(
            _xid(window["xid"]), window["pid"], window["witness"],
            row["incarnation"], row["display"],
        )
        return binding, row["lease"]["holder"], row["lease"]["authority_epoch"], row["state"]

    def _verify(self, session_id, registered, desktop):
        self._backend.verify_window(registered[0])
        if self._backend.desktop() != desktop:
            raise WindowRefused(_UNAVAILABLE, "operator focus, cursor or workspace changed")
        if self._registered(session_id) != registered:
            raise WindowRefused(_UNAVAILABLE, "session binding or authority changed")

    def _present(self, session_id, *, dismiss):
        with self._lock:
            registered = self._registered(session_id)
            binding = registered[0]
            desktop = self._backend.desktop()
            source = self._backend.workspace(binding)
            target = self._agent_workspace if dismiss else desktop.workspace
            if not (0 <= source < desktop.workspace_count
                    and 0 <= target < desktop.workspace_count
                    and 0 <= desktop.workspace < desktop.workspace_count):
                raise WindowRefused(_UNAVAILABLE, "workspace is outside the current topology")
            if self._backend.workspace(binding) != source:
                raise WindowRefused(_UNAVAILABLE, "window workspace changed before move")
            self._verify(session_id, registered, desktop)
            if source != target:
                if binding.xid == desktop.active_xid:
                    raise WindowRefused(_UNAVAILABLE, "moving the active window could change focus")
                self._move_and_verify(session_id, registered, desktop, target)
            reply = {
                "moved": source != target,
                "from_ws": source,
                "to_ws": target,
                "backend": self._backend_name,
                "witness_ok": True,
            }
            self._reply_schema.validate(reply)
            return reply

    def _move_and_verify(self, session_id, registered, desktop, target):
        binding = registered[0]
        try:
            self._backend.move(binding, target)
            # The window manager consumes the request asynchronously.
            deadline = monotonic() + 1
            while True:
                if self._backend.desktop() != desktop:
                    raise WindowRefused(_UNAVAILABLE, "operator state changed during move")
                if self._backend.workspace(binding) == target:
                    self._verify(session_id, registered, desktop)
                    return
                if monotonic() >= deadline:
                    raise WindowRefused(_UNAVAILABLE, "workspace readback did not converge")
                sleep(0.02)
        except WindowRefused as error:
            raise WindowRefused(_UNCERTAIN, str(error)) from error

"""Explicit operator presentation for S4. S3 still handles passive moves."""
from dataclasses import dataclass
from time import monotonic, sleep

from bus.broker.qualification import load_wl0
from window import WindowBinding, WindowController, WindowRefused, _xid
from window.x11 import WmctrlBackend, _cardinal, _window_rows


@dataclass(frozen=True)
class FocusWitness:
    xid: int
    pid: int
    witness: str
    workspace: int


class HandoffWindows(WmctrlBackend):
    def __init__(self, bin_dir, get_session, receipt=None, **backend_options):
        super().__init__(bin_dir, **backend_options)
        try:
            receipt = load_wl0(receipt)
        except ValueError as exc:
            raise WindowRefused("host_window_unavailable", str(exc)) from exc
        self._wl0 = receipt
        self._present = WindowController(get_session, {"wmctrl": self}, receipt=receipt)
        self._agent_workspace = receipt["steps"]["L0-3"]["agent_workspace"]

    def qualification_receipt(self):
        return load_wl0(self._wl0)

    def save_focus(self):
        desktop = self.desktop()
        xid = desktop.active_xid
        rows = _window_rows(self._command(self._host_tool, "list"))
        witnesses = [r.get("witness") for r in rows if _xid(r.get("xid")) == xid]
        if len(witnesses) != 1 or not isinstance(witnesses[0], str) or not witnesses[0]:
            raise WindowRefused("identity_mismatch", "operator window is ambiguous")
        pid = _cardinal(self._command("xprop", "-id", hex(xid), "_NET_WM_PID"), "_NET_WM_PID")
        saved = FocusWitness(xid, pid, witnesses[0], desktop.workspace)
        self._verify_saved(saved)
        if self.desktop().active_xid != xid:
            raise WindowRefused("identity_mismatch", "operator focus changed during take")
        return saved

    def _verify_saved(self, saved):
        binding = WindowBinding(saved.xid, saved.pid, saved.witness, "", ":0")
        self.verify_window(binding)
        if (self.desktop().workspace != saved.workspace
                or self.workspace(binding) not in (saved.workspace, 0xffffffff)):
            raise WindowRefused("identity_mismatch", "operator workspace changed")

    def verify_focus(self, saved):
        self._verify_saved(saved)
        if self.desktop().active_xid != saved.xid:
            raise WindowRefused("UNCERTAIN_OUTCOME", "operator focus changed during return")

    def restore_focus(self, saved):
        self._verify_saved(saved)
        if self.desktop().active_xid != saved.xid:
            self._command("wmctrl", "-i", "-a", hex(saved.xid))
        self._await(lambda: self.desktop().active_xid == saved.xid)
        self.verify_focus(saved)

    @staticmethod
    def _await(predicate):
        deadline = monotonic() + 1
        while not predicate():
            if monotonic() >= deadline:
                raise WindowRefused("UNCERTAIN_OUTCOME", "window manager readback timed out")
            sleep(0.02)

    def summon(self, session):
        self._present.vp_window_summon(session["session_id"])

    def dismiss(self, session):
        window = session["window"]
        binding = WindowBinding(_xid(window["xid"]), window["pid"], window["witness"],
                                session["incarnation"], session["display"])
        if self.desktop().active_xid != binding.xid:
            self._present.vp_window_dismiss(session["session_id"])
            return
        # S3 refuses to move an active window. The explicit operator return
        # authorizes this move, followed by restoration of the witnessed XID.
        self.verify_window(binding)
        if self.workspace(binding) != self._agent_workspace:
            self.move(binding, self._agent_workspace)
        self._await(lambda: self.workspace(binding) == self._agent_workspace)
        self.verify_window(binding)

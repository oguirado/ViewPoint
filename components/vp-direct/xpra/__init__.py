"""Rootless Xpra presentation through the S4 human handoff."""

from contextlib import contextmanager
from pathlib import Path
import re
from threading import local
from typing import Protocol

from human import HandoffRefused, HumanHandoff, require
from window import WindowBinding, _xid


class XpraRunner(Protocol):
    """Trusted H1 port. Readback identifies the private source, including its PID.

    Attach must atomically select and verify the source binding before forwarding
    it. Detach removes only that client attachment and leaves the app running.
    Forwarded readback must refuse while a presentation change is unsettled.
    Start must set the child's HOME to the supplied path.
    """

    def start(self, *, mode: str, display: str, app: str, home: Path): ...
    def mode(self, display: str) -> str: ...
    def windows(self, display: str) -> tuple[WindowBinding, ...]: ...
    def forwarded(self, display: str) -> tuple[WindowBinding, ...]: ...
    def attach(self, binding: WindowBinding) -> None: ...
    def detach(self, binding: WindowBinding) -> None: ...


def _private_display(display):
    require(isinstance(display, str) and re.fullmatch(r"xpra:[1-9][0-9]*", display),
            "DISPLAY_UNSUPPORTED")
    return display


def launch_rootless(runner: XpraRunner, *, display, app, private_home, operator_home,
                    mode="start", persistent_home_apps=()):
    """Launch through H1 with a provisioned private HOME or explicit app opt-in."""
    _private_display(display)
    require(mode == "start", "DISPLAY_UNSUPPORTED")
    require(isinstance(app, str) and bool(app), "-32602")
    private = Path(private_home)
    operator = Path(operator_home)
    require(private.is_absolute() and operator.is_absolute(), "HOLD")
    require(private.resolve() != operator.resolve(), "HOLD")
    require(not isinstance(persistent_home_apps, str), "-32602")
    persistent = app in persistent_home_apps
    return runner.start(mode=mode, display=display, app=app, home=operator if persistent else private)


class _SeamlessWindows:
    """S4 presentation callbacks for one registered private app."""

    def __init__(self, runner, focus, authorize):
        self._runner = runner
        self._authorize = authorize
        self.save_focus = focus.save_focus
        self.restore_focus = focus.restore_focus
        self.verify_focus = focus.verify_focus

    def _binding(self, session):
        display = _private_display(session["display"])
        require(self._runner.mode(display) == "start", "DISPLAY_UNSUPPORTED")
        window = session["window"]
        return WindowBinding(_xid(window["xid"]), window["pid"], window["witness"],
                             session["incarnation"], display)

    def _check_source(self, binding, *, exclusive=False):
        windows = self._runner.windows(binding.display)
        require(isinstance(windows, tuple) and windows.count(binding) == 1, "identity_mismatch")
        require(not exclusive or windows == (binding,), "DISPLAY_UNSUPPORTED")

    def _change(self, session, *, attach):
        self._authorize(session, "take" if attach else "return")
        try:
            binding = self._binding(session)
            self._check_source(binding, exclusive=attach)
            current = self._runner.forwarded(binding.display)
            require(current in ((), (binding,)), "UNCERTAIN_OUTCOME")
            expected = (binding,) if attach else ()
            if current != expected:
                if attach:
                    self._runner.attach(binding)
                else:
                    self._runner.detach(binding)
            require(self._runner.forwarded(binding.display) == expected, "UNCERTAIN_OUTCOME")
            self._check_source(binding)
        except OSError as exc:
            raise HandoffRefused("UNCERTAIN_OUTCOME") from exc

    def summon(self, session):
        self._change(session, attach=True)

    def dismiss(self, session):
        self._change(session, attach=False)


class XpraHandoff(HumanHandoff):
    """Use S4's wire handlers, broker lease and runtime with Xpra presentation."""

    def __init__(self, *, runner: XpraRunner, focus, **handoff_options):
        self._presentation = local()
        windows = _SeamlessWindows(runner, focus, self._authorize_presentation)
        super().__init__(windows=windows, **handoff_options)

    @contextmanager
    def _during(self, phase):
        require(getattr(self._presentation, "phase", None) is None, "NO_LEASE")
        self._presentation.phase = phase
        try:
            yield
        finally:
            del self._presentation.phase

    def _take(self, session, reason):
        with self._during("take"):
            return super()._take(session, reason)

    def _return(self, session, binding):
        with self._during("return"):
            return super()._return(session, binding)

    def _authorize_presentation(self, session, phase):
        require(getattr(self._presentation, "phase", None) == phase, "NO_LEASE")
        require(self.get_session(self.session_id) == session, "identity_mismatch")
        lease = self.broker.lease(self.session_id)
        taken, _ = self.broker.history(self.session_id)
        require(taken is not None and taken["lease"]["state"] == "HUMAN", "NO_LEASE")
        take_epoch = taken["lease"]["epoch_after"]
        fenced = lease["state"] == "HUMAN" and lease["authority_epoch"] == take_epoch
        returning = (phase == "return" and lease["state"] == "AGENT" and lease["holder"] is None
                     and lease["authority_epoch"] == take_epoch + 1)
        require(fenced or returning, "NO_LEASE")
        require(self.runtime.attachments() == (), "HOLD")
        if phase == "take":
            retained = self.broker.perception_state(self.surface_id)
            require(retained and retained["state"] == "ASLEEP"
                    and retained["incarnation"] == session["incarnation"]
                    and retained["authority_epoch"] == take_epoch, "HOLD")

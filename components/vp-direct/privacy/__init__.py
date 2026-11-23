"""A2 pre-acquisition guard for an authorized, witnessed source."""

from dataclasses import dataclass
from enum import Enum
from threading import Lock
from time import monotonic_ns


class PrivacyState(Enum):
    CLEAR = "clear"
    LOGIN = "login"
    REAUTHENTICATION = "reauthentication"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PrivacyBinding:
    surface_id: str
    incarnation: str
    authority_epoch: int
    source: str
    witness: str

    def __post_init__(self):
        if any(type(value) is not str or not value.strip() for value in (
            self.surface_id, self.incarnation, self.source, self.witness,
        )) or type(self.authority_epoch) is not int or self.authority_epoch < 0:
            raise ValueError("invalid privacy binding")


@dataclass(frozen=True)
class PrivacyMetadata:
    binding: PrivacyBinding
    revision: int
    state: PrivacyState


@dataclass(frozen=True)
class HostPrivacyEvidence:
    """Content-free evidence from the owner of one capture footprint.

    The owner correlates the login registration, host window and raw AT-SPI
    roles under one revision. An incomplete role inventory is represented by
    None. covers_capture includes portions of the footprint outside AT-SPI.
    """

    binding: PrivacyBinding
    revision: int
    login_active: bool | None
    reauthenticating: bool | None
    window_active: bool | None
    input_drained: bool | None
    covers_capture: bool | None
    atspi_roles: tuple[str, ...] | None


def classify_host_privacy(binding, evidence):
    """Project native observations to the A2 sentinel's minimal metadata."""
    if (type(evidence) is not HostPrivacyEvidence or evidence.binding != binding
            or type(evidence.revision) is not int or evidence.revision < 0):
        return None

    # login_lock.go treats active or pending registration as a veto.
    if evidence.login_active is True:
        state = PrivacyState.LOGIN
    elif evidence.reauthenticating is True:
        state = PrivacyState.REAUTHENTICATION
    else:
        roles = evidence.atspi_roles
        # host_window.go requires an active window and drained input. Check
        # raw AT-SPI roles before atspi_tree.py normalizes or prunes them.
        clear = (evidence.login_active is False and evidence.reauthenticating is False
                 and evidence.window_active is True and evidence.input_drained is True
                 and evidence.covers_capture is True and type(roles) is tuple and bool(roles)
                 and all(type(role) is str and role.strip() for role in roles)
                 and all(role.strip().lower() != "password text" for role in roles))
        state = PrivacyState.CLEAR if clear else PrivacyState.UNKNOWN
    return PrivacyMetadata(binding, evidence.revision, state)


def host_privacy_for(binding, read_evidence, *, record_gap, request_human):
    """Supply S5's privacy_for selector from one owner-provisioned host source.

    read_evidence(binding) must revalidate the capture identity and return a
    revision that advances on every footprint or privacy change, including
    a change that reverses between reads. Use native roles from the bound
    AT-SPI window, with no names, text or values. Handoff must enqueue work
    without waiting for the active observation or lifecycle drain to finish.
    """
    sentinel = PrivacySentinel(
        binding, lambda bound: classify_host_privacy(bound, read_evidence(bound)),
        record_gap=record_gap, request_human=request_human,
    )

    def select(state):
        if state.privacy_binding() == binding:
            return sentinel
        return None

    return select


class PrivacyBlocked(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class PrivacySentinel:
    """Serialize observation attempts at the bound source's privacy boundary.

    The trusted metadata reader returns only PrivacyMetadata for the requested
    binding. CLEAR requires current evidence covering the full acquisition
    footprint. The reader must advance its revision on document or privacy
    changes, including a change that reverses before the next read. If it
    cannot establish that coverage, it must return UNKNOWN or no evidence.

    record_gap(active, mono_ns) writes the recorder's privacy gap at the
    bound position. request_human(reason) calls the broker with provisioned
    session, goal and owner identifiers. Neither receives observation content.
    Ports are synchronous and must not call back into the sentinel.
    """

    def __init__(self, binding, read_metadata, *, record_gap, request_human, clock=monotonic_ns):
        if type(binding) is not PrivacyBinding:
            raise ValueError("privacy source must be bound")
        self._binding = binding
        self._read_metadata = read_metadata
        self._record_gap = record_gap
        self._request_human = request_human
        self._clock = clock
        self._lock = Lock()
        self._last = None
        self._gap_open = False
        self._pending_gap = None
        self._handoff_requested = False

    def observe(self, acquire, publish):
        """Acquire privately, then hand off only after metadata revalidation.

        acquire(metadata) must use the bound source and keep its result within
        trusted staging. It must not forward it to OCR, models, recording or
        other consumers. publish(observation) is the authorized handoff.
        """
        with self._lock:
            before = self._read()
            if self._pending_gap is not None:
                self._gap(*self._pending_gap)
                before = self._read()
            if before is None or before.state is not PrivacyState.CLEAR:
                self._block(before, self._clock(), "HOLD")

            if self._gap_open:
                self._gap(False, self._clock())
                # The gap sink may have run while the source changed.
                before = self._read()
                if before is None or before.state is not PrivacyState.CLEAR:
                    self._block(before, self._clock(), "HOLD")

            started = self._clock()
            observation = None
            acquired = False
            try:
                observation = acquire(before)
                acquired = True
            except Exception:
                # Backend errors may contain observation content.
                pass

            after = self._read()
            if after != before:
                del observation
                self._block(after, started, "STALE_FRAME")
            if not acquired:
                raise PrivacyBlocked("HOLD")
            return publish(observation)

    def _read(self):
        try:
            metadata = self._read_metadata(self._binding)
        except Exception:
            return None
        if (type(metadata) is not PrivacyMetadata or type(metadata.binding) is not PrivacyBinding
                or metadata.binding != self._binding or type(metadata.state) is not PrivacyState
                or type(metadata.revision) is not int or metadata.revision < 0):
            return None
        if self._last is not None and (
            metadata.revision < self._last.revision
            or (metadata.revision == self._last.revision and metadata.state is not self._last.state)
        ):
            return None
        self._last = metadata
        return metadata

    def _block(self, metadata, when, code):
        self._gap(True, when)
        if metadata is not None and metadata.state in (PrivacyState.LOGIN, PrivacyState.REAUTHENTICATION):
            if not self._handoff_requested:
                try:
                    self._request_human("login")
                except Exception:
                    pass
                else:
                    self._handoff_requested = True
                if not self._handoff_requested:
                    raise PrivacyBlocked("HOLD")
            raise PrivacyBlocked("HUMAN_NEEDED")
        raise PrivacyBlocked(code)

    def _gap(self, active, when):
        if active != self._gap_open:
            self._pending_gap = (active, when)
            try:
                self._record_gap(active, when)
            except Exception:
                pass
            else:
                self._gap_open = active
                self._pending_gap = None
            if active != self._gap_open:
                raise PrivacyBlocked("HOLD")
        if not active:
            self._handoff_requested = False

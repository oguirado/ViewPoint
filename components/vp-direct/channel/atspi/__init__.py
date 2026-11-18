"""AT-SPI channel backend using live registry witnesses and polling evidence."""

from collections import Counter
from dataclasses import dataclass
import re
from threading import RLock
from time import monotonic
from uuid import uuid4

from channel.core.qualification import load_host_qualifications, qualified_atspi_click
from focus import FocusRefused
from jsonschema import ValidationError
from schemas.validation import validate_contract
from wender.perceive import atspi, atspi_tree, ax

from .transport import Refused, SessionBus


_ACCESSIBLE = "org.a11y.atspi.Accessible"
_REGISTRY = "org.a11y.atspi.Registry"
_PROPERTIES = "org.freedesktop.DBus.Properties.Get"
_ACTION = "org.a11y.atspi.Action"
_COMPONENT = "org.a11y.atspi.Component"
_EDITABLE_TEXT = "org.a11y.atspi.EditableText"
_SELECTION = "org.a11y.atspi.Selection"
_ROOT = atspi.ROOT_PATH

# Atspi.StateType indices in Accessible.GetState, verified against the
# installed Atspi-2.0 typelib (gir1.2-atspi-2.0 2.52.0-1build1).
_STATE_TYPE = {
    "CHECKED": 4,
    "DEFUNCT": 6,
    "ENABLED": 8,
    "SELECTED": 23,
    "SENSITIVE": 24,
}
_DEFUNCT = 1 << _STATE_TYPE["DEFUNCT"]
_ENABLED = 1 << _STATE_TYPE["ENABLED"]
_SENSITIVE = 1 << _STATE_TYPE["SENSITIVE"]


@dataclass(frozen=True)
class ObjectRef:
    bus: str
    path: str

    def __post_init__(self):
        if (not re.fullmatch(r":[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+", self.bus)
                or not re.fullmatch(r"(?:/[A-Za-z0-9_]+)+", self.path)
                or self.path == "/org/a11y/atspi/null"):
            raise ValueError("Expected a unique bus owner and an accessible object path")


@dataclass(frozen=True)
class WindowIdentity:
    """Trusted live witness supplied by the registry and window association layer."""

    display: str
    pid: int
    xid: str
    witness: str
    incarnation: str
    authority_epoch: int
    window: ObjectRef
    input_allowed: bool


@dataclass(frozen=True)
class HostIdentity(WindowIdentity):
    """Host association with the exact hidden tab qualified independently of its title."""

    hidden: bool
    qualified_tab: ObjectRef | None = None


@dataclass(frozen=True)
class PrivateIdentity(WindowIdentity):
    """The xpra controller supplies the actions qualified for this fixture/app."""

    qualified_actions: frozenset[str]


@dataclass(frozen=True)
class _Node:
    ref: ObjectRef
    parent_path: tuple[ObjectRef, ...]
    native_role: str
    name: str
    state_bits: int
    row: dict

    @property
    def parent(self):
        # The application root is outside the bound window.
        return self.parent_path[-1] if len(self.parent_path) > 1 else None


@dataclass(frozen=True)
class _Tree:
    observation_id: str
    identity: WindowIdentity
    nodes: tuple[_Node, ...]
    raw_count: int
    ax_sha256: str
    ax_stable_sha256: str

    def snapshot(self):
        return {"observation_id": self.observation_id, "atspi_capture": "polling_only",
                "raw_count": self.raw_count, "ax_sha256": self.ax_sha256,
                "ax_stable_sha256": self.ax_stable_sha256,
                "nodes": [dict(node.row) for node in self.nodes]}

    def select(self, node_id):
        matches = [node for node in self.nodes if node.row["id"] == node_id]
        if not matches:
            raise Refused("ATSPI_RETIRED_OBJECT")
        if len(matches) != 1 or matches[0].row["cardinality"] != 1:
            raise Refused("ATSPI_AMBIGUOUS_NODE")
        return matches[0]


class AtspiExecutor:
    """Run under the channel core's authority and canonical target lock.

    The trusted identity lookup must return current, independently associated
    window evidence. Only one complete observation per session is actionable.
    When supplied, focus_monitor_for binds that identity to (surface_id, A3
    monitor) on the same display. Host actions always require this binding.
    """

    def __init__(self, identity_lookup, *, session_env, receipt_path=None,
                 transport=None, focus_monitor_for=None, max_nodes=2000, max_depth=40, timeout=5):
        self._identity_lookup = identity_lookup
        self._transport = SessionBus(session_env) if transport is None else transport
        self._host_qualified = qualified_atspi_click(
            load_host_qualifications(receipt_path), "google-chrome"
        ) is not None
        self._focus_monitor_for = focus_monitor_for
        self._max_nodes = max_nodes
        self._max_depth = max_depth
        self._timeout = timeout
        self._trees = {}
        self._lock = RLock()

    def _call(self, ref, method, reply, deadline, sig="", args=()):
        return self._transport.call(ref.bus, ref.path, method, sig, args, reply, deadline)

    def _children(self, ref, deadline):
        result = self._call(ref, _ACCESSIBLE + ".GetChildren", "(a(so))", deadline)
        try:
            return [ObjectRef(bus, path) for bus, path in result]
        except (TypeError, ValueError):
            raise Refused("ATSPI_TREE_INCOMPLETE") from None

    def _owner_pid(self, owner, deadline):
        return self._transport.call(
            "org.freedesktop.DBus", "/org/freedesktop/DBus",
            "org.freedesktop.DBus.GetConnectionUnixProcessID", "(s)", (owner,), "(u)", deadline)

    def _live_state(self, ref, deadline):
        words = self._call(ref, _ACCESSIBLE + ".GetState", "(au)", deadline)
        if not 1 <= len(words) <= 2:
            raise Refused("ATSPI_TREE_INCOMPLETE")
        bits = sum(word << (32 * i) for i, word in enumerate(words))
        if bits & _DEFUNCT:
            raise Refused("ATSPI_RETIRED_OBJECT")
        return bits, words

    def _identity(self, request, session):
        try:
            identity = self._identity_lookup(request["session_id"])
        except (OSError, ValueError):
            raise Refused("identity_mismatch") from None
        if identity is None:
            raise Refused("identity_mismatch")
        display = session["display"]
        if re.fullmatch(r"xpra:0+", display):
            raise Refused("DISPLAY_UNSUPPORTED")
        expected_type = HostIdentity if display == ":0" else PrivateIdentity
        window = session.get("window", {})
        if (not isinstance(identity, expected_type) or not isinstance(identity.window, ObjectRef)
                or (identity.display, identity.pid, identity.xid, identity.witness,
                    identity.incarnation, identity.authority_epoch)
                != (display, window.get("pid"), window.get("xid"), window.get("witness"),
                    request["incarnation"], request["authority_epoch"])
                or request.get("target", {}).get("window_id", identity.xid) != identity.xid):
            raise Refused("identity_mismatch")
        return identity

    def _verify_window(self, identity, deadline):
        apps = self._transport.call(_REGISTRY, _ROOT, _ACCESSIBLE + ".GetChildren",
                                    "", (), "(a(so))", deadline)
        matches = set()
        owners = {}
        for bus, path in apps:
            try:
                app = ObjectRef(bus, path)
            except ValueError:
                raise Refused("ATSPI_TREE_INCOMPLETE") from None
            if bus not in owners:
                owners[bus] = self._owner_pid(bus, deadline)
            if owners[bus] == identity.pid:
                matches.add(app)
        if len(matches) > 1:
            raise Refused("ATSPI_AMBIGUOUS_PID")
        if not matches:
            raise Refused("identity_mismatch")
        app, = matches
        if (app.bus != identity.window.bus or identity.window == app
                or identity.window not in self._children(app, deadline)):
            raise Refused("identity_mismatch")
        return app

    def _walk(self, app, window, deadline):
        nodes, seen = [], set()
        stack = [(window, (app,), 0, ())]
        while stack:
            ref, parent_path, depth, ancestors = stack.pop()
            if ref in seen or ref.bus != window.bus:
                raise Refused("ATSPI_TREE_INCOMPLETE")
            if len(seen) >= self._max_nodes or depth > self._max_depth:
                raise Refused("ATSPI_TREE_TRUNCATED")
            seen.add(ref)
            bits, words = self._live_state(ref, deadline)
            native_role = self._call(ref, _ACCESSIBLE + ".GetRoleName", "(s)", deadline)
            role = atspi_tree.ATSPI_ROLE_MAP.get(native_role)
            if role:
                name = self._call(ref, _PROPERTIES, "(v)", deadline, "(ss)", (_ACCESSIBLE, "Name"))
                if not isinstance(name, str):
                    raise Refused("ATSPI_TREE_INCOMPLETE")
                row = {"role": role, "name": ax.normalize_name(name), "depth": len(ancestors),
                       "ancestors": ancestors, "attrs": atspi_tree._attrs(native_role, str(words))}
                nodes.append(_Node(ref, parent_path, native_role, name, bits, row))
                ancestors += (role,)
            children = self._children(ref, deadline)
            if len(children) + len(stack) + len(seen) > self._max_nodes:
                raise Refused("ATSPI_TREE_TRUNCATED")
            stack.extend((child, parent_path + (ref,), depth + 1, ancestors) for child in reversed(children))

        keys = [(node.row["role"], node.row["name"]) for node in nodes]
        cardinality, ordinals = Counter(keys), Counter()
        for node, key in zip(nodes, keys):
            node.row.update(cardinality=cardinality[key], ordinal=ordinals[key])
            node.row["id"] = ax.stable_id(node.row)
            ordinals[key] += 1
        return tuple(nodes), len(seen)

    def _capture(self, request, session, deadline):
        identity = self._identity(request, session)
        app = self._verify_window(identity, deadline)
        nodes, raw_count = self._walk(app, identity.window, deadline)
        if self._identity(request, session) != identity:
            raise Refused("identity_mismatch")
        rows = [node.row for node in nodes]
        return _Tree(uuid4().hex, identity, nodes, raw_count,
                     ax.snapshot_sha(rows), ax.snapshot_sha(rows, with_state=False))

    def _qualified(self, action, identity, node, session):
        if isinstance(identity, HostIdentity):
            return (self._host_qualified and identity.hidden is True
                    and session.get("browser") == "google-chrome" and action == "click"
                    and identity.qualified_tab == node.ref and node.native_role == "page tab")
        return action in identity.qualified_actions

    def _action(self, request, node, deadline):
        action = request["action"]
        text = request.get("input_text")
        if any(field in request for field in ("slot_id", "key", "url")):
            raise Refused("ATSPI_ACTION_UNSUPPORTED")
        if action == "click" and text is None:
            return node.ref, _ACTION + ".DoAction", "(i)", (0,)
        if action == "focus" and text is None:
            return node.ref, _COMPONENT + ".GrabFocus", "", ()
        if (action in {"insert_text", "type"} and node.native_role in {"text", "entry"}
                and isinstance(text, str) and text and "\0" not in text):
            offset = self._call(node.ref, _PROPERTIES, "(v)", deadline, "(ss)",
                                ("org.a11y.atspi.Text", "CaretOffset"))
            if type(offset) is not int or not 0 <= offset < 2**31:
                raise Refused("ATSPI_ACTION_UNSUPPORTED")
            try:
                length = len(text.encode("utf-8"))
            except UnicodeEncodeError:
                raise Refused("ATSPI_ACTION_UNSUPPORTED") from None
            if length >= 2**31:
                raise Refused("ATSPI_ACTION_UNSUPPORTED")
            return node.ref, _EDITABLE_TEXT + ".InsertText", "(isi)", (offset, text, length)
        if action == "select" and text is None and node.parent is not None:
            children = self._children(node.parent, deadline)
            if children.count(node.ref) != 1:
                raise Refused("ATSPI_RETIRED_OBJECT")
            return node.parent, _SELECTION + ".SelectChild", "(i)", (children.index(node.ref),)
        raise Refused("ATSPI_ACTION_UNSUPPORTED")

    def _verify_action_binding(self, request, session, identity, node, effect, deadline):
        if self._identity(request, session) != identity:
            raise Refused("identity_mismatch")
        if self._owner_pid(node.ref.bus, deadline) != identity.pid:
            raise Refused("ATSPI_RETIRED_OBJECT")
        native_role = self._call(node.ref, _ACCESSIBLE + ".GetRoleName", "(s)", deadline)
        name = self._call(node.ref, _PROPERTIES, "(v)", deadline, "(ss)", (_ACCESSIBLE, "Name"))
        if (native_role, name) != (node.native_role, node.name):
            raise Refused("ATSPI_RETIRED_OBJECT")
        bits, _ = self._live_state(node.ref, deadline)
        if not bits & _ENABLED or not bits & _SENSITIVE:
            raise Refused("ATSPI_ACTION_UNQUALIFIED")
        _, method, _, args = effect
        for parent, child in zip(node.parent_path, (*node.parent_path[1:], node.ref)):
            siblings = self._children(parent, deadline)
            if siblings.count(child) != 1:
                raise Refused("ATSPI_RETIRED_OBJECT")
            if child == node.ref and method == _SELECTION + ".SelectChild" and siblings.index(child) != args[0]:
                raise Refused("ATSPI_STALE_OBSERVATION")

    def _dispatch(self, request, session, identity, node, effect, deadline, focus):
        attempted = False

        def send_and_readback():
            nonlocal attempted
            self._verify_action_binding(request, session, identity, node, effect, deadline)
            ref, method, sig, args = effect
            attempted = True
            try:
                accepted = self._call(ref, method, "(b)", deadline, sig, args)
            except Refused:
                accepted = False
            try:
                post = self._capture(request, session, monotonic() + self._timeout)
                if post.identity != identity:
                    raise Refused("identity_mismatch")
            except Refused:
                return "UNCERTAIN_OUTCOME", None
            return ("OK" if accepted is True else "UNCERTAIN_OUTCOME"), post

        if focus is None:
            return send_and_readback()
        surface_id, monitor = focus
        try:
            result, _ = monitor.run_passive(surface_id, send_and_readback, qualified=True)
        except FocusRefused:
            if not attempted:
                raise Refused("ATSPI_ACTION_UNQUALIFIED") from None
            return "UNCERTAIN_OUTCOME", None
        return result

    @staticmethod
    def _validate(request, session, op):
        try:
            validate_contract("vp.channel.req/v1", request)
            validate_contract("vp.session/v1", session)
        except ValidationError:
            raise Refused("ATSPI_INVALID_REQUEST") from None
        if request["backend"] != "atspi" or request["op"] != op:
            raise Refused("ATSPI_INVALID_REQUEST")
        if session["state"] != "AGENT" or not session["lease"]["holder"]:
            raise Refused("NO_LEASE")
        if (any(request[key] != session.get(key) for key in ("session_id", "capsule_id", "incarnation"))
                or request["authority_epoch"] != session["lease"]["authority_epoch"]):
            raise Refused("identity_mismatch")

    @staticmethod
    def _result(code, tree=None):
        reply = {"schema": "vp.channel.reply/v1", "ok": code == "OK", "code": code}
        if tree is not None:
            reply.update(ax_sha256=tree.ax_sha256, ax_stable_sha256=tree.ax_stable_sha256)
        validate_contract("vp.channel.reply/v1", reply)
        return reply, tree.snapshot() if tree is not None else None

    def observe(self, request, *, session):
        with self._lock:
            try:
                self._validate(request, session, "observe")
                session_id = request["session_id"]
                self._trees.pop(session_id, None)
                tree = self._capture(request, session, monotonic() + self._timeout)
            except Refused as error:
                return self._result(error.code)
            self._trees[session_id] = tree
            return self._result("OK", tree)

    def act(self, request, *, session, node_id):
        with self._lock:
            validated_observation = False
            try:
                self._validate(request, session, "act")
                session_id = request["session_id"]
                prior = self._trees.get(session_id)
                identity = self._identity(request, session)
                if identity.input_allowed is not True:
                    raise Refused("NO_LEASE")
                if prior is None or request["observation_id"] != prior.observation_id:
                    raise Refused("ATSPI_STALE_OBSERVATION")
                validated_observation = True
                if prior.identity != identity:
                    raise Refused("identity_mismatch")
                old_node = prior.select(node_id)
                deadline = monotonic() + self._timeout
                current = self._capture(request, session, deadline)
                node = current.select(node_id)
                if (node.ref, node.parent_path, node.native_role, node.name) != (
                        old_node.ref, old_node.parent_path, old_node.native_role, old_node.name):
                    raise Refused("ATSPI_RETIRED_OBJECT")
                if (current.ax_stable_sha256 != prior.ax_stable_sha256
                        or request["target"].get("ax_stable_sha256", prior.ax_stable_sha256)
                        != prior.ax_stable_sha256):
                    raise Refused("ATSPI_STALE_OBSERVATION")
                if (not node.state_bits & _ENABLED or not node.state_bits & _SENSITIVE
                        or not self._qualified(request["action"], identity, node, session)):
                    raise Refused("ATSPI_ACTION_UNQUALIFIED")
                effect = self._action(request, node, deadline)
                ref, method, _, _ = effect
                interfaces = self._call(ref, _ACCESSIBLE + ".GetInterfaces", "(as)", deadline)
                if method.rsplit(".", 1)[0] not in interfaces:
                    raise Refused("ATSPI_ACTION_UNSUPPORTED")
                focus = self._focus_monitor_for(identity) if self._focus_monitor_for is not None else None
                if focus is None and (isinstance(identity, HostIdentity) or self._focus_monitor_for is not None):
                    raise Refused("ATSPI_ACTION_UNQUALIFIED")
                if request["dry_run"]:
                    self._verify_action_binding(request, session, identity, node, effect, deadline)
                    return self._result("OK")
                self._trees.pop(session_id, None)
                code, post = self._dispatch(request, session, identity, node, effect, deadline, focus)
            except Refused as error:
                if validated_observation:
                    self._trees.pop(session_id, None)
                return self._result(error.code)

            if code == "OK":
                self._trees[session_id] = post
            return self._result(code, post)

    def detach(self, session_id):
        with self._lock:
            self._trees.pop(session_id, None)

    def close(self):
        with self._lock:
            self._trees.clear()
            self._transport.close()

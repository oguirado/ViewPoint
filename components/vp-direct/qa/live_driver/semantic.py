"""Bind the existing CDP and AT-SPI executors to a synthetic checkbox."""

from contextlib import ExitStack, contextmanager, nullcontext
from time import monotonic
from uuid import uuid4

from runtime import WORK, eventually, fixture_site, open_browser, require
from ports import CHECKBOX, OWNER, binding_for, call_entry, session_for, write_record


_REGISTRY = "org.a11y.atspi.Registry"
_ROOT = "/org/a11y/atspi/accessible/root"
_CHILDREN = "org.a11y.atspi.Accessible.GetChildren"
_DBUS = "org.freedesktop.DBus"


def install_checkbox(browser):
    browser.check()
    require(browser.page.read_bool(f"""(() => {{
        if (document.getElementById('qd-checkbox')) return false;
        const label = document.createElement('label');
        const box = document.createElement('input');
        box.type = 'checkbox';
        box.id = 'qd-checkbox';
        label.append(box, '{CHECKBOX}');
        document.body.prepend(label);
        return box.isConnected && !box.checked;
    }})()"""), "could not bind synthetic checkbox")


def fixture_checkbox(nodes):
    matches = [row for row in nodes if row["role"] == "checkbox" and row["name"] == CHECKBOX]
    require(len(matches) == 1, "fixture checkbox AX match was missing or ambiguous")
    return matches[0]


def resolve_accessible_window(browser):
    from channel.atspi import ObjectRef
    from channel.atspi.transport import Refused, SessionBus

    transport = SessionBus(browser.display.env)

    def probe():
        browser.check()
        deadline = monotonic() + 2
        try:
            apps = transport.call(_REGISTRY, _ROOT, _CHILDREN, "", (), "(a(so))", deadline)
            owned = [ObjectRef(name, path) for name, path in apps
                     if transport.call(_DBUS, "/org/freedesktop/DBus", _DBUS + ".GetConnectionUnixProcessID",
                                       "(s)", (name,), "(u)", deadline) == browser.child.pid]
            if not owned:
                return None
            require(len(owned) == 1, "Chrome AT-SPI application identity is ambiguous")
            app = owned[0]
            windows = transport.call(app.bus, app.path, _CHILDREN, "", (), "(a(so))", deadline)
        except Refused:
            return None
        if not windows:
            return None
        require(len(windows) == 1 and windows[0][0] == app.bus, "Chrome AT-SPI window identity is ambiguous")
        return ObjectRef(*windows[0])

    try:
        return eventually(probe, "owned Chrome window did not register on the private AT-SPI bus")
    finally:
        transport.close()


@contextmanager
def checkbox_browser(display):
    with fixture_site() as port, open_browser(display, f"http://127.0.0.1:{port}/index.html") as browser:
        install_checkbox(browser)
        yield browser


def run_atspi_check(display, script):
    with checkbox_browser(display) as browser:
        ref = resolve_accessible_window(browser)
        session_path = write_record("session.json", session_for(browser, "atspi"))
        call_entry(script, [session_path, ref.bus, ref.path, CHECKBOX,
                            "--atspi-bus-address", display.env["AT_SPI_BUS_ADDRESS"],
                            "--surface-id", binding_for(browser)["surface_id"], "--x-server-pid", display.server.pid])


def reply(code="OK", **fields):
    return {"schema": "vp.channel.reply/v1", "ok": code == "OK", "code": code, **fields}


class SemanticFixture:
    def __init__(self, browser, backend, stack):
        from channel.atspi import AtspiExecutor, PrivateIdentity
        from channel.cdp import CdpBackend
        from channel.core import TargetContext
        from focus import FocusMonitor
        from focus.x11 import X11FocusSource

        self.browser = browser
        self.backend = backend
        self.session = session_for(browser, backend)
        self.session_id = self.session["session_id"]
        self.binding = binding_for(browser)
        self.target = TargetContext(self.session, self.binding["surface_id"], "private", "google-chrome",
                                    frozenset({backend}))
        self.prior = None
        self.act_request_id = None
        self.monitor = FocusMonitor(X11FocusSource(browser.display.name, browser.display.server.pid))

        if backend == "cdp":
            self.executor = CdpBackend(self.session_id, self.read_session, effect_guard=self.effect_guard)
            stack.callback(self.executor.detach)
        else:
            ref = resolve_accessible_window(browser)

            def identity(session_id):
                if self.read_session(session_id) is None:
                    return None
                return PrivateIdentity(browser.display.label, browser.child.pid, browser.xid, browser.witness,
                                       browser.incarnation, 1, ref, True, frozenset({"click"}))

            self.executor = AtspiExecutor(identity, session_env=browser.display.env,
                                          focus_monitor_for=lambda _: (self.binding["surface_id"], self.monitor))
            stack.callback(self.executor.close)

    def read_session(self, session_id):
        if session_id != self.session_id:
            return None
        self.browser.check()
        return self.session

    def effect_guard(self, request):
        require(request["request_id"] == self.act_request_id and request["action"] == "click",
                "fixture CDP effect was outside the single reviewed click")
        self.browser.check()
        return nullcontext()

    def request_for(self, op):
        target = ({"cdp_target_id": self.session["cdp_target_id"]} if self.backend == "cdp"
                  else {"window_id": self.browser.xid})
        request = {"schema": "vp.channel.req/v1", "request_id": uuid4().hex, "op": op,
                   "backend": self.backend, "session_id": self.session_id, "capsule_id": self.session["capsule_id"],
                   "incarnation": self.session["incarnation"], "authority_epoch": 1, "target": target,
                   "risk_class": "preparar", "dry_run": False, "require_vision_witness": False}
        if op == "act":
            require(self.prior is not None and self.act_request_id is None, "fixture action was already admitted")
            observation_id, stable, _ = self.prior
            self.act_request_id = request["request_id"]
            request.update(action="click", observation_id=observation_id)
            target["ax_stable_sha256"] = stable
        return request

    def observation_for(self, request, target):
        from channel.core import Observation

        if self.prior is None:
            return None
        observation_id, stable, node_id = self.prior
        return Observation(self.session_id, self.session["capsule_id"], target.surface_id,
                           self.session["incarnation"], 1, observation_id, self.backend, stable, (node_id,))

    def prepare(self, request, _target, observation):
        return lambda _lock: self.perform(request, observation)

    def perform(self, request, observation):
        self.browser.check()
        if self.backend == "cdp":
            operation = lambda: self.cdp(request, observation)
            result, _ = self.monitor.run_passive(self.binding["surface_id"], operation, qualified=True)
            return result
        return self.atspi(request, observation)

    @staticmethod
    def checked_cdp(nodes):
        node = fixture_checkbox(nodes)
        return dict(node["attrs"]).get("checked") == "true"

    def cdp(self, request, observation):
        op = request["op"]
        if op == "attach":
            self.executor.attach_target(request)
            return reply()
        if op == "detach":
            self.executor.detach()
            return reply()
        if op == "observe":
            result = self.executor.observe(request)
            node = fixture_checkbox(result.nodes)
            require(not self.checked_cdp(result.nodes), "fixture checkbox was already checked")
            self.prior = result.id, result.ax_stable_sha256, node["id"]
            return reply(ax_sha256=result.ax_sha256, ax_stable_sha256=result.ax_stable_sha256)
        if op == "act":
            return self.executor.act(request, node_id=observation.node_matches[0], verify=self.checked_cdp)
        require(op == "assert", "unsupported CDP fixture operation")
        return self.executor.assert_observation(request, verify=self.checked_cdp)

    def atspi(self, request, observation):
        op = request["op"]
        if op == "attach":
            probe = dict(request, op="observe")
            result, _ = self.executor.observe(probe, session=self.session)
            return reply() if result["ok"] else result
        if op == "detach":
            self.executor.detach(self.session_id)
            return reply()
        if op == "act":
            result, tree = self.executor.act(request, session=self.session, node_id=observation.node_matches[0])
            if result["ok"]:
                require(dict(fixture_checkbox(tree["nodes"])["attrs"]).get("checked") == "true",
                        "AT-SPI effect lacked the checked-state readback")
            return result
        require(op in {"assert", "observe"}, "unsupported AT-SPI fixture operation")
        result, tree = self.executor.observe(dict(request, op="observe"), session=self.session)
        if result["ok"]:
            node = fixture_checkbox(tree["nodes"])
            expected = "false" if op == "observe" else "true"
            require(dict(node["attrs"]).get("checked") == expected, "unexpected fixture checkbox readback")
            if op == "observe":
                self.prior = tree["observation_id"], tree["ax_stable_sha256"], node["id"]
        return result


@contextmanager
def open_channel(display, backend):
    from bus.broker import Broker
    from channel.core import ChannelCore, RequestStore

    require(display.verify(display.label) and display.label.startswith("xpra:"), "channel requires owned Xpra")
    require(backend in {"cdp", "atspi"}, "unknown channel qualification backend")
    with ExitStack() as stack:
        browser = stack.enter_context(checkbox_browser(display))
        fixture = SemanticFixture(browser, backend, stack)
        broker = stack.enter_context(Broker(WORK / "channel-broker"))
        broker.bind(fixture.session_id, fixture.binding, lease_state="AGENT")
        def authorize(request, session, risk):
            return (request["request_id"] == fixture.act_request_id and request["action"] == "click"
                    and session == fixture.session and risk == "preparar")

        channel = ChannelCore(broker=broker, target_for=lambda sid: fixture.target if fixture.read_session(sid) else None,
                              observation_for=fixture.observation_for, backends={backend: fixture.prepare},
                              request_store=RequestStore(WORK / "channel-requests.sqlite"),
                              risk_for=lambda *_: "preparar", authorize=authorize)
        yield channel, OWNER, fixture.request_for


class AttachmentSources:
    """Derive independent, content-free A1 evidence from live readbacks."""

    def __init__(self, browser, *, directory=WORK):
        self.browser = browser
        self.directory = directory

    def evidence(self, channel, fields):
        from attach import EvidenceRef
        from hashlib import sha256

        path = write_record(self.directory / f"{channel}-witness.json", fields)
        return EvidenceRef(str(path), 1, 1, sha256(path.read_bytes()).hexdigest())

    def current_window(self, xid):
        from attach import WindowIdentity

        browser = self.browser
        if xid != int(browser.xid, 16):
            return None
        browser.check()
        return WindowIdentity(browser.display.name, xid, browser.child.pid, browser.witness)

    def cdp_targets(self, window):
        from attach import CdpWitness

        browser = self.browser
        if window != self.current_window(window.xid):
            return ()
        target = browser.find_target()
        if target is None or target["id"] != browser.target["id"]:
            return ()
        endpoint = f"http://127.0.0.1:{browser.port}"
        ref = self.evidence("cdp", {"endpoint": endpoint, "target_id": target["id"],
                                    "browser_window": browser.cdp_window, "witness": window.witness})
        return (CdpWitness(ref, window, endpoint, target["id"]),)

    def atspi_objects(self, window):
        from attach import AtspiWitness

        if window != self.current_window(window.xid):
            return ()
        current = resolve_accessible_window(self.browser)
        ref = self.evidence("atspi", {"bus_name": current.bus, "object_path": current.path,
                                      "pid": window.pid, "witness": window.witness})
        return (AtspiWitness(ref, window, current.bus, current.path),)


def prepare_attachment(display, stack):
    from attach import AttachmentService
    from bus.broker import Broker
    from dataclasses import asdict

    port = stack.enter_context(fixture_site())
    browser = stack.enter_context(open_browser(display, f"http://127.0.0.1:{port}/index.html"))
    sources = AttachmentSources(browser)
    window = sources.current_window(int(browser.xid, 16))
    cdp, = sources.cdp_targets(window)
    atspi, = sources.atspi_objects(window)
    args = {"op": "attach", "xid": browser.xid, "witness": browser.witness,
            "evidence": [asdict(cdp.evidence), asdict(atspi.evidence)]}
    binding = dict(binding_for(browser), purpose="attach")
    session_id = f"qd-attach-{browser.incarnation}"
    broker = stack.enter_context(Broker(WORK / "attach-broker"))
    broker.bind(session_id, binding, lease_state="AGENT")
    return AttachmentService(sources, display=display.name), args, session_id, broker.binding(binding["surface_id"])

"""Trusted T2 fixture adapter for the private Chrome timer and CDP observe."""

from copy import deepcopy
from dataclasses import asdict, replace
from hashlib import sha256
import json
import os
from pathlib import Path
import socket
from time import monotonic_ns, sleep
from uuid import uuid4

from ports import FramePorts, binding_for, demand_for, session_for
from runtime import MAX_XWD_BYTES, SCREEN_HEIGHT, SCREEN_WIDTH, require
from semantic import AttachmentSources

EXPECTED_READBACK = b"Fixture timer page"
COUNTERS = ("acquisitions", "analyses", "ocr", "inferences", "decisions")


def prepare(plan):
    from driver import get_ports

    workload = getattr(get_ports(), "acceptance", None)
    require(workload is not None and getattr(workload, "plan", None) == plan,
            "T2 needs the exact provisioned workload plan")
    return workload


class TimerCell:
    def __init__(self, directory, stack, browser, fixture_cgroup):
        from attach import AttachmentService
        from bus.broker import Broker
        from channel.cdp import CdpBackend
        from frames.source import FrameSource
        from perception.baseline.inventory import collect
        from perception.demand import DemandCoordinator
        from perception.lifecycle import PerceptionLifecycle, RuntimeEvidence
        from perception.receipts import verify_transition
        from perception.stream import PerceptionStream
        from privacy import PrivacySentinel
        from vadapt import rtring, x11_ramcap

        self.directory, self.browser = directory, browser
        directory.mkdir(mode=0o700)
        self.session, self.binding = session_for(browser), binding_for(browser)
        self.sources = AttachmentSources(browser, directory=directory)
        display = browser.display
        self.service = AttachmentService(self.sources, display=display.name)
        self.broker = stack.enter_context(Broker(directory / "broker"))
        self.broker.bind(self.session["session_id"], self.binding, lease_state="AGENT")
        self.cdp = CdpBackend(self.session["session_id"], self.read_session)
        stack.callback(self.cdp.detach)
        self.gaps, self.handoffs = [], []
        self.counts = dict.fromkeys(COUNTERS, 0)
        self.frames = FramePorts(browser)
        agent = demand_for(self.binding, "agent")
        self.capture = demand_for(self.binding, "t2-frames")
        coordinator = DemandCoordinator(self.binding["surface_id"], interests={"VERIFIED"},
                                        authorize=lambda row: row in (agent, self.capture), drain=lambda: True)
        self.privacy_binding = self.frames.current().privacy_binding()
        sentinel = PrivacySentinel(self.privacy_binding, self.frames.metadata,
                                   record_gap=self.record_gap, request_human=self.request_human)
        self.source = FrameSource(lambda sid: self.frames.current() if sid == self.binding["surface_id"] else None,
                                  {"cdp": self.frames.acquire}, coordinator.register,
                                  privacy_for=lambda state: sentinel if state.privacy_binding() == self.privacy_binding else None)
        width, height = display.size
        config = x11_ramcap.SourceConfig(str(display.frame_dir), "Xvfb_screen0", width, height,
                                         f"Xvfb process {display.server.pid} -nocursor")
        stream = PerceptionStream(self.binding, config, ring_slots=2, interval_ns=20_000_000,
                                  ttl_ns=3_000_000_000, max_frame_bytes=MAX_XWD_BYTES,
                                  max_ring_bytes=rtring.ring_bytes(2, width, height))
        stack.callback(stream.close)

        def runtime():
            scope, = collect()["scopes"]
            pids = {row["pid"] for row in scope["processes"]}
            require(os.getpid() in pids and not {browser.child.pid, display.server.pid} & pids,
                    "T2 producer shares the benchmark runner")
            return RuntimeEvidence(self.binding["incarnation"], tuple(sorted(pids - {os.getpid()})),
                                   scope["current_bytes"])

        self.published = None

        def publication(receipt, proof):
            self.published = (verify_transition(receipt, proof), proof.observation)

        self.lifecycle = PerceptionLifecycle(self.broker, self.session["session_id"], self.binding,
                                             coordinator, agent, stream, qualified=self.qualified,
                                             runtime=runtime, drain_agent=lambda _: not runtime().exclusive_pids,
                                             on_transition=publication)
        self.fixture_cgroup = fixture_cgroup
        self.observation = None
        self.frame = None

    def record_gap(self, active, time):
        self.gaps.append((active, time))

    def request_human(self, reason):
        self.handoffs.append(reason)

    def qualified(self, binding):
        self.browser.check()
        return binding == self.binding

    def read_session(self, session_id):
        return self.session if session_id == self.session["session_id"] and self.qualified(self.binding) else None

    def verify(self, session, binding):
        if session != self.session or not self.qualified(binding):
            return False
        for pid in (self.browser.child.pid, self.browser.display.server.pid):
            membership = Path(f"/proc/{pid}/cgroup").read_text().strip()
            if membership != f"0::{self.fixture_cgroup}":
                return False
        return (self.browser.profile.parent == self.directory.parents[1] and self.browser.profile.is_dir()
                and self.session["window"]["witness"] == self.browser.witness)

    def request(self, op):
        session = self.session
        return {"schema": "vp.channel.req/v1", "request_id": uuid4().hex, "op": op,
                "backend": "cdp", "session_id": session["session_id"], "capsule_id": session["capsule_id"],
                "incarnation": session["incarnation"], "authority_epoch": self.binding["authority_epoch"],
                "target": {"cdp_target_id": session["cdp_target_id"]}, "risk_class": "preparar",
                "dry_run": False, "require_vision_witness": False}

    def transition(self, operation):
        self.published = None
        receipt = self.lifecycle.vp_perception_call({"op": operation, "session_id": self.session["session_id"],
                                                    "reason": "t2"})
        require(self.published is not None and self.published[0] == receipt, "T2 transition lacks P7 verification")
        _, self.observation = self.published
        return receipt

    def attach(self):
        browser = self.browser
        window = self.sources.current_window(int(browser.xid, 16))
        cdp, = self.sources.cdp_targets(window)
        atspi, = self.sources.atspi_objects(window)
        args = {"op": "attach", "xid": browser.xid, "witness": browser.witness,
                "evidence": [asdict(cdp.evidence), asdict(atspi.evidence)]}
        result = self.service.attach(args, session_id=self.session["session_id"], binding=self.binding)
        try:
            self.cdp.attach_target(self.request("attach"))
            self.transition("wake")
        except Exception:
            self.detach(self.session["session_id"])
            raise
        return result

    def detach(self, session_id):
        require(session_id == self.session["session_id"], "foreign detach refused")
        self.cdp.detach()
        return self.service.detach({"op": "detach", "session_id": session_id})

    def from_agent(self, activity, counter, operation):
        require(self.observation is not None, "T2 surface lacks a verified active observation")

        def run():
            self.counts[counter] += 1
            return operation()

        return self.lifecycle.run_agent(activity, self.observation.observation_id, run)

    def frame_capture(self, metadata):
        require(metadata.binding == self.privacy_binding, "protected frame source changed")

        def capture():
            descriptor = self.source.capture(self.capture)
            self.source.release(descriptor["ram_ref"])
            self.frame = descriptor
            return descriptor

        return self.from_agent("acquisition", "acquisitions", capture)

    def observe(self, marks):
        from channel.cdp import Refused as CdpRefused

        request = self.request("observe")

        def read():
            marks.admitted()
            try:
                observed = self.cdp.observe(request)
            except CdpRefused:
                return {"ok": False, "code": "HOLD"}
            marks.first_byte()
            headings = [node["name"] for node in observed.nodes if node["role"] == "heading"]
            require(len(headings) == 1 and isinstance(headings[0], str), "unique timer heading absent")
            value = headings[0].encode()
            return {"ok": True, "code": "OK", "ax_sha256": observed.ax_sha256,
                    "ax_stable_sha256": observed.ax_stable_sha256,
                    "value_len": len(value), "value_sha256": sha256(value).hexdigest()}

        reply = self.from_agent("analysis", "analyses", read)
        return request, {"schema": "vp.channel.reply/v1", **reply}


class T2Workload:
    def __init__(self, plan, directory, stack, apps, login, fixture_cgroup):
        from ports import Footprint

        require(set(plan["combinations"]) == set(apps) and
                all(row["backend"] == "cdp" and row["op"] == "observe"
                    for row in plan["combinations"].values()), "T2 workload lacks a requested provider")
        require(login is not None, "T2 synthetic login fixture AUSENTE")
        self.plan = deepcopy(plan)
        self.cells = {combo: TimerCell(directory / f"cell-{index}", stack, browser, fixture_cgroup)
                      for index, (combo, browser) in enumerate(apps.items())}
        self.login = Footprint(login)

    def target(self, combo):
        cell = self.cells[combo]
        display = cell.browser.display
        return display.name, display.server.pid, deepcopy(cell.session), deepcopy(cell.binding)

    def verify_fixture(self, combo, session, binding):
        return self.cells[combo].verify(session, binding)

    def inventory(self):
        # The parent reads host-side inventories. The worker never sees host sockets or host processes.
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(12)
            connection.connect("/work/inventory.sock")
            with connection.makefile("rb") as stream:
                raw = stream.read((1 << 20) + 1)
        require(len(raw) <= 1 << 20, "host attach inventory exceeds bound")
        result = json.loads(raw)
        require(isinstance(result, dict) and set(result) == {"containers", "guests", "private_display_servers"},
                "host attach inventory AUSENTE")
        return result

    def attach(self, combo):
        return self.cells[combo].attach()

    def privacy_ports(self, combo, *, login):
        cell = self.cells[combo]
        if login:
            browser = self.login.browser
            browser.check()
            require(browser.page.read_bool("document.querySelector('input[type=password]') !== null"),
                    "synthetic login password field AUSENTE")
            binding = replace(cell.privacy_binding, witness=browser.witness)
            return binding, self.login.read, cell.frame_capture, cell.record_gap, cell.request_human
        return cell.privacy_binding, cell.frames.metadata, cell.frame_capture, cell.record_gap, cell.request_human

    def warmup(self, combo):
        class Warmup:
            def admitted(self):
                pass

            def first_byte(self):
                pass

        _, reply = self.cells[combo].observe(Warmup())
        return reply.get("value_sha256") == self.plan["combinations"][combo]["readback_sha256"]

    def perform(self, combo, index, frame, marks):
        cell = self.cells[combo]
        require(cell.frame is not None and frame == cell.frame, "T2 operation lacks its protected frame")
        cell.frame = None
        return cell.observe(marks)

    def rest(self, combo, duration_ns):
        cell = self.cells[combo]
        receipt = cell.transition("sleep")
        ack = monotonic_ns()
        before = dict(cell.counts)
        while (remaining := ack + duration_ns - monotonic_ns()) > 0:
            sleep(remaining / 1_000_000_000)
        return {"receipt": receipt, "ack_mono_ns": ack, "end_mono_ns": monotonic_ns(),
                "before": before, "after": dict(cell.counts)}

    def prepare_wake(self, combo, state):
        cell = self.cells[combo]
        prior = cell.broker.perception_state(cell.binding["surface_id"])
        if prior["state"] != state:
            cell.transition({"ASLEEP": "sleep", "OFF": "off"}[state])
        return cell.broker.perception_state(cell.binding["surface_id"])

    def wake(self, combo, state, marks):
        cell = self.cells[combo]
        require(cell.broker.perception_state(cell.binding["surface_id"])["state"] == state,
                "T2 wake state changed")
        marks.admitted()
        receipt = cell.transition("wake")
        observation = cell.observation
        require(sha256(cell.lifecycle.read_frame(observation.observation_id)).hexdigest() == observation.frame_sha256,
                "T2 wake frame digest differs")
        marks.first_byte()
        cell.counts["acquisitions"] += 1
        return receipt

    def detach(self, combo, session_id):
        return self.cells[combo].detach(session_id)


def exercise(harness, driver, plan):
    """Run every producer hook with stock A2, A3, schema and wake checks."""
    from schemas.validation import validate_contract
    from score import check_wake

    cells = {}
    for combo, expected in plan["combinations"].items():
        display, pid, session, binding = driver.target(combo)
        harness.session_check(session, binding, display, pid, expected)

        def run():
            require(driver.verify_fixture(combo, session, binding), "T2 fixture witness AUSENTE")
            cell = {"operations": [], "wakes": []}
            before = driver.inventory()
            attached = driver.attach(combo)
            try:
                validate_contract("vp_workspace_call.attach.result/v1", attached)
                require(attached == {"session_id": session["session_id"], "binding": binding}, "attach changed identity")
                after = driver.inventory()
                cell["inventory"] = {"before": before, "after": after}
                cell["inventory_delta"] = harness.check_inventory(before, after)
                cell["privacy_denial"] = harness.protected(driver, combo, binding, login=True)
                require(cell["privacy_denial"] == {"acquisitions": 0, "publications": 0, "code": "HUMAN_NEEDED"},
                        "login reached the capture callback")
                for _ in range(plan["warmups"]):
                    require(driver.warmup(combo), "T2 warm-up failed")
                for index in range(2):
                    marks = harness.Markers()
                    observation = harness.protected(driver, combo, binding, login=False)
                    require(observation is not None, "T2 protected capture refused")
                    frame = observation["frame"]
                    require((frame["width"], frame["height"]) == (SCREEN_WIDTH, SCREEN_HEIGHT),
                            "T2 protected viewport size changed")
                    request, reply = driver.perform(combo, index, frame, marks)
                    validate_contract("vp.channel.req/v1", request)
                    validate_contract("vp.channel.reply/v1", reply)
                    require(reply.get("value_sha256") == expected["readback_sha256"], "T2 semantic readback differs")
                    cell["operations"].append({"request": request, "reply": reply,
                                               "privacy": observation, **marks.finish()})
                rest = driver.rest(combo, 100_000_000)
                validate_contract("vp.transition_receipt/v1", rest["receipt"])
                require(rest["receipt"]["complete"] and rest["receipt"]["to"] == "ASLEEP"
                        and rest["before"] == rest["after"], "T2 sleep drain not witnessed")
                cell["rest"] = rest
                for state in harness.WAKE:
                    prior = driver.prepare_wake(combo, state)
                    marks = harness.Markers()
                    row = {"prior_state": prior, "receipt": driver.wake(combo, state, marks), **marks.finish()}
                    check_wake(row, state, binding, session["session_id"],
                                       rest["end_mono_ns"], monotonic_ns())
                    cell["wakes"].append(row)
            finally:
                cell["detach"] = driver.detach(combo, session["session_id"])
                validate_contract("vp_workspace_call.detach.result/v1", cell["detach"])
                require(driver.verify_fixture(combo, session, binding), "detach lost the fixture")
            return cell

        monitor = harness.FocusMonitor(harness.X11FocusSource(display, pid), clock=harness.now)
        cell, cell_focus = monitor.run_passive(binding["surface_id"], run, qualified=True)
        cell["focus"] = cell_focus
        cells[combo] = cell
    return cells

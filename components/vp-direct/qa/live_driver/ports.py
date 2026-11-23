"""Product bindings for the QD private-display checks."""

from contextlib import contextmanager
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import re
import runpy
import sys
import time
from uuid import uuid4

from runtime import MAX_XWD_BYTES, Child, Refused, WORK, eventually, fixture_site, loopback_url, open_browser, require

DIRECT = Path(__file__).resolve().parents[2]
DRIVER = Path(__file__).with_name("driver.py")
OWNER = "qd-agent"
CHECKBOX = "QD fixture check"


class Footprint:
    """Privacy state for one pinned synthetic document and its whole CDP viewport."""

    def __init__(self, browser, *, scene=False):
        self.browser = browser
        self.login = loopback_url(browser.url)[1] == "/login.html"
        self.invalid = False
        if not self.login:
            require(scene or loopback_url(browser.url)[1] == "/timer.html",
                    "no clear-footprint rule for this fixture")
            keep = "qd-scene" if scene else "timer-region"
            # Only text changes in the known timer/scene region are admitted.
            expression = r"""(() => {
                if (document.readyState !== 'complete' || document.querySelector(
                    'input,textarea,[contenteditable],iframe,frame,object,embed,dialog,video,canvas')) {
                    return false;
                }
                const region = document.getElementById(KEEP);
                if (!region) return false;
                let clean = true;
                const allows = r => r.target === region && r.type === 'childList' &&
                    [...r.addedNodes, ...r.removedNodes].every(n => n.nodeType === Node.TEXT_NODE);
                const record = records => { if (!records.every(allows)) clean = false; };
                const observer = new MutationObserver(record);
                observer.observe(document, {subtree:true, childList:true, attributes:true, characterData:true});
                Object.defineProperty(window, '__qdFootprintClean', {value: () => {
                    record(observer.takeRecords());
                    return clean && document.getElementById(KEEP) === region;
                }});
                return true;
            })()""".replace("KEEP", json.dumps(keep))
            require(eventually(lambda: browser.page.read_bool(expression), "fixture footprint was not pinned"),
                    "fixture footprint is unavailable")

    def read(self, binding):
        from privacy import PrivacyMetadata, PrivacyState

        self.browser.check()
        if not self.login and not self.browser.page.read_bool("window.__qdFootprintClean?.() === true"):
            self.invalid = True
        state = PrivacyState.LOGIN if self.login else PrivacyState.CLEAR
        return PrivacyMetadata(binding, int(self.invalid), PrivacyState.UNKNOWN if self.invalid else state)

    def authorize(self, binding):
        from privacy import PrivacyState

        snapshot = self.read(binding)
        require(snapshot.state is PrivacyState.CLEAR, "private footprint does not authorize raster or AX")
        return snapshot


class FramePorts:
    def __init__(self, browser, *, scene=False):
        self.browser = browser
        self.footprint = Footprint(browser, scene=scene)
        self.geometry = browser.geometry(), browser.display.hidden(browser.xid)

    def current(self):
        from frames.source import HostFrameState

        browser = self.browser
        browser.check()
        area, size = browser.geometry()
        hidden = browser.display.hidden(browser.xid)
        require(((area, size), hidden) == self.geometry, "frame geometry or workspace changed")
        return HostFrameState(f"qd-{browser.incarnation}", browser.incarnation, 1, 1, browser.witness, area,
                              size, browser.display.label, hidden, "cdp",
                              browser.target["id"])

    def metadata(self, binding):
        require(binding == self.current().privacy_binding(), "privacy binding changed")
        return self.footprint.read(binding)

    def acquire(self, state):
        from frames.source import Raster, inspect_png

        require(state == self.current(), "frame binding changed before capture")
        binding = state.privacy_binding()
        prior = self.footprint.authorize(binding)
        nodes = self.browser.page.call("Accessibility.getFullAXTree")["nodes"]
        require(nodes, "CDP AX witness was empty")
        ax = sha256(json.dumps(nodes, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        png = self.browser.page.screenshot()
        captured = time.monotonic_ns()
        inspect_png(png, state.raster_size)
        require(self.footprint.authorize(binding) == prior and self.current() == state,
                "frame binding changed during capture")
        return Raster(state, "cdp_png", png, captured, ax)


def binding_for(browser):
    return {"schema": "vp.surface_binding/v1", "endpoint": "qd-fixture", "surface_id": f"qd-{browser.incarnation}",
            "owner": OWNER, "purpose": "recording", "incarnation": browser.incarnation, "authority_epoch": 1,
            "obligations": []}


def demand_for(binding, name, *, owner=OWNER):
    return {"schema": "vp.demand/v1", "demand_id": f"qd-{name}", "surface_id": binding["surface_id"],
            "consumer": owner, "interest": "VERIFIED", "purpose": name, "lease": f"qd-{owner}", "credits": 1}


def session_for(browser, backend="cdp", *, window_witness=None):
    from schemas.validation import validate_contract

    require(browser.display.verify(browser.display.label) and browser.display.label.startswith("xpra:"),
            "session export requires the owned Xpra witness")
    browser.check()
    session = {"schema": "vp.session/v1", "session_id": f"qd-session-{browser.incarnation}",
               "kind": "browser", "site_group": "fixture", "browser": "google-chrome",
               "profile_id": f"qd-profile-{browser.incarnation}", "display": browser.display.label,
               "backend": backend, "cdp_allowed": backend == "cdp",
               "window": {"xid": browser.xid, "pid": browser.child.pid, "witness": window_witness or browser.witness},
               "capsule_id": f"qd-capsule-{browser.incarnation}", "incarnation": browser.incarnation,
               "state": "AGENT", "lease": {"holder": OWNER, "authority_epoch": 1},
               "pace": {"min_interval_ms": 1000}, "risk_ceiling": "preparar"}
    if backend == "cdp":
        session.update(cdp_port=browser.port, cdp_target_id=browser.target["id"])
    validate_contract("vp.session/v1", session)
    return session


def write_record(name, record):
    path = WORK / name
    path.write_text(json.dumps(record, sort_keys=True) + "\n")
    return path


def call_entry(script, args, *, stdin=None):
    old_argv, old_stdin = sys.argv, sys.stdin
    file = DIRECT / script
    sys.argv = [str(file), *map(str, args)]
    if stdin is not None:
        sys.stdin = io.StringIO(json.dumps(stdin))
    try:
        try:
            runpy.run_path(str(file), run_name="__main__")
        except SystemExit as exc:
            if exc.code not in (None, 0):
                raise
    finally:
        sys.argv, sys.stdin = old_argv, old_stdin


class Ports:
    def __init__(self, display, stack):
        self.display, self.stack = display, stack

    @contextmanager
    def open_frame_fixture(self, url, display):
        require(self.display.verify(display) and display == self.display.label and display.startswith("xpra:"),
                "S5+ requires the owned Xpra endpoint")
        with open_browser(self.display, url) as browser:
            if loopback_url(url)[1] == "/timer.html":
                self.display.park(browser.xid)
            frame = FramePorts(browser)
            yield frame.current, frame.metadata, frame.acquire

    @contextmanager
    def open_login_fixture(self, url, display):
        require(self.display.verify(display) and loopback_url(url)[1] == "/login.html",
                "A2 requires the bound private login")
        with open_browser(self.display, url) as browser:
            frame = FramePorts(browser)
            binding = frame.current().privacy_binding()

            def acquire(metadata):
                require(metadata.binding == binding, "unexpected login binding")
                return frame.acquire(frame.current())

            yield binding, frame.metadata, acquire

    @contextmanager
    def open_vision_fixture(self, display):
        from workload import open_vision

        require(self.display.verify(display) and display.startswith("xpra:"), "R2 requires owned Xpra")
        with open_vision(self.display) as ports:
            yield ports

    def open_fixture(self, backend):
        from semantic import open_channel

        return self.stack.enter_context(open_channel(self.display, backend))

    def prepare_attachment(self):
        from semantic import prepare_attachment

        return prepare_attachment(self.display, self.stack)

def distinct_pixels(first, second):
    require(isinstance(first, str) and isinstance(second, str)
            and re.fullmatch(r"[0-9a-f]{64}", first) and re.fullmatch(r"[0-9a-f]{64}", second)
            and first != second, "timer did not yield distinct validated pixel digests")


def smoke(display):
    from frames.source import inspect_png

    with fixture_site() as port, open_browser(display, f"http://127.0.0.1:{port}/timer.html") as browser:
        frames = FramePorts(browser)
        state = frames.current()
        first = frames.acquire(state)
        a = inspect_png(first.png, state.raster_size)

        def changed():
            candidate = frames.acquire(state)
            pixels = inspect_png(candidate.png, state.raster_size)
            if a != pixels and first.ax_sha256 != candidate.ax_sha256:
                return candidate, pixels
            return None

        second, b = eventually(changed, "timer did not advance")
        distinct_pixels(a, b)
        require(second.captured_mono_ns > first.captured_mono_ns, "capture time did not advance")
        print(json.dumps({"status": "PASS", "evidence": "MEASURED", "fixture": "fixtures/site/timer.html",
                          "fixture_sha256": browser.fixture_digest, "affinity": sorted(os.sched_getaffinity(0)),
                          "pid_namespace": True, "xvfb_pid": display.server.pid, "display": display.name,
                          "chrome_pid": browser.child.pid, "xid": browser.xid, "witness": browser.witness,
                          "cdp_listener": f"127.0.0.1:{browser.port}", "raster_size": state.raster_size,
                          "pixels_sha256": [a, b], "ax_sha256": [first.ax_sha256, second.ax_sha256],
                          "capture_mono_ns": [first.captured_mono_ns, second.captured_mono_ns]},
                         sort_keys=True), flush=True)
    print(json.dumps({"chrome_profile_removed": not browser.profile.exists()}, sort_keys=True), flush=True)


def activate_auxiliary(display, stack, browser):
    child = Child(["xmessage", "-title", "QD passive witness", "-geometry", "250x100+5+5",
                   "-buttons", "OK", "Private fixture focus witness"], display.env, stack)

    def separate():
        require(child.live(), "private auxiliary window exited")
        value = display.command("xprop", "-root", "-notype", "_NET_ACTIVE_WINDOW").decode()
        match = re.search(r"0x[0-9a-f]+", value)
        if match is None or int(match[0], 16) == 0 or match[0] == browser.xid:
            return False
        props = display.command("xprop", "-id", match[0], "-notype", "WM_NAME").decode()
        return props.strip() == 'WM_NAME = "QD passive witness"'

    eventually(separate, "private auxiliary window did not take focus")


def window_witness(display, browser):
    from window.x11 import _window_rows, _xid

    browser.check()
    rows = _window_rows(display.command("/srf-bin/srf-host-window", "list", "--display", display.name,
                                        "--xauthority", display.env["XAUTHORITY"]).decode())
    matches = [row for row in rows if _xid(row.get("xid")) == int(browser.xid, 16)]
    require(len(matches) == 1 and isinstance(matches[0].get("witness"), str) and matches[0]["witness"],
            "SRF did not witness the private Chrome window")
    witness = matches[0]["witness"]
    require(sum(row.get("witness") == witness for row in rows) == 1, "SRF window witness is ambiguous")
    return witness


def run_check(script, kind, ports, backend):
    display = ports.display
    if script == "smoke":
        smoke(display)
        return
    if kind in {"privacy", "frames", "vision"}:
        call_entry(script, ["--driver", DRIVER, "--display", display.label])
        return
    if kind == "channel":
        call_entry(script, [DRIVER, "--backend", backend])
        return
    if kind in {"attach", "timeline", "compiler", "selector"}:
        call_entry(script, ["--driver", DRIVER])
        return
    if kind == "atspi":
        from semantic import run_atspi_check

        run_atspi_check(display, script)
        return

    page = {"cdp": "index", "window": "timer", "focus": "chat",
            "demand": "timer", "pool": "timer", "stream": "timer",
            "lifecycle": "timer", "receipts": "timer"}[kind]
    with fixture_site() as port, open_browser(display, f"http://127.0.0.1:{port}/{page}.html") as browser:
        binding = binding_for(browser)
        common = ["--xvfb-pid", display.server.pid, "--fixture-port", port]
        if kind in {"cdp", "window", "focus"}:
            activate_auxiliary(display, ports.stack, browser)
            witness = window_witness(display, browser) if kind in {"window", "focus"} else None
            session = session_for(browser, window_witness=witness)
            window = ["--display", display.name, "--surface-id", binding["surface_id"]]
            if kind == "cdp":
                call_entry(script, common + window, stdin=session)
                return
            session_file = write_record("session.json", session)
            args = window + ["--xvfb-pid", display.server.pid, "--session-record", session_file,
                             "--srf-bin-dir", "/srf-bin"]
            if kind == "focus":
                shim = WORK / "srf-shim"
                shim.mkdir()
                executable = shim / "srf-host-window"
                executable.write_text(
                    '#!/bin/sh\n[ "$#" -eq 1 ] && [ "$1" = list ] || exit 1\n'
                    f'exec /srf-bin/srf-host-window list --display {display.name} '
                    f'--xauthority {display.env["XAUTHORITY"]}\n')
                executable.chmod(0o700)
                args[args.index("/srf-bin")] = shim
                args += ["--fixture-port", port]
            else:
                args += ["--xauthority", display.env["XAUTHORITY"], "--xvfb-start-ticks", display.server.start,
                         "--window-start-ticks", browser.child.start,
                         "--session-sha256", sha256(session_file.read_bytes()).hexdigest()]
        else:
            args = common + ["--binding", write_record("binding.json", binding)]
            if kind == "pool":
                args += ["--profile-dir", browser.profile, "--browser-pid", browser.child.pid,
                         "--max-xwd-bytes", MAX_XWD_BYTES]
            else:
                args += ["--max-frame-bytes", MAX_XWD_BYTES]
                if kind in {"demand", "lifecycle", "receipts"}:
                    args += ["--agent-demand", write_record("agent.json", demand_for(binding, "agent")),
                             "--synthetic-profile", browser.profile, "--browser-pid", browser.child.pid]
                if kind == "demand":
                    args += ["--recording-demand", write_record("recording.json",
                                                                demand_for(binding, "recording", owner="qd-recorder"))]
                if kind in {"lifecycle", "receipts"}:
                    from driver import enter_runner

                    args += ["--session", write_record("session.json", session_for(browser))]
                    if kind == "receipts":
                        lineage = WORK / "lineage"
                        lineage.mkdir(mode=0o700)
                        args += ["--lineage-dir", lineage]
                    enter_runner()
        call_entry(script, args)

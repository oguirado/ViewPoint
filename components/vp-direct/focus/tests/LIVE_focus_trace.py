"""LIVE: qualify private S3 dismiss/summon and U1b acts on the fixture chat.

Supply a current, trusted vp.session/v1 export for a dedicated fixture browser
on an existing private Xvfb, with a separate active window and two workspaces.
Keep the fixture chat tab open on the active workspace before starting. Run in
the writer's capped scope. This script does not start a browser or X server.
"""

import argparse
from contextlib import nullcontext
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4

DIRECT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(DIRECT), str(DIRECT.parent / "wender-agent")]

from channel.cdp import CdpBackend
from focus import FocusMonitor, FocusRefused
from focus.x11 import X11FocusSource
from schemas.validation import validate_contract
from window import WindowController
from window.x11 import WmctrlBackend, _cardinal, _window_rows, _xid


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


class PrivatePresentation(WmctrlBackend):
    """Use the S3 workspace machinery with its display pinned to the test server."""

    def __init__(self, source, bin_dir):
        self._source = source
        super().__init__(bin_dir, run=self._run_private, pointer=source.pointer)

    def _run_private(self, argv, *, env, **kwargs):
        scoped = {"PATH": os.defpath, "LC_ALL": "C", "DISPLAY": self._source.display}
        if "XAUTHORITY" in env:
            scoped["XAUTHORITY"] = env["XAUTHORITY"]
        return subprocess.run(argv, env=scoped, **kwargs)

    def verify_window(self, binding):
        require(binding.display == f"xpra:{self._source.display[1:]}", "private session display changed")
        rows = _window_rows(self._command(self._host_tool, "list"))
        matches = [row for row in rows if _xid(row.get("xid")) == binding.xid]
        witnesses = [row for row in rows if row.get("witness") == binding.witness]
        require(len(matches) == 1 and witnesses == matches, "fixture XID and witness do not match")
        output = self._command("xprop", "-id", hex(binding.xid), "_NET_WM_PID")
        require(_cardinal(output, "_NET_WM_PID") == binding.pid, "fixture browser PID changed")


def fixture_url(port):
    expected = (DIRECT / "fixtures" / "site" / "chat.html").read_bytes()
    conn = HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        conn.request("GET", "/chat.html")
        reply = conn.getresponse()
        require(reply.status == 200 and reply.getheader("X-Fixture-Server") == "vp-direct"
                and reply.read(len(expected) + 1) == expected, "fixture page could not be verified")
    finally:
        conn.close()
    return f"http://127.0.0.1:{port}/chat.html"


def verify_browser_listener(session):
    port = session["cdp_port"]
    require(type(port) is int, "fixture CDP port is unavailable")
    rows = subprocess.check_output(["ss", "-H", "-ltnp", f"sport = :{port}"], text=True, timeout=3)
    sockets = rows.splitlines()
    require(len(sockets) == 1 and sockets[0].split()[3] == f"127.0.0.1:{port}"
            and re.search(rf"\bpid={session['window']['pid']}(?:,|\))", sockets[0]),
            "fixture CDP listener does not match the private browser")


def run(args):
    display = args.display
    require(re.fullmatch(r":[1-9][0-9]*", display), "a private local display is required")
    require(args.xvfb_pid > 0 and (Path("/proc") / str(args.xvfb_pid) / "exe")
            .resolve(strict=True).name == "Xvfb", "expected a private Xvfb PID")

    def read_session(_):
        return json.loads(args.session_record.read_text(encoding="utf-8"))

    session = read_session(None)
    validate_contract("vp.session/v1", session)
    require(session["display"] == f"xpra:{display[1:]}" and session["site_group"] == "fixture"
            and session["state"] == "AGENT" and session["lease"]["holder"],
            "the trusted session export must identify an exclusive private fixture turn")
    page = fixture_url(args.fixture_port)
    session_id = session["session_id"]
    source = X11FocusSource(display, args.xvfb_pid)
    presentation = WindowController(
        read_session, {"wmctrl": PrivatePresentation(source, str(args.srf_bin_dir.resolve(strict=True)))},
    )
    act_request = None

    def check_authority(request):
        require(request is act_request and read_session(session_id) == session,
                "fixture act is outside the authorized request")
        verify_browser_listener(session)
        return nullcontext()

    backend = CdpBackend(session_id, read_session, effect_guard=check_authority)
    monitor = FocusMonitor(source)

    def request(op, **fields):
        return {
            "schema": "vp.channel.req/v1", "request_id": uuid4().hex, "op": op, "backend": "cdp",
            "session_id": session_id, "capsule_id": session["capsule_id"],
            "incarnation": session["incarnation"], "authority_epoch": session["lease"]["authority_epoch"],
            "dry_run": False, "risk_class": "preparar", "require_vision_witness": False,
            "target": {"cdp_target_id": session["cdp_target_id"]}, **fields,
        }

    def prompt(nodes):
        return [node for node in nodes if node["role"] == "textbox" and node["name"] == "Prompt"]

    def sequence(first):
        nonlocal act_request
        require(read_session(session_id) == session, "fixture session changed")
        dismissed = presentation.vp_window_dismiss(session_id)
        summoned = presentation.vp_window_summon(session_id)
        require(dismissed["moved"] and summoned["moved"]
                and dismissed["to_ws"] == summoned["from_ws"]
                and dismissed["from_ws"] == summoned["to_ws"], "S3 did not round-trip")
        verify_browser_listener(session)
        if first:
            backend.attach_target(request("attach"))
        observation = backend.observe(request("observe"))
        require(observation.document[2] == page, "CDP target is outside the verified fixture")
        nodes = prompt(observation.nodes)
        require(len(nodes) == 1, "fixture prompt absent or ambiguous")
        node = nodes[0]
        value = "A3 fixture" if node["value_len"] == 2 else "A3"

        def verify(nodes_after):
            current = prompt(nodes_after)
            return len(current) == 1 and current[0]["value_len"] == len(value)

        act_request = request("act", action="type", input_text=value, observation_id=observation.id)
        act_request["target"]["ax_stable_sha256"] = observation.ax_stable_sha256
        reply = backend.act(act_request, node_id=node["id"], verify=verify)
        validate_contract("vp.channel.reply/v1", reply)
        require(reply["ok"], reply["code"])

    try:
        for index in range(100):  # [TARGET] A3 live sequence count
            _, trace = monitor.run_passive(args.surface_id, lambda: sequence(index == 0), qualified=True)
            print(json.dumps(trace, sort_keys=True), flush=True)
    except FocusRefused as error:
        if error.trace is not None:
            print(json.dumps(error.trace, sort_keys=True), flush=True)
        raise SystemExit(f"{error.code} {error}") from None
    finally:
        backend.detach()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--display", required=True)
    parser.add_argument("--xvfb-pid", type=int, required=True)
    parser.add_argument("--session-record", type=Path, required=True)
    parser.add_argument("--surface-id", required=True)
    parser.add_argument("--fixture-port", type=int, choices=range(1, 65536), metavar="PORT", required=True)
    parser.add_argument("--srf-bin-dir", type=Path, required=True)
    run(parser.parse_args())

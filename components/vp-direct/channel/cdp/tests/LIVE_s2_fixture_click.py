"""LIVE: Check passive CDP attach, background creation and semantic readback.

Run in the capped scope with an existing, dedicated fixture browser on a private
Xvfb display and an active window distinct from the browser. Pass the trusted
vp.session/v1 export on stdin. The operator must reserve that fixture session
through detach and open the fixture index before the check. The check leaves
the newly created blank tab available for inspection.
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


VP_DIRECT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(VP_DIRECT), str(VP_DIRECT.parent / "wender-agent")]

from channel.cdp import CdpBackend, Refused
from focus import FocusMonitor, FocusRefused
from focus.x11 import X11FocusSource
from schemas.validation import validate_contract


def require(condition, reason):
    if not condition:
        raise SystemExit(reason)


def verify_fixture(port):
    http = HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        for page in ("index.html", "chat.html"):
            expected = (VP_DIRECT / "fixtures" / "site" / page).read_bytes()
            http.request("GET", f"/{page}")
            reply = http.getresponse()
            require(reply.status == 200 and reply.getheader("X-Fixture-Server") == "vp-direct"
                    and reply.read(len(expected) + 1) == expected, "fixture page could not be verified")
    finally:
        http.close()


def verify_browser(session, display):
    window = session["window"]
    port = session["cdp_port"]
    require(type(port) is int, "fixture CDP port is unavailable")
    rows = subprocess.check_output(["ss", "-H", "-ltnp", f"sport = :{port}"], text=True, timeout=3).splitlines()
    require(len(rows) == 1 and len(rows[0].split()) > 3 and rows[0].split()[3] == f"127.0.0.1:{port}"
            and re.search(rf"\bpid={window['pid']}(?:,|\))", rows[0]),
            "fixture CDP listener does not match the private browser")
    env = dict(os.environ, DISPLAY=display, LC_ALL="C")
    xid = int(window["xid"], 16)
    value = subprocess.check_output(["xprop", "-id", hex(xid), "_NET_WM_PID"], env=env, text=True, timeout=3)
    require(value.strip() == f"_NET_WM_PID(CARDINAL) = {window['pid']}", "fixture browser window changed")
    value = subprocess.check_output(["xprop", "-root", "_NET_ACTIVE_WINDOW"], env=env, text=True, timeout=3)
    active = re.fullmatch(r"_NET_ACTIVE_WINDOW\(WINDOW\): window id # (0x[0-9a-f]+)", value.strip())
    require(active is not None and int(active[1], 16) not in {0, xid}, "a separate active fixture window is required")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-port", required=True, type=int, choices=range(1, 65536), metavar="PORT")
    parser.add_argument("--display", required=True)
    parser.add_argument("--xvfb-pid", required=True, type=int)
    parser.add_argument("--surface-id", required=True)
    args = parser.parse_args()
    source = X11FocusSource(args.display, args.xvfb_pid)
    require((Path("/proc") / str(args.xvfb_pid) / "exe").resolve(strict=True).name == "Xvfb",
            "a private Xvfb PID is required")
    session = json.load(sys.stdin)
    validate_contract("vp.session/v1", session)
    require(session["state"] == "AGENT" and session["site_group"] == "fixture"
            and session["display"] == f"xpra:{args.display[1:]}" and session["lease"]["holder"],
            "the session must identify an exclusive private fixture turn")
    verify_fixture(args.fixture_port)

    fixture = f"http://127.0.0.1:{args.fixture_port}"
    expected_page = f"{fixture}/chat.html"
    effect_request = None

    def guard(request):
        require(request is effect_request, "LIVE permits only the background tab and fixture link click")
        verify_browser(session, args.display)
        return nullcontext()

    backend = CdpBackend(session["session_id"], lambda _: session, effect_guard=guard)

    def request(op, **fields):
        return {
            "schema": "vp.channel.req/v1", "request_id": uuid4().hex, "op": op, "backend": "cdp",
            "session_id": session["session_id"], "capsule_id": session["capsule_id"],
            "incarnation": session["incarnation"], "authority_epoch": session["lease"]["authority_epoch"],
            "dry_run": False, "risk_class": "leer", "require_vision_witness": False,
            "target": {"cdp_target_id": session["cdp_target_id"]}, **fields,
        }

    def chat_heading(nodes):
        return any(node["role"] == "heading" and node["name"] == "Fixture chat" for node in nodes)

    def sequence():
        nonlocal effect_request
        verify_browser(session, args.display)
        backend.attach_target(request("attach"))
        initial = backend.observe(request("observe"))
        require(initial.document[2] in {f"{fixture}/", f"{fixture}/index.html"},
                "attach changed the document or the target is outside the fixture index")
        effect_request = request("attach")
        created = backend.create_target(effect_request)
        require(created != initial.target_id, "creation did not return a distinct target")

        links = [node for node in initial.nodes if node["role"] == "link" and node["name"] == "chat.html"]
        require(len(links) == 1, "the fixture chat link was absent or ambiguous")
        effect_request = request("act", action="click", observation_id=initial.id)
        effect_request["target"]["ax_stable_sha256"] = initial.ax_stable_sha256
        require(backend.validate_observation(effect_request).document == initial.document,
                "background creation changed the registered document")
        clicked = backend.act(
            effect_request, node_id=links[0]["id"], verify=chat_heading, expected_document_url=expected_page,
        )
        require(clicked["ok"], clicked["code"])
        try:
            backend.validate_observation(effect_request)
        except Refused as exc:
            require(exc.code == "identity_mismatch", exc.code)
        else:
            raise SystemExit("the retired index observation was accepted")
        asserted = backend.assert_observation(
            request("assert"), verify=chat_heading, expected_document_url=expected_page,
        )
        validate_contract("vp.channel.reply/v1", asserted)
        require(asserted["ok"], asserted["code"])
        return asserted

    try:
        reply, trace = FocusMonitor(source).run_passive(args.surface_id, sequence, qualified=True)
        print(json.dumps(trace, sort_keys=True))
        print(json.dumps(reply, sort_keys=True))
    except FocusRefused as exc:
        if exc.trace is not None:
            print(json.dumps(exc.trace, sort_keys=True))
        raise SystemExit(f"{exc.code} {exc}") from None
    except Refused as exc:
        raise SystemExit(exc.code) from None
    finally:
        backend.detach()


if __name__ == "__main__":
    main()

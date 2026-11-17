"""LIVE: Check CDP attach, observe, click and assert on an existing S2 session.

Run after the operator opens the fixture index in the registered S2 target.
Pass the current frozen vp.session/v1 record on stdin and the fixture server's
port with --fixture-port. The session must already be in AGENT mode and reserved
for this check through detach.
"""

import argparse
from contextlib import nullcontext
import json
from pathlib import Path
import sys
from uuid import uuid4


VP_DIRECT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(VP_DIRECT), str(VP_DIRECT.parent / "wender-agent")]

from channel.cdp import CdpBackend
from schemas.validation import validate_contract


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-port", required=True, type=int)
    args = parser.parse_args()
    if not 1 <= args.fixture_port <= 65535:
        parser.error("fixture port outside the TCP range")
    session = json.load(sys.stdin)
    validate_contract("vp.session/v1", session)
    if session["state"] != "AGENT" or session["site_group"] != "fixture":
        raise SystemExit("The registered S2 fixture session needs an exclusive AGENT turn")

    fixture = f"http://127.0.0.1:{args.fixture_port}"
    expected_page = f"{fixture}/chat.html"
    click_request = None

    def guard(request):
        if request is not click_request:
            raise RuntimeError("LIVE permits only the fixture link click")
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

    try:
        backend.attach_target(request("attach"))
        initial = backend.observe(request("observe"))
        if initial.document[2] not in {f"{fixture}/", f"{fixture}/index.html"}:
            raise SystemExit("The registered target is outside the fixture index")
        links = [node for node in initial.nodes if node["role"] == "link" and node["name"] == "chat.html"]
        if len(links) != 1:
            raise SystemExit("The fixture chat link was absent or ambiguous")
        click_request = request("act", action="click", observation_id=initial.id)
        click_request["target"]["ax_stable_sha256"] = initial.ax_stable_sha256
        clicked = backend.act(
            click_request, node_id=links[0]["id"], verify=chat_heading, expected_document_url=expected_page,
        )
        if not clicked["ok"]:
            raise SystemExit(clicked["code"])
        asserted = backend.assert_observation(
            request("assert"), verify=chat_heading, expected_document_url=expected_page,
        )
        validate_contract("vp.channel.reply/v1", asserted)
        if not asserted["ok"]:
            raise SystemExit(asserted["code"])
        print(json.dumps(asserted, sort_keys=True))
    finally:
        backend.detach()


if __name__ == "__main__":
    main()

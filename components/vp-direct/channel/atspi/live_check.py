"""LIVE: check one initially unchecked checkbox in an operator-opened xpra fixture.

Use an existing private xpra fixture and its provisioned accessibility bus.
Supply the trusted live vp.session/v1 export, associated AT-SPI window owner
and object path, surface ID, and private X server PID. Run in the capped scope.
"""

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4


DIRECT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(DIRECT), str(DIRECT.parent / "wender-agent")]

from channel.atspi import AtspiExecutor, ObjectRef, PrivateIdentity
from focus import FocusMonitor
from focus.x11 import X11FocusSource
from jsonschema import ValidationError
from schemas.validation import validate_contract
from wender.perceive.ax import normalize_name


def _request(session, op, **fields):
    return {"schema": "vp.channel.req/v1", "request_id": uuid4().hex, "op": op,
            "backend": "atspi", "session_id": session["session_id"], "capsule_id": session["capsule_id"],
            "incarnation": session["incarnation"], "authority_epoch": session["lease"]["authority_epoch"],
            "target": {"window_id": session["window"]["xid"]}, "dry_run": False,
            "risk_class": "preparar", "require_vision_witness": False, **fields}


def _checkbox(snapshot, name):
    if snapshot is None or snapshot["atspi_capture"] != "polling_only":
        raise ValueError("Missing complete polling evidence")
    result = [row for row in snapshot["nodes"] if row["role"] == "checkbox" and row["name"] == name]
    if len(result) != 1:
        raise ValueError("Fixture checkbox must resolve uniquely")
    return result[0]


class _FixtureFocusMonitor(FocusMonitor):
    def run_passive(self, surface_id, operation, *, qualified):
        result, self.trace = super().run_passive(surface_id, operation, qualified=qualified)
        return result, self.trace


def _run(args):
    def load_session():
        value = json.loads(args.session.read_text(encoding="utf-8"))
        validate_contract("vp.session/v1", value)
        return value

    session = load_session()
    window = session.get("window", {})
    xid = window.get("xid", "")
    if (not session["display"].startswith("xpra:") or session["state"] != "AGENT"
            or session["site_group"] != "fixture"
            or session.get("backend") != "atspi" or not re.fullmatch(r"0x[0-9a-fA-F]+", xid)
            or not session.get("capsule_id") or re.fullmatch(r"xpra:0+", session["display"])):
        raise ValueError("A registered private xpra fixture with a bound window and agent lease is required")

    target = ObjectRef(args.owner, args.object_path)
    xdisplay = ":" + session["display"].split(":", 1)[1]

    def identity(session_id):
        if session_id != session["session_id"] or load_session() != session:
            return None
        result = subprocess.run(["xprop", "-display", xdisplay, "-id", xid, "-notype", "_NET_WM_PID"],
                                capture_output=True, text=True, timeout=2, check=False)
        pid = re.fullmatch(r"_NET_WM_PID\s*=\s*(\d+)\s*", result.stdout)
        if result.returncode or not pid or int(pid[1]) != window["pid"]:
            return None
        return PrivateIdentity(session["display"], window["pid"], xid, window["witness"], session["incarnation"],
                               session["lease"]["authority_epoch"], target, True, frozenset({"click"}))

    monitor = _FixtureFocusMonitor(X11FocusSource(xdisplay, args.x_server_pid))
    executor = AtspiExecutor(identity, session_env={"AT_SPI_BUS_ADDRESS": args.atspi_bus_address},
                             focus_monitor_for=lambda _: (args.surface_id, monitor))
    try:
        before_reply, before = executor.observe(_request(session, "observe"), session=session)
        if not before_reply["ok"]:
            raise ValueError(before_reply["code"])
        name = normalize_name(args.checkbox)
        prior = _checkbox(before, name)
        if dict(prior["attrs"]).get("checked") != "false":
            raise ValueError("The fixture checkbox must start unchecked")
        reply, after = executor.act(_request(session, "act", action="click", observation_id=before["observation_id"]),
                                    session=session, node_id=prior["id"])
        if not reply["ok"]:
            raise ValueError(reply["code"])
        current = _checkbox(after, name)
        if (after["observation_id"] == before["observation_id"] or after["ax_sha256"] == before["ax_sha256"]
                or current["id"] != prior["id"] or dict(current["attrs"]).get("checked") != "true"):
            raise ValueError("Post-action polling did not verify the requested checkbox change")
        print(json.dumps({"code": reply["code"], "atspi_capture": after["atspi_capture"],
                          "before_ax_sha256": before["ax_sha256"], "after_ax_sha256": after["ax_sha256"],
                          "focus_trace": monitor.trace},
                         sort_keys=True))
    finally:
        executor.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    parser.add_argument("owner", help="Associated unique AT-SPI bus owner")
    parser.add_argument("object_path", help="Associated AT-SPI window object path")
    parser.add_argument("checkbox", help="Accessible name of the synthetic fixture checkbox")
    parser.add_argument("--atspi-bus-address", required=True, help="Provisioned private fixture bus address")
    parser.add_argument("--surface-id", required=True, help="Trusted private fixture surface ID")
    parser.add_argument("--x-server-pid", required=True, type=int, help="Private X server PID")
    try:
        _run(parser.parse_args())
    except (OSError, ValueError, ValidationError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

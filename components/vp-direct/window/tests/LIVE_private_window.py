"""Qualify S3 workspace moves under a continuous A3 trace on private Xvfb.

Supply an existing private display with a window manager and two workspaces.
Use an owned fixture app, a separate active window, and a trusted fixture
session export. Take both process start times and the export digest from the
provisioning receipt. Pass the dedicated fixture Xauthority file explicitly.
Run in the capped scope. The runner does not launch or stop processes.
"""

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
from stat import S_ISREG
import sys

from jsonschema import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from focus import CoverageLost, FocusMonitor, FocusRefused
from focus.x11 import X11FocusSource
from schemas.validation import validate_contract
from window import WindowController, WindowRefused, _UNAVAILABLE
from window.x11 import WmctrlBackend, _owned_process_start


def run(args):
    def read_session(_):
        try:
            raw = args.session_record.read_bytes()
        except OSError as error:
            raise WindowRefused(_UNAVAILABLE, "fixture session export is unavailable") from error
        if sha256(raw).hexdigest() != args.session_sha256:
            raise WindowRefused(_UNAVAILABLE, "fixture session export changed")
        return json.loads(raw)

    session = read_session(None)
    validate_contract("vp.session/v1", session)
    if (session["display"] != f"xpra:{args.display[1:]}" or session["site_group"] != "fixture"
            or session["state"] != "AGENT" or not session["lease"]["holder"]
            or "window" not in session or "incarnation" not in session):
        raise ValueError("a registered agent window on the private fixture display is required")
    window_pid = session["window"]["pid"]

    authority = args.xauthority.lstat()
    if not args.xauthority.is_absolute() or not S_ISREG(authority.st_mode) or authority.st_uid != os.geteuid():
        raise ValueError("an absolute, owned fixture Xauthority file is required")
    source = X11FocusSource(args.display, args.xvfb_pid)
    backend = WmctrlBackend(
        args.srf_bin_dir, display=args.display, server_pid=args.xvfb_pid,
        server_start_ticks=args.xvfb_start_ticks, xauthority=args.xauthority, pointer=source.pointer,
    )
    window = WindowController(read_session, {"wmctrl": backend})
    monitor = FocusMonitor(source)

    def preflight():
        read_session(session["session_id"])
        if _owned_process_start(window_pid) != args.window_start_ticks:
            raise ValueError("fixture app process identity changed")
        backend.check_private_display()

    def round_trip():
        dismissed = window.vp_window_dismiss(session["session_id"])
        summoned = window.vp_window_summon(session["session_id"])
        if (not dismissed["moved"] or not summoned["moved"]
                or dismissed["from_ws"] != summoned["to_ws"]
                or dismissed["to_ws"] != summoned["from_ws"]):
            raise ValueError("fixture window did not round-trip between workspaces")
        return {"dismiss": dismissed, "summon": summoned}

    # X11FocusSource opens its own connection in this standalone process.
    os.environ["XAUTHORITY"] = str(args.xauthority)
    for _ in range(100):  # [TARGET] A3 passive sequences
        preflight()
        result, trace = monitor.run_passive(args.surface_id, round_trip, qualified=True)
        print(json.dumps({**result, "trace": trace}, sort_keys=True), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--display", required=True)
    parser.add_argument("--xvfb-pid", type=int, required=True)
    parser.add_argument("--xvfb-start-ticks", type=int, required=True)
    parser.add_argument("--window-start-ticks", type=int, required=True)
    parser.add_argument("--session-record", type=Path, required=True)
    parser.add_argument("--session-sha256", required=True)
    parser.add_argument("--xauthority", type=Path, required=True)
    parser.add_argument("--srf-bin-dir", type=Path, required=True)
    parser.add_argument("--surface-id", required=True)
    try:
        run(parser.parse_args())
    except FocusRefused as error:
        print(json.dumps({"code": error.code, "reason": str(error), "trace": error.trace}),
              file=sys.stderr)
        return 1
    except (CoverageLost, OSError, ValueError, ValidationError, WindowRefused) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

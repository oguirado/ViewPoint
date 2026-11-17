"""LIVE: in the operator session, dismiss then passively summon a registered window.

Supply a vp.session/v1 JSON record exported from the trusted live registry.
Start with the target on the operator workspace and type in another active
window, keeping the pointer still while the script runs.
"""

import argparse
import json
import os
from pathlib import Path
import sys

# Isolated Python omits the script directory from sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from window import WindowController, WindowRefused
from window.x11 import WmctrlBackend


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_id")
    parser.add_argument("session_record", type=Path, help="trusted live registry export")
    args = parser.parse_args()

    def get_session(session_id):
        if session_id == args.session_id:
            return json.loads(args.session_record.read_text(encoding="utf-8"))
        return None

    try:
        backend = WmctrlBackend(os.environ.get("MV_SRF_BIN_DIR"))
        controller = WindowController(get_session, {"wmctrl": backend})
        initial_session = get_session(args.session_id)
        initial_desktop = backend.desktop()
        binding = controller._registered(args.session_id)[0]
        if backend.workspace(binding) != initial_desktop.workspace:
            raise ValueError("start with the target on the operator's workspace")
        dismissed = controller.vp_window_dismiss(args.session_id)
        if not dismissed["moved"] or dismissed["from_ws"] != initial_desktop.workspace:
            raise ValueError("start with the target on the operator's workspace")
        summoned = controller.vp_window_summon(args.session_id)
        if (not summoned["moved"] or summoned["from_ws"] != dismissed["to_ws"]
                or summoned["to_ws"] != dismissed["from_ws"]):
            raise ValueError("the two workspace moves did not round-trip")
        if backend.desktop() != initial_desktop or get_session(args.session_id) != initial_session:
            raise ValueError("operator state or the registered session changed")
    except WindowRefused as error:
        print(json.dumps({"code": error.code, "reason": str(error)}), file=sys.stderr)
        return 1
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1

    print(json.dumps({"dismiss": dismissed, "summon": summoned}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

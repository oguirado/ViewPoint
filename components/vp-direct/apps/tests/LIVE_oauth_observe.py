"""LIVE operator check for Codex Desktop OAuth and one scoped CDP observation.

Use a fresh dedicated browser profile and a trusted app capsule binding.
Close an existing Codex Desktop instance yourself before starting. During the
check, switch to the agent workspace without moving the app, perform login
manually and verify the browser uses the dedicated profile. The observation
checks target identity and emits no page content.
"""

import argparse
import json
from pathlib import Path
import sys

DIRECT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(DIRECT), str(DIRECT.parent / "wender-agent")]

from apps import AppLauncher, AppSessionRegistry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operator-run", action="store_true", required=True)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--capsule-id", required=True)
    parser.add_argument("--min-interval-ms", type=int, required=True)
    parser.add_argument("--data-home", type=Path, required=True)
    options = parser.parse_args()

    app = AppLauncher("Codex Desktop", options.profile_id, "google-chrome", options.capsule_id)
    key = ("codex_oauth_check", app.browser)
    registry = AppSessionRegistry({key: app}, data_home=options.data_home)
    session = registry.vp_session_open({
        "kind": "app", "site_group": key[0], "browser": key[1],
        "pace": {"min_interval_ms": options.min_interval_ms}, "risk_ceiling": "leer",
    })
    sid = session["session_id"]
    try:
        print(f"Complete the app login manually and verify the browser profile is {options.profile_id}.")
        if input("Type PROFILE_CONFIRMED after observing the OAuth browser window: ") != "PROFILE_CONFIRMED":
            raise SystemExit("OAuth browser profile was not confirmed")
        registry.probe_owned_login_target(sid)
    finally:
        registry.vp_session_close(sid)
    print(json.dumps({"oauth_profile": "operator_confirmed", "target_observation": "PASS"}))


if __name__ == "__main__":
    main()

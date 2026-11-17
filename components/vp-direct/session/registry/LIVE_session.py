"""LIVE: open, list and close a dedicated Chrome session on the F1 fixture.

Run in the operator's X11 session with the fixture server listening on loopback.
Use a dedicated test profile ID and the operator-provisioned fixture capsule ID.
"""

import argparse
from http.client import HTTPConnection
import json
from pathlib import Path
import sys
from time import monotonic, sleep

DIRECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(DIRECT))
sys.path.insert(0, str(DIRECT.parent / "wender-agent"))

from session.launch import LaunchError, Launcher
from session.registry import LaunchNotRunning, OwnedInstance, SessionRegistry, SiteProfile
from wender.perceive.cdp_port import CdpPort


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-port", type=int, required=True)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--capsule-id", required=True)
    parser.add_argument("--data-home", type=Path)
    options = parser.parse_args()
    if not 1 <= options.fixture_port <= 65535:
        parser.error("the fixture port must be a valid TCP port")

    address = "127.0.0.1"
    fixture_path = "/chat.html"
    fixture_url = f"http://{address}:{options.fixture_port}{fixture_path}"
    connection = HTTPConnection(address, options.fixture_port, timeout=5)
    try:
        connection.request("GET", fixture_path)
        response = connection.getresponse()
        require(response.status == 200 and response.getheader("X-Fixture-Server") == "vp-direct",
                "The loopback fixture did not answer")
    finally:
        connection.close()

    site = SiteProfile(options.profile_id, Path("/opt/google/chrome/chrome"), ":0", "cdp", True)

    def launch(spec, profile_directory):
        try:
            child = Launcher(profile_directory.parent).launch(spec.executable, spec.profile_id)
        except LaunchError as error:
            if error.code == "PORT_BUSY":
                raise LaunchNotRunning(error.code, str(error)) from error
            raise
        try:
            with CdpPort(host=address, port=child.cdp_port, timeout=5) as cdp:
                target = cdp.send("Target.createTarget", {"background": True, "url": fixture_url})
                target_id = target["targetId"]
                deadline = monotonic() + 5
                while True:
                    observed = cdp.send("Target.getTargetInfo", {"targetId": target_id})["targetInfo"]
                    if observed.get("targetId") == target_id and observed.get("url") == fixture_url:
                        break
                    require(monotonic() < deadline, "Chrome did not report the fixture target")
                    sleep(0.05)
        except Exception as error:
            child.stop()
            raise LaunchNotRunning("host_window_unavailable", "Fixture target could not be verified") from error
        return OwnedInstance(child.window_binding, options.capsule_id, child.witness, child.stop,
                             cdp_port=child.cdp_port, cdp_target_id=target_id)

    registry = SessionRegistry({("fixture_chat", "google-chrome"): site}, launch,
                               data_home=options.data_home)
    opened = registry.vp_session_open({
        "site_group": "fixture_chat", "browser": "google-chrome", "kind": "browser",
        "pace": {"min_interval_ms": 500}, "risk_ceiling": "leer",
    })
    sid = opened["session_id"]
    try:
        listed = registry.vp_session_list()
        require({"session_id": sid, "state": "LOGIN_PENDING"} in listed and
                all(row.keys() == {"session_id", "state"} for row in listed),
                "The fixture session was not listed in human login mode")
        profile = registry.profile_root / options.profile_id
        inode = profile.lstat().st_ino
    finally:
        closed = registry.vp_session_close(sid)
    require(closed == {"session_id": sid, "state": "CLOSED"} and
            registry.get_session(sid) is None and profile.lstat().st_ino == inode,
            "The fixture session did not close while retaining its profile")
    print(json.dumps({"session_id": sid, "opened": "LOGIN_PENDING", "closed": "CLOSED"}))


if __name__ == "__main__":
    main()

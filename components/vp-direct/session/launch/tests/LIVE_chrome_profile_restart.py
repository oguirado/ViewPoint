"""LIVE: Operator-driven Chrome launch, synthetic sign-in and restart check.

Run from the operator's X11 session on a workspace other than the agent
workspace. Supply a newly provisioned, exclusively reserved S1 profile.
The check retains the profile and closes only its own browser children.
"""

import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
from queue import Empty, Queue
import sys
from threading import Thread

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from launch import Launcher


PAGE = b"""<!DOCTYPE html>
<html lang="en">
<meta charset="utf-8">
<title>S2 synthetic sign-in</title>
<h1>S2 synthetic sign-in</h1>
<p id="state"></p>
<button id="sign-in">Sign in to the local fixture</button>
<script>
const state = document.querySelector("#state");
const button = document.querySelector("#sign-in");
const storageName = "vp-s2-fixture";
if (localStorage.getItem(storageName) === "yes") {
  state.textContent = "Synthetic sign-in retained after restart";
  button.disabled = true;
  fetch("/retained", {method: "POST"});
} else {
  state.textContent = "No synthetic sign-in yet";
  button.onclick = () => {
    localStorage.setItem(storageName, "yes");
    state.textContent = "Synthetic sign-in saved";
    button.disabled = true;
    fetch("/signed-in", {method: "POST"});
  };
}
</script>
</html>
"""


class Fixture(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def do_POST(self):
        if self.path not in ("/signed-in", "/retained"):
            self.send_error(404)
            return
        self.server.events.put(self.path)
        self.send_response(204)
        self.end_headers()

    def log_message(self, _format, *args):
        pass


def expect(server, event, timeout):
    try:
        received = server.events.get(timeout=timeout)
    except Empty:
        raise SystemExit(f"FAIL: no browser witness for {event}") from None
    if received != event:
        raise SystemExit(f"FAIL: unexpected browser witness {received}")


def run_browser(launcher, profile_id, surface_id, server, event, instruction, timeout):
    browser = launcher.launch("/opt/google/chrome/chrome", profile_id)
    try:
        print(json.dumps({
            "focus_take_observed": browser.focus_take_observed,
            "focus_trace": browser.focus_trace(surface_id),
            "window": browser.window_binding,
        }, sort_keys=True), flush=True)
        print(instruction.format(port=server.server_port), flush=True)
        expect(server, event, timeout)
        return browser.witness
    finally:
        browser.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-root", required=True, type=Path)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--surface-id", required=True)
    parser.add_argument("--operator-timeout-s", type=int, default=180)
    args = parser.parse_args()
    if args.operator_timeout_s < 1 or not args.surface_id:
        parser.error("a positive timeout and a surface ID are required")
    launcher = Launcher(args.profile_root)
    with HTTPServer(("127.0.0.1", 0), Fixture) as server:
        server.events = Queue()
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            first = run_browser(
                launcher, args.profile_id, args.surface_id, server, "/signed-in",
                "Switch to the agent workspace, open http://127.0.0.1:{port}/ in the "
                "new browser, and click the fixture sign-in button once.",
                args.operator_timeout_s,
            )
            input("Return to the operator workspace, then press Enter to relaunch. ")
            restarted = run_browser(
                launcher, args.profile_id, args.surface_id, server, "/retained",
                "Switch to the agent workspace. Open http://127.0.0.1:{port}/ in the "
                "relaunched browser if it has not already loaded the fixture.",
                args.operator_timeout_s,
            )
            if first == restarted:
                raise SystemExit("FAIL: restart reused the original window witness")
            print("PASS: synthetic sign-in persisted across an owned browser restart")
        finally:
            server.shutdown()
            worker.join()


if __name__ == "__main__":
    main()

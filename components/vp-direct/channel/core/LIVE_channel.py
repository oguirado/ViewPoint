#!/usr/bin/env python3
"""LIVE: attach, observe, click once, and assert against a provisioned fixture.

Run with python3 -I -B. Supply a trusted driver exporting open_fixture(backend).
It returns (channel, agent_id, request_for), bound to one local fixture whose
click changes the AX state. request_for(op) builds the admitted envelope from
that fixture's current observation and binds the corresponding AX predicate.
"""

import argparse
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("driver", type=Path, help="Trusted local fixture driver")
    parser.add_argument("--backend", choices=("cdp", "atspi"), required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    spec = spec_from_file_location("vp_live_channel_driver", args.driver.resolve(strict=True))
    driver = module_from_spec(spec)
    spec.loader.exec_module(driver)
    channel, agent_id, request_for = driver.open_fixture(args.backend)

    def perform(op, request):
        if request["backend"] != args.backend or request["op"] != op or request["dry_run"]:
            raise SystemExit(f"LIVE {op}: wrong fixture request")
        reply = channel.dispatch(request, agent_id=agent_id)
        if reply["ok"] is not True or "fallback_from" in reply:
            raise SystemExit(f"LIVE {op}: {reply['code']}")
        return reply

    perform("attach", request_for("attach"))
    try:
        observed = perform("observe", request_for("observe"))
        click = request_for("act")
        if click["action"] != "click":
            raise SystemExit("LIVE act: expected fixture click")
        acted = perform("act", click)
        if channel.dispatch(click, agent_id=agent_id) != acted:
            raise SystemExit("LIVE act: duplicate receipt mismatch")
        asserted = perform("assert", request_for("assert"))
        before, after = observed.get("ax_sha256"), acted.get("ax_sha256")
        if not before or not after or before == after or asserted.get("ax_sha256") != after:
            raise SystemExit("LIVE assert: missing or inconsistent AX readback")
    finally:
        perform("detach", request_for("detach"))
    print(f"LIVE {args.backend}: PASS")


if __name__ == "__main__":
    main()

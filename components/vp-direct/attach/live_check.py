"""LIVE: attach and detach an already-open operator window.

Run under the capped scope with ``python3 -IB live_check.py --driver DRIVER``.
The trusted operator driver supplies ``prepare()``, returning the configured
AttachmentService, frozen attach arguments, session ID, and broker binding.
The driver must select an already-open window and use live read-only sources.
"""

import argparse
from importlib.util import module_from_spec, spec_from_file_location
import os
from pathlib import Path
import select
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from attach import _xid


def verify(service, args, session_id, binding):
    xid = _xid(args["xid"])
    before = service.current_window(xid)
    if before is None or before.witness != args["witness"]:
        raise RuntimeError("Operator window is absent or its witness changed")

    pidfd = os.pidfd_open(before.pid)
    try:
        poller = select.poll()
        poller.register(pidfd, select.POLLIN)
        if poller.poll(0) or service.current_window(xid) != before:
            raise RuntimeError("Operator window changed before attachment")
        receipt = service.attach(args, session_id=session_id, binding=binding)
        association = service.association(receipt["session_id"])
        detached = service.detach({"op": "detach", "session_id": receipt["session_id"]})
        if association is None or association.window != before:
            raise RuntimeError("Association does not match the original operator window")
        if detached != {"detached": True, "closed_foreign": False}:
            raise RuntimeError("Detach receipt does not show preserved custody")
        if service.association(session_id) is not None:
            raise RuntimeError("Association remains after detach")
        if poller.poll(0) or service.current_window(xid) != before:
            raise RuntimeError("Original operator process or window disappeared during detach")
    finally:
        os.close(pidfd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--driver", required=True, type=Path, help="Trusted operator driver")
    args = parser.parse_args()
    spec = spec_from_file_location("a1_operator_driver", args.driver.resolve())
    if spec is None or spec.loader is None:
        parser.error("driver must be a Python source file")
    driver = module_from_spec(spec)
    sys.modules[spec.name] = driver
    spec.loader.exec_module(driver)
    verify(*driver.prepare())
    print("A1 LIVE PASS")


if __name__ == "__main__":
    main()

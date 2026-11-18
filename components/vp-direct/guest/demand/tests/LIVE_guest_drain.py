"""LIVE operator check for an already admitted private managed guest.

Supply an operator-owned Python provider file. Its prepare() returns the guest
owner's GuestDemand instance, a drain_participants(ticket) callback, and an
observe_requests(ticket, ack_ns) callback. The drain callback withdraws agent
demand and obtains guest, device and surface acknowledgements. The observer
reads continuous agent-attributed guest request evidence from the live path.
Run inside the capped scope after the guest provider is qualified.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import sys
from time import monotonic_ns, sleep

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from guest.demand import MIN_REST_NS


def check(controller, drain_participants, observe_requests):
    if controller.status()["agent"] != "ACTIVE":
        raise RuntimeError("arm the probe on an active private managed guest")
    ticket = controller.begin_drain()
    acks = tuple(drain_participants(ticket))
    ack_ns = controller.acknowledge(ticket, acks)
    deadline_ns = ack_ns + MIN_REST_NS
    while (remaining_ns := deadline_ns - monotonic_ns()) > 0:
        sleep(remaining_ns / 1_000_000_000)
    sample = controller.verify_rest(observe_requests(ticket, ack_ns))
    return {
        "status": "PASS", "reservation_id": ticket.reservation_id,
        "surface_id": ticket.surface_id, "workload_id": ticket.workload_id,
        "incarnation": ticket.incarnation, "authority_epoch": ticket.authority_epoch,
        "participants": sorted(ack.participant for ack in acks),
        "ack_mono_ns": ack_ns, "through_mono_ns": sample.through_mono_ns,
        "agent_requests": sample.agent_requests,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True, type=Path)
    provider_path = parser.parse_args().provider.resolve(strict=True)
    spec = importlib.util.spec_from_file_location("guest_drain_live_provider", provider_path)
    provider = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(provider)
    print(json.dumps(check(*provider.prepare()), sort_keys=True))

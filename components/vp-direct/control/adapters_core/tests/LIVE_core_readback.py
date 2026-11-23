"""LIVE: verify a RAM screenshot through a provisioned C1 and C2 fixture.

Run with an operator-provisioned dispatcher, identity and private fixture.
"""


def run(dispatcher, identity, *, binding_context, request_id):
    context = dict(binding_context, caller=identity.caller, agent_instance=identity.agent_instance)
    request = {
        "schema": "vp.control.req/v1",
        "request_id": request_id,
        "op": "vp_capture_call",
        "context": context,
        "args": {"op": "screenshot", "session_id": context["session_id"]},
        "dry_run": False,
    }
    reply = dispatcher.dispatch_mailbox(request, identity)
    if not reply["ok"]:
        raise RuntimeError(f"Core screenshot refused with {reply['code']}")
    frame = reply["result"]
    if frame["frame_kind"] not in {"cdp_png", "xwd", "xpra"} or not frame.get("ram_ref"):
        raise RuntimeError("Core screenshot did not return a RAM pixel descriptor")
    return reply


if __name__ == "__main__":
    raise SystemExit("Load run() from the operator's provisioned LIVE fixture driver")

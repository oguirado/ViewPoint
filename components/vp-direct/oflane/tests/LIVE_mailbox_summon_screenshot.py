"""LIVE: summon a qualified session, then read a RAM-only screenshot via C4.

Run from an operator fixture driver with a provisioned C4 mailbox, a running
C1 worker, and an independent verifier. Start with the registered window on
another workspace. Both steps require verified terminal replies.
"""

from time import monotonic, sleep


def run(mailbox, verifier_key, *, binding_context, expected_workspace, request_prefix, timeout):
    session_id = binding_context["session_id"]
    context = dict(binding_context, caller=mailbox.identity.caller,
                   agent_instance=mailbox.identity.agent_instance)

    def exchange(suffix, tool, args):
        request_id = f"{request_prefix}-{suffix}"
        request = {
            "schema": "vp.control.req/v1", "request_id": request_id, "op": tool,
            "context": context, "args": args, "dry_run": False,
        }
        mailbox.submit(request)
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            reply = mailbox.completion(request_id, verifier_key)
            if reply is not None:
                if not reply["ok"]:
                    raise RuntimeError(f"{tool} refused with {reply['code']}")
                return reply
            sleep(min(0.05, max(0, deadline - monotonic())))
        mailbox.abort(request_id)
        raise TimeoutError(f"Missing independent terminal verification for {tool}")

    summon = exchange("summon", "vp_window_summon", {"session_id": session_id})
    window = summon["result"]
    if (window.get("moved") is not True or window.get("witness_ok") is not True
            or window.get("to_ws") != expected_workspace):
        raise RuntimeError("Summon lacked qualified workspace move evidence")

    screenshot = exchange("screenshot", "vp_capture_call", {"op": "screenshot", "session_id": session_id})
    frame = screenshot["result"]
    if frame.get("frame_kind") not in ("cdp_png", "xwd", "xpra") or not frame.get("ram_ref"):
        raise RuntimeError("Screenshot lacked a RAM pixel descriptor")

    return summon, screenshot


if __name__ == "__main__":
    raise SystemExit("Load run() from the operator's provisioned LIVE fixture driver")

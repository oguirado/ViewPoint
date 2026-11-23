"""LIVE, operator only. Run from the provisioned S4 fixture driver.

Supply a witnessed session, an already-active operator window, authenticated
C1 exchange, independent transport readback and a qualified one-shot action
with semantic verification. Use a private fixture for rehearsal. The operator
performs login and explicitly initiates return. No credentials enter this probe.
"""
from uuid import uuid4


def run(*, exchange, operator_context, desktop, attachments, human_login, verified_act,
        broker, read_human_receipt):
    """exchange takes a normalized request on the operator lane and returns a terminal reply."""
    context = dict(operator_context)
    if context.get("caller") != "operator":
        raise ValueError("a provisioned operator lane is required")

    def call(op, **args):
        reply = exchange({"schema": "vp.control.req/v1", "request_id": uuid4().hex, "op": op,
                          "context": dict(context), "args": dict(args, session_id=context["session_id"]),
                          "dry_run": False})
        if not reply["ok"]:
            raise RuntimeError(reply["code"])
        return reply["result"]

    saved = desktop().active_xid
    call("vp_human_take", reason="login")
    take = read_human_receipt(context["session_id"])
    if (take["state"] != "HUMAN" or take["epoch_before"] != context["authority_epoch"]
            or broker.lease(context["session_id"])["state"] != "HUMAN" or attachments() != ()):
        raise RuntimeError("fenced, disconnected handoff was not observed")
    if human_login() is not True:  # Explicit operator confirmation, no automatic return.
        raise RuntimeError("operator retained the session")
    if attachments() != ():
        raise RuntimeError("an executor attached during human custody")
    context["authority_epoch"] = take["epoch_after"]
    call("vp_human_return")
    result = read_human_receipt(context["session_id"])
    epoch = result["epoch_after"]
    observation = broker.returned_observation(context["session_id"]).observation_id
    if (result["state"] != "AGENT" or epoch != context["authority_epoch"] + 1
            or not observation or desktop().active_xid != saved):
        raise RuntimeError("return failed independent epoch, observation or XID readback")
    reply = verified_act(authority_epoch=epoch, observation_id=observation)
    if (reply.get("schema") != "vp.channel.reply/v1" or reply.get("ok") is not True
            or reply.get("code") != "OK" or not (reply.get("ax_sha256") or reply.get("frame_sha256"))):
        raise RuntimeError("action has no verified semantic readback")
    return {"take": take, "return": result, "restored_xid": saved, "act": reply}


if __name__ == "__main__":
    raise SystemExit("Call run() from the operator's provisioned S4 fixture driver")

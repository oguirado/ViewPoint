"""LIVE, operator only, after H1 qualifies a private Firefox Xpra service.

Call run() from the provisioned driver. Supply the authenticated C1 exchange,
the S4 broker, and independent source/forwarded readback from the Xpra runner.
The operator supplies the explicit turn. The vision callback must observe the
given private Firefox session and return its vp.channel.reply/v1.
"""

from uuid import uuid4

if __name__ == "__main__":
    raise SystemExit("Call run() from the operator's provisioned H1 Firefox Xpra driver")

from schemas.validation import validate_contract
from window import WindowBinding, _xid
from xpra import _private_display


def run(*, session, operator_context, exchange, broker, runner, vision_observe, operator_turn):
    validate_contract("vp.session/v1", session)
    context = dict(operator_context)
    sid, display = session["session_id"], _private_display(session["display"])
    if (context.get("caller") != "operator" or context.get("session_id") != sid
            or context.get("incarnation") != session.get("incarnation")
            or context.get("authority_epoch") != session["lease"]["authority_epoch"]
            or session.get("browser") != "firefox" or session["backend"] != "vision"
            or session["state"] != "AGENT" or not session.get("window")
            or not session.get("incarnation") or runner.mode(display) != "start"):
        raise ValueError("qualified private Firefox vision session and operator lane required")

    window = session["window"]
    binding = WindowBinding(_xid(window["xid"]), window["pid"], window["witness"],
                            session["incarnation"], display)

    def call(op, **args):
        request = dict(schema="vp.control.req/v1", request_id=uuid4().hex, op=op,
                       context=dict(context), args=dict(args, session_id=sid), dry_run=False)
        validate_contract("vp.control.req/v1", request)
        reply = exchange(request)
        validate_contract("vp.control.reply/v1", reply)
        if (reply["request_id"], reply["op"]) != (request["request_id"], op):
            raise RuntimeError("control reply belongs to a different request")
        if reply["ok"] is not True or reply["code"] != "OK":
            raise RuntimeError(reply["code"])
        return reply

    def observe(epoch):
        reply = vision_observe(sid, epoch)
        validate_contract("vp.channel.reply/v1", reply)
        if reply["ok"] is not True or reply["code"] != "OK" or "frame_sha256" not in reply:
            raise RuntimeError("private Firefox vision observation has no verified frame")
        return reply

    if runner.windows(display) != (binding,) or runner.forwarded(display) != ():
        raise RuntimeError("private Firefox source is ambiguous or already forwarded")
    before = observe(context["authority_epoch"])
    take = call("vp_human_take", reason="operator")
    lease = broker.lease(sid)
    if (lease["state"] != "HUMAN" or lease["authority_epoch"] != context["authority_epoch"] + 1
            or runner.forwarded(display) != (binding,)):
        raise RuntimeError("S4 did not fence and forward exactly the witnessed private Firefox window")
    if operator_turn() is not True:
        raise RuntimeError("operator retained the session")

    context["authority_epoch"] = lease["authority_epoch"]
    returned = call("vp_human_return")
    epoch = context["authority_epoch"] + 1
    lease = broker.lease(sid)
    if (lease["state"] != "AGENT" or lease["authority_epoch"] != epoch
            or runner.forwarded(display) != () or runner.windows(display) != (binding,)):
        raise RuntimeError("return did not detach and preserve the witnessed Firefox process")
    observation = broker.returned_observation(sid)
    if (observation.session_id != sid or observation.authority_epoch != epoch
            or observation.incarnation != binding.incarnation or not observation.observation_id):
        raise RuntimeError("S4 return did not publish fresh private observation authority")
    return {"take": take, "return": returned, "before": before, "after": observe(epoch)}

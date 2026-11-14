"""Minimal correct bridge core used as the green fixture of G5 (tests mutate it to make G5 red).

Not a Blender script: only the inbox/outbox contract of COMMON.md, with no bpy dependency.
"""
import os
import traceback


def poll_once(rt, nonce, namespace, log=print):
    inbox = os.path.join(rt, "inbox")
    outbox = os.path.join(rt, "outbox")
    rejected = os.path.join(inbox, "rejected")
    for d in (inbox, outbox, rejected):
        os.makedirs(d, exist_ok=True)
    ran = []
    for name in sorted(os.listdir(inbox)):
        src = os.path.join(inbox, name)
        if not name.endswith(".py") or not os.path.isfile(src):
            continue
        head, sep, event_id = name[:-3].partition("_")
        if not nonce or not sep or not event_id or head != nonce:
            event_id = None
        if event_id is None:
            os.replace(src, os.path.join(rejected, name))
            log("REJECTED %s" % name)
            continue
        with open(src) as f:
            code = f.read()
        os.remove(src)
        status = "OK"
        try:
            exec(compile(code, event_id + ".py", "exec"), dict(namespace))
        except BaseException:
            status = "ERROR"
            traceback.print_exc()
        with open(os.path.join(outbox, event_id + ".out"), "w") as f:
            f.write(status + "\n")
        ran.append(event_id)
    return ran

"""ViewPoint reference bridge (Track P, Claude-written). Blender startup script.

    VP_SWARM_RT=<RT> VP_SWARM_NONCE=<nonce> blender --factory-startup --python bridge.py

Polls RT/inbox for `<nonce>_<event_id>.py`, runs each job on Blender's main thread
(bpy.app.timers) and writes RT/outbox/<event_id>.out: first line OK or ERROR, second
line `META <json>` (changed collections, duration), then captured stdout/stderr.
*.py files that do not carry the nonce are moved to RT/inbox/rejected/ and never run.
The core (`poll_once`) has no bpy dependency so it is unit-testable outside Blender.
"""
import contextlib
import hashlib
import hmac
import io
import json
import os
import time
import traceback

RT = os.environ.get("VP_SWARM_RT", "/dev/shm/vqa-cu/ref")
NONCE = os.environ.get("VP_SWARM_NONCE", "")
POLL_S = 0.3
OUT_TAIL = 20000


def scene_digest(bpy):
    """{"col:<name>": hex} over the objects of each collection (the 'scene-digest helper')."""
    out = {}
    cols = list(bpy.data.collections)
    for sc in bpy.data.scenes:
        cols.append(sc.collection)
    for col in cols:
        h = hashlib.sha256()
        for ob in sorted(col.objects, key=lambda o: o.name):
            mw = [round(v, 5) for row in ob.matrix_world for v in row]
            nv = len(ob.data.vertices) if ob.type == "MESH" and ob.data else -1
            mats = [s.material.name if s.material else "" for s in ob.material_slots]
            h.update(repr((ob.name, ob.type, mw, ob.data.name if ob.data else "", nv, mats)).encode())
        out["col:" + col.name] = h.hexdigest()[:16]
    return out


def _split(name, nonce):
    """Return event_id if `name` is `<nonce>_<event_id>.py` with the right nonce, else None."""
    if not nonce or not name.endswith(".py"):
        return None
    stem = name[:-3]
    head, sep, event_id = stem.partition("_")
    if not sep or not event_id:
        return None
    if not hmac.compare_digest(head.encode(), nonce.encode()):
        return None
    if "/" in event_id or event_id.startswith("."):
        return None
    return event_id


def poll_once(rt, nonce, namespace, digest=None, log=print):
    """Process every *.py in rt/inbox once. Returns the list of event_ids that were run."""
    inbox = os.path.join(rt, "inbox")
    outbox = os.path.join(rt, "outbox")
    rejected = os.path.join(inbox, "rejected")
    running = os.path.join(inbox, "running")
    for d in (inbox, outbox, rejected, running):
        os.makedirs(d, exist_ok=True)
    ran = []
    for name in sorted(os.listdir(inbox)):
        if not name.endswith(".py"):
            continue
        src_path = os.path.join(inbox, name)
        if not os.path.isfile(src_path):
            continue
        event_id = _split(name, nonce)
        if event_id is None:  # NONCE CHECK: removing this branch runs unauthenticated jobs
            try:
                os.replace(src_path, os.path.join(rejected, "%d_%s" % (time.time_ns(), name)))
                log("BRIDGE REJECTED %s (no valid nonce)" % name)
            except OSError:
                pass
            continue
        run_path = os.path.join(running, name)
        try:  # atomic claim: after this rename the broker can no longer retract the job
            os.rename(src_path, run_path)
        except OSError:
            continue
        with open(run_path, encoding="utf-8") as f:
            src = f.read()
        before = digest() if digest else {}
        buf = io.StringIO()
        ok = True
        t0 = time.monotonic()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                ns = dict(namespace)
                ns["__name__"] = "__bridge__"
                exec(compile(src, event_id + ".py", "exec"), ns)
            except BaseException:  # includes SystemExit raised by a job
                ok = False
                traceback.print_exc()
        after = digest() if digest else {}
        changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        status = "OK" if ok else "ERROR"
        sha = hashlib.sha256(src.encode()).hexdigest()
        # mac: lets the broker tell a bridge answer from a file dropped in outbox by anyone else
        mac = hmac.new(nonce.encode(), ("%s\n%s\n%s" % (event_id, status, sha)).encode(), hashlib.sha256).hexdigest()
        meta = {"event_id": event_id, "changed": changed if digest else None,
                "ms": int((time.monotonic() - t0) * 1000), "script_sha256": sha, "mac": mac}
        body = (status + "\n") + "META " + json.dumps(meta, sort_keys=True) + "\n" + buf.getvalue()[-OUT_TAIL:]
        tmp = os.path.join(outbox, event_id + ".out.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(tmp, os.path.join(outbox, event_id + ".out"))
        try:
            os.remove(run_path)
        except OSError:
            pass
        ran.append(event_id)
    return ran


def _register():
    import bpy

    def _poll():
        try:
            poll_once(RT, NONCE, {"bpy": bpy}, digest=lambda: scene_digest(bpy))
        except Exception:
            traceback.print_exc()
        return POLL_S

    bpy.app.timers.register(_poll, first_interval=1.0, persistent=True)
    print("VP REF BRIDGE READY rt=%s nonce=%s" % (RT, "set" if NONCE else "MISSING (all jobs rejected)"))


try:
    import bpy as _bpy  # noqa: F401
    _IN_BLENDER = True
except ImportError:
    _IN_BLENDER = False
if _IN_BLENDER:
    _register()

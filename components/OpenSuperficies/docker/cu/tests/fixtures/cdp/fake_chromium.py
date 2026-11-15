#!/usr/bin/env python3
"""Fake chromium for the proxy tests: reads \\0-framed CDP on fd 3, writes on fd 4. FAKE_LOG: every frame received is appended."""
import json, os, sys
mode = os.environ.get("FAKE_MODE", "echo")
log = open(os.environ["FAKE_LOG"], "ab") if os.environ.get("FAKE_LOG") else None
buf = b""
def send(o):
    os.write(4, (o if isinstance(o, bytes) else json.dumps(o).encode()) + b"\0")
if mode == "oversize":
    os.write(4, b"x" * int(os.environ.get("FAKE_BYTES", "5000")))
    sys.exit(0)
if mode == "badjson":
    send(b"not json")
    sys.exit(0)
while True:
    d = os.read(3, 65536)
    if not d:
        break
    buf += d
    while b"\0" in buf:
        f, buf = buf.split(b"\0", 1)
        if log:
            log.write(f + b"\n"); log.flush()
        o = json.loads(f)
        if o.get("method") == "Target.setAutoAttach" and mode in ("attach_blank", "attach_other"):
            ti = {"targetId": "T1", "type": "page", "url": "about:blank"}
            send({"method": "Target.attachedToTarget", "params": {"sessionId": "S1", "waitingForDebugger": False, "targetInfo": ti}})
            send({"method": "Target.targetInfoChanged", "params": {"targetInfo": dict(ti, url=os.environ["FAKE_URL"] if mode == "attach_blank" else "https://evil.example/")}})
        elif o.get("method") == "Target.setAutoAttach":
            send({"method": "Target.attachedToTarget", "params": {"sessionId": "S1", "waitingForDebugger": False, "targetInfo": {"targetId": "T1", "type": "page", "url": os.environ.get("FAKE_URL", "")}}})
        send({"method": "Runtime.consoleAPICalled", "params": {}})
        send({"id": o["id"], "result": {}})

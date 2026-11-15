#!/usr/bin/env python3
"""osb double with a room file ($FAKE_OSB_ROOM, JSONL) and scripted faults.

$FAKE_OSB_PLAN is a JSON list consumed one entry per `post` call (state in
$FAKE_OSB_ROOM.n): "ok" posts; "fail" exits 1 without posting; "land_fail"
posts and then exits 1 (the daemon took it, the CLI reported failure).
After the list: "ok". `read --json --since N` prints the room.
"""
import json, os, sys
argv = sys.argv[1:]
room = os.environ["FAKE_OSB_ROOM"]
rows = [json.loads(l) for l in open(room)] if os.path.exists(room) else []
def opt(name):
    for i, a in enumerate(argv):
        if a == name:
            return argv[i + 1]
        if a.startswith(name + "="):
            return a[len(name) + 1:]
    return None
if argv[0] == "read":
    since = int(opt("--since") or 0)
    for r in rows:
        if r["lamport"] > since:
            print(json.dumps(r))
    sys.exit(0)
plan = json.loads(os.environ.get("FAKE_OSB_PLAN", "[]"))
nf = room + ".n"
n = int(open(nf).read()) if os.path.exists(nf) else 0
open(nf, "w").write(str(n + 1))
mode = plan[n] if n < len(plan) else "ok"
if mode == "fail":
    sys.stderr.write("sbd send rc 1: daemon busy\n")
    sys.exit(1)
ev = {"agent": opt("--agent"), "act": opt("--act"), "text": opt("--text"), "ref": opt("--ref"),
      "lamport": len(rows) + 1}
with open(room, "a") as f:
    f.write(json.dumps(ev) + "\n")
if mode == "land_fail":
    sys.stderr.write("sbd send: reply lost\n")
    sys.exit(1)
print(json.dumps({"id": "fake-%d" % ev["lamport"], "lamport": ev["lamport"]}))

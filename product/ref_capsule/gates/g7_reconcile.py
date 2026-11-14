"""G7 capsule reconcile: declared (spec) -> applied (engine) -> effective (kernel), for one incarnation.

This gate starts nothing and stops nothing; it reads files. It reuses ct-capsule's schema and checks
(../validate.py rules S-/T-/X-/L-..., ../observe.py rules O-00..O-07) and adds what a comparison of
two JSON files cannot say (KU-0001 C1, CT-0022 C1, KU-0007):

  R-FRESH        the observation has an age; older than `freshness_ms` (or undated) is UNOBSERVED
  R-INCARNATION  the observed container Id is the one the start effect recorded; a different Id means
                 the earlier evidence (health, verified frames) belongs to another incarnation
  R-RUNNING      State.Running is true (health text alone can be a cached answer)
  R-KERNEL       memory.max / pids.max read from the container's own cgroup equal the spec.
                 A docker container lives in <cgroot>/system.slice/docker-<Id>.scope, NOT in the
                 caller's systemd-run scope (ct-audit F1, measured), so this is the only place where
                 "the cap really applies" can be observed.
  plan           findings mapped to proposed actions (data only; stops are by container Id)

Inputs:
  spec      vp-capsule/v1 JSON
  snapshot  {"observed_at_ms": int, "inspect": [<docker inspect objects>],
             "effects": [{"op": "start", "name": "...", "id": "<container id>"}]}
            (a bare `docker inspect` list is accepted but is undated, hence UNOBSERVED at best)
"""
import json
import os
import sys
import time

from common import finding, result

_REF = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIB = 1024 * 1024


def _capsule_modules():
    if _REF not in sys.path:
        sys.path.insert(0, _REF)
    import observe  # noqa: E402  (ct-capsule, same Track P tree)
    import validate  # noqa: E402
    return validate, observe


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def kernel_limits(cid, cgroot):
    """(memory.max, pids.max) as the kernel reports them for a docker container id, or (None, None)."""
    for rel in ("system.slice/docker-%s.scope" % cid, "docker/%s" % cid):
        d = os.path.join(cgroot, rel)
        if os.path.isdir(d):
            return _read(os.path.join(d, "memory.max")), _read(os.path.join(d, "pids.max")), d
    return None, None, None


def reconcile(spec, snapshot, now_ms=None, freshness_ms=30000, cgroot=None):
    """Return (findings, plan, observed_count). Findings use common.finding()."""
    V, O = _capsule_modules()
    findings, plan = [], []
    pre = V.validate(spec)
    for f in pre:
        findings.append(finding(f["rule"], "desired:%s" % f["where"], f["msg"]))
    if pre:  # an infeasible desired state is not compared with anything (KU-0001: declare the target first)
        return findings, plan, len(spec.get("containers") or []) if isinstance(spec, dict) else 0
    if isinstance(snapshot, list):
        snapshot = {"inspect": snapshot}
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("inspect"), list):
        findings.append(finding("R-FRESH", "snapshot", "no observation snapshot", "unobserved"))
        return findings, plan, 0
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    at = snapshot.get("observed_at_ms")
    if not isinstance(at, int):
        findings.append(finding("R-FRESH", "snapshot", "observation carries no observed_at_ms", "unobserved"))
    elif now_ms - at > freshness_ms or now_ms < at:
        findings.append(finding("R-FRESH", "snapshot", "observation is %d ms old (limit %d)" % (now_ms - at, freshness_ms),
                                "unobserved"))
    inspected = snapshot["inspect"]
    by_name = {str(d.get("Name", "")).lstrip("/"): d for d in inspected if isinstance(d, dict)}
    for f in O.observe(spec, inspected):
        findings.append(finding(f["rule"], "observed:%s" % f["where"], f["msg"]))
        d = by_name.get(f["where"]) or {}
        if f["rule"] == "O-00":
            plan.append({"op": "start", "name": f["where"]})
        elif f["rule"] == "O-07":
            plan.append({"op": "stop_by_id", "name": f["where"], "id": d.get("Id")})
        elif f["rule"] in ("O-01", "O-02", "O-03", "O-04"):
            plan.append({"op": "replace", "name": f["where"], "id": d.get("Id"), "why": f["rule"]})
    started = {e.get("name"): e.get("id") for e in snapshot.get("effects") or []
               if isinstance(e, dict) and e.get("op") == "start"}
    for c in spec["containers"]:
        n = c["name"]
        d = by_name.get(n)
        if d is None:
            continue
        w = "observed:%s" % n
        st = d.get("State") or {}
        if st.get("Running") is not True:
            findings.append(finding("R-RUNNING", w, "State.Running is %r" % st.get("Running")))
            plan.append({"op": "restart", "name": n, "id": d.get("Id")})
        if n in started:
            if started[n] != d.get("Id"):
                findings.append(finding("R-INCARNATION", w, "observed Id is not the Id recorded by the start effect; "
                                        "earlier evidence does not cover this incarnation"))
        elif snapshot.get("effects") is not None:
            findings.append(finding("R-INCARNATION", w, "running container has no recorded start effect"))
        if cgroot:
            mem, pids, cgdir = kernel_limits(str(d.get("Id")), cgroot)
            if cgdir is None:
                findings.append(finding("R-KERNEL", w, "no cgroup directory for this container id under %s" % cgroot,
                                        "unobserved"))
                continue
            want = str(c["memory_mib"] * MIB)
            if mem != want:
                findings.append(finding("R-KERNEL", w, "kernel memory.max=%s, spec wants %s (%s)" % (mem, want, cgdir)))
                plan.append({"op": "replace", "name": n, "id": d.get("Id"), "why": "R-KERNEL"})
            if pids != str(c["pids_limit"]):
                findings.append(finding("R-KERNEL", w, "kernel pids.max=%s, spec wants %s" % (pids, c["pids_limit"])))
    seen, uniq = set(), []
    for p in plan:
        k = json.dumps(p, sort_keys=True)
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return findings, uniq, len(spec["containers"]) + len(by_name)


def run(spec_path=None, snapshot_path=None, now_ms=None, freshness_ms=30000, cgroot=None):
    gate = "G7_capsule_reconcile"
    if not spec_path or not os.path.isfile(spec_path):
        return dict(result(gate, {}, {}, [finding("SPEC_MISSING", str(spec_path), "no capsule spec", "unobserved")], 0),
                    plan=[])
    try:
        with open(spec_path) as f:
            spec = json.load(f)
    except ValueError as e:
        return dict(result(gate, {}, {}, [finding("S-00", spec_path, "spec is not JSON: %s" % e)], 1), plan=[])
    snapshot = None
    if snapshot_path and os.path.isfile(snapshot_path):
        try:
            with open(snapshot_path) as f:
                snapshot = json.load(f)
        except ValueError:
            snapshot = None
    findings, plan, n = reconcile(spec, snapshot, now_ms, freshness_ms, cgroot)
    desired = {"capsule": spec.get("name") if isinstance(spec, dict) else None, "spec": spec_path,
               "containers": [c.get("name") for c in spec.get("containers", []) if isinstance(c, dict)]
               if isinstance(spec, dict) else []}
    r = result(gate, desired, {"snapshot": snapshot_path, "kernel_cgroot": cgroot}, findings, n)
    r["plan"] = plan
    return r

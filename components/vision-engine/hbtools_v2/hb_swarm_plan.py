#!/usr/bin/env python3
"""HB2-09: swarm plan. Tasks {id, targets, writes, reads}; W = targets + governing sections (+ explicit writes), R = hb_impact closure
(+ explicit reads). Height h(v) = 1 + max h(u) over tasks u it depends on (v reads something u writes, u listed before v); within a
height, first-fit colouring with the hazard test  Wa & (Rb | Wb) == 0  and  Wb & Ra == 0  on int bitmasks. Waves wave_<h>_<c>.json are
pairwise write-disjoint.

hb_swarm_plan.py --tasks tasks.json --out DIR [--root HB]
"""
import argparse, os, sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hblib2 as L  # noqa: E402


def masks(tasks):
    bit = {}
    out = []
    for t in tasks:
        w = 0
        r = 0
        for x in t["writes"]:
            w |= 1 << bit.setdefault(x, len(bit))
        for x in t["reads"]:
            r |= 1 << bit.setdefault(x, len(bit))
        out.append((w, r))
    return out


def hazard(a, b):
    (wa, ra), (wb, rb) = a, b
    return bool(wa & (rb | wb)) or bool(wb & ra)


def plan(tasks):
    """tasks: [{id, writes[], reads[]}] with resolved resource names. Returns (waves, heights); waves = [{name, h, c, tasks:[ids]}]."""
    tasks = sorted(tasks, key=lambda t: t["id"])
    ids = [t["id"] for t in tasks]
    if len(set(ids)) != len(ids):
        raise ValueError("E_DUP_TASK")
    mk = masks(tasks)
    h = {}
    for j, t in enumerate(tasks):
        deps = [i for i in range(j) if mk[i][0] & mk[j][1]]
        h[j] = 1 + max([h[i] for i in deps], default=0)
    waves = {}
    for height in sorted(set(h.values())):
        members = [j for j in range(len(tasks)) if h[j] == height]
        colours = []
        for j in members:
            for c, cl in enumerate(colours):
                if not any(hazard(mk[j], mk[i]) for i in cl):
                    cl.append(j)
                    break
            else:
                colours.append([j])
        for c, cl in enumerate(colours):
            waves[(height, c)] = [tasks[j]["id"] for j in cl]
    out = [{"name": "wave_%d_%d" % k, "h": k[0], "c": k[1], "tasks": v} for k, v in sorted(waves.items())]
    return out, {tasks[j]["id"]: h[j] for j in h}


def resolve(hyper, task):
    """Fill writes/reads of a task from the hyperbook: W = targets + governing sections; R = SIG closure of every target."""
    import hb_impact
    g = hyper.graph()
    writes = list(task.get("writes", []))
    reads = list(task.get("reads", []))
    for t in task["targets"]:
        writes.append(t)
        for sec in hyper.symbols[t]["governs"]["sections"]:
            writes.append("book/book.md::" + sec.replace("-", "_"))
        core = hb_impact.impact_core(hyper, "change the signature", symbol=t)
        if core:
            reads += core["R"]
    return {"id": task["id"], "writes": sorted(set(writes)), "reads": sorted(set(reads) - set(writes))}


def write_waves(waves, out):
    os.makedirs(out, exist_ok=True)
    for w in waves:
        L.write_json(os.path.join(out, w["name"] + ".json"), w)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--root", default=L.HB)
    a = ap.parse_args(argv)
    import hb_query
    h = hb_query.Hyper(a.root)
    tasks = [resolve(h, t) for t in L.jload(a.tasks)]
    waves, hts = plan(tasks)
    write_waves(waves, a.out)
    print(L.dumps({"waves": len(waves), "tasks": len(tasks)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

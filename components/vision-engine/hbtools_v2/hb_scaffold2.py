#!/usr/bin/env python3
"""T05: scaffold book/book.md from book/seed/slots.json (11 sections of v1; hb-s8..hb-s10 are empty generated fences).

After the scaffold the book is the source of truth: workers edit prose outside the generated fences; `hb_build.py --render-derived`
regenerates hb-s8..hb-s10, symbols.jsonl, nodes.jsonl, anchors.jsonl, graph.json and claims.jsonl.
"""
import argparse, os, sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hblib2 as L  # noqa: E402

import hb_lib_bundle as SB
TITLES = ["Reading map", "Contract of the toolchain", "Types, weights and budgets", "Build and routing DAG", "Engineering triptych",
          "Subsystems", "Limits and search entries", "Narrative: from a query to a shortlist", "Symbol and file table (generated)",
          "Edges: imports, calls, tests (generated)", "Provenance (generated)"]
KINDS = ["reading", "contracts", "types_budgets", "dag", "triptych", "subsystems", "limits_search", "narrative", "symbols", "edges", "provenance"]
GEN = {8, 9, 10}


def fence(schema, obj):
    return "<!-- HB:BEGIN %s -->\n```json\n%s\n```\n<!-- HB:END -->\n" % (schema, L.dumps(obj))


def section_block(n, anchor, s, covers=None, hb_id=None):
    hb_id = SB.book_id(explicit=hb_id)
    o = {"schema": "hb.section.v2", "hb_id": hb_id, "n": n, "anchor": anchor, "kind": s.get("kind", KINDS[n]), "diataxis": s.get("diataxis", "reference"),
         "title": s["title"], "purpose": s["purpose"], "code_anchors": sorted(s.get("anchors", [])), "claims": [],
         "route": {"emit": n not in GEN, "tags": s.get("tags", ["software/router"])}}
    if covers:
        o["covers"] = covers
    return fence("hb.section.v2", o)


def gen_region(n):
    return "<!-- HB:GEN BEGIN hb-s%d -->\n<!-- HB:GEN END hb-s%d -->\n" % (n, n)


def scaffold(slots, hb_id=None):
    hb_id = SB.book_id(explicit=hb_id, config=slots)
    out = ["# %s - the context router v2 toolchain (index builder, router, tuner, evaluator, gates, pool builder)\n\n"
           "Hyperbook v2 of the frozen router v2 code and a small generated Godot scene fixture. Sections 8, 9 and 10 are generated "
           "(`hb_build.py --render-derived`); everything else is authored prose and machine blocks.\n\n" % hb_id]
    for n in range(11):
        a = "hb-s%d" % n
        out.append('## <a id="%s"></a>%d. %s\n\n' % (a, n, TITLES[n]))
        if n in GEN:
            out.append(gen_region(n) + "\n")
            continue
        s = slots["sections"][a]
        out.append(section_block(n, a, s, hb_id=hb_id))
        out.append("\n" + s["prose"].strip() + "\n\n")
        if n == 4:
            for c in slots["claims"]:
                obj = {"schema": "hb.claim.v2", "id": c["id"], "kind": c["kind"], "statement": c["statement"], "anchors": sorted(c["anchors"]),
                       "etag": c["etag"], "receipts": c["receipts"], "value": c["value"]}
                out.append(fence("hb.claim.v2", obj) + "\n")
        if n == 5:
            for i, path in enumerate(sorted(slots["modules"]), 1):
                m = slots["modules"][path]
                stem = os.path.splitext(os.path.basename(path))[0]
                anc = "hb-s5.%s" % stem
                out.append('### <a id="%s"></a>5.%d %s\n\n' % (anc, i, m["title"]))
                out.append(section_block(5, anc, {"title": m["title"], "purpose": m["summary"], "kind": "subsystems", "tags": ["software/router"]}, covers=path, hb_id=hb_id))
                out.append("\n" + m["summary"] + "\n\n")
                blk = {"module": path, "inv": [{"id": i_, "text": t, "symbols": sorted(sy)} for i_, t, sy in m["inv"]],
                       "au": [{"id": i_, "text": t, "symbols": sorted(sy)} for i_, t, sy in m["au"]]}
                out.append(fence("hb.inv.v2", blk) + "\n")
    return "".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--slots", default=os.path.join(L.HB, L.BOOK_SUB, "seed", "slots.json"))
    ap.add_argument("--hb-id", default=os.environ.get("HBV2_HB_ID"))
    ap.add_argument("--out", default=os.path.join(L.HB, L.BOOK_SUB, "book.md"))
    a = ap.parse_args(argv)
    txt = scaffold(L.jload(a.slots), a.hb_id)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", encoding="utf-8", newline="\n") as f:
        f.write(txt)
    print(len(txt.encode("utf-8")), "bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())

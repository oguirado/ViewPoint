#!/usr/bin/env python3
"""T04: collect cards into book/cards.jsonl (+ out/cards.sha256).

Source order per tool symbol: a validated worker card from work/cards/out/*.json (seat gemini), else the deterministic
extractive card of `local_card` (declared degrade: the worker pool vetoed nested dispatch, rc 43; see DEVIATIONS.md).
Row: {"card": {purpose, tasks, not_for}, "id": sid, "source": "gemini"|"local-extractive"}.
"""
import argparse, glob, json, os, re, sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hblib2 as L  # noqa: E402
import hb_extract2 as X  # noqa: E402

PURPOSE_MAX, TASK_MAX, NOTFOR_MAX = 240, 120, 120


def words(name):
    return L.split_ident(name.split(".")[-1]).lower()


def fit(s, n):
    s = " ".join(s.split())
    if L.blen(s) <= n:
        return s
    cut = L.clip_bytes(s, n)
    return cut.rsplit(" ", 1)[0] if " " in cut else cut


def first_sentence(doc):
    d = " ".join(doc.split())
    m = re.match(r"(.+?\.)(\s|$)", d)
    return m.group(1) if m else d


def local_card(sid, sym, txt, callees, callers, params):
    name = sid.split("::")[1]
    w = words(name)
    doc = first_sentence(txt["doc"]) if txt["doc"] else ""
    base = doc or ("%s in module %s." % (w.capitalize(), txt["stem"].replace("_", " ")))
    use = ", ".join(sorted({words(c.split("::")[1]) for c in callees})[:6])
    purpose = base + ((" Uses: " + use + ".") if use else "")
    tasks = ["find or change how %s works" % w]
    if params:
        tasks.append("work with %s and its parameters %s" % (w, " ".join(params[:5])))
    com = [c.strip() for c in txt["com"].split("  ") if c.strip()]
    sec = " ".join(txt["doc"].split())[len(doc):].strip()
    extra = (com[0] if com else sec)
    if extra:
        tasks.append(extra)
    not_for = []
    cs = sorted({words(c.split("::")[1]) for c in callers})[:3]
    if cs:
        not_for.append("work that belongs to its callers: " + ", ".join(cs))
    not_for.append("anything unrelated to module " + txt["stem"].replace("_", " "))
    return {"purpose": fit(purpose, PURPOSE_MAX), "tasks": [fit(t, TASK_MAX) for t in tasks[:3]],
            "not_for": [fit(t, NOTFOR_MAX) for t in not_for[:2]]}


def valid(card):
    return (isinstance(card, dict) and set(card) <= {"purpose", "tasks", "not_for"} and isinstance(card.get("purpose"), str)
            and 0 < L.blen(card["purpose"]) <= PURPOSE_MAX and "\n" not in card["purpose"]
            and isinstance(card.get("tasks", []), list) and len(card.get("tasks", [])) <= 3
            and all(isinstance(t, str) and L.blen(t) <= TASK_MAX for t in card.get("tasks", []))
            and isinstance(card.get("not_for", []), list) and len(card.get("not_for", [])) <= 2
            and all(isinstance(t, str) and L.blen(t) <= NOTFOR_MAX for t in card.get("not_for", [])))


def worker_cards(outdir):
    got = {}
    for p in sorted(glob.glob(os.path.join(outdir, "*.json"))):
        if p.endswith(".rojo.json"):
            continue
        try:
            raw = L.jload(p)
            txt = raw[-1]["result"] if isinstance(raw, list) else raw.get("result")
            txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt.strip())
            obj = json.loads(txt)
        except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError):
            continue
        for k, v in obj.items():
            if valid(v):
                got[k] = v
    return got


def collect(subject, workdir):
    repo = X.Repo(subject)
    x = X.extract(subject)
    txts = X.symbol_texts(repo)
    callees, callers = {}, {}
    for a, b, t in x["call_edges"]:
        callees.setdefault(a, set()).add(b)
        callers.setdefault(b, set()).add(a)
    wc = worker_cards(os.path.join(workdir, "out"))
    rows = []
    for m in repo.mods:
        if not m.path.startswith("tools/"):
            continue
        for s in m.syms:
            if s["kind"] not in ("function", "method", "class"):
                continue
            n = s["node"]
            params = [] if s["kind"] == "class" else [a.arg for a in n.args.posonlyargs + n.args.args + n.args.kwonlyargs if a.arg not in ("self", "cls")]
            if s["id"] in wc:
                card, src = wc[s["id"]], "gemini"
            else:
                card, src = local_card(s["id"], s, txts[s["id"]], callees.get(s["id"], ()), callers.get(s["id"], ()), params), "local-extractive"
            card = {k: v for k, v in card.items() if v}
            rows.append({"card": card, "id": s["id"], "source": src})
    rows.sort(key=lambda r: r["id"].encode("utf-8"))
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--subject", default=L.SUBJECT)
    ap.add_argument("--work", default=os.path.join(L.HB, "work", "cards"))
    ap.add_argument("--out", default=os.path.join(L.HB, L.BOOK_SUB, "cards.jsonl"))
    ap.add_argument("--sha", default=os.path.join(L.HB, "out", "cards.sha256"))
    a = ap.parse_args(argv)
    rows = collect(a.subject, a.work)
    L.write_jsonl(a.out, rows)
    h = L.sha_file(a.out)
    os.makedirs(os.path.dirname(a.sha), exist_ok=True)
    with open(a.sha, "w", newline="\n") as f:
        f.write(h + "  book/cards.jsonl\n")
    src = {}
    for r in rows:
        src[r["source"]] = src.get(r["source"], 0) + 1
    print(L.dumps({"cards": len(rows), "sources": src, "sha256": h}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

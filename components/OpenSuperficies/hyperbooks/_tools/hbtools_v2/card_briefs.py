#!/usr/bin/env python3
"""T04 (HB2-02): cards briefs. One brief file per tool module (batch of its symbols' slices + module docstring + a fixed
instruction); every brief asks for exactly one card per listed symbol. No DEV/TEST text is ever included.

Writes work/cards/briefs/<stem>.md and work/cards/jobs.tsv (seat, permiso, brief, output).
"""
import argparse, ast, os, sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hblib2 as L  # noqa: E402
import hb_extract2 as X  # noqa: E402

NEWTON = ("You run as a cheap synthesis worker inside newton-shell. All material is inline in this brief; you have no tools and "
          "cannot read the disk. Answer with one JSON object only, no prose, no code fence.\n\n")
INSTR = """TASK. For every symbol listed below write ONE documentation card from its source slice and the module docstring only.
Card schema (all strings plain English, no newlines):
{"purpose": <string <= 240 bytes: what the symbol does>, "tasks": [<up to 3 strings <= 120 bytes each: concrete developer tasks for which a person would look this symbol up>], "not_for": [<up to 2 strings <= 120 bytes each: tasks it must NOT be used for or could be confused with>]}
Use the vocabulary a developer would use when searching (verbs and nouns of the task), not a paraphrase of identifiers only.
Do not invent behaviour that the slice does not show. Output format: {"<symbol id>": <card>, ...} with exactly the ids listed below, each once.
"""
MAXB = 90 * 1024 - 4096


def modules(repo):
    return [m for m in repo.mods if m.path.startswith("tools/")]


def cardable(m):
    return [s for s in m.syms if s["kind"] in ("function", "method", "class")]


def brief_for(m, syms):
    doc = ast.get_docstring(m.tree) or "(none)"
    parts = [NEWTON, INSTR, "MODULE %s\nModule docstring: %s\n\nSYMBOLS (ids to document): %s\n" % (m.path, doc, ", ".join(s["id"] for s in syms))]
    for s in syms:
        parts.append("=== %s (lines %d-%d)\n%s\n" % (s["id"], s["start_line"], s["end_line"], m.data[s["start_byte"]:s["end_byte"]].decode("utf-8")))
    return "".join(parts)


def chunks(m, syms):
    """Split a module's symbols so that each brief stays under MAXB bytes (greedy, id order)."""
    out, cur, size = [], [], 0
    for s in syms:
        b = s["end_byte"] - s["start_byte"] + 200
        if cur and size + b > MAXB - 3000:
            out.append(cur)
            cur, size = [], 0
        cur.append(s)
        size += b
    if cur:
        out.append(cur)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--subject", default=L.SUBJECT)
    ap.add_argument("--work", default=os.path.join(L.HB, "work", "cards"))
    a = ap.parse_args(argv)
    repo = X.Repo(a.subject)
    os.makedirs(os.path.join(a.work, "briefs"), exist_ok=True)
    os.makedirs(os.path.join(a.work, "out"), exist_ok=True)
    jobs, n = [], 0
    for m in modules(repo):
        for k, syms in enumerate(chunks(m, cardable(m))):
            name = "%s_%d" % (m.stem, k)
            bp = os.path.join(a.work, "briefs", name + ".md")
            txt = brief_for(m, syms)
            assert len(txt.encode("utf-8")) <= 90 * 1024
            with open(bp, "w", encoding="utf-8", newline="\n") as f:
                f.write(txt)
            jobs.append("\t".join(["gemini", "sintesis", bp, os.path.join(a.work, "out", name + ".json")]))
            n += len(syms)
    with open(os.path.join(a.work, "jobs.tsv"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(jobs) + "\n")
    print(len(jobs), "briefs", n, "symbols")
    return 0


if __name__ == "__main__":
    sys.exit(main())

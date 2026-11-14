#!/usr/bin/env python3
"""Build a complete mini hyperbook (book, cards, trace, index) for a small subject tree such as fixtures/graph (tests, MF-2 fixture).

make_mini(subject_src, dst, claims=()) -> dst root with subject/, book/, out/trace.json, book/index/ ready for hb_query / hb_impact.
"""
import os, shutil, subprocess, sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hblib2 as L  # noqa: E402
import hb_extract2 as X  # noqa: E402
import hb_scaffold2 as SC  # noqa: E402
import card_collect as CC  # noqa: E402
import hb_build as B  # noqa: E402
import hb_index as I  # noqa: E402

TRACE = os.path.join(L.HERE, "hb_trace.py")


def mini_slots(subject, with_inv=True):
    repo = X.Repo(subject)
    sections = {}
    titles = SC.TITLES
    for n in range(8):
        a = "hb-s%d" % n
        sections[a] = dict(title=titles[n], kind=SC.KINDS[n], diataxis="reference", purpose="Mini section %d of the fixture hyperbook." % n,
                           anchors=[], tags=["software/fixture"], prose="Prose of mini section %d." % n)
    mods = {}
    for m in repo.mods:
        if m.path.startswith("tools/"):
            first = next((s for s in m.syms if s["kind"] in ("function", "class")), None)
            inv, au = [], []
            stem = m.stem
            if with_inv and first:
                inv = [["INV-%s-1" % stem, "Invariant of %s: stays pure." % stem, [first["id"]]]]
                au = [["AU-%s-1" % stem, "Anti-use of %s: never call with None." % stem, [first["id"]]]]
            mods[m.path] = dict(title=stem + " - fixture module", summary="Fixture module %s." % stem, inv=inv, au=au)
    return {"sections": sections, "modules": mods, "claims": []}


def make_mini(src, dst, with_inv=True):
    if os.path.exists(dst):
        shutil.rmtree(dst)
    os.makedirs(os.path.join(dst, L.BOOK_SUB))
    os.makedirs(os.path.join(dst, "out"))
    shutil.copytree(src, os.path.join(dst, "subject"), ignore=shutil.ignore_patterns("__pycache__"))
    subj = os.path.join(dst, "subject")
    r = subprocess.run([sys.executable, TRACE, "--subject", subj, "--out", os.path.join(dst, "out", "trace.json")], capture_output=True, text=True,
                       env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    if r.returncode != 0:
        raise RuntimeError("trace failed: " + r.stderr[-300:])
    L.clean_pycache(subj)
    with open(os.path.join(dst, L.BOOK_SUB, "book.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(SC.scaffold(mini_slots(subj, with_inv)))
    L.write_jsonl(os.path.join(dst, L.BOOK_SUB, "cards.jsonl"), CC.collect(subj, os.path.join(dst, "work")))
    ctx, d = B.build_all(dst)
    I.build_index(dst)
    return dst

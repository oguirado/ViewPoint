#!/usr/bin/env python3
"""T09: drift mutants (13 as v1 + 6 new + asset cases) generated on temp copies of the subject; kill matrix against HB2-06.

hb_drift2.py --subject subject --out out/drift_DEV.json [--godot godot_fixture/project]
Labels live in the manifest of each row; seeds are fixed (targets are hard-coded symbol ids, edits are deterministic).
Scored targets: expected class == observed class of the target anchor. False positive: a non-target, non-module anchor whose class
is in BAD (EDIT, SIG, DELETE, RENAME, MOVE+EDIT, E_AMBIG, DOC, ...) or a SHIFT/FORMAT mutant that leaves anything but SAME after
the auto repair. Module anchors aggregate their children and are excluded from the false-positive count (declared).
"""
import argparse, ast, os, re, shutil, sys, tempfile

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hblib2 as L  # noqa: E402
import hb_extract2 as X  # noqa: E402
import hb_anchor2 as A  # noqa: E402

# (mutant id, file, symbol, op, expected class of the target, group)
V1_SUITE = [
    ("RN1", "route_v2.py", "weights", "rename:weights_for", "RENAME"), ("RN2", "build_index_v2.py", "tree", "rename:tree_files_of", "RENAME"),
    ("RN3", "eval_v2.py", "pct", "rename:percentile", "RENAME"),
    ("ED1", "route_v2.py", "detect_intent", "body_insert", "EDIT"), ("ED2", "eval_v2.py", "quality", "body_insert", "EDIT"),
    ("ED3", "make_pool_v2.py", "kappa", "body_insert", "EDIT"),
    ("DL1", "gates_v2.py", "g_calib", "delete", "DELETE"), ("DL2", "eval_v2.py", "judged_ids", "delete", "DELETE"),
    ("DL3", "build_index_v2.py", "load_json", "delete", "DELETE"),
    ("SH1", "route_v2.py", None, "shift_top", None), ("SH2", "build_index_v2.py", "build", "shift_before:1", None),
    ("SH3", "eval_v2.py", "main", "shift_before:2", None),
    ("SG1", "route_v2.py", "why_terms", "sig", "SIG")]
NEW_SUITE = [
    ("N1", "route_v2.py", "envelope", "comment_only", "FORMAT"), ("N2", "make_pool_v2.py", "final_grade", "doc_only", "DOC"),
    ("N3", None, None, "reindent", "EDIT"), ("N4", "route_v2.py", "route", "move_same", "MOVE"),
    ("N5", "gates_v2.py", "g_compact", "move_edit15", "MOVE+EDIT"), ("N6", "tune_v2.py", "thresholds", "dup_helper", "E_AMBIG")]
T = "tools/"


def find(tree, name):
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name:
            return n
    return None


def span_lines(n):
    return min([n.lineno] + [d.lineno for d in n.decorator_list]), n.end_lineno


def reindent_target(root):
    """First (sorted) tools file / `if` without else whose last body statement can be dedented one block. Returns (file, line range, lines)."""
    for fn in sorted(os.listdir(os.path.join(root, "tools"))):
        if not fn.endswith(".py"):
            continue
        text = open(os.path.join(root, "tools", fn), encoding="utf-8").read()
        tree = ast.parse(text)
        for top in tree.body:
            if not isinstance(top, ast.FunctionDef):
                continue
            for n in ast.walk(top):
                if isinstance(n, ast.If) and not n.orelse and len(n.body) >= 2 and not isinstance(n.body[-1], (ast.Pass, ast.Continue, ast.Break)):
                    last = n.body[-1]
                    if last.col_offset - n.col_offset == 4 and last.end_lineno == last.lineno:
                        lines = text.split("\n")
                        new = list(lines)
                        new[last.lineno - 1] = lines[last.lineno - 1][4:]
                        try:
                            ast.parse("\n".join(new))
                        except SyntaxError:
                            continue
                        if ast.dump(ast.parse("\n".join(new))) == ast.dump(tree):
                            continue
                        return fn, top.name, last.lineno
    return None


def apply(root, fname, symbol, op):
    """Apply one mutation to <root>/tools/<fname>. Returns the target id (or None for file-level ops) or False if inapplicable."""
    fp = os.path.join(root, T + fname) if fname else None
    if op == "reindent":
        r = reindent_target(root)
        if r is None:
            return False
        fname, symbol, ln = r
        fp = os.path.join(root, T + fname)
        lines = open(fp, "rb").read().decode("utf-8").split("\n")
        lines[ln - 1] = lines[ln - 1][4:]
        open(fp, "wb").write("\n".join(lines).encode("utf-8"))
        return T + fname + "::" + symbol
    text = open(fp, "rb").read().decode("utf-8")
    lines = text.split("\n")
    tree = ast.parse(text)
    tid = T + fname + "::" + symbol if symbol else None
    if op == "shift_top":
        lines[1:1] = ["# hb shift"] * 3
    else:
        n = find(tree, symbol)
        if n is None:
            return False
        start, end = span_lines(n)
        if op.startswith("rename:"):
            new = op.split(":", 1)[1]
            defined = {x.name for x in tree.body if hasattr(x, "name")}
            if new in defined:
                return False
            lines[n.lineno - 1] = re.sub(r"\bdef %s\b" % re.escape(symbol), "def " + new, lines[n.lineno - 1], count=1)
        elif op == "body_insert":
            b0 = n.body[0]
            doc = isinstance(b0, ast.Expr) and isinstance(getattr(b0, "value", None), ast.Constant) and isinstance(b0.value.value, str)
            at = b0.end_lineno if doc else b0.lineno - 1
            lines.insert(at, " " * b0.col_offset + "_hb_mut = 0")
        elif op == "delete":
            del lines[start - 1:end]
        elif op.startswith("shift_before"):
            k = int(op.split(":")[1])
            lines[start - 1:start - 1] = ["# hb shift"] if k == 1 else ["", "# hb shift"]
        elif op == "sig":
            lines[n.lineno - 1] = re.sub(r"\)(\s*(->[^:]*)?:)\s*$", r", _hb=None)\1", lines[n.lineno - 1], count=1)
        elif op == "comment_only":
            b0 = n.body[0]
            doc = isinstance(b0, ast.Expr) and isinstance(getattr(b0, "value", None), ast.Constant) and isinstance(b0.value.value, str)
            at = b0.end_lineno if doc else b0.lineno - 1
            lines.insert(at, " " * b0.col_offset + "# hb note: comment only")
        elif op == "doc_only":
            b0 = n.body[0]
            if not (isinstance(b0, ast.Expr) and isinstance(getattr(b0, "value", None), ast.Constant) and isinstance(b0.value.value, str)):
                return False
            old = lines[b0.lineno - 1]
            lines[b0.lineno - 1] = old.replace('"""', '"""Reworded: ', 1) if '"""' in old else old.replace('"', '"Reworded: ', 1)
        elif op in ("move_same", "move_edit15", "dup_helper"):
            block = lines[start - 1:end]
            del lines[start - 1:end]
            if op == "move_edit15":
                block = grow_to(block, symbol, 150000)
            dests = ["hb_dup1.py", "hb_dup2.py"] if op == "dup_helper" else ["hb_moved.py"]
            for d in dests:
                open(os.path.join(root, T + d), "wb").write(("\n".join(block) + "\n").encode("utf-8"))
    open(fp, "wb").write("\n".join(lines).encode("utf-8"))
    return tid


def grow_to(block, symbol, target_ppm):
    """Insert assignment statements at the top of the body until the token indel ratio first reaches target_ppm."""
    src = "\n".join(block) + "\n"
    base = A.norm_tokens(ast.parse(src).body[0])
    for k in range(1, 40):
        pad = ["    _hb_pad_%d = %d" % (i, i) for i in range(k)]
        # after the def line(s): insert after the signature line of the first statement
        node = ast.parse(src).body[0]
        at = node.body[0].lineno - 1 - (min([d.lineno for d in node.decorator_list] + [node.lineno]) - 1)
        mod = block[:at] + pad + block[at:]
        toks = A.norm_tokens(ast.parse("\n".join(mod) + "\n").body[0])
        d = (10 ** 6 * A.indel(base, toks)) // max(len(base), len(toks))
        if d >= target_ppm - 20000:
            return mod
    return block


def copy_scope(src, dst):
    for d in ("tools", "tests"):
        os.makedirs(os.path.join(dst, d))
        for f in sorted(os.listdir(os.path.join(src, d))):
            if f.endswith(".py"):
                shutil.copy2(os.path.join(src, d, f), os.path.join(dst, d, f))


def run_one(base_anchors, old_repo, subject, spec, tau=A.TAU_REL):
    mid, fname, symbol, op, expected = spec
    tmp = tempfile.mkdtemp(prefix="hbdrift2")
    try:
        copy_scope(subject, tmp)
        tid = apply(tmp, fname, symbol, op)
        if tid is False:
            return {"mutant": mid, "operation": op, "invalid": True, "expected": expected}
        new = X.Repo(tmp)
        res = A.classify(base_anchors, new, {}, old_repo, tau=tau)
        by = {r["id"]: r for r in res}
        obs = by[tid]["class"] if tid in by else None
        # "others": every non-target, non-module anchor whose class is bad
        fp = [r["id"] + ":" + r["class"] for r in res if r["id"] != tid and r["kind"] != "module" and r["class"] in A.BAD]
        REBIND = ("RENAME", "MOVE", "MOVE+EDIT", "ALIAS")
        fr = sum(1 for r in res if r["id"] != tid and r["class"] in REBIND) + (1 if (obs in REBIND and expected not in REBIND) else 0)
        row = {"mutant": mid, "operation": op, "target": tid, "expected": expected or "SHIFT", "observed": obs, "others": A.summarize(
            [r for r in res if r["id"] != tid]), "false_positives": fp, "false_rebinds": fr}
        if expected:
            row["correct"] = obs == expected
            if expected in ("MOVE",) and by[tid].get("proposal"):
                row["correct"] = row["correct"] and by[tid]["proposal"]["auto_apply"] is True
            if expected in ("RENAME", "MOVE+EDIT", "E_AMBIG"):
                row["auto_applied"] = False
        if not expected:        # shift: nothing bad anywhere, and auto repair reaches all-SAME
            row["correct"] = not fp
            auto, manual = A.repair_plan(res)
            ids = {r["id"] for r in auto}
            rebased = A.rebaseline(base_anchors, new, ids)
            res2 = A.classify(rebased, new, {}, old_repo, tau=tau)
            row["repair_all_same"] = all(r["class"] == "SAME" for r in res2)
            row["shift_count"] = sum(1 for r in res if r["class"] == "SHIFT")
            row["correct"] = row["correct"] and row["repair_all_same"] and not manual
        if expected in ("FORMAT",):
            auto, manual = A.repair_plan(res)
            row["auto_repair_ids"] = len(auto)
        return row
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def clean_copy_row(base_anchors, old_repo, subject):
    tmp = tempfile.mkdtemp(prefix="hbdrift2")
    try:
        copy_scope(subject, tmp)
        res = A.classify(base_anchors, X.Repo(tmp), {}, old_repo)
        return {"mutant": "CLEAN", "classes": A.summarize(res), "false_positives": sum(1 for r in res if r["class"] != "SAME")}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def asset_cases(godot):
    """Same uid moved / same uid edited on a copy of the Godot fixture (needs the T12 fixture)."""
    if not godot or not os.path.isdir(godot):
        return []
    rows = []
    base = A.asset_anchors(godot)
    if not base:
        return []
    target = next((a for a in base if a["path"].endswith(".gd")), base[0])
    for mid, op, exp in (("A1", "move", "MOVE"), ("A2", "edit", "EDIT"), ("A3", "delete", "DELETE")):
        tmp = tempfile.mkdtemp(prefix="hbasset")
        try:
            dst = os.path.join(tmp, "p")
            shutil.copytree(godot, dst)
            p = os.path.join(dst, target["path"])
            if op == "move":
                os.makedirs(os.path.join(dst, "moved"), exist_ok=True)
                shutil.move(p, os.path.join(dst, "moved", os.path.basename(p)))
                shutil.move(p + ".uid", os.path.join(dst, "moved", os.path.basename(p) + ".uid"))
            elif op == "edit":
                with open(p, "ab") as f:
                    f.write(b"\n# edited\n")
            else:
                os.remove(p)
                os.remove(p + ".uid")
            res = A.classify_assets(base, A.asset_anchors(dst))
            by = {r["id"]: r for r in res}
            obs = by[target["id"]]["class"]
            others = [r["id"] + ":" + r["class"] for r in res if r["id"] != target["id"] and r["class"] != "SAME"]
            rows.append({"mutant": mid, "operation": "asset_" + op, "target": target["id"], "expected": exp, "observed": obs,
                         "correct": obs == exp and not others, "false_positives": others, "auto_apply": by[target["id"]].get("auto_apply", False)})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return rows


def run(subject, godot=None, tau=A.TAU_REL):
    anchors = X.anchors_from_symbols(X.symbols_from_repo(X.Repo(subject)))
    old = X.Repo(subject)
    rows = [run_one(anchors, old, subject, s, tau) for s in V1_SUITE + NEW_SUITE]
    rows += asset_cases(godot)
    clean = clean_copy_row(anchors, old, subject)
    v1 = [r for r in rows if r["mutant"][:2] in ("RN", "ED", "DL", "SG")]
    scored = [r for r in rows if r.get("expected") not in (None, "SHIFT") or r["mutant"].startswith("SH")]
    summ = {"v1_targets_correct": sum(1 for r in v1 if r.get("correct")), "v1_targets_n": len(v1),
            "all_correct": sum(1 for r in rows if r.get("correct")), "all_n": len(rows),
            "false_positives": sum(len(r.get("false_positives", [])) for r in rows) + clean["false_positives"],
            "clean_false_positives": clean["false_positives"], "invalid": [r["mutant"] for r in rows if r.get("invalid")],
            "false_rebinds": sum(r.get("false_rebinds", 0) for r in rows), "tau_rel": tau,
            "kill_new": sum(1 for r in rows if r["mutant"].startswith(("N", "A")) and r.get("correct")),
            "new_n": sum(1 for r in rows if r["mutant"].startswith(("N", "A")))}
    return {"summary": summ, "rows": rows, "clean": clean}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--subject", default=L.SUBJECT)
    ap.add_argument("--out", default=os.path.join(L.HB, "out", "drift_DEV.json"))
    ap.add_argument("--godot", default=os.path.join(L.HB, "godot_fixture", "project"))
    ap.add_argument("--tau-rel", type=int)
    a = ap.parse_args(argv)
    tau = a.tau_rel
    pp = os.path.join(L.HB, "params.json")
    if tau is None:
        tau = L.jload(pp).get("anchors", {}).get("tau_rel_ppm", A.TAU_REL) if os.path.isfile(pp) else A.TAU_REL
    r = run(a.subject, a.godot, tau)
    doc = {"schema": "hb.receipt.v2", "kind": "mutation", "tool": "hb_drift2.py/2.0", "metrics": {
        k: v for k, v in r["summary"].items() if isinstance(v, int)}, "inputs": {}, "summary": r["summary"], "rows": r["rows"], "clean": r["clean"]}
    L.write_json(a.out, doc)
    s = r["summary"]
    print("v1_targets=%d/%d all=%d/%d fp=%d invalid=%s" % (s["v1_targets_correct"], s["v1_targets_n"], s["all_correct"], s["all_n"], s["false_positives"], s["invalid"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

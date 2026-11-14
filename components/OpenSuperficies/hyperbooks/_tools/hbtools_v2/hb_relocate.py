#!/usr/bin/env python3
"""HB2-06: apply an accepted `hb.alias-proposal.v2` (re-verified first) by writing aliases.json; `--tombstone` records a deletion.

hb_relocate.py --old OLD --new NEW --anchors anchors.jsonl --aliases aliases.json [--apply proposal.json | --tombstone SID]
rc 0 applied, 1 alias cycle, 3 proposal does not re-verify, 4 refused (not applicable / auto classes need no proposal).
"""
import argparse, json, os, sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hblib2 as L  # noqa: E402
import hb_extract2 as X  # noqa: E402
import hb_anchor2 as A  # noqa: E402
import schemalib  # noqa: E402


def save(path, al):
    L.write_json(path, {"aliases": dict(sorted(al.items()))})


def apply_proposal(prop, anchors, old_repo, new_repo, aliases):
    """-> (rc, message). Re-runs the relocation search and requires the identical proposal; then records the alias."""
    errs = schemalib.validate(prop, "hb.alias-proposal.v2")
    if errs:
        return 3, "schema: %s" % errs[0]
    if prop["op"] == "SHIFT" or prop["op"] == "FORMAT":
        return 4, "auto class: no proposal needed"
    a = next((x for x in anchors if x["id"] == prop["from"]), None)
    if a is None:
        return 3, "unknown anchor"
    ix = A.cur_index(new_repo)
    if prop["from"] in ix:
        return 3, "source still present"
    r = A.relocate(a, old_repo, new_repo, ix, {x["id"] for x in anchors}, prop["tau_ppm"], prop["margin_ppm"])
    if r.get("proposal") != prop:
        return 3, "proposal does not re-verify (%s)" % r["class"]
    nxt = dict(aliases)
    nxt[prop["from"]] = {"to": prop["to"], "status": "alias", "op": prop["op"]}
    try:
        A.resolve_chains(nxt)
    except A.AliasError as e:
        return 1, str(e)
    aliases.clear()
    aliases.update(nxt)
    return 0, "applied"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", required=True)
    ap.add_argument("--new", required=True)
    ap.add_argument("--anchors", required=True)
    ap.add_argument("--aliases", required=True)
    ap.add_argument("--apply")
    ap.add_argument("--tombstone")
    a = ap.parse_args(argv)
    try:
        al = A.load_aliases(a.aliases)
    except A.AliasError as e:
        sys.stderr.write("E_ALIAS_CYCLE %s\n" % e)
        return 1
    if a.tombstone:
        al[a.tombstone] = {"to": None, "status": "tombstone"}
        save(a.aliases, al)
        return 0
    prop = L.jload(a.apply)
    rc, msg = apply_proposal(prop, L.read_jsonl(a.anchors), X.Repo(a.old), X.Repo(a.new), al)
    if rc == 0:
        save(a.aliases, al)
    sys.stderr.write(msg + "\n")
    return rc


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Permutation witness (HB2-08): print the router answers over a synthetic corpus whose id->content assignment is a Fisher-Yates
permutation (seed k). Run inside a tree (clean or mutated); the oracle compares the outputs of the two trees.

oracle_witness.py --root TREE --seed K
"""
import argparse, json, os, random, shutil, sys, tempfile

sys.dont_write_bytecode = True


def fisher_yates(n, seed):
    r = random.Random(seed)
    p = list(range(n))
    for i in range(n - 1, 0, -1):
        j = r.randint(0, i)
        p[i], p[j] = p[j], p[i]
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--seed", type=int, required=True)
    a = ap.parse_args()
    sys.path.insert(0, os.path.join(a.root, "tools"))
    sys.path.insert(0, os.path.join(a.root, "tests", "fixtures"))
    import repo_gen, build_index_v2 as B, route_v2 as R
    n = 24
    docs = repo_gen.synth(n, seed=a.seed)
    perm = fisher_yates(n, a.seed)
    relabelled = []
    for i, d in enumerate(docs):
        c = dict(docs[perm[i]])
        for k in ("id", "fam", "sid"):
            c[k] = d[k]
        c["edges"] = []
        relabelled.append(c)
    tmp = tempfile.mkdtemp(prefix="hbwit")
    try:
        root = repo_gen.make_repo(os.path.join(tmp, "repo"), docs=relabelled)
        out = os.path.join(tmp, "ix")
        B.build(root, out, os.path.join(a.root, "tests", "fixtures", "thesaurus_empty.json"))
        ix = R.Index(out)
        words = sorted(repo_gen.WORDS)
        res = []
        for w in words:
            res.append(R.route(ix, w, {}, k=20, budget=2000))
        for i in range(0, len(words) - 1, 3):
            res.append(R.route(ix, words[i] + " " + words[i + 1], {"profile": "P1", "lcp5": 1}, k=20, budget=2000))
        print(json.dumps(res, sort_keys=True))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()

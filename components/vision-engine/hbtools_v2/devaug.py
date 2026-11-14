#!/usr/bin/env python3
"""T15: DEV-AUG authoring (degraded path: worker seats were vetoed with rc 43, see DEVIATIONS.md).

20 FIND + 10 IMPACT rows are written here from code slices only, by the implementing seat, WITHOUT running the ranker on them;
20 ABSENT rows come from a fixed capability list, each confirmed absent by a case-insensitive text search of tools/ and tests/ (the
rg step; the 'second seat' review is this script's assertion that every keyword has 0 hits); 10 CLAIM rows come from devaug_claims.py.
Checks (the Opus-seat substitute, mechanical): every gold id exists, every IMPACT gold test has a TESTS edge to its gold symbol, no
query repeats its gold identifier verbatim, no duplicate texts. Output eval/dev_aug.jsonl + sha256, out/dev_aug.json.

devaug.py [--out eval/dev_aug.jsonl]   (the overlap check against TEST is run by the orchestrator: tools/dev_overlap_check.py, counts only)
"""
import argparse, json, os, re, sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hblib2 as L  # noqa: E402
import devaug_claims as DC  # noqa: E402

FIND = [
 ("build_index_v2.py::aq_terms", "Which routine turns anchor text into at most two dozen distinct lowercase terms, keeping the order in which they first appear?"),
 ("route_v2.py::exact_ids", "Which function finds explicit paper identifiers inside a query and maps them, or their family names, to document positions?"),
 ("route_v2.py::zone_x", "Where is the per-field length-normalised term frequency contribution computed with an integer weight given in permille?"),
 ("gates_v2.py::rebuild_perturbed", "Which helper reruns the index builder in a child process with a changed time zone, epoch and hash seed?"),
 ("gates_v2.py::paper_anchors", "Which function reads the catalogue routes table and returns a mapping from node ids to the paper paths?"),
 ("eval_v2.py::objective_J", "Where is the exact mean tuning objective computed over the answerable queries for budgeted answers, with per-class means?"),
 ("make_pool_v2.py::kappa", "Which function computes chance-corrected agreement between two binary graders?"),
 ("route_v2.py::term_scores", "Which function turns the per-field term frequencies of one posting into saturated per-document scores, optionally with the negative zone?"),
 ("merge_logs.py::merge", "Which routine concatenates the per-agent query-log spool files into a single journal and aborts when a line is corrupt?"),
 ("route_v2.py::choose_anchor", "Which function selects the read anchor returned for a ranked document depending on the query intent and term overlap?"),
 ("route_v2.py::vposting", "Where is the posting of one query term assembled, including the union over indexed terms that share a five-letter prefix?"),
 ("make_pool_v2.py::write_json", "Which helper writes a JSON object with sorted keys and indentation to a file using LF newlines?"),
 ("eval_v2.py::write_freeze", "Which function records the digest map of the frozen files into a freeze file and returns that map?"),
 ("tune_v2.py::configs", "Where are the candidate tuning configurations generated as the cartesian product of the grid values?"),
 ("route_v2.py::rank", "Which function parses a query into terms, explicit ids, intent, weights and posting lookups before any scoring happens?"),
 ("gates_v2.py::g_size", "Which gate checks that every index file stays under the size cap and that the manifest records the largest size?"),
 ("eval_v2.py::per_query", "Where are recall at one and five, reciprocal rank and discounted gain computed for a single ranked list?"),
 ("make_pool_v2.py::norm_qid", "Which function repairs grader query ids that lost or gained a prefix and maps them only to pooled ids?"),
 ("gates_v2.py::g_fresh", "Which gate compares the digests recorded in the index manifest with the current sources to detect a stale index?"),
 ("route_v2.py::why_terms", "Which function picks the few matched query terms with the highest weight to explain a result?"),
]
IMPACT = [
 ("tune_v2.py::configs", "test_grid_size", "Modify how the tuning grid is expanded so that one switch can be pinned to a single value without changing the number of switches listed."),
 ("eval_v2.py::sign_flip_p", "test_permutation", "Change the exact sign-flip permutation test so that it enumerates every flip for up to twenty-four pairs instead of twenty."),
 ("make_pool_v2.py::kappa", "test_kappa_known", "Change the chance-corrected agreement statistic between two graders so that perfect expected agreement yields a value instead of nothing."),
 ("merge_logs.py::merge", "test_merge_fails_closed_on_corrupt_line", "Modify the single-writer log merge so that a corrupt spool line is quarantined instead of aborting the whole merge."),
 ("build_index_v2.py::idf_ppm", "test_idf_hand_computed", "Alter the inverse document frequency in the index builder to use a different smoothing constant while keeping integer parts per million."),
 ("gates_v2.py::g_size", "test_neg_size", "Change the file-size gate so that it also fails when a shard exceeds a smaller per-file cap than the manifest maximum."),
 ("route_v2.py::write_log", "test_log_line_content", "Extend the per-query journal line written by the router with the number of rows returned."),
 ("route_v2.py::vposting", "test_lcp5_semantics", "Revise how a query term's posting is assembled when the term shares its first five letters with several indexed terms."),
 ("eval_v2.py::perm_p", "test_permutation", "Change the paired permutation p-value between two systems so that it ignores queries without any relevant item."),
 ("route_v2.py::choose_anchor", "test_asset_anchor_dispatched_to_asset_resolver", "Change how the read anchor of a ranked document is chosen when the document lists anchors tagged with an intent."),
]
ABSENT = [
 ("Which module renders result pages to PDF documents for printing?", ["pdf", "reportlab"]),
 ("Where does the code send notification e-mails through an SMTP server?", ["smtp", "email", "e-mail"]),
 ("Which component opens a websocket connection to stream results to a browser?", ["websocket"]),
 ("Which routine authenticates users with OAuth tokens before answering a query?", ["oauth", "jwt"]),
 ("Where are database migrations applied to a SQL schema?", ["sqlite", "psycopg", "migration"]),
 ("Which function resizes and compresses images for thumbnails?", ["pillow", "jpeg", "thumbnail"]),
 ("Which code trains a neural network on the benchmark queries?", ["torch", "tensorflow", "neural", "backprop"]),
 ("Which module translates queries between natural languages?", ["translat"]),
 ("Where does the system apply rate limiting to concurrent callers?", ["ratelimit", "rate limit", "throttl"]),
 ("Which routine caches answers in Redis or memcached?", ["redis", "memcached"]),
 ("Which function parses YAML configuration files?", ["yaml"]),
 ("Which component exposes an HTTP server with REST endpoints?", ["flask", "fastapi", "http.server", "socketserver"]),
 ("Where is speech audio transcribed into text?", ["speech", "audio"]),
 ("Which code spell-checks the query text before searching?", ["spell", "soundex"]),
 ("Which routine encrypts index files with a symmetric cipher?", ["encrypt", "cipher", "aes"]),
 ("Where is a graphical user interface drawn with widgets?", ["tkinter", "widget", "curses"]),
 ("Which module exports metrics to Prometheus dashboards?", ["prometheus", "grafana"]),
 ("Which function schedules periodic jobs with a cron expression?", ["cron", "apscheduler"]),
 ("Where are sentence vectors computed with a transformer model?", ["transformer", "word2vec", "sentence"]),
 ("Which component handles multi-tenant authorization with role based permissions?", ["tenant", "rbac"]),
]


def corpus(subject):
    txt = ""
    for d in ("tools", "tests"):
        for f in sorted(os.listdir(os.path.join(subject, d))):
            if f.endswith(".py"):
                txt += open(os.path.join(subject, d, f), encoding="utf-8").read().lower() + "\n"
    return txt


def row(qid, cls, text, gold=(), gold_tests=(), intent="find_code", notes="", source=""):
    return {"class": cls, "gold": list(gold), "gold_tests": list(gold_tests), "intent": intent, "notes": notes, "qid": qid, "receipt": None,
            "source_event": source, "split": "DEV", "text": text, "verdict": None}


def build(subject, trace):
    syms = {s["id"] for s in L.read_jsonl(os.path.join(L.HB, L.BOOK_SUB, "symbols.jsonl"))}
    edges = {(t, s) for t, s in trace["edges"]}
    byname = {}
    for t, s in trace["edges"]:
        byname.setdefault(t.split("::")[1], set()).add(t)
    rows, problems = [], []
    for i, (g, t) in enumerate(FIND, 1):
        gid = "tools/" + g
        if gid not in syms:
            problems.append("missing gold " + gid)
        rows.append(row("HB-AF%02d" % i, "FIND", t, [gid], source="devaug.py authored from the code slice"))
        name = g.split("::")[1]
        if re.search(r"\b%s\b" % re.escape(name), t, re.I):
            problems.append("query repeats identifier: " + name)
    for i, (g, tn, t) in enumerate(IMPACT, 1):
        gid = "tools/" + g
        cand = [x for x in byname.get(tn, ()) if (x, gid) in edges]
        if gid not in syms or len(cand) != 1:
            problems.append("impact gold %s %s -> %s" % (gid, tn, cand))
            continue
        rows.append(row("HB-AI%02d" % i, "IMPACT", t, [gid], cand, "impact", source="devaug.py authored from the code slice"))
    text = corpus(subject)
    for i, (t, kws) in enumerate(ABSENT, 1):
        hits = {k: text.count(k) for k in kws}
        if any(hits.values()):
            problems.append("ABSENT not absent: %s %s" % (t, hits))
        rows.append(row("HB-AU%02d" % i, "ABSENT", t, notes="absent: 0 hits for " + ",".join(kws), source="fixed capability list"))
    rows += DC.build(subject)
    texts = [r["text"] for r in rows]
    if len(set(texts)) != len(texts):
        problems.append("duplicate texts")
    return rows, problems


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--subject", default=L.SUBJECT)
    ap.add_argument("--out", default=os.path.join(L.HB, "eval", "dev_aug.jsonl"))
    a = ap.parse_args(argv)
    rows, problems = build(a.subject, L.jload(os.path.join(L.HB, "out", "trace.json")))
    if problems:
        sys.stderr.write("\n".join(problems) + "\n")
        return 5
    L.write_jsonl(a.out, rows)
    h = L.sha_file(a.out)
    with open(os.path.splitext(a.out)[0] + ".sha256", "w", newline="\n") as f:
        f.write(h + "  eval/dev_aug.jsonl\n")
    cnt = {}
    for r in rows:
        cnt[r["class"]] = cnt.get(r["class"], 0) + 1
    L.write_json(os.path.join(L.HB, "out", "dev_aug.json"), {"schema": "hb.receipt.v2", "kind": "build", "tool": "devaug.py/2.0", "metrics": cnt,
                                                            "inputs": {"dev_aug_sha256": h}, "authoring": "implementing seat (worker seats vetoed rc 43)"})
    print(L.dumps(cnt), h[:12])
    return 0


if __name__ == "__main__":
    sys.exit(main())

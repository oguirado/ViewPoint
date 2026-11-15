#!/usr/bin/env python3
"""Stamp an hb_eval run receipt with the sha256 of the query file and of the book it ran over.
usage: stamp.py RUN.json QUERIES.jsonl BOOK.md"""
import hashlib, json, sys


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def main(argv):
    if len(argv) != 4:
        sys.stderr.write(__doc__)
        return 2
    run, queries, book = argv[1:]
    doc = json.load(open(run, encoding="utf-8"))
    doc["queries_sha256"] = sha(queries)
    doc["book_md_sha256"] = sha(book)
    with open(run, "w", encoding="utf-8") as f:
        json.dump(doc, f, sort_keys=True, indent=1)
        f.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

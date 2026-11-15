# QUICKSTART

Requirements: Python 3.11+; numpy >= 2.0 (not needed for `tools/`).

```
python3 tools/gates.py --all                      # rc 0 = all gates pass, rc 5 = a gate failed
python3 tools/build_catalogue.py                  # regenerate catalogue/*.tsv|json|jsonl from *.src
python3 tools/build_catalogue.py --check          # rc 4 if generated files are stale
python3 tools/route_code.py --q "format a field id"   # top-3 anchors; rc 3 = abstain
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -t .
```

Every CLI prints one canonical JSON object with `code` and `rc`. Exit codes: 0 OK, 2 usage/schema, 3 reject,
4 stale, 5 gate failed, 6 budget.

Edit flow: change code -> update `catalogue/*.src` if a threshold, failure code or route changed -> rebuild catalogue
-> run gates and tests.

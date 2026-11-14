# Contributing

vision-engine is licensed AGPL-3.0-only (see LICENSE and NOTICE).

- Sign off every commit (`git commit -s`, Developer Certificate of Origin).
- By submitting a contribution you also grant the project owner a perpetual,
  irrevocable licence to use, modify and relicense it, so that a dual-licensing
  option stays available. If you cannot agree to this, say so in the pull request.
- Before opening a pull request run:
  - `python3 tools/gates.py --all`
  - `python3 tools/build_catalogue.py --check`
  - `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -t .`
- Edit rules, layers and sealed trees are described in AGENTS.md.

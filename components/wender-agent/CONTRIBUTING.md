# Contributing to wender-agent

wender-agent is licensed AGPL-3.0-only (see `LICENSE`). By sending a contribution you agree that it
is licensed under the same terms.

- Sign off every commit (`git commit -s`, Developer Certificate of Origin 1.1).
- Run both suites before opening a pull request:
  `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -t .` and
  `node --test 'js/tests/*.test.js'`.
- Follow the edit rules in `AGENTS.md` (module headers, unique anchors, `python3 tools/reseal.py`
  after editing a HyperBook).
- Never commit screenshots, frames, logs, credentials or captures from real sessions.

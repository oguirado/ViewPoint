# wender-agent

Wender is the hard layer of Computer Use for AI agents: it lets an agent work on any web page or
desktop program, known or unknown, without recording it first. Not a browser. It pairs with
vision-engine (integer-only screen vision) for pixels.

- PERCEIVE: read ladder (structured state -> AX tree -> pixels via vision-engine -> vision model on
  crops -> blind coordinates). Output: a deterministic JSON STATE (`docs/STATE.md`).
- ACT: most structured path first (app API/MCP -> AX/DOM -> coordinates); one actor at a time.
- REMEMBER: program map graph + skills stored only after verified reproduction.
- LEARN: demonstrations -> segments -> intents -> structured actions -> verified skills.

Status: v0 scaffold. See `AGENTS.md` for what is real and what is a stub.

```
PYTHONDONTWRITEBYTECODE=1 CHROME_BIN=/usr/bin/google-chrome python3 -m unittest discover -s tests -t .
node --test 'js/tests/*.test.js'
```

Optional environment: `VISION_ENGINE_ROOT` (default `../vision-engine`); `SRF_IDENTITY_BINARY`
(default: `srf-identity-surface` on PATH, built by `../OpenSuperficies`); `SRF_IDENTITY_ENV`;
`OPENSUPERFICIES_ROOT` / `SRF_HOLDINGS_CAPSULE_BINARY` (used by `tools/cu_live.py`);
`WENDER_CU_TASKS_FILE` (the Holdings task corpus, not shipped here; tests that need it are skipped when absent).

## Licence

AGPL-3.0-only. See `LICENSE` and `NOTICE`. If you run a modified version as a network service,
AGPL section 13 requires offering its source to its users.

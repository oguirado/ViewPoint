# AGENTS.md - wender-agent 0.0.0 (v0 scaffold)

Wender is the hard layer of Computer Use for AI agents: it lets an agent work on any web page or
desktop program, known or unknown, without recording it first. It is NOT a browser. Pixels come
from the vision-engine component (`../vision-engine`, or `$VISION_ENGINE_ROOT`) (integer-only screen vision); Wender never re-implements it.

## Ladder (read the cheapest rung that answers; stop early)
1. R0 `llms.txt` (<= 600 B): purpose and links.
2. R1 this file (<= 6,000 B): ladder, layers, ownership, edit rules.
3. R2 `python3 tools/anchors.py --q "<word>"`: matching `# @anchor:` ids with file:line.
4. R3 the `# @module {...}` header of the file (first line).
5. R4 the slice from `# @anchor:<id>` to the next anchor.
6. R5 the HyperBook of the area: `hyperbooks/HB-WA-*/book.md`.
Budget: task -> anchor in <= 3 reads + 1 slice.

## Architecture (HB-WA-a0-arch)
| Pillar | Package | v0 state |
|---|---|---|
| PERCEIVE | `wender/perceive/` | real: cdp_pipe, ax, browser, atspi (discovery), ladder; stub: vision_bridge |
| ACT | `wender/act/` | real: path choice, single-actor lock; stub: executors |
| REMEMBER | `wender/remember/` | real: in-memory program map, skill admission; stub: persistence |
| LEARN | `wender/learn/` | real: reproduced-share metric; stub: segment/label/map/replay |
| STATE | `wender/state/` | real: `wender.state/v1` build/validate/canonical dump |

Read ladder (perception): 1 structured state, 2 AX tree (CDP / AT-SPI), 3 pixel diff
(vision-engine), 4 vision model on engine-selected crops, 5 blind coordinates.
Act ladder: app API/MCP -> AX/DOM action -> coordinates. One actor per surface; readers in parallel.

## Layers (a module imports only the same or a lower layer)
| L | modules |
|---|---|
| 0 | state.schema, perceive.ladder, js/inpage/* |
| 1 | perceive.cdp_pipe, perceive.ax, perceive.atspi, act.actor, remember.* |
| 2 | perceive.browser, perceive.vision_bridge, learn.* |
`wender` never imports vision-engine code in v0 (bridge contract is TARGET).

## Ownership
PERCEIVE: perceive/, js/inpage/. ACT: act/. REMEMBER: remember/. LEARN: learn/. STATE + ARCH:
state/, tools/, hyperbooks/, docs/, AGENTS.md, llms.txt. rust/ is a placeholder (ARCH).

## Stack ruling
Rust = low level (rust/, empty in v0). JavaScript = in-page and browser layer (js/inpage, plain
node, no npm deps at test time). Python 3.11+ stdlib = agent layer (wender/).

## Rules for edits
- Every source file starts with `# @module {json}` (JS: `// @module {json}`); every public symbol
  has `# @anchor:wa.<pkg>.<unit>.<symbol>`. Anchors are unique (test_repo_conventions).
- Never put raw AX/HTML into model context: only the `wender.state/v1` STATE (docs/STATE.md).
- STATE is canonical JSON: sorted keys, compact, integers only (boxes in CSS px, ratios in ppm).
- Hashes cover only prune_ax_v1 fields; boxes, CDP ids and focus flags are never hashed.
- STORAGE LAW: no frames or screenshots on disk, ever (RAM only).
- Chromium: `--remote-debugging-pipe` only (no TCP port); user-data-dir via
  `tempfile.mkdtemp(dir="/tmp")` (unix socket path limit 108 B); kill only the process you started.
- Skills are stored only after a verified reproduction (`remember.skills.admit`).
- Numbers carry [MEASURED, <evidence>], [CITED, <source>] or [TARGET]; unknowns say AUSENTE.
- Ported code credits its origin path in the module docstring.
- HyperBooks: edit book.md / mathlinks.jsonl, then `python3 tools/reseal.py` (manifest hashes).

## Commands
- Python tests: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -t .`
  (Chromium integration runs only with `CHROME_BIN=/usr/bin/google-chrome`; skipped otherwise).
- JS tests: `node --test 'js/tests/*.test.js'`.
- Reseal books: `python3 tools/reseal.py` (`--check` rc 4 if stale).
- Anchors: `python3 tools/anchors.py [--q word]`.

## External web agents
Work on a branch `web/<tool>/<task-id>`; never push to `main`. VM timings are [VM], never [MEASURED].

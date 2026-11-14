# How to use this route

`route/` is a cited map of this repository for AI readers: findings with byte-pinned citations, a card, and a
compiled context pack per task. It is standard library only, offline, and runs from the repository root with
`python3 -I route/route.py <command>`. Findings are model output, not proof: read the cited code before you act.

## Commands

| Command | Returns | o200k tokens |
|---|---|---|
| `card` | L0 card: what the repo is, entry points, laws, traps, where to look | ~1,600 |
| `locate "<symptom>" --k N` | files to open, ranked, each with the signals that matched (path, symbol, literal, finding, question) | ~300 |
| `ask "<question>" --k N` | findings that answer it, each with its state and cited code | ~1,200 for k 5 |
| `pack "<task>" --budget N` | Markdown brief: card, findings, fresh dossiers, then cited code; never over N | ≤ N |
| `show <path::symbol\|path#La-b>` | the cited code | varies |
| `file <path>` | symbols and findings of one file | ~1,500 |
| `find <symbol>`, `tests <symbol\|path>`, `history <path>` | definitions; tests that name it; commits that touched it (fixes flagged) | ~200 |
| `check` | citation states for the whole route against the working tree | ~80 |
| `map`, `stats`, `build` | folder map; counts and study pin; rebuild `route/index/` | small |

Examples (run by `tests/test_route.py`, so they stay valid):

```sh
python3 -I route/route.py card
python3 -I route/route.py locate "screenshot of an electron window" --k 5
python3 -I route/route.py ask "screenshot of an electron window" --k 3
python3 -I route/route.py pack "screenshot of an electron window" --budget 3000
python3 -I route/route.py file vis
python3 -I route/route.py find parse_vis_stdout
python3 -I route/route.py check
```

## What to trust

| State | Meaning | Use |
|---|---|---|
| FRESH | the cited bytes are unchanged at the same place | trust the location, still read the code |
| SHIFT | the same bytes moved; the route points at the new place | as FRESH |
| STALE | the cited bytes changed or the file is gone | re-read the code; the finding may be wrong |
| UNKNOWN | no byte citation | a hint only |

Folder dossiers (L1) cite whole files: one changed byte makes the dossier STALE, and `pack` leaves STALE dossiers
out. `check` reports dossiers apart from findings. Ranking is lexical: use the words the code uses.

## Budgets and the index

`pack` estimates tokens as bytes/3, at or above the real o200k count on 201 calibration packs, and keeps the last
35 % of the budget for cited code. The first call writes `route/index/` (gitignored); any call rebuilds it when
HEAD has moved. Uncommitted edits do not trigger a rebuild: run `build` after them.

## After a commit

Findings are pinned to the study commit (`stats` shows it, `route/manifest.json` lists every delta). After a
change, the route keeps working, with the touched citations turning SHIFT or STALE. To refresh: re-study the
changed files, add their findings with citations hashed on the new tree, drop old findings whose bytes changed in
those files, and keep the rest. `check` must then show no STALE findings.

## Sibling repositories

- `newton-py`: the Python port of newton-shell and its skill.
- `relevo-skill`: campaign memory across context compactions.
- `swarm-skill`: policy-approved worker waves.
Each has its own `route/` with the same commands.

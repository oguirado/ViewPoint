# vp docs: the documentation and command router

`vp docs` answers "which command, which file, which line" for ViewPoint, offline, with bounded output.
Entry point: `python3 -I product/agent_interface/router/vp_docs.py <command>` (the `vp` dispatcher forwards
`vp docs <command>` to it). Every call ends with one JSON receipt line `{ok, code, rc, data, next}`.

## Commands

| Command | Returns | Size |
|---|---|---|
| `vp docs card` | L0 card: what ViewPoint is, safety rules, first commands, where to look | about 1,500 tokens |
| `vp docs ask "<question>" --k N` | findings that answer it, each `[STATE] file:line (kind, coverage)` plus text | k rows, k at most 20 |
| `vp docs pack "<task>" --budget N` | byte-stable Markdown brief: card, findings, cited code, files to open | never over N tokens (bytes/3) |
| `vp docs locate "<symptom>"` | files to open, ranked, with the signal that matched | k rows |
| `vp docs route "<intent>"` | the `vp` commands that do it (whole-command ranking over `design/COMMANDS.md` plus the family pages), each cited, and the team skill from `newton route` | 3 commands |
| `vp docs show <path#La-b>` or `<path:a-b>` | the cited lines | 4,096 characters, rest paged |
| `vp docs file <path>` | findings and symbols of one corpus file | k rows |
| `vp docs find <symbol>` | definitions in the indexed code | k rows |
| `vp docs stats` | counts of the index (files, findings, symbols) | one line |
| `vp docs check` | counts of FRESH, SHIFT, STALE for the whole route | one line |
| `vp docs build [--strict]` | regenerates `findings.jsonl` and `manifest.json` | one line |

All commands accept `--dry-run` (code `DRY_RUN`; reads and writes nothing) and `--json` (receipt line only).
Human text is capped at 4,096 characters per call; a pack is always spooled whole and the receipt carries
`data.spool = {path, lines, bytes, sha256}` with `next: ["vp out read <path> 1:60"]`.

## Receipt codes

Codes and exit classes follow `design/COMMANDS.md` section 4 (vpops/1.1).

| code | rc | Meaning | What to do |
|---|---|---|---|
| `OK` | 0 | answered | follow `next` |
| `DRY_RUN` | 0 | `--dry-run`: `data.argv` holds what would run | run again without the flag |
| `AUDIT_VIOLATIONS` | 1 | `check` found STALE citations, or `build --strict` could not pin a seed | `vp docs build`; fix the seed's `path` or `anchor` |
| `USAGE` | 2 | bad arguments | fix the command line |
| `AUSENTE` | 5 | the corpus does not cover the question or intent | do not invent; try `vp docs locate`, other words, or ask the operator |
| `NOT_FOUND` | 5 | `show`: the citation cannot be read | copy the citation from `vp docs ask` |
| `DEPENDENCY` | 8 | newton-py `route/route.py` not found (`data.missing`) | set `VP_ROUTE_ENGINE`, or read `docs/AGENT_GUIDE.md` directly |

## Freshness marks

Each finding cites bytes, not just a line number. At answer time the cited bytes are hashed again.

| State | Meaning | Use |
|---|---|---|
| FRESH | the cited bytes are unchanged at the same place | trust the location, still read the code |
| SHIFT | the same bytes moved; the citation shows the new lines | as FRESH |
| STALE | the cited bytes changed or the file is gone | re-read the file; run `vp docs build` |
| UNKNOWN | no byte citation | a hint only |

## AUSENTE rule

A question is split into terms (stop words dropped, singular and plural matched together). Each term weighs
its inverse document frequency; a term found nowhere in the corpus gets the maximum weight. If the best
finding covers less than 0.5 of the total weight, the answer is `AUSENTE` (rc 5) with the absent terms listed.
Ranking is lexical: use the words the commands and the code use.

## What is reused from newton-py and what is local

| Piece | Origin |
|---|---|
| Ranking (`rank_nodes`), citation states (`cite`, `row`), `pack`, `locate`, `show`, `find`, `file`, `check`, index writing | newton-py `route/route.py`, class `Map`, imported at run time, not copied |
| Paging of long output (`vp out read`, that is `newton read`) and the skill half of `route` (`newton route`) | newton-py executable |
| Corpus list `sources.json`, seeded facts `seeds.jsonl`, the builder, the AUSENTE rule, the receipt line | this directory |

The engine is pinned in `manifest.json` (`engine.sha16`). `VP_ROUTE_ENGINE` overrides its path.

## Corpus and rebuild

`sources.json` lists documentation pages and key code. Each entry is a list of alternatives: a path inside
this repository first, then a sibling worktree. `seeds.jsonl` holds one fact per line:
`{"family", "kind", "text", "path", "anchor", "span"}`; `anchor` is a literal that must occur exactly once in
`path`, and the citation is pinned to the line that holds it. `vp docs build` turns every paragraph, list item,
table row and code fence of the documentation pages, plus every seed, into a finding with a byte-pinned
citation. Run it after editing any page; `vp docs check` then shows no STALE findings for files in this
repository. The index under `router/index/` is a cache and rebuilds itself when a corpus file changes.

`pack` output has no timestamps, process ids or host state, so two calls on the same tree give the same
bytes (the receipt carries the `sha256`). It is also written to the spool directory (`VP_SPOOL_DIR`, default
`$VP_OPS_STATE/spool`, that is `$XDG_RUNTIME_DIR/viewpoint-ops/spool`).

## Tests

`python3 -I product/agent_interface/router/tests/test_router.py` runs 12 questions with expected top
citations (`tests/questions.json`) and the negative controls (absent topic, edited citation, moved citation,
bad seed, missing engine, bad usage, dry-run writes nothing). The family pages are generated by
`docs/build_pages.py` from `design/MCP_TOOLS.json`, `docs/family_notes.json` and `seeds.jsonl`; run it before
`vp docs build` when the command table changes.

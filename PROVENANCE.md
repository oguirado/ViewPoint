# Provenance

Who and what produced each part of this repository. Licence for all of it:
`AGPL-3.0-only` ([LICENSE](LICENSE), [NOTICE-EVALUATION.md](NOTICE-EVALUATION.md)).

## Map

| Path | Origin | Authorship | Record |
|---|---|---|---|
| `core/`, `daemon-adapter/`, `contracts/`, `agents/`, `ui/`, `route/`, `router/`, `catalogue/`, `memoryweb-builtin/`, `tools/` (except `tools/vis-skill/`), `scripts/`, `tests/`, `docs/`, `examples/` | MetaViewer, pre-existing before the hackathon (v0.2.6) | Maintainer with AI coding tools (Meta Muse Spark 1.3 as the main code generator, plus other assistants); exceptions made during assembly by Claude: the vendored mode of `agents/metaviewer_agents/perception/deps_lock.py`, the licence lines of the core docs, and the router refresh: `tools/router_index.py` accepts the tracked in-repo symlink `tools/vis-skill/vision` (written by Muse Ember `muse-ember-1.0`, reviewed by Claude) and skips link checks in immutable hackathon briefs and proof logs (Claude); `route/symbols.jsonl` rows for three removed test files dropped (Muse Ember); `route/docs.jsonl`, `catalogue/` and `router/index/` rebuilt with `tools/rebuild_monorepo_indexes.py` | [docs/PREEXISTING.md](docs/PREEXISTING.md), [META_AI_USAGE.md](META_AI_USAGE.md), [CHANGELOG.md](CHANGELOG.md) |
| `components/vision-engine/`, `components/wender-agent/`, `components/OpenSuperficies/` | Maintainer's own engines, pre-existing, vendored at the commits pinned in [deps.lock](deps.lock) | Maintainer with AI coding tools | each component's README and `deps.lock` |
| `tools/vis-skill/` | The maintainer's vision CLI (`vision`/`vis`), a separate private repository, vendored during assembly | Maintainer with AI coding tools; per-file authorship is not recorded here | its `README.md` and `docs/` |
| `hackathon/swarm/` | Hackathon build, Track H | Gemini (`gemini-4-hydrogen-high` via the Antigravity CLI `agy`), with logs, except the 7 files edited by Claude during assembly (listed below) | [hackathon/PROVENANCE.md](hackathon/PROVENANCE.md), `hackathon/swarm/proof/` |
| `hackathon/product/` | Hackathon build, Track P | Claude (`claude-opus-5-5`, `claude-fable-5-2`), declared as non-Gemini | [hackathon/PROVENANCE.md](hackathon/PROVENANCE.md), `hackathon/product/proof/` |
| `hackathon/gateway/`, `hackathon/mcp/`, `hackathon/launcher/`, `hackathon/docs/` | Hackathon build (Cloud Run gateway, `viewpoint-agent` MCP server, docs) | see [hackathon/PROVENANCE.md](hackathon/PROVENANCE.md) and [hackathon/README.md](hackathon/README.md) | same |
| Repository assembly: root `README.md`, `INSTALL.md`, this file, `NOTICE-EVALUATION.md`, `OWNERSHIP.json`, the licence lines of the core docs, the `tools/check_public.sh` copy, and the assembly commits `ec07799`..`e682a57` (including the vendored mode of `agents/metaviewer_agents/perception/deps_lock.py`) | Assembly of the core, the engines and the hackathon layer into one tree (14–15 Nov 2026) | Claude Code sessions (`claude-opus-5-5`), declared; per-file attribution inside those commits was not recorded | branch `assembled` on GitHub keeps commits `ec07799`..`e682a57`; `main` receives them as one commit on top of `4af2fe5` |

Nothing written by Claude is labelled as Gemini work. Gemini work is the Track H rows of
[hackathon/PROVENANCE.md](hackathon/PROVENANCE.md) only.

## Edits made during assembly to files from the hackathon release

The hackathon layer comes from release `v0.3.0-hackathon` (commit `79a69b5`) and commit
`4af2fe5` (H5_r3 viewer explainer, Gemini). During assembly Claude made these changes; they
are not Gemini-authored:

| File (under `hackathon/`) | Change |
|---|---|
| `swarm/agents/__init__.py`, `swarm/agents/prompts.py`, `swarm/agents/test_agents.py` | Hard-coded checkout path replaced by `REPO_ROOT` from `$VP_HACKATHON_ROOT` or the file location |
| `swarm/broker/broker.py` | Vision CLI default `$VP_VISION_CLI` or `tools/vis-skill/vision`; events log under `$VP_HACKATHON_ROOT` |
| `swarm/broker/bridge.py` | Path in the usage docstring made relative (no code change) |
| `swarm/projection/projector.py`, `swarm/projection/test_projection.py` | External tool paths from `$VP_OSB_CLI` / `$VP_CW_REPO` with relative defaults; events log under `$VP_HACKATHON_ROOT` or the file location |
| `product/proof/P5_1252.jsonl`, `product/proof/P6_1252.jsonl` | Numeric user id in temp paths redacted to `$UID` |
| `README.md` | Assembly header added above the original README |
| `PROVENANCE.md` | Moved from the repository root to `hackathon/` (content unchanged) |
| `LICENSE` | Added: a copy of the root `LICENSE` (AGPL-3.0-only), so the README's licence link resolves |

Everything else under `hackathon/` is byte-identical to `origin/main` at `4af2fe5`
(previous root paths `swarm/`, `product/`, `gateway/`, `launcher/`, `mcp/`, `docs/`,
`tools/` now live under `hackathon/`; `tools/check_public.sh` moved to the root `tools/`;
`LICENSE` stays at the root, unchanged).

## What moved off `main`

| What | Where it is now | Why |
|---|---|---|
| `product/` at the root (Claude reference stack: `ref_*` units, `ref_demo`, the `vp` CLI and its MCP server) | branch `claude-reference` | Written by Claude after the hackathon as reference and comparison material; nothing on `main` imports it and ViewPoint runs without it |

`CLAUDE.md` stays: it is the MetaViewer core's 7-line pointer to [AGENTS.md](AGENTS.md)
for Claude Code users, and `tests/test_route_docs.py` checks it.

The branch `assembled` keeps the earlier assembled tree (with `product/`) for reference.

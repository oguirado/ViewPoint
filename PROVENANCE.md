# Provenance

Who and what produced each part of this repository. The root licence is [AGPL-3.0-only](LICENSE). The [repository notice](NOTICE-EVALUATION.md) records component and third-party terms.

## Map

| Path | Origin | Authorship | Record |
|---|---|---|---|
| `core/`, `daemon-adapter/`, `contracts/`, `agents/`, `ui/`, `route/`, `router/`, `catalogue/`, `memoryweb-builtin/`, `tools/` (except `tools/vis-skill/`), `scripts/`, `tests/`, `docs/`, `examples/` | MetaViewer, pre-existing before the hackathon (v0.2.6) | Maintainer with AI coding tools (Meta Muse Spark 1.3 as the main code generator, plus other assistants); exceptions made during assembly by Claude: the vendored mode of `agents/metaviewer_agents/perception/deps_lock.py`, the licence lines of the core docs, and the router refresh: `tools/router_index.py` accepts the tracked in-repo symlink `tools/vis-skill/vision` (written by Muse Ember `muse-ember-1.0`, reviewed by Claude) and skips link checks in immutable hackathon briefs and proof logs (Claude); `route/symbols.jsonl` rows for three removed test files dropped (Muse Ember); `route/docs.jsonl`, `catalogue/` and `router/index/` rebuilt with `tools/rebuild_monorepo_indexes.py` | [docs/PREEXISTING.md](docs/PREEXISTING.md), [META_AI_USAGE.md](META_AI_USAGE.md), [CHANGELOG.md](CHANGELOG.md) |
| `components/vision-engine/`, `components/wender-agent/`, `components/OpenSuperficies/` | Maintainer's own engines. [deps.lock](deps.lock) records the upstream revisions and the separate digests of the adapted vendor trees. | Maintainer with AI coding tools, as credited in the component notices | [Vision Engine notice](components/vision-engine/NOTICE), [Wender Agent notice](components/wender-agent/NOTICE), [OpenSuperficies third-party notices](components/OpenSuperficies/THIRD_PARTY_NOTICES.md) |
| `components/blender-visual-qa/` | Maintainer-owned Blender and Godot Visual QA sources and subsequent ViewPoint work | Maintainer with AI coding tools | [Import and authorship](#blender-visual-qa-import-15-nov-2026) |
| `components/asset-factory/` | Scoped Asset Factory baseline from the ViewPoint integration history | Muse Ember prepared context. Gemini Carbon compiled and verified the offline packet. Sol and Claude Sonnet reviewed it. Codex rebased the selected sources and applied the review fixes. | The [component notice](components/asset-factory/NOTICE) credits oguirado. The [upstream patch notices](components/asset-factory/providers/trellis2/build/THIRD_PARTY_NOTICES.txt) apply separately. Downstream patch credits are AUSENTE. |
| `tools/vis-skill/` | The maintainer's vision CLI (`vision`/`vis`), a separate private repository, vendored during assembly | Maintainer with AI coding tools; per-file authorship is not recorded here | its `README.md` and `docs/` |
| `hackathon/swarm/` | Hackathon build, Track H | Gemini (`gemini-4-hydrogen-high` via the Antigravity CLI `agy`), with logs, except the 7 files edited by Claude during assembly (listed below) | [hackathon/PROVENANCE.md](hackathon/PROVENANCE.md), `hackathon/swarm/proof/` |
| `hackathon/product/` | Hackathon build, Track P | Claude (`claude-opus-5-5`, `claude-fable-5-2`), declared as non-Gemini | [hackathon/PROVENANCE.md](hackathon/PROVENANCE.md), `hackathon/product/proof/` |
| `hackathon/gateway/`, `hackathon/mcp/`, `hackathon/launcher/`, `hackathon/docs/` | Hackathon build (Cloud Run gateway, `viewpoint-agent` MCP server, docs) | see [hackathon/PROVENANCE.md](hackathon/PROVENANCE.md) and [hackathon/README.md](hackathon/README.md) | same |
| `site/` | Project site published at https://oguirado.github.io/ViewPoint/ (copied from its working folder on 15 Nov 2026, scratch folder `_work/` excluded) | Page, CSS, JavaScript, diagrams and data files by Claude (`claude-opus-5-5`); section copy by Gemini (`gemini-4-hydrogen-high`); the 3D pieces shown were built by Gemini agents; fact audits by Sol 6.1, design reviews by Gemini Carbon | [site/PROVENANCE.md](site/PROVENANCE.md) |
| Repository assembly: root `README.md`, `INSTALL.md`, this file, `NOTICE-EVALUATION.md`, `OWNERSHIP.json`, the licence lines of the core docs, the `tools/check_public.sh` copy, and the assembly commits `ec07799`..`e682a57` (including the vendored mode of `agents/metaviewer_agents/perception/deps_lock.py`) | Assembly of the core, the engines and the hackathon layer into one tree (14–15 Nov 2026) | Claude Code sessions (`claude-opus-5-5`), declared; per-file attribution inside those commits was not recorded | branch `assembled` on GitHub keeps commits `ec07799`..`e682a57`; `main` receives them as one commit on top of `4af2fe5` |

The hackathon work is recorded in the Track H rows of [hackathon/PROVENANCE.md](hackathon/PROVENANCE.md). The site, assembly and Visual QA records identify additional work by author and role.

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

## Hardening after the first public push (15 Nov 2026, 02:45–05:30 CET)

Goal: hosted CI green without repository secrets, and a repository a new engineer can clone and use on a DGX Spark class machine or an Ubuntu x86_64 machine. Method: units with exclusive file surfaces, each diff reviewed by a second model family, final review by two judges. Commit messages do not name tools (repository law); the authorship is recorded here.

| Change | Files | Written by | Reviewed by |
|---|---|---|---|
| Secret-free CI: public kit checkout, vendored engine verification, narrow gitleaks allowlist, workflow regression tests | `.github/workflows/{ci,package,volume,build-other,assembly}.yml`, `.github/workflows/vendored_assembly.py`, `.github/workflows/test_secret_free.py`, `.gitleaks.toml`, `tests/fixtures/secrets/*`, `tests/test_gitleaks_config.py`, `tests/ci/test_ci_wender_agents.py`, `tests/test_assembly_smoke.py` | Sol 6.1 (`gpt-6.1-sol`) | hosted CI; Claude Opus judge |
| Quickstart workflow (x86_64 and arm64 runners) | `.github/workflows/quickstart.yml` | Muse Ember (`muse-ember-1.0`); two probe and sysctl fixes by Claude | hosted CI |
| Clean-install audit on a DGX Spark class host (no repository edits) | — | Gemini Carbon (`gemini-4-carbon-medium`) | — |
| Install guides for DGX Spark and x86_64 | `INSTALL.md`, `docs/INSTALL.md` | Muse Ember; one test-conformance fix by Claude | Gemini Hydrogen, Sol 6.1 judge |
| `vision doctor` no longer crashes on vendored engines and still fails closed | `tools/vis-skill/vis`, `tools/vis-skill/tests/test_vision.py` | Muse Ember | Gemini Hydrogen |
| Frozen-dependency gate accepts vendored `deps.lock` entries | `tools/gate.sh`, `tests/test_gate_deps.py` | Muse Ember | hosted CI |
| Router hygiene mirror and kill-guard scope (hackathon kill sites pinned, not excluded) | `tests/test_hygiene.py`, `tests/test_kill_guard.py`, `.gitignore` | Muse Ember, then Sol 6.1 after the judge review | Claude Opus judge |
| README links, platform table, CI badges; integration, index rebuilds and verification | `README.md`, `route/`, `catalogue/`, `router/index/` | Claude (`claude-opus-5-5`) | hosted CI |
| Logo and social preview images | `docs/media/logo.png`, `docs/media/social_preview.png` | Renders of the project's own logo, a 3D model built in Blender by four Gemini agents (`gemini-4-hydrogen-high`, Antigravity CLI `agy`) on 14 Nov 2026 16:31–16:47 CET (take `1114_1628_multiagent_logo`) from the 2D ViewPoint logo; cropped, resized and composed with the title text by Claude (`claude-opus-5-5`) | — |

The hackathon code itself (Track H, Gemini) was not edited in this pass; the kill-guard law now covers it with an explicit, pinned list of its eleven existing kill sites.

## Release v0.3.1-hackathon (15 Nov 2026)

| Change | Files | Written by |
|---|---|---|
| Project site moved into this repository and published with GitHub Pages | `site/`, `.github/workflows/pages.yml` | site: see its own provenance; workflow by Claude (`claude-opus-5-5`) |
| README clips of the hackathon build | `docs/media/{gemdroid-intro,wall-4-gemini,blender-4-agents,connect4-domino,rover-drone-mujoco,fight-3-2}.gif` | Cut from the final demo video and the site's game loops (see [site/PROVENANCE.md](site/PROVENANCE.md)); GemDroid adapts Android robot artwork created and shared by Google under [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/) |
| Contact page, release notes, README links | `CONTACT.md`, `docs/releases/v0.3.1-hackathon.md`, `README.md` | Claude (`claude-opus-5-5`) |

## What moved off `main`

| What | Where it is now | Why |
|---|---|---|
| `product/` at the root (Claude reference stack: `ref_*` units, `ref_demo`, the `vp` CLI and its MCP server) | branch `claude-reference` | Written by Claude after the hackathon as reference and comparison material; nothing on `main` imports it and ViewPoint runs without it |

`CLAUDE.md` stays: it is the MetaViewer core's 7-line pointer to [AGENTS.md](AGENTS.md)
for Claude Code users, and `tests/test_route_docs.py` checks it.

The branch `assembled` keeps the earlier assembled tree (with `product/`) for reference.

## blender-visual-qa import (15 Nov 2026)

Paths in this section are relative to `components/blender-visual-qa/` where no repository-root path is stated.

Originally imported on `feat/visual-qa-3d`. Source: the 306 tracked files of
`blender-visual-qa` at `b5c6d76`, minus `.wavemind/` (web-agent exchange reports).
Edits to the imported files:

| File | Edit |
|---|---|
| `blender_visual_qa/judge/__init__.py` | default worker and shim paths rewritten without personal home directories |
| `tools/gates.py` | `_resolve_repo` resolves `@monorepo` and `env:` sources without personal home paths |
| `tools/VENDORED.json`, `tools/MIGRATION_SOURCE.json`, `tests/golden/truth/bt_manifest.json` | pinned to `@monorepo` (`components/vision-engine` at `8dec8e83`) or `env:BVQA_MIGRATION_SRC` |
| `tests/test_bt_kill.py`, `tests/test_bpy_side_w9.py` | engine checkout from `BVQA_VISION_ENGINE_SRC`, default repo-relative `components/vision-engine` |
| `hyperbooks/HB-BVQA-{DRIVE,ORACLES,TRUTH}.md`, `reports/W10_RELAUNCH_PLAN.md`, `blender_visual_qa/drive/PROVENANCE.md`, `tests/test_w9_pin_entry.py`, `docs/negative-knowledge/lessons.json` | absolute home paths replaced by repository-relative or neutral text |
| `blender_visual_qa/drive/profiles/*.json` | tracked with `git add -f` because the root `.gitignore` ignores `profiles/` |

Added: `route/` (index v2, query CLI, frozen benchmark) and `tests/test_route_v2.py`.

Phase C added `vqa3d/`, `tests/test_vqa3d_core.py`, `tests/test_vqa3d_adapters.py` and the root
`tests/test_vqa3d_core.py`. The changes were implemented from a written specification, independently reviewed, integrated and re-tested. The integration also updated `tests/test_route_v2.py` and `tests/test_bvqa_route.py` to cover MuJoCo.
`catalogue/` was imported from `blender-visual-qa` (`b5c6d76`), both new routes (`report reconcile` -> `bv.report.reconcile` and `route a text question to a code anchor` -> `tools.routecode.main`) are registered in `catalogue/routes.src` and refreshed via `tools/refresh_catalogue.py`, and `catalogue/router/catalogue/{routes.tsv,symbols.jsonl,thresholds.tsv}` + `hyperbooks/nodes.jsonl` updated via `catalogue/router/build_router_catalogue.py` (historical PR1/d6da01d9 baseline: `tools/refresh_catalogue.py --check` formerly flagged only the three `router/catalogue/{routes.tsv,symbols.jsonl,thresholds.tsv}` projections built by `build_router_catalogue.py`, returning `rc=4`; current result: `tools/refresh_catalogue.py --check` returns `rc=0` with ownership delegated to `catalogue/router/build_router_catalogue.py`; the historical integration receipt is retained in the campaign evidence). `examples/cube3.blend` was regenerated cleanly in PR1 with `/usr/bin/blender` via `bpy_side/scene_example.py` using isolated `HOME` and `/tmp` output (zero personal home paths).

Round 2 and profiles (16 Nov 2026). Findings came from the takes 1631, 1827, 0448 and the nightclub take, reported by
the video montage session. The core and Blender, `blender_live`, and profile changes received independent review.
The film profile was checked against independent bpy measurements and revised. Integration included a film profile
re-run on take 1827 and a docstring edit (`unit_quat_ppm`). That docstring had pushed the
TEST bench below its frozen floor; floors, queries and the router were not changed.

PR1 publication reconstruction and fix rounds (18 Nov 2026): under the operator’s recorded ownership and licensing decisions, both `godot-visual-qa@8575ca2` and `blender-visual-qa@b5c6d76` are the operator's own repositories whose `"Private repository; no license is granted"` `LICENSE` notice was the operator's own placeholder; operator-owned code (including material derived from `godot-visual-qa@8575ca2` and `blender-visual-qa`) is relicensed `AGPL-3.0-only`, while third-party material keeps its own terms. Added `components/blender-visual-qa/LICENSE` (`AGPL-3.0-only`) and `license = {text = "AGPL-3.0-only"}` in `components/blender-visual-qa/pyproject.toml`; omitted `tools/UPSTREAM_LICENSE` and updated `copy_manifest.tsv` and `migration/copy_manifest.tsv` (`LICENSE -> LICENSE REWRITE` and refreshed `dst_sha256` digests). Adapted `tools/route_code.py` (`anchor-prefix-and-route-doc`) and `tools/build_code_index.py` (`bvqa-adapt`), and kept `tools/build_catalogue.py` byte-identical to `8dec8e83:components/vision-engine/tools/`. The component's `tools/VENDORED.json` and `tools/gates.py:g_vendor` record that selection; pinned `tools/VENDORED.json`, `config/vision.json` (`mode:"shadow"` for B0–B5 `vision_stage`, distinct from `config/vision_asset_calib.json` `vision_mode:"on"` for calibrated asset turntables), `tools/ADOPTED.json`, `tests/golden/truth/params_change_proof.json`, and `tests/{test_law_assets,test_repin_law2}.py` to `8dec8e83:components/vision-engine`, and built `tools/wheels/vision_engine-1.1.0-py3-none-any.whl` deterministically from that tree; replaced `blender_visual_qa/drive/{png.py,launch.py}` with clean-room `AGPL-3.0-only` implementations (see [DRIVE provenance](components/blender-visual-qa/blender_visual_qa/drive/PROVENANCE.md)) that drain the owned process group even after leader exit; restricted `vqa3d/adapters/blender.py` `validate_environment` against `DISPLAY`, `WAYLAND_DISPLAY`, `XAUTHORITY`, `XDG_*`, `DBUS_*`, and profile overrides; regenerated `examples/cube3.blend` cleanly via `bpy_side/scene_example.py`; scrubbed all personal and home-relative paths in `tests/`, `vqa3d/`, `blender_visual_qa/judge/__init__.py`, `tools/MIGRATION_SOURCE.json`, and `PROVENANCE.md` (re-sealing `trace_sha256` on affected traces); consolidated the 18 repeated RAM workspace root literals into one `RAMWS_ROOT` constant read from `VQA_RAMWS_ROOT` at `components/blender-visual-qa/bpy_side/canon.py:26` (reviewed exception for `tools/check_public.sh`: `--allow PATH:components/blender-visual-qa/bpy_side/canon.py:26`); preserved exact frozen TSV bytes via `components/blender-visual-qa/.gitattributes`; hardened `route/query.py bench` against unknown/empty splits; patched `validate_clean_pair` in mocked unit tests in `tests/test_run_bench_fx.py`; registered the added routes in `catalogue/routes.src`, refreshed `catalogue/` via `tools/refresh_catalogue.py`, and rebuilt `catalogue/router/` (stripping HTML `<a id="..."></a>` noise in `build_router_catalogue.py`) and `route/index/`.

## Blender Visual QA public projection

The publication preview projects the reviewed Blender Visual QA files onto the public ViewPoint baseline. The governed integration history remains the internal acceptance record.

The optional headless CUDA worker permits an enrolled worker to name its own threads. CPU remains the routine mode. The worker changes received independent review. The separately authorized private X11 trial used a disposable fixture. Its result applies to that fixture. See [Visual QA 3D](docs/VISUAL_QA_3D.md) and the [worker provenance](components/blender-visual-qa/blender_visual_qa/drive/PROVENANCE.md).

The current Vision Engine wheel `vision_engine-1.1.1-py3-none-any.whl` is built from the [public Vision Engine source at `c3b0f53560dcb45e6c503d95e2e848cd3b8f16b7`](https://github.com/oguirado/ViewPoint/tree/c3b0f53560dcb45e6c503d95e2e848cd3b8f16b7/components/vision-engine). The component's `config/vision.json` pins this wheel at version `1.1.1` and records its SHA-256. The adapted router tooling retains its separate source pin [`8dec8e83768afef63445b03476c6ed673f04c5bc`](https://github.com/oguirado/ViewPoint/commit/8dec8e83768afef63445b03476c6ed673f04c5bc), an ancestor of the public ViewPoint baseline. The earlier wheel records remain historical build evidence. The current wheel and embedded notice digests appear in the [release notice](NOTICE-EVALUATION.md#blender-visual-qa-and-its-pinned-vision-engine-wheel).

The Vision Engine wheel and adapted tooling retain their pinned source identity in the [vendoring manifest](components/blender-visual-qa/tools/VENDORED.json). The [release notice](NOTICE-EVALUATION.md#blender-visual-qa-and-its-pinned-vision-engine-wheel) describes the exact Visual QA component and its pinned wheel. The inherited source history has a separate publication scope.

## Engine pin and AT-SPI update

This update carries the selected Vision Engine sculpt-frame and Wender Agent
sculpt, scene-state, and AT-SPI changes from the maintainer's integration
history. The Wender AT-SPI change uses synthetic bus fixtures for the state-bit
regressions. Codex replayed the selected source, reconciled the lock and
manifest gate, and assembled offline verification evidence. The component
licences and third-party notices continue to apply to the vendored source.

The Vision Engine lock digest includes the inherited PR1 documentation, packaging,
and runtime-isolation updates. It covers the adapted subtree with the PR3 additions.

# Wender agent

Base: components/wender-agent
Wender is the hard layer of computer use for AI agents. It lets an agent
operate any web page or desktop program, known or unknown, without recording
it first. It is not a browser. Pixels come from the paired vision-engine
component; Wender consumes its fingerprint and crop products and never
re-implements them. [CITED: `components/wender-agent/README.md`,
`components/wender-agent/AGENTS.md`]

The packaged version is `0.3.0` with Python `>=3.11` and a single runtime
dependency, `numpy>=2.0`. [CITED: `components/wender-agent/pyproject.toml`,
`components/wender-agent/wender/__init__.py`]

## Entrypoints and commands

Every entry below is a real module or tool file inside
`components/wender-agent`. All test runs execute inside a memory-capped
scope; the Chromium integration suite runs only when a Chrome binary is
configured.

| Entry | What it does |
| --- | --- |
| `wender/plan/cli.py` (with `wender/plan/policy.py`) | Pipe-only planner CLI. Reads one UTF-8 JSON request on stdin and writes one JSON response on stdout. Accepts either `{observation, element_table, goal}` or a plain `observation/v0.2` request. Exits nonzero with `code: detail` on stderr on any refusal. The stdin cap is `MAX_STDIN_BYTES` (1 MiB). [CITED: `components/wender-agent/wender/plan/cli.py`] |
| `tools/mw_run.py` | Declarative MemoryWeb workflow runner. Requires `--mode` with one of `validate`, `replay`, `shadow`, `promoted`, `repair`. Refuses standalone provisioning until trusted live dependencies are installed. [CITED: `components/wender-agent/tools/mw_run.py`] |
| `tools/cu_web.py` | One web cycle attached to an operator-started identity capsule. `--endpoint` is a full OpenAI chat/completions URL. Scoped to this entry and its reviewed outputs: only reviewed reproduction metadata is persisted; observations and answer text stay in RAM or stdout. [CITED: `components/wender-agent/tools/cu_web.py`] |
| `tools/mw_learn.py`, `tools/mw_profile_init.py` | MemoryWeb learning and profile provisioning helpers alongside `tools/mw_run.py`. [CITED: `components/wender-agent/tools/mw_learn.py`] |
| `tools/anchors.py`, `tools/reseal.py` | Anchor lookup (`--q "<word>"`) and HyperBook reseal (`--check` fails when stale). [CITED: `components/wender-agent/AGENTS.md`, `components/wender-agent/tools/anchors.py`, `components/wender-agent/tools/reseal.py`] |

Run the component test suites from the component root:

```bash
systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 -- python3 -B -m unittest discover -s tests -t .
systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 -- node --test 'js/tests/*.test.js'
systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 -- python3 -B tools/reseal.py --check
```

[CITED: `components/wender-agent/README.md`, `components/wender-agent/AGENTS.md`]

Before changing this component: preserve handle/generation/epoch checks,
attachment ownership and AX cleanup in browser/tab paths. Standalone
MemoryWeb validation is distinct from live provisioning: the default
`trusted_live_dependencies` seam raises `OUT_OF_SUPPORT`; live support
needs explicitly installed trusted services. Source entry availability
does not qualify compiled exports or stored browser workflows; check
IMP-03 before claiming support. Callers must serialize a single writer
per store directory. Change navigation starts from
`wender/learn/model_client.py`, `wender/perceive/element_table.py` and
the session registry used by tab custody in
`wender/perceive/tab_manager.py`. [CITED:
`components/wender-agent/wender/perceive/browser.py`,
`components/wender-agent/wender/perceive/tab_manager.py`,
`components/wender-agent/tools/mw_run.py`]
`CHROME_BIN=/usr/bin/google-chrome` additionally enables the Chromium
integration suite; without it those tests skip with an explicit message.
[CITED: `components/wender-agent/README.md`,
`components/wender-agent/tests/test_integration_chrome.py`]

## Main modules

Responsibility follows the four pillars plus state, planning and scripts.

| Area | Modules | Responsibility |
| --- | --- | --- |
| PERCEIVE | `wender/perceive/ladder.py` (`choose_rung`), `wender/perceive/browser.py` (`page_state`), `wender/perceive/ax.py`, `wender/perceive/atspi.py`, `wender/perceive/text.py`, `wender/perceive/stopstate.py`, `wender/perceive/wait.py`, `js/inpage/extract.js`, `js/inpage/viewport.js` | Reads the cheapest rung that the surface offers, from structured state through the AX tree to pixels and blind coordinates. `browser.page_state` emits canonical STATE after pruning AX. Other CU paths use projected elements, vision describe metadata, marks and images; inspect `wender/learn/model_client.py` before changing model inputs. Raw lane AX/HTML fields are excluded by its explicit projection. [CITED: `components/wender-agent/wender/perceive/ladder.py`, `components/wender-agent/wender/perceive/browser.py`, `components/wender-agent/wender/perceive/text.py`, `components/wender-agent/wender/perceive/stopstate.py`, `components/wender-agent/wender/learn/model_client.py`, `components/wender-agent/js/inpage/extract.js`, `components/wender-agent/js/inpage/viewport.js`] |
| ACT | `wender/act/actor.py` (`choose_path`), `wender/act/cdp_executor.py` (`OPS`), `wender/act/hosted_guard.py` | Most structured path first (app API/MCP, then AX/DOM, then coordinates); one actor per surface; readers do not take actor leases. `OPS` lists the supported operations. When hosted inside MetaViewer or the pipe-only planner entry, direct actuation is refused before any I/O. [CITED: `components/wender-agent/wender/act/actor.py`, `components/wender-agent/wender/act/cdp_executor.py`, `components/wender-agent/wender/act/hosted_guard.py`] |
| REMEMBER | `wender/remember/program_map.py`, `wender/remember/skills.py` (`admit`), `wender/remember/store.py` | Stores program maps and admitted skill metadata. Admission checks supplied reproduction evidence; trusted replay/callers must establish that evidence. `append_map` treats seen counts as observation-batch deltas; changed skill records require newer integer evidence versions. Callers must serialize a single writer per store directory. [CITED: `components/wender-agent/wender/remember/skills.py`, `components/wender-agent/wender/remember/store.py`] |
| LEARN | `wender/learn/segmenter.py`, `wender/learn/explore.py` (`Driver`), `wender/learn/cu_loop.py` (`run_web_cycle`), `wender/learn/mw_workflow.py`, `wender/learn/mw_profiles.py`, `wender/learn/failure_taxonomy.py` | Demonstrations become segments, then intents, then structured actions, and finally verified skills. Exploration is deterministic; crops stay in RAM. `wender/learn/failure_taxonomy.py` defines closed T2 refusal reasons and harness codes. [CITED: `components/wender-agent/wender/learn/segmenter.py`, `components/wender-agent/wender/learn/explore.py`, `components/wender-agent/wender/learn/cu_loop.py`, `components/wender-agent/wender/learn/failure_taxonomy.py`] |
| STATE | `wender/state/schema.py` (`build_state`, `validate_state`, `dumps`), `wender/state/diff.py` (`diff_states`), `wender/state/wender.state.v1.schema.json` | Canonical-JSON STATE: sorted keys, compact separators, integers only. `diff_states` returns semantic changes between validated STATE snapshots and never mutates either input. [CITED: `components/wender-agent/wender/state/schema.py`, `components/wender-agent/wender/state/diff.py`] |
| PLAN / SCRIPTS | `wender/plan/cli.py`, `wender/plan/policy.py`, `wender/scripts/compiler.py`, `wender/scripts/runner.py` | Planner CLI described above, plus the compiled script path that turns verified reproductions into runnable workflow units. [CITED: `components/wender-agent/wender/scripts/compiler.py`, `components/wender-agent/wender/scripts/runner.py`] |
| Rust | `rust/wender_native/src/lib.rs` | Placeholder crate in this snapshot; low-level work is reserved for Rust per the stack ruling. [CITED: `components/wender-agent/rust/wender_native/src/lib.rs`, `components/wender-agent/AGENTS.md`] |

`hyperbooks/HB-WA-a0-arch/book.md` is the architecture book;
`hyperbooks/HB-WA-s1-perceive`, `hyperbooks/HB-WA-s2-act`,
`hyperbooks/HB-WA-s3-remember` and `hyperbooks/HB-WA-s4-learn` are the
pillar books. [CITED: `components/wender-agent/hyperbooks/HB-WA-a0-arch/book.md`]

## Contracts and schemas

| Schema or doc | Content |
| --- | --- |
| `wender.state/v1` (`wender/state/wender.state.v1.schema.json`, `wender/state/schema.py`, `docs/STATE.md`) | Canonical structured output of `browser.page_state`; not the sole input product used by all model paths. Top level: `schema`, `source`, `prune`, `ax_sha256`, `ax_stable_sha256`, `viewport`, `nodes`, `actionable`, `counts`. Node ids are 16-hex reload-stable digests; boxes are integer CSS px. [CITED: `components/wender-agent/docs/STATE.md`, `components/wender-agent/wender/state/wender.state.v1.schema.json`, `components/wender-agent/wender/perceive/browser.py`, `components/wender-agent/wender/learn/model_client.py`] |
| `agent_lane/v1` (`contracts/agent_lane.v1.schema.json`) | Lane envelope with `migration` (chain pinned to `["agent_lane/v1"]`), `target` (`kind: cdp_tab`), `revision`, `tick`, `frame` (RGB24), `state`, digest fields and `elements`. [CITED: `components/wender-agent/contracts/agent_lane.v1.schema.json`] |
| `cu.action/v1` (`wender/learn/cu_action.schema.json`) | Model action envelope: `schema`, `observation`, `action.kind` in `click`, `key`, `type`, `wait`, `done`, plus `x`, `y`, `keysym`. The wire schema also requires `expectation`, `confidence` and nullable `need_reasoning`; action slots include `text`, `ms`, `element_id` and `mark_id`. `model_client.validate_action` normalizes nullable slots, enforces kind-specific fields and exactly one click selector (the paired coordinates `x` and `y`, or `element_id`, or `mark_id`), and requires a region for `changed_region`. C3 normalization and the wire schema are separate contracts. [CITED: `components/wender-agent/wender/learn/cu_action.schema.json`, `components/wender-agent/wender/learn/model_client.py`] |
| `mw.v1` family (`docs/spec/mw.v1.schema.json`) | MemoryWeb site, node, pattern, step, oracle and stats definitions used by the MemoryWeb workflow tools. [CITED: `components/wender-agent/docs/spec/mw.v1.schema.json`] |
| The four pillar contracts (`docs/spec/WA_S1_PERCEIVE_CONTRACTS.md`, `docs/spec/WA_S2_ACT_CONTRACTS.md`, `docs/spec/WA_S3_REMEMBER_CONTRACTS.md`, `docs/spec/WA_S4_LEARN_CONTRACTS.md`) | Written contracts per pillar; the executable behaviour is pinned by the matching suites under `tests/`. [CITED: `components/wender-agent/docs/spec/WA_S1_PERCEIVE_CONTRACTS.md`] |
| `docs/STATE.md`, `docs/spec/KNOWN_ISSUES_P1_WEB.md` | STATE encoding and pruning notes; pinned known-issue register with historical issue descriptions and current regression/availability checks. [CITED: `components/wender-agent/docs/STATE.md`, `components/wender-agent/docs/spec/KNOWN_ISSUES_P1_WEB.md`] |

STATE encoding rules that the code enforces: canonical JSON (sorted keys,
compact separators, UTF-8), integers only (a float is refused), AX
snapshot hashes exclude boxes, CDP ids and focus flags, and content
inside `article` opaque by design. [CITED:
`components/wender-agent/docs/STATE.md`,
`components/wender-agent/wender/state/schema.py`,
`components/wender-agent/wender/state/diff.py`]

## How it is tested

Suites live under `components/wender-agent/tests` (Python), `js/tests`
(Node) and `tests/scripts` (script-path suites).

- Perceive: `tests/test_ax.py`, `tests/test_atspi_tree.py`,
  `tests/test_cdp_pipe.py`, `tests/test_browser_isolation_refusals.py`,
  `tests/test_element_table.py`, `tests/test_disambig.py`,
  `tests/test_known_issues_p1_web.py`. [CITED:
  `components/wender-agent/tests/test_known_issues_p1_web.py`]
- Act: `tests/test_cdp_executor.py`, `tests/test_api_path.py`,
  `tests/test_jq1_a7_actor_safety_contract.py`,
  `tests/test_hosted_guard.py`. [CITED:
  `components/wender-agent/tests/test_cdp_executor.py`]
- Remember: `tests/test_remember_store.py`, `tests/test_skill_replay.py`,
  `tests/test_segmenter.py`. [CITED:
  `components/wender-agent/tests/test_remember_store.py`]
- Learn and CU loop: `tests/test_cu_loop_unit.py`, `tests/test_cu_web_unit.py`,
  `tests/test_cascade.py`, `tests/test_explore.py`, `tests/test_web_goal_unit.py`.
  [CITED: `components/wender-agent/tests/test_cu_loop_unit.py`]
- State: `tests/test_state_schema.py`, `tests/test_state_diff.py`,
  `tests/test_state_budget.py`. [CITED:
  `components/wender-agent/tests/test_state_schema.py`]
- Planner and MemoryWeb: `tests/test_mv_planner_cli.py`, `tests/test_mw_export.py`,
  `tests/test_mw_learn_unit.py`, `tests/test_memoryweb_gate_unit.py`.
  [CITED: `components/wender-agent/tests/test_mv_planner_cli.py`]
- Scripts: `tests/scripts/test_compiler.py`, `tests/scripts/test_execution.py`,
  `tests/scripts/test_ast_gate.py`. [CITED:
  `components/wender-agent/tests/scripts/test_compiler.py`]
- JavaScript: `js/tests/extract.test.js`, `js/tests/viewport.test.js`,
  `js/tests/extract_pins.test.js` against `js/inpage/extract.js` and
  `js/inpage/viewport.js`. [CITED:
  `components/wender-agent/js/tests/extract.test.js`]

`tests/test_integration_chrome.py` drives a real Chromium over
`--remote-debugging-pipe` on a local `file://` fixture and is skipped unless
`CHROME_BIN` is set. Chromium is used with the remote-debugging pipe only
(no TCP port); the user-data directory is a short disk-backed temporary
directory under `/tmp` because of the Unix socket path limit. [CITED:
`components/wender-agent/tests/test_integration_chrome.py`,
`components/wender-agent/AGENTS.md`]

## Known failure modes and limits

The register describes historical WP1-01 through WP1-06 issues. The
current `tests/test_known_issues_p1_web.py` contains ordinary
regression/availability checks, with no expected-failure decorators.
Their success does not prove live-site resolution.

| ID | Observed behaviour | Handling in code |
| --- | --- | --- |
| WP1-01 | DOM selectors fragile: a waiter looked for a site data attribute the UI no longer renders and counted zero messages. | Waiters and extractors read the AX tree only (roles and names), never site CSS or data attributes. |
| WP1-02 | No AX completion signal: a "copy message" button fired a premature done. | Generic `wait_done(rules)` in perceive; rules come from site-profile data, not code. |
| WP1-03 | A JS click did nothing on pointer-driven menus. | After AX dispatch/verification attempts fail, an enabled non-irreversible click may fall back to a fresh-target box-centre pointer click. Unchanged-state verification is role/expectation dependent; `press` and `insert_text` are separate operations. |
| WP1-04 | Extraction kept UI chrome ("... can make mistakes ..." trailer). | `wender/perceive/text.py` `extract` supports caller-supplied answer boundaries/targets, exclusions and trailer literals; without selectors it can traverse the whole supplied tree. Callers must handle `incomplete_tree` and `ambiguous_boundary`. |
| WP1-05 | Bot walls were not a state: an unusual-traffic wall returned on the first query. | `wender/perceive/stopstate.py` `detect` reports supplied wall/challenge evidence; its caller must stop or hand control to the human. |
| WP1-06 | One tab per tool, attach-only: a missing tab returned rc 2. | `wender/perceive/tab_manager.py` and `wender/perceive/cdp_port.py` `ensure_tab` open or reuse one tab per site, then navigate to the site route. |

Further limits and guards, all from the component tree:

- Closed failure taxonomy: `wender/learn/failure_taxonomy.py` defines
  `T2_REASONS` (schema, sequence, ownership, consent and related refusals)
  and `HARNESS_CODES` (capture, selector, adapter, trace, infra, replay and
  evidence planes). [CITED:
  `components/wender-agent/wender/learn/failure_taxonomy.py`]
- Hosted refusal: when `MV_HOSTED=1` or the pipe-only planner entry is
  used, actuation through any `wender.act` executor is refused with
  `hosted_actuation_refused` before any I/O. [CITED:
  `components/wender-agent/wender/act/hosted_guard.py`]
- Fake-clock rule: every fake clock passed to `CdpPipe`/`CdpPort` must
  return monotonic nanoseconds; non-integer clocks raise `TypeError`.
  The harvest broker clock still uses seconds. [CITED:
  `components/wender-agent/docs/spec/KNOWN_ISSUES_P1_WEB.md`]
- Storage law (scoped): pixel/frame storage and the reviewed RAM-backed
  capture paths keep no frames on disk; frames live in RAM only. The
  generic Muse action backend writes a temporary prompt using the
  default tempfile location; do not infer RAM-only observation text
  from the frame-storage rule. [CITED:
  `components/wender-agent/AGENTS.md`,
  `components/wender-agent/wender/learn/segmenter.py`,
  `components/wender-agent/wender/learn/model_client.py`]
- Irreversible actions never fall back to raw coordinates and require a
  unique L0/L1 match. [CITED: `components/wender-agent/wender/act/cdp_executor.py`]
- `tools/mw_run.py` refuses standalone provisioning until trusted live
  dependencies are installed; the default image cap is a stated target,
  and missing semantic detectors refuse admission. [CITED:
  `components/wender-agent/tools/mw_run.py`]

## Improvement register (IMP-01 to IMP-04)

Only ids and titles are reproduced here. Descriptions, evidence and
dispositions live in the improvement register and are not repeated.

| ID | Title |
| --- | --- |
| IMP-01 | Action reply shape and changed-region contract |
| IMP-02 | Repeated actions on unchanged targets |
| IMP-03 | Compiled package entry points and browser workflows |
| IMP-04 | Clipped-node state consistency |

Documentation rules applied in this revision: a doc line must not
disagree with the contract the code enforces [SW-0244]; a matching
digest or passing symbol check is identity, not acceptance [LX-0004];
ranking finds candidates, evidence is admitted only after resolving
against the identified source generation [VE-0028]; do not hide
unresolved work behind visible register status and disposition
[FA-0020].

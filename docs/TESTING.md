# How MetaViewer is tested

MetaViewer has root source checks, pinned component suites, a monorepo
integration gate, Electron journeys and opt-in live capsule checks. Passing one
does not establish the others. This page describes the shipped runners; it does
not claim a new test run. The current release identity is in [VERSION](../VERSION),
and the evidence boundary is in [EVIDENCE.md](EVIDENCE.md).

The complete local integration gate is [tools/vp_gate.py](../tools/vp_gate.py).
It runs every root leg below and then every inventoried component suite. Its
source boundary, pinned checkouts and component inventory are described in
[MONOREPO_GATES.md](MONOREPO_GATES.md).

## Which check should I run?

Choose the check for the behavior you changed, then run the complete gate before
integration. A passing fixture is evidence about that fixture, not a live capsule.

Answers: testing guide; test commands; unit tests versus live acceptance

| Change or question | Runner | What a pass establishes |
| --- | --- | --- |
| Documentation, command paths and required pages | `python3 -B -m unittest discover -s tests -p 'test_*doc*'` | The documentation assertions in the selected suites hold |
| One root leg | `python3 -B tools/vp_gate.py --leg contracts` | That root leg passed under its own memory-capped scope |
| Complete local monorepo integration | `python3 -B tools/vp_gate.py` | Every root leg and every inventoried component suite passed, and the source revision and component tree stayed unchanged |
| The component step only | `python3 -B tools/vp_gate.py --components` | Every inventoried component suite passed and no lane was blocked |
| The root runner itself | `bash tools/gate.sh --self-test` | Its fault-injection fixtures passed |
| Electron journeys | `python3 tests/e2e/run.py` under Xvfb | The selected wall journeys and cross-routing negative control passed |
| Real capsule and cleanup | `node tests/e2e/e2e1_slice/index.js --live` with the live prerequisites below | The live slice report and its cleanup assertions passed |
| Agent action and state verification | `python3 tools/gate_g_ai.py --surface mv-live-1` with the live prerequisites below | The local live gate's assertions, negative controls and cleanup passed |

Sources: [monorepo gate](../tools/vp_gate.py), [root gate](../tools/gate.sh),
[journey runner](../tests/e2e/run.py), [live slice](../tests/e2e/e2e1_slice/index.js),
[agent gate](../tools/gate_g_ai.py).

## What does the local gate run?

The monorepo wrapper invokes this runner once per root leg, so the table below is
also the root half of the monorepo gate. Run the root runner alone only when you
mean to check the root repository legs and not the imported component suites.

The root runner runs each leg listed below and prints `PASS aggregate` only when
all selected legs return success. Use `--leg` to select a leg while iterating.

Answers: root gate; gate legs; PASS aggregate

```sh
bash tools/gate.sh --leg contracts
bash tools/gate.sh --leg unit
bash tools/gate.sh --self-test
bash tools/gate.sh
```

| Leg | Checks in the shipped runner |
| --- | --- |
| `manifests` | Release manifests, pinned dependencies, frozen offline resolution and required ignore rules |
| `laws` | Release version and repository commit laws |
| `contracts` | Closed schemas and their valid and invalid examples |
| `unit` | Python suites under `tests/`, including the live-slice harness unit suites |
| `agents` | Python agent suites and their mutation harness |
| `js` | The pinned capsule UI kit and its content hash |
| `rust` | Pinned formatter, Clippy and offline locked Cargo tests |
| `go` | Vet and race tests on the host, plus vet and compile checks for the other supported Linux architecture |
| `ui` | Offline npm installation, Node tests, Vue type checking and production build |
| `ui-smoke` | Electron renderer and main-process smoke checks with their required success markers |
| `perf` | Offline resource-budget assertions, a rejected leak fixture and an optional live RSS probe |

This table describes `LEAVES`, `leaf`, `build_leg`, `go_leg`, `ui_smoke_leg` and
`perf_leg` in [tools/gate.sh](../tools/gate.sh). A cross-architecture compile is
not a run on that architecture. The laws leg checks [LAWS.md](../LAWS.md) through
[check_laws.py](../tools/check_laws.py); it is not a secret scanner.

## What does the monorepo gate add?

Answers: monorepo gate; pinned component suites; disposable checkouts

`tools/vp_gate.py` is the complete local gate. With no arguments it runs every
root leg by calling `bash tools/gate.sh --leg L`, then runs the component step.
`--leg L` selects one root leg and skips the component step; `--components` runs
only the component step; `--parallel` runs the root lane alongside the component
step and, inside that step, starts one lane per owner after the native builders.

Component commands run from disposable checkouts of the commits in
[deps.lock](../deps.lock). The wrapper resolves each pin from local Git objects
only, clones with `--shared` from the local repository, verifies HEAD, refuses a
dirty checkout and performs no remote fetch or download for those checkouts. It
records `component_tree_unchanged` and `source_revision_unchanged` in the receipt
and fails the aggregate when either is false.

The component inventory is explicit in `component_cases`:

- `wender-agent`: Python discovery, per-file health, JavaScript tests, reseal,
  route-code, code-index, anchors, layers, state-budget and MemoryWeb.
- `vision-engine`: Python discovery, per-file health, gates, mutants, catalogue,
  code-index, native build, wheel, peer wheel, one release build per crate
  manifest directly under `components/vision-engine/rust/`, hyperbook reseal,
  `langx/x1` and `langx/x1/host/adapters` Rust and Go waves 1-5, and TypeScript
  waves 1-3.
- `OpenSuperficies`: cu-gates, cu-suite, hyperbook, oracle, build, go-vet,
  go-race, worker contracts, cockpit, and frontend install, tests and build.

Component suites retain upstream selections. `OpenSuperficies/go-race` excludes
the packages `components/OpenSuperficies/src/kernelbook/cgroup`,
`components/OpenSuperficies/src/kernelbook/escalera`,
`components/OpenSuperficies/src/surfacebook/pbx` and
`components/OpenSuperficies/src/surfacebook/grabador`, and skips
`TestEstadoPresupuestoDeTokens`. `OpenSuperficies/frontend-tests` excludes
`components/OpenSuperficies/cockpit/frontend/tests/terminal.test.ts`; the
separate `OpenSuperficies/frontend-terminal` lane remains `BLOCKED`. Per-file
health also retains its declared heavy-file exclusion; full Python discovery is
a separate case. Changes affecting these exclusions require separately selected
tests and evidence. A pass from these selected commands does not establish the
omitted assertions.

The source boundary is [OWNERSHIP.json](../OWNERSHIP.json): only declared
component prefixes are delegated, an unknown component path or invalid manifest
fails closed, and the component suites are mandatory in the parent aggregate.
Accepted history is recorded in [LAWS_BASELINE.json](../LAWS_BASELINE.json) and
audited by `tools/check_laws.py --owned`. [CITED: docs/MONOREPO_GATES.md]
Root legs and component suites remain separate rows and separate evidence; a
root-only result cannot certify a component suite.

Four conditional lanes are always recorded `BLOCKED` in the current inventory
because the wrapper does not provision their prerequisites: a live Chrome
fixture, an optional live Chromium fixture, Playwright Chromium with a terminal
fixture, or an operator-selected bridge task. They are `wender-agent/live-cdp`,
`vision-engine/live-chromium`, `OpenSuperficies/frontend-terminal` and
`components/task-acceptance`. The `js` and `ui` root legs are also `BLOCKED` when
the pinned UI kit cannot be resolved. A blocked lane is not a pass, and it
prevents a successful aggregate.

## What memory scope does every test use?

Answers: memory scope; systemd-run; test isolation

Every case command runs in its own transient user scope:

```sh
systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 -- <command>
```

`tools/vp_gate.py` uses the same properties and replaces `-q` with
`--expand-environment=no`, because systemd 259 would otherwise expand `${...}`
in component `bash -c` scripts. It also wraps the command in
`timeout --signal=TERM --kill-after=10s <seconds>s`. The limits are execution
choices, not measured product requirements. [CITED: ../tools/vp_gate.py, `CAP`
and `run_case`]

Each leg or component case receives its own scope; a whole run is not one scope.
Running `tools/gate.sh` directly gives its command deadlines and process-tree
cleanup but not this memory cap, so use `tools/vp_gate.py` for the complete gate.

## Which architectures does the gate cover?

Answers: arm64; x64; cross-architecture compile; host receipts

`tools/vp_gate.py` accepts `aarch64`, `arm64` and `x86_64` and refuses any other
machine before running a case. `linux-arm64` is the primary product target and
`linux-x64` is supported [CITED: docs/INSTALL.md]; the packaging tool rejects any
other architecture [CITED: ../ui/tools/package.js, `PKG_ARCH_UNSUPPORTED`]. One
invocation runs on the host that starts it. The `go` root leg additionally vets
and compiles the daemon adapter for the other architecture under `GOARCH`, but
that build is executed with `-run '^$' -exec /bin/true`; it is a compile check,
not a run on that architecture.

A candidate therefore needs a separate receipt from each host it claims: an
arm64 `PASS` does not qualify x64, and an x64 `PASS` does not qualify arm64.
Both hosts run the same leg and suite names; only the host toolchain and the
other architecture chosen by the `go` leg differ.

## What is a gate receipt?

Answers: gate receipt; results.json; PASS FAIL BLOCKED

A monorepo run creates a timestamped gate directory under ctl/runs beside the
checkout. Each case writes one log; results.json holds the evidence and one row
per case. A row records the case name, status, return code, elapsed seconds,
working directory, the full command including the memory scope, the log path
and the SHA-256 of the log.

The evidence block records the architecture, scratch and Go-scratch filesystem
types, the Wender pin from `deps.lock`, the Cargo job limit, the starting
revision and branch, the kit checkout, the component workdirs,
`component_tree_unchanged`, `final_revision` and `source_revision_unchanged`.

Status meaning:

| Status | rc | Meaning |
| --- | --- | --- |
| `PASS` | 0 | The case command returned success |
| `BLOCKED` | 2 | The case was not executed, or could not be launched; the log records the reason |
| `FAIL` | any other | The case returned a nonzero rc other than 2, or its watchdog expired (124) |

The process exits 0 only when at least one row exists, every row is `PASS`, the
component tree is unchanged and the final revision equals the starting revision.
A completed run prints `RECEIPT <path>` before exiting. A nonzero exit, a `FAIL`
or a `BLOCKED` row means the aggregate is not green. The receipt proves which
commands ran and what they returned; it does not prove that the assertions are
adequate or that a private runtime works. [CITED: ../tools/vp_gate.py, `run_case`,
`main`]

## What must be installed before the local gate?

Use the pinned build prerequisites in [INSTALL.md](INSTALL.md), a local checkout
of the pinned capsule UI kit, cached build dependencies and a local Electron
binary. Offline resolution deliberately fails when the cache is incomplete.

Answers: gate prerequisites; offline dependencies; missing kit; missing Electron

Set `CAPSULE_UI_KIT_REPO` or `MV_KIT_CHECKOUT` to the kit checkout. Set
`ELECTRON_OVERRIDE_DIST_PATH` if Electron is outside `ui/node_modules/electron/dist`.
The gate verifies the kit pin from [ui/kit.lock.json](../ui/kit.lock.json).
It uses the formatter toolchain named in `build_leg`, which is distinct from
the workspace compiler requirement in [Cargo.toml](../Cargo.toml).

Install the UI dependencies before a standalone `unit` leg: some packaging
fixtures load the package tooling. The full gate is a verifier, not an online
dependency installer. See `validate_frozen_dependencies`, `electron_dist` and
`build_leg` in [tools/gate.sh](../tools/gate.sh).

The monorepo wrapper additionally needs a working `systemd-run --user` and a
scratch filesystem that reports `ext2/ext3`, `xfs` or `btrfs`; it refuses a
symlinked scratch or an unqualified filesystem. Go tests need a short disk-backed
temporary directory because test names consume Unix socket path space; set
`VP_GO_TMPDIR` when `/tmp` is tmpfs. `VP_KIT_SOURCE` names the local kit checkout
for the wrapper, and `VP_CARGO_BUILD_JOBS` accepts 1 or 2.
[CITED: ../tools/vp_gate.py, `environment`]

## How do I interpret a skipped or blocked check?

A missing prerequisite, skipped test or unexecuted live probe is not a passing
result for that behavior. Read the individual result as well as the aggregate.

Answers: skipped checks; blocked checks; absent evidence; gate failure

The `unit` and `agents` legs reject empty selections, skipped tests, expected
failures and unexpected successes. Other runners have their own rules. In
particular, `perf_leg` can pass its offline checks while printing that the live
probe was skipped. Even with `MV_GATE_PERF_LIVE=1`, missing Electron or Xvfb leaves
that probe skipped. A live resource claim requires evidence that the probe ran.
[CITED: ../tools/gate.sh, `UNIT_CODE`, `AGENTS_UNIT_CODE`, `perf_leg`]

The assembly web runner also returns a successful process exit with
`status: "SKIPPED"` when live mode is not enabled. Inspect its report, not just
its exit status. [CITED: ../tests/e2e/e2e1_slice/asm_web_live.py, `main`]

The monorepo wrapper records three statuses: `PASS` (rc 0), `FAIL` (any other
rc except 2) and `BLOCKED` (rc 2). An explicit missing prerequisite and a
command that could not be launched both become `BLOCKED`; read the case log. A
`BLOCKED` row prevents a successful aggregate exactly like a `FAIL`.

## What is the current gate baseline?

Answers: current baseline; x64 failures; blocked suites

The retained x64 run for base revision `c95bd4173` contains 81 cases: 55 `PASS`,
20 `FAIL` and 6 `BLOCKED`. Because the aggregate requires every row to be
`PASS`, that receipt is not a green gate. Treat the totals as a dated baseline,
not as a property of later revisions; it is not acceptance evidence for any
other revision. The receipt is the campaign evidence artifact
w2b-x64c/results.json (66,351 bytes; SHA-256
`7cdb08f5c57f650ac847874e2ee74a5557ec903109bbfaabe2492d6e1b4931cc`). It is
stored outside this repository in the access-controlled campaign archive; an
authorized reviewer retrieves it by artifact name and verifies that digest
before comparing case names, statuses, the starting revision and the aggregate.
Case names below are the values recorded in that receipt; the wrapper composes
root names from the leg and component names from the owner and label.

`PASS` (55):

- root: root/manifests, root/laws, root/contracts, root/ui-smoke, root/perf
- wender-agent: wender-agent/javascript, wender-agent/reseal,
  wender-agent/anchors, wender-agent/layers, wender-agent/memoryweb
- vision-engine: vision-engine/native, vision-engine/wheel,
  vision-engine/release-visioncore_native,
  vision-engine/release-vqa_k_gm_proto, vision-engine/release-vqa_k_gm_v2,
  vision-engine/release-vqa_k_s1_desc,
  vision-engine/release-vqa_k_s2_motion,
  vision-engine/release-vqa_k_s2_sadwin,
  vision-engine/release-vqa_k_st_block4,
  vision-engine/release-vqa_k_st_proto8,
  vision-engine/release-vqa_native_v2k, vision-engine/release-vqa_ring,
  vision-engine/release-vqa_ring_fast, vision-engine/peer-wheel,
  vision-engine/gates, vision-engine/mutants, vision-engine/catalogue,
  vision-engine/hyperbook-reseal, vision-engine/langx-x1-rust-w1,
  vision-engine/langx-x1-rust-w2, vision-engine/langx-x1-rust-w3,
  vision-engine/langx-x1-rust-w4, vision-engine/langx-x1-go-w4,
  vision-engine/langx-x1-rust-w5,
  vision-engine/langx-x1-host-adapters-rust-w1,
  vision-engine/langx-x1-host-adapters-go-w1,
  vision-engine/langx-x1-host-adapters-rust-w2,
  vision-engine/langx-x1-host-adapters-go-w2,
  vision-engine/langx-x1-host-adapters-rust-w3,
  vision-engine/langx-x1-host-adapters-go-w3,
  vision-engine/langx-x1-host-adapters-rust-w4,
  vision-engine/langx-x1-host-adapters-go-w4,
  vision-engine/langx-x1-host-adapters-rust-w5,
  vision-engine/langx-x1-host-adapters-go-w5, vision-engine/typescript-w1,
  vision-engine/typescript-w2, vision-engine/typescript-w3
- OpenSuperficies: OpenSuperficies/hyperbook, OpenSuperficies/build,
  OpenSuperficies/go-vet, OpenSuperficies/worker-contracts,
  OpenSuperficies/cockpit, OpenSuperficies/frontend-install,
  OpenSuperficies/frontend-tests, OpenSuperficies/frontend-build

`FAIL` (20):

- root: root/unit, root/agents, root/rust, root/go
- wender-agent: wender-agent/python, wender-agent/ci-health,
  wender-agent/route-code, wender-agent/code-index,
  wender-agent/state-budget
- vision-engine: vision-engine/python, vision-engine/ci-health,
  vision-engine/code-index, vision-engine/langx-x1-go-w1,
  vision-engine/langx-x1-go-w2, vision-engine/langx-x1-go-w3,
  vision-engine/langx-x1-go-w5
- OpenSuperficies: OpenSuperficies/cu-gates, OpenSuperficies/cu-suite,
  OpenSuperficies/oracle, OpenSuperficies/go-race

A `FAIL` is a red result from that run, not an accepted waiver.

`BLOCKED` (6):

- root/js, root/ui
- wender-agent/live-cdp, vision-engine/live-chromium
- OpenSuperficies/frontend-terminal, components/task-acceptance

root/js and root/ui were blocked by the wrapper's UI kit precondition, not by a
test failure. The four conditional lanes record their reasons: missing
`CHROME_BIN` or a dedicated live fixture, an unprovisioned optional live
Chromium fixture, missing Playwright Chromium and terminal fixture, and the
absence of an active bridge task selection with reviewed operator commands.
A blocked suite is unexecuted evidence, not a pass. root/ui-smoke passed while
root/ui was blocked; a pass on one leg does not cover a sibling leg.

## How do I run the Electron journeys?

Build the production UI, then run the journey orchestrator under Xvfb with the
pinned kit and a local Electron distribution available.

Answers: Electron E2E; wall journeys; routing negative control

From the repository root, after setting the prerequisite environment variables:

```sh
node tools/sync_kit.mjs "$CAPSULE_UI_KIT_REPO"
(cd ui && npm ci --offline && npm run build)
xvfb-run -a python3 tests/e2e/run.py
```

The runner builds a fresh temporary E2E bundle, discovers the existing Node
journey files and runs them serially. It then enables `MV_E2E_CROSS_ROUTE` for
the wall test. Success requires that the changed route fail the intended wall
assertion with `E2E_ROUTING`; a prerequisite failure does not count as detecting
the mutation. [CITED: ../tests/e2e/run.py, `main`, `is_cross_route_mutant_killed`]

These journeys test their configured fixtures. They do not by themselves prove
that every capsule kind, installed application or external runtime works.

## How do I verify a real capsule?

Use the live runners only after provisioning their named test identity and
pinned external runtimes. The public source contains the harness; a normal
checkout alone does not supply those runtime assets.

Answers: live capsule; G-W1; G-AI; cleanup evidence

The live slice requires `MV_E2E_LIVE=1`, the core, adapter and replay binaries,
and the native runtime paths listed by `requireLive` in
[the slice entry point](../tests/e2e/e2e1_slice/index.js). Keep the test identity
separate from any personal browser profile. The expected environment keys are:

```text
MV_E2E_CORE_BIN       MV_E2E_ADAPTER_BIN     MV_E2E_REPLAY_BIN
MV_SRF_BIN_DIR        MV_SRF_LIVE_PROFILE    MV_SRF_LIVE_RAM_ROOT
MV_SRF_STATE_DIR      SRF_IDENTITY_STORE_ROOT
SRF_PROXY_REGISTRY
```

The agent gate also requires `VQA_VE_ROOT` and `MV_WENDER_ROOT` to identify
runtime installations whose provenance matches [deps.lock](../deps.lock).
[CITED: ../tools/gate_g_ai.py, `preflight`;
../agents/metaviewer_agents/perception/deps_lock.py]

Once those prerequisites are verified, the commands are:

```sh
node tests/e2e/e2e1_slice/index.js --live
python3 tools/gate_g_ai.py --surface mv-live-1
```

The slice checks frame delivery, action receipts, stale-frame refusal, control
handoff, replay, rendering and cleanup. Its `DONE` result requires the named
assertions and cleanup fields to pass. The agent gate additionally resolves its
pinned runtime dependencies in `preflight`; `DONE` requires all assertions,
negative controls and cleanup. Read the full report before declaring success.
[CITED: ../tests/e2e/e2e1_slice/index.js, `checkLiveResult`;
../tools/gate_g_ai.py, `main`]

For an evaluator's installed-package route, use [JUDGE_QUICKSTART.md](JUDGE_QUICKSTART.md)
and [JUDGE_TOUR.md](JUDGE_TOUR.md). The fixture harness and the judge tour answer
different questions.

## How are web workflows and MemoryWeb checked?

The repository provides hosted fixtures and an explicit opt-in live runner.
Start with the hosted checks; they do not need a personal browsing session.
The MemoryWeb hosted suite still requires the pinned Wender checkout through
`MV_MW_WENDER_ROOT` or `MV_WENDER_ROOT`; hosted does not mean dependency-free.

Answers: web workflow tests; MemoryWeb home; hosted versus live web acceptance

```sh
node --test tests/e2e/asm_web_live_hosted.test.js
node --test tests/e2e/asm_web_memoryweb_hosted.test.js
python3 -B -m unittest tests/test_mw_home_cli.py
```

[asm_web_live.py](../tests/e2e/e2e1_slice/asm_web_live.py) requires
`MV_LIVE_WEB=1`, `MV_E2E_LIVE=1`, its dedicated profile and every `REQUIRED_ENV`
entry before native execution. Do not infer these from a hosted pass. Workflow
support and refusal behavior are implemented in the runner and
[mw_owner.py](../agents/metaviewer_agents/mw_owner.py); their fixtures are in
[the MemoryWeb hosted suite](../tests/e2e/asm_web_memoryweb_hosted.test.js).

For a local MemoryWeb home check, run:

```sh
python3 tools/mw_home_check.py --repo "$PWD"
```

That command may create the private home. It emits a JSON admission result and
checks ignored/untracked storage, home policy and optional workflow provenance.
It does not execute a workflow or certify a website.
[CITED: ../tools/mw_home_check.py, `main`]

## What do hosted CI and nightly runs establish?

Hosted CI checks the revision and environment named in its job results. A workflow
file's existence does not prove a completed run or configured branch protection.

Answers: ci-required; hosted CI; nightly volume; private assembly

[ci.yml](../.github/workflows/ci.yml) defines the `ci-required` aggregator,
platform jobs and receipt validation. It rejects missing required jobs, failed
assertions and mismatched product or revision evidence. The separate
[secrets workflow](../.github/workflows/secrets.yml) owns secret scanning.
[volume.yml](../.github/workflows/volume.yml) is scheduled/manual coverage, not
a required pull-request status. [assembly.yml](../.github/workflows/assembly.yml)
needs access to the private components and is not reproducible from public source
alone. Evaluation permission is defined in [NOTICE-EVALUATION.md](../NOTICE-EVALUATION.md).

Read actual job output and the commit identity before quoting a CI result.
Current-checkout totals after the baseline above, and live performance receipts,
are not supplied by this page: **ABSENT — do not invent**.

## How are documentation and negative controls kept honest?

Documentation checks require real pages, existing command paths and matching
index entries. A changed document needs a refreshed generated index before the
full freshness gate can pass.

Answers: documentation tests; stale index; missing suite; non-vacuity

```sh
python3 -B -m unittest discover -s tests -p 'test_*doc*'
python3 route/route_query.py check
python3 tools/router_index.py --check
```

[The presence gate](../tests/test_docs_presence.py) rejects missing pages,
wrong required headings, nonexistent test-suite names and stale document digests.
[The command-path checks](../tests/test_router_docs.py) reject Python commands
that name missing files. [The non-vacuity matrix](../tests/test_novac_matrix.py)
checks that its gates accept intended inputs and reject altered ones.

The index owner refreshes generated indexes after documentation changes. Report
index drift separately; do not edit a stored digest or weaken a test to obtain
a pass. For bug evidence and safe recovery, use [DEBUG.md](DEBUG.md).

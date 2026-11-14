# product/ref_agents: agent runner and role briefs (unit F4, reference, Track P)

Written by Claude (claude-fable-5-2), declared NON-Gemini product work. Reference /
backup candidate for the agent side of the ViewPoint multi-agent capsule. Stdlib only.

## Files

| file | what it is |
| --- | --- |
| `contract.py` | shared-contract constants and the intent validator (library + CLI) |
| `briefs/_shared_prefix.md` | the brief prefix all four agents receive byte for byte (ends at `<!-- CACHE_BREAKPOINT -->`) |
| `briefs/{modeler,props,look,camera}.md`, `briefs/_task_tail.md` | role sections and the per-session tail |
| `briefs.py` | builds `prefix + role + tail`; `python3 briefs.py --agent modeler-agent` prints one |
| `runner.py` | starts the agents (real `agy` or `--stub`), saves streams, records PIDs, stops by PID, validates intents |
| `stub_agent.py` | scripted fake agent: 10 deterministic valid intents over the four roles (7 bpy jobs, 3 key lists) |
| `tools/make_frame.py` | publishes one SYNTHETIC frame record (flat PNG, copied PNG, or `blender -b` render) for tests and live checks |
| `tests/test_ref_agents.py` | unittest suite (stub mode, no model) |
| `guard.py` | `instrument(src, budget_s)`: a script-lane job with a self-enforced time budget (library + CLI) |
| `tests/test_hardening.py` | hostile / broken intents and scripts, never-ending jobs, one real `blender -b` run |
| `evidence/live_modeler/` | the one live run with a real `agy` modeler-agent |

## How to run

All commands from `product/ref_agents`, inside the memory scope:

```bash
S="systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0"
RT=/dev/shm/vqa-cu/ref            # or export VP_SWARM_RT; must resolve under /dev/shm/vqa-cu/ref*

# tests
$S python3 -m unittest discover -s tests -v

# stub team (no model). Needs at least one frame in RT/frames (the broker's job; for a dry run:)
python3 tools/make_frame.py --rt $RT --frame 0          # synthetic frame, only without a broker
$S python3 runner.py run --rt $RT --evidence evidence/stub1 --stub --init-rt

# real team: four headless agy agents (gemini-4-hydrogen-high)
$S python3 runner.py run --rt $RT --evidence evidence/run1 --init-rt --max-seconds 1800 --max-intents 6
python3 runner.py status --evidence evidence/run1       # from another shell
python3 runner.py stop   --evidence evidence/run1       # stop by recorded PID

# validate whatever is in RT/intents (exit 0 = all valid, 1 = some invalid)
python3 contract.py --rt $RT [--agent modeler-agent] [--json]
```

Real-agent command (one process per role, cwd = RT, own session, `-p <brief>` last):
`~/.local/bin/shims/agy --model gemini-4-hydrogen-high --dangerously-skip-permissions --output-format stream-json --print-timeout 1800s --add-dir <RT> --log-file <evidence>/<agent>/agy_cli.log -p "<brief>"`.
`--add-dir` and `--log-file` are additions to the mandated flag list (RT must be in the
agent's workspace; the CLI log is evidence).

Evidence layout: `<evidence>/pids.json` (PID, kernel start time, command, prefix sha256,
rc per agent; written before the next agent starts), `<evidence>/summary.json`,
`<evidence>/<agent_id>/{prompt.md, agy_stream.jsonl, agy_stderr.log, agy_cli.log, finished_rc.txt}`.
`runner.py run` exits 0 only if every started agent has at least one intent and every
intent file in RT is valid.

## What the validator checks

Required fields (`v`=1, `rec`="enqueue", `event_id`, `agent_id`, `lane`, `kind`,
`based_on_frame`, `write_set`, `expect`); agent id known and equal to the intents
directory; `event_id` = `<agent_id>-<n>` and equal to the file name; lane/kind
consistency (script: job; gui: key or xy); `write_set` entries are `col:<Collection>`
and inside the agent's claim from `RT/claims.json` (default table if the file is absent),
non-empty for script jobs; `based_on_frame` an integer >= 0 and not newer than the newest
published frame; script lane: `script` present, compiles with `compile()`, <= 64 KB,
imports only `bpy bmesh mathutils math random colorsys`, no `exec/eval/open/__import__`,
no `bpy.ops.wm.*`, no `keys`; gui lane: camera-agent only, 1 to 8 keys from
`KP_1 KP_3 KP_4 KP_Add Home`, no `script`.

### Hardening (lane sw-agents-rewind, wave w186, claude-fable-5-2, Track P)

Script lane, all by AST, receiver-independent so an alias cannot hide a reach:
- `bpy.ops` is an allow-list: only `bpy.ops.<mesh|object|transform|curve|surface|armature|
  material|collection|node|uv>.<op>(...)`; `bpy.ops` and `bpy` cannot be stored, aliased,
  passed or imported under another name; `bmesh.ops` only as a direct call.
- Refused names: `getattr setattr delattr globals locals vars exit quit SystemExit
  KeyboardInterrupt GeneratorExit BaseException`, any dunder name except `__name__`.
- Refused attributes on any receiver: anything starting with `_` (closes
  `random._os.system`, `().__class__...`), `wm app utils handlers timers driver_namespace
  libraries as_module save save_render save_as_mainfile preferences`.
- Refused control flow that would swallow a stop: bare `except:`, `except BaseException`,
  `continue/break/return` inside `finally`, dunder method definitions.
- Numeric caps on arguments that make one C call run for minutes (`levels`, `segments`,
  `subdivisions`, `cuts`, ... see `contract.SCRIPT_NUM_CAPS`).
- Interpreter state: only `bpy.data`, `bpy.context`, `bpy.ops`, `bpy.types` are reachable
  on `bpy` (no `bpy.app / utils / path / props / msgbus`); module names cannot be aliased;
  assignment or `del` through a module (`bpy.data = None`, `math.sin = None`,
  `random.Random.seed = ...`, `bpy.types.Object.x = ...`) and calls through `bpy.types`
  (draw handlers, menu appends) are refused. Reason: module objects are shared by every
  later job and are not in a `.blend`; `bpy.data = None` and `math.sin = None` survive
  `open_mainfile` [MEASURED in `blender -b` 4.0.2, `GuardInBlender`], so a rewind cannot
  undo them.
- Hostile source never raises out of the validator (deep nesting, null bytes, > 20,000
  syntax nodes are refused).

Record and file form: ids matched with `\Z` (a trailing newline no longer passes);
unknown fields refused (an agent cannot pre-write `verdict`, and `x`/`y` exist only on
gui/xy); intent files must be regular files (no symlink), <= 256 KiB, UTF-8, without
duplicate keys or NaN/Infinity; a JSON nesting bomb is an error string, not a crash;
`RT/claims.json` is used only if every agent is known and claims are pairwise disjoint,
otherwise the default table applies.

`guard.py`: `instrument(src, budget_s)` returns the job with a self-enforced wall-clock
budget (a tick in every loop body, function body, lambda and comprehension clause). The
stop is a `BaseException` whose text starts with `VP_BUDGET_EXCEEDED`; the reference
bridge already answers `ERROR` for it, so the broker only has to instrument the text
before writing it to RT/inbox. `python3 guard.py job.py --budget-s 20` prints it.
`contract.determinism_warnings(src)` flags unseeded `random` (warning, not a gate).

Rewind contract rw1 (`product/ref_rewind/CONTRACT.md` section 5, branch p-fable-r0): the
optional fields are accepted and checked here, because the F1 broker and
`schema.validate_intent_ext` check their shape only: every entry of `alternatives` gets
the full script check; `expect` may be `{probe, equals}` and the probe gets the script
check plus `import json` restricted to `json.dumps` (`json.codecs` reaches `builtins`
and `sys`); `touches` is `mat:<name> | render | ui:view3d`, never `col:*`.
`guard.prepare_intent(rec, claims, budget_s)` is the single call for a broker: it
returns the instrumented script, alternatives and probe only when the whole record is
valid, plus determinism / foreign-collection warnings.

Stub team under the rewind judge [MEASURED with the R1 digest, `blender -b`, files in
`evidence/rewind_digest_probe/`]: as written, 5 of the 7 stub jobs have a digest delta
outside `write_set` (`mat:CHARMAT_*` in 5, `col:Scene Collection` in the first job of
each agent), which the rw1 judge rejects. `stub_agent.py --rewind` /
`build_intents(..., rewind=True)` adds `touches` for the materials a job names, and
`contract.genesis_script(claims)` is a broker-owned job that creates the claimed
collections before the genesis checkpoint; with both, 7 of 7 deltas are inside.

Tests: `tests/test_hardening.py` (29 tests, one of them an end-to-end slice: prepared intent -> RT/inbox -> the unmodified F1 `bridge.poll_once` in `blender -b` -> RT/outbox `ERROR ... VP_BUDGET_EXCEEDED`, next job `OK`). The original validator (e81c580) ACCEPTED
41 of the 46 hostile scripts in `HOSTILE` [MEASURED]; now 0. 11 mutation controls (one
check disabled at a time on a scratch copy) each turn a test red [MEASURED]. Controls: the legit corpus
(7 stub jobs + the real gemini agent's job) and 14 benign idioms stay valid; the same
never-ending job without instrumentation is still alive after 2 s while the instrumented
one stops within its 0.2 s budget; in `blender -b` 4.0.2 a `while True` job creating
datablocks is stopped at 0.5 s, a raising job answers ERROR, and the next agent's job
then runs OK in the same session.

Not covered: one long C call (`sorted(range(10**9))`, `10**10**8`, a heavy operator under
the caps) cannot be stopped from Python: the broker's wall-clock timeout, a Blender
restart and a rewind remain necessary. A stopped job has usually written part of its
effect, so the turn must be restored from the last checkpoint, not treated as a no-op.
A disjoint but swapped `claims.json` cannot be detected here: the file must be writable
by the broker only. Driver expressions and node-group scripting were not audited.
`guard.instrument` / `guard.prepare_intent` are not yet called by any broker: the F1
`Broker._validate` checks field types only, so today a hostile script that reaches
RT/intents is dispatched. `evidence/rewind_digest_probe/` holds a read-only probe of the
R1 digest (snapshot of an uncommitted file): stable across processes and across a
save/open round trip, blind to light energy/color, camera lens, face topology, shade
smooth, custom properties, `hide_set`, `display_type`.

## Verified (what was actually run)

- `python3 -m unittest discover -s tests`: 53 tests, 53 pass, 0 fail (in the systemd scope; 24 original + 29 hardening).
  - Negative control `test_negative_control_foreign_collection_rejected`: a modeler-agent
    intent with `write_set ["col:CHAR_props"]` is rejected with `CLAIM_REFUSED`, and the
    same record passes once the claim table grants that collection, so the test goes red
    if the claim check is removed. Runner-level twin: `test_run_goes_red_on_invalid_intent`.
  - Shared prefix is byte-identical across the four briefs; the four tails differ.
  - Stub end-to-end: four stub processes, 10 valid intents, PIDs recorded, streams saved,
    no file created in RT/inbox, RT/outbox, RT/events.jsonl, RT/cursor.jsonl.
  - Stop by PID: a timed-out agent and its process group are stopped with SIGTERM; a PID
    whose start time does not match is left alone.
  - The 7 stub bpy jobs run twice in `blender -b` (4.0.2) without error; every object sits
    in exactly its owner's collection and the second pass creates no duplicates.
- Live check, 2026-11-14, one real `agy` modeler-agent (`gemini-4-hydrogen-high`, init
  record in the stream), RT with one synthetic frame rendered by `blender -b`:
  runner rc 0, agent rc 0, SUCCESS in 50.8 s [MEASURED, agy result record], one intent
  `modeler-agent-1` written, validator: valid. The intent's script was then executed in
  `blender -b`: objects `CHAR_mesh.hips` and `CHAR_mesh.torso`, both only in `CHAR_mesh`
  (`evidence/live_modeler/blender_exec_check.txt`). The agent's shell commands in the
  stream touch only RT paths.

## Not verified / open

- No broker, bridge or vision job was involved: nothing here was executed through
  RT/inbox, no turn or close record was produced, no GUI key intent was ever typed.
  Integration with the other three reference units is untested.
- Only modeler-agent ran live, once, with `--max-intents 1`. The props, look and camera
  briefs have never been given to a model; four concurrent agy processes were not tried.
- The brief tells agents to read `close` verdicts in events.jsonl and react; with no
  broker that loop was not exercised. An agy session is one print-mode turn: an agent
  writes its batch and exits; re-launch the runner for the next batch (event numbers
  continue from the directory).
- Contract gaps filled by a local choice: (a) `kind:"xy"` on the gui lane is validated as
  integer `x`,`y` fields, which the shared contract does not define; (b) gui intents carry
  `write_set: []`; (c) the gui lane is reserved to camera-agent; (d) look-agent tunes the
  values of the shared `CHARMAT_*` material datablocks (materials are not objects in a
  collection, so the claim model does not cover them); (e) the validator does not prune
  to the 8-deep FIFO, since whether executed intent files stay on disk is the broker's
  decision.
- The script checks are static (AST). They do not prove a script only writes its own
  collection at run time; `foreign_collections()` is a warning helper, not a gate. The
  run-time guarantee has to come from the broker / bridge.
- `evidence/live_modeler/modeler-agent/agy_cli.log` is the unedited agy CLI log; it names
  a credential store path (no credential content was found by a keyword scan).

## fix-io (xr-io cross-review fixes, claude-fable-5-2, Track P)

Tests: `tests/test_fix_io.py` (10 tests; each was red on ff6b6b1, see below). Full suite: 63 run, OK [MEASURED].

| Finding | Fix | Test (red before, green after) |
|---|---|---|
| A1 BLOCKER: `bpy.types` alias / `type(x)` / header append poison the shared session | `bpy.types` may only appear as the literal `isinstance(x, bpy.types.<Name>)` (or a literal tuple); no alias, store, argument, base class, container, walrus. Names `type`, `super`, `object`, `classmethod`, `staticmethod`, `property` and attributes `bl_rna`, `rna_type`, `mro` are refused; `isinstance` may not be rebound. | `A1TypesPoisoningTest` |
| A5 MAJOR: second SIGTERM during stop orphaned the agents | Only the first signal raises; later ones are recorded (`summary.json.signals`). `stop_all` always reaches SIGKILL, summary is written before the handlers are restored. | `A5SecondSignalTest` (stubborn SIGTERM-ignoring child, SIGTERM + SIGTERM + SIGINT) |
| A3 MAJOR: keymaps / drivers | Attributes `window_manager(s)`, `keyconfigs`, `keymaps`, `keymap_items`, `driver_add`, `driver_remove`, `drivers`, `expression`, `use_module` refused on any receiver. | `A3KeymapDriverTest` |
| A2 MAJOR: host I/O inside allowed op categories | Attributes `export_layout`, `multires_external_save/pack`, `ocean_bake`, `load`, `pack*`, `unpack*`, `save_external` refused; keyword arguments `filepath`, `directory`, `files`, `filename`, `relative_path` and `**kwargs` in calls refused; stores to `.filepath`, `.filepath_raw`, `.directory` refused. | `A2HostIoTest` |
| A6 MINOR | `except <Name>` only for a fixed list of builtin Exception subclasses (which may not be rebound); `iter` only as `iter(<one arg>)`. | `A6BudgetEvasionTest` |
| A4 MAJOR (partial) | The runner pins the sha256 of `RT/claims.json` at start; a different file at the end fails the run (`summary.json.claims_changed`). | `A4ClaimsPinTest` |

Still open:
- A4 remainder: `agent_id` is self-declared and every `intents/<agent>` directory is writable by all agents (they share one `--add-dir RT`); an agent can still file intents as another agent. The claims pin detects a rewrite only at the end of the run; the broker must load claims from a copy outside RT (or compare with `pids.json.claims_sha256`) before it trusts them. A rewrite-and-restore during the run is not detected.
- A2 is a deny-list inside the allowed categories, not a per-operator allow-list; other file-writing operators in `object`/`mesh`/`node` that take no path keyword were not enumerated.
- A3: the bridge does not assert at startup that auto-exec (`-y`) is off.
- The A1/A2/A3 fixes are verified statically only (no Blender run in this lane).
- write_set is still not enforced at run time; check_rt accepts any `/dev/shm/vqa-cu/ref*`.

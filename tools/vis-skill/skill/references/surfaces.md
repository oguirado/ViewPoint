# Choose the evidence channel

| Surface | Launch | Available observation | State predicates |
|---|---|---|---|
| Browser UI | `--web URL`, headless by default; `--headed` adds private Xvfb | Pruned wender.state/v1 STATE, AX receipts, painted frames | Exact role/name and states/geometry exposed by STATE |
| Linux desktop | `--app 'COMMAND'`, private Xvfb and session | Pixels, numbered marks, visible window titles | AUSENTE; `--state`/`--state-absent` return E_ABSENT |
| Electron | `--electron DIR`, VIS_ELECTRON_BIN required | Same pixel/title path; no external CDP adapter | AUSENTE |
| Terminal interface | `--app` with a locally available terminal emulator and the TUI command | Pixels of that terminal on the private display | No TUI or LSP adapter implied |
| Headless logic/API/service | Use the program's tests/CLI | Unit/integration/static results | Outside this skill |

All commands use `python3 -I @VISION_PATH@`. Run doctor first and inspect the surface-specific `missing` list, `state_fields`, selected refs and reasons. Doctor's success is not an application test. Godot visual work belongs to godot-visual-qa when available; otherwise report the gap.

## Pin selection and immutable engines

Pass `--target-repo DIR` to doctor and start, or set VIS_TARGET_REPO. `DIR/deps.lock` is JSON: engine entries named `wender-agent` and `vision-engine`, each with a full commit SHA in `commit`. A `version` label never selects code. Supported containers are a direct object, a `dependencies`/`deps` object, or a named-entry list under those containers. Example structure: `{"dependencies":{"wender-agent":{"commit":"FULL_SHA"},"vision-engine":{"commit":"FULL_SHA"}}}`; use real resolved SHAs, not those placeholders.

An absent lock or absent engine entry uses that engine's default and explicitly reports the reason. A present malformed lock or commit refuses; it cannot silently downgrade. There are no selectable product profiles. Defaults live in vis.json; historical versions are compatibility fixtures only. Start records resolved identities in job state and receipts so later verbs keep the same engines regardless of the caller's environment.

Snapshots are extracted atomically from pinned git archives into a read-only SHA cache. VIS_WENDER_REPO and VIS_VE_REPO select the source repositories. Never execute or modify those checkouts. Capability probing handles older engines without frame-raw; framebuffer bytes are encoded as PNG using the standard library. No engine code is vendored.

## What each predicate proves

`look` returns observation, not acceptance. `act --expect-change any` proves a visible change according to both the engine regions and independent decoded pixels. It cannot distinguish `Done` from `Failed` unless a separate semantic oracle checks that result. For a web button that must become Done, follow the action with `check --state 'button=Done'`; a wrong accessible name fails that predicate.

STATE intentionally excludes static/provider text and opaque article interiors. Raw AX and HTML never go to a model. Exact role/name checks cover the represented STATE nodes, not arbitrary DOM strings. A failed/truncated tree cannot satisfy absence. Positive targets need supported visible, nonzero geometry; missing geometry is not evidence of visibility. An absence request with an opaque article subtree refuses because its interior is not observable.

A successful title predicate proves the title and nothing about the document body. A region predicate compares the current painted region to that step's retained **pre-action** frame. It does not prove the changed content is correct. Unknown step: E_USAGE; evicted baseline: E_EXPECT. `--require-stable` compares consecutive relevant observations, not historical rectangles.

After `--type` or AX `--op type`, read back the actual value. A changed input box may only show a caret or focus border. `role=name` does not inspect arbitrary values: use a documented app test hook externally or inspect STATE/crops where those actually expose the result. Desktop hooks are external evidence until an adapter exists. OCR alone is never accepted. If the only remaining semantic judgement would require unauthorized hosted pixels, report incomplete verification.

For modal dismissal, row deletion and tab replacement, verify the new result and the vacated state/region. Presence of new content does not prove old content disappeared. Cite matching `act`/`check` pass receipts plus the semantic evidence used. Operational receipts for look or stop cannot repair a failed acceptance check. App stdout/stderr, exit success and model self-verdicts are passive evidence.

## Timing and health

The settle window samples onset and quiescence on fixed monotonic ticks rather than sleeping blindly; `sampling.settle_degraded` flags captures over twice the tick (troubleshooting). Digests guide waiting; the engine and independent pixels decide the effect expectation. `none` fails on any sampled unmasked transient, even a change that reverts before the final frame. Finite polling can miss changes between samples and does not promise continuous monitoring. Mask clocks/carets/spinners deliberately; do not mask the feature being tested.

A mapped window, non-black framebuffer or two warm-up reads cannot prove first paint. A real black interface is valid. Capture availability, target responsiveness and feature readiness are separate. For supported desktop targets, Tk send executes a constant in an authorized target interpreter, including adopted targets; advertised _NET_WM_PING checks the application loop. Web probes execute a bounded CDP expression. A missing loop witness permits acts using verified PIDs, an existing desktop window and pixel checks on started, adopted and act-authorized attached jobs; it does not produce E_ABSENT. A demonstrated probe timeout is E_TARGET. Act responses and receipts expose `loop_witness.before` and `after` (true: obtained, false: unavailable, null: not completed); `obtained` requires both. A healthy reply is not semantic acceptance.

Private HOME, XDG directories, TMPDIR, language and session bus stay in the job. This prevents routine singleton/config routing into the real session; it is not a filesystem/network security boundary. Keep input fixtures and saved files in RAM. Untrusted programs require a capsule; vision will not create one or downgrade to plain Xvfb.

Never read credentials, tokens, keys or auth files, and never type real secrets; on-screen labels such as token or password are ordinary UI to look at and click. Never probe real credentials to test isolation. The secret filter checks each text payload for key-shaped strings; split payloads and desktop key events bypass it. Browser protected fields are refused; X11 password fields are not detected.

## Hosted images and other owners

`ask`, `check --ask-file` and `--untrusted` are absent (F10); the parser refuses them with rc 2. Read job frames with your own image reader. Sending pixels to a hosted model needs the operator's explicit consent. A model's answer is consultation data, never an acceptance receipt.

Adoption/attachment are operator-coordinated workflows. Do not inspect owner tokens or assume an occupied display is free. Adopted/attached displays survive stop. Identity capsules, shared games and logged-in browsers require their owner's coordination; vision does not supply their admission protocol.

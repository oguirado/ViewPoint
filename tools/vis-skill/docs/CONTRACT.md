# vision CLI contract v1 (binding for the builder and for the independent gate author)

Skill name: vision (formerly vis). Install from the retained checkout with `VISION_INSTALL_OK=1 ./install.sh --apply`; preview with `./install.sh --dry-run` (no writes). See [README](../README.md) for compatibility names and installation guards.

`vision` = one executable, Python 3.11+ stdlib only, calling the engines by subprocess. Run as `python3 -I <repo>/vision ...` (the `vis` alias stays supported) or directly. Every verb prints exactly ONE canonical JSON line on stdout: `{"ok":bool,"rc":int,"code":str,"verb":str,...}`. Diagnostics go to stderr. A green result must be earned; any uncertainty is a failure.

## Exit codes
| rc | code | Meaning |
|---|---|---|
| 0 | OK | Passed |
| 2 | E_USAGE | Bad arguments, unknown job |
| 3 | E_EXPECT | The action ran but the expectation failed |
| 4 | E_RETRIES | Retry budget for this step is spent (3 attempts) |
| 5 | E_RULE | A hard rule refused the request; nothing was done to the target |
| 6 | E_BUDGET | RAM or time budget |
| 7 | E_ABSENT | Capability missing here (tool, engine feature, surface) |
| 8 | E_TARGET | Target app or display died, never appeared, or stopped responding |
Never exit 1 on purpose. An unexpected exception prints code E_INTERNAL with rc 9.

## Job layout
`/dev/shm/vqa-cu/vis/<job>/` mode 0700, on tmpfs (checked with statvfs f_type or `df -T`; else rc 5). `job.json` holds display, recorded PIDs with `/proc/<pid>/stat` start times, ownership kind (`started|adopted|attached`), refs, step counters. `<job>` matches `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`.
Frames, marks and crops live only there. Keep at most the last 40 frames; RAM budget per job 2 GiB (rc 6 beyond).
`receipts.jsonl` in the job dir; `vision stop` copies it (text only, no images) to `~/.local/state/vis/receipts/<job>.jsonl` and reports the path.

## Engines
`vis.json` (repo root) holds default full commit shas: wender `96120967b435b72fc6d29635f3844dae140388a9`, vision-engine `3bb54835db8a2e545311cfcc45185a0dfbd9313a` (resolved locally with `git rev-parse`). No product-named profiles. `--target-repo DIR` or `VIS_TARGET_REPO` reads the two pins from `DIR/deps.lock` (entries `wender-agent` and `vision-engine`, field `commit`); if it is absent or has no such entries, use the defaults and say so in the output. `vision doctor` reports which refs are in use and why. Extract with `git -C <repo> archive <sha> | tar -x` into `~/.cache/vis/<name>-<sha>/` read-only; atomic on first use. Source repos: `VIS_WENDER_REPO` (default `components/wender-agent`), `VIS_VE_REPO` (default `components/vision-engine`). Never run from the checkouts, never modify them.
Capabilities are probed, never assumed: `cu_describe --help` for `--frame-raw`; file checks for `hosted_guard`, `vserve`. Old refs (for example vision-engine `9bbb49f`) have no `--frame-raw`: encode PNG with stdlib zlib in that case.

## Verbs
`vision doctor [--skill] [--target-repo DIR]`: tools (Xvfb, xdotool, dbus-run-session, openbox, ffmpeg, Chrome), engines and capabilities, RAM, tessdata. `--skill` compares exact rendered file contents and file sets in all four vision copies (`drift:true|false`), returning rc 3 on changed, missing or orphan files. `legacy_copies` and warning `legacy_skill_copy_present` report vis copies without changing rc. rc 0 if the core is usable, listing what is missing per surface; rc 7 if not.

`vision start --job J (--app 'CMD' | --web URL | --electron DIR) [--size 1280x800] [--wait-title REGEX] [--timeout 30] [--untrusted] [--target-repo DIR]`
- Private display by `Xvfb -displayfd <fd> -screen 0 WxHx24 -fbdir <job>/fb -nocursor -nolisten tcp -noreset`. Never a fixed number. `-displayfd` may return `:0`: ownership is by recorded PID, never by number.
- Everything in one `systemd-run --user --scope` with `MemoryMax=4G MemorySwapMax=0` (`--mem` to change). The scope name is recorded.
- Desktop session: `dbus-run-session -- bash -c 'openbox & exec CMD'` with `DISPLAY`, `LANG=C.UTF-8`, `LANGUAGE=C`, `HOME`, `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`, `XDG_DATA_HOME`, `XDG_RUNTIME_DIR` all inside the job dir. Inherited `DISPLAY` is ignored everywhere.
- `--electron DIR`: uses `VIS_ELECTRON_BIN`, else rc 7; flags `--no-sandbox --disable-gpu`. Pixels only.
- `--web URL`: a per-job helper process holds `wender.perceive.cdp_pipe.CdpPipe` (headless Chrome over a pipe; `CHROME_BIN` default `/usr/bin/google-chrome`), and answers `vision` verbs over a mode-0600 unix socket in the job dir. It also has a private Xvfb only if `--headed`.
- `--untrusted`: rc 5, reason: Xvfb isolates only the display; use a capsule.
- Waits for a visible window (`xdotool search --onlyvisible`, `--wait-title`) or the page load; else rc 8, cleaning up.

`vision adopt --job J --display :N --xvfb-pid P`: allowed only if P is alive, owned by this uid, its `/proc/P/cmdline` is an Xvfb for `:N`. Kind `adopted`: full access; `vision stop` never kills it. Otherwise rc 5.

`vision attach --job J --display :N --owner-ok TOKEN [--allow-act]`: any other display. TOKEN must equal the content of `VIS_OWNER_TOKEN_FILE` (absent or different: rc 5). Kind `attached`: without `--allow-act` every act verb gives rc 5. Capture by `ffmpeg x11grab`.

`vision look --job J`: captures; runs `cu_describe --marks-out <job>/marks_N.png --ocr-marks [--marks-extra]`. Returns `frame`, `frame_sha256`, `marks_png`, `marks` (id, x, y, w, h, kind, ocr_text), and for web `state` (the `wender.state/v1` STATE; raw AX or HTML never). `TESSDATA_PREFIX` default `~/.local/share/tessdata`.

`vision act --job J --step S (--click ID | --xy X,Y | --type TEXT | --key K | --ax NODE_ID --op click|focus|type [--text T]) (--expect-change any|none|X,Y,W,H) [--mask X,Y,W,H]... [--settle-ms 800]`
- `--expect-change` is mandatory (rc 2 without). `--click ID` uses the id from the latest `look`, clicks the box centre. Move, wait 300 ms, press, wait 100 ms, release, settle.
- Captures before and after, runs `cu_describe --prev` with `--crop-dir`. `any` requires at least one changed region after masks. `X,Y,W,H` requires a changed region intersecting it. `none` requires no change.
- `--ax` is web only (else rc 7): uses `run_ax_action` with pruned AX nodes (`ax.prune(ax.raw_tree(...))`), and its receipt must have ok=1 and verified=1.
- Attempts are counted per `--step`, whether they pass or fail: a 4th attempt on the same step gives rc 4 without touching the target. Same step id after a pass is a usage error (rc 2).
- Target dead or frozen: before each action and after settle, check PIDs alive and (desktop) that the X window still exists; if not, rc 8. Unchanged frames are NEVER a pass unless `none` was requested.

`vision check --job J (--state 'role=name' | --state-absent 'role=name' | --region-changed-since STEP X,Y,W,H | --window-title REGEX | --ask-file)`: rc 0 or 3. OCR text alone is never accepted as a pass: there is no verb option for it.

`vision ask --job J --seat sol|muse --q 'text' [--crop N|--marks] --allow-hosted-pixels`: without the flag rc 5 and no request is sent. Commands as in `VISION_WENDER_PLAYBOOK.md` section 7. Records seat, frame sha and answer in the receipt. A Claude orchestrator reads `marks_png` or crops by itself.

`vision stop --job J`: signals only recorded PIDs after re-verifying start time; removes the job dir; copies receipts; adopted and attached displays are left alone.
`vision sweep`: for job dirs whose owner process and display are gone, same cleanup. Never touches anything else.

## Rules enforced on every act verb
1. `MV_HOSTED=1` in the environment: rc 5, on every ref, no input sent. (Old wender refs have no guard, so vision does it itself.)
2. Exclusive `flock` per job display: second actor rc 5.
3. Secrets: refuse (rc 5) text matching `sk-`, `ghp_`, `gho_`, `AIza`, `xox[bp]-`, `-----BEGIN .*PRIVATE KEY`. Web: refuse typing into a node whose state is `protected`. Refuse `--type-file` under `~/.ssh`, `~/.config/*/api_key`, `auth.json`, `.credentials*`, by path and without reading it. X11 password fields cannot be detected: documented limit. vision never reads secret files.
4. Act only on `started` or `adopted` jobs, or `attached` with `--allow-act`.

## Receipt (one JSON line per verb that touches or reads the target)
`{schema:"vis.receipt/v1", job, step, verb, action, target_kind:"desktop|web|electron", display, ownership, frame_sha_before, frame_sha_after, expected, changed_regions, verdict:"pass|fail|refused", rc, code, attempts, ref_wa, ref_ve, t_ms}`. Never pixels, never typed text if it is longer than 80 chars (store its sha256 instead).

## Skill files (builder)
`skill/SKILL.md.tmpl` (at most about 150 lines): when to use and when not, the loop, verbs, exit codes, hard rules, absent list. `skill/references/`: `make_it_visible.md`, `surfaces.md`, `worker_block.md` (byte-stable, no dates or variable paths except the compatibility `@VIS_PATH@` placeholder), `troubleshooting.md`. Frontmatter only `name: vision` and `description`.
`./install.sh --dry-run` (default) prints every planned write, directory creation and deletion without writing. `VISION_INSTALL_OK=1 ./install.sh --apply` syncs `dist/claude/vision/`, `dist/codex/vision/`, `~/.claude/skills/vision` and `~/.agents/skills/vision`. `VIS_INSTALL_OK=1` remains accepted. Orphans are deleted only within those directories. Home targets inside the checkout and symlink paths are refused with rc 5 before writes. Legacy vis directories remain untouched.

## Test programs on this host
gedit, /usr/games/gnome-mines, Tk via python3, /usr/bin/google-chrome, Electron at `$VIS_ELECTRON_BIN`.

## Writer clarifications

The existing exit/status and receipt schemas remain authoritative. Engine entries use the `commit` field exclusively. An absent entry falls back per engine with an explicit reason; malformed present pins/locks refuse with E_USAGE. JSON locks accept direct engine maps, `dependencies`/`deps` maps, and named-entry lists under those containers. No product profile is selectable.

Mark handles belong to the latest explicit look and expire when an action dispatches. Act responses do not renew handles. A mark click also rechecks the marked pixels against its look frame; unrelated repainting elsewhere does not renew or invalidate that mark.

Settling uses bounded temporal samples within --settle-ms, with masks applied consistently. Any sampled transient defeats `none`; finite sampling cannot prove continuous absence of change. Digest stability only guides waiting. No brightness/warm-up heuristic establishes first paint.

`check --require-stable N` (default consecutive matching reads) requires the relevant current observation to match across N reads. Historical region checks compare fresh captures with the named step's retained **pre-action** frame. Unknown step is E_USAGE; missing/evicted baseline is E_EXPECT. Frame production prunes the ring and records frames_dropped and oldest_retained_step.

STATE checks cover only supported pruned STATE roles; static/provider text is outside this channel. Positive targets need visible nonzero geometry. Incomplete retrieval and unobservable absence refuse. Desktop/Electron STATE and unspecified --ask-file are E_ABSENT.

Responsiveness uses a target-loop witness when one exists: bounded Tk send when the target is Tk (depending on the target, not on the job kind), advertised _NET_WM_PING, or CDP Runtime.evaluate. A demonstrated timeout is E_TARGET. **Orchestrator ruling, on the independent gate author's finding:** a missing witness is never E_ABSENT for an act. On started, adopted, and attached with `--allow-act` jobs, the act runs whenever recorded PIDs verify alive and the desktop window exists, and falls back to PID/window plus pixel change (rc 0 or rc 3). E_ABSENT for acts is only for genuinely missing capabilities, such as `--ax` on desktop or a missing Electron binary. Process/window existence and changed pixels alone do not prove semantic correctness.

Image-only effective judge capabilities are not yet independently verified. `ask` without authorization remains E_RULE; with the flag it currently returns E_ABSENT before dispatch. **This is a temporary gap, tracked as missing**, not a semantic acceptance route and not a permanent term. Verdicts stay owned by `vision` either way.

Private TMPDIR joins HOME/XDG/session-bus isolation. Stop and sweep coordinate with readers/actions through the lifecycle lock, preserve display-wide actor exclusion, verify owned scope membership and emptiness, and leave adopted/attached displays running. A successful stop receipt is published only after teardown/removal. Null frame hashes mean evidence was unavailable, never measured non-mutation. Uniform frame SHA fields hash decoded RGB pixels; masks affect comparisons, not frame identity.

The gedit private-session gate compares only application-owned real config/data metadata and verifies private HOME/config writes, with a controlled negative write witness. It does not assert stability of unrelated host configuration or arbitrary filesystem confinement.

Additive receipt schema notes: `vis.receipt/v1` STATE checks add `predicate:{kind:"state|state-absent",operand:{role,name_sha256},require_stable}` and `observation:{reads,stable,conditions,match_counts,matches_sha256}` (null when no complete observation was obtained). Names and observed node contents in the added fields are represented by digests, never raw AX. The role is bounded; unusually long or credential-pattern roles use a digest. Observation arrays are bounded by the accepted stability requirement. Legacy `expected` is unchanged.

Additive run identity note: `job.json` and every receipt add immutable `run_id`. Text archives use `<job>-<run_id>.jsonl` beneath the existing receipts directory, including failed starts; publication refuses replacement. Existing live jobs without an ID acquire one under the state lock on first load.

Archive path compatibility clarification: every published run archive at `~/.local/state/vis/receipts/<job>-<run_id>.jsonl` is immutable and never overwritten. The documented `~/.local/state/vis/receipts/<job>.jsonl` path is a relative symlink atomically replaced after publication to resolve to the latest archived run for that job, including failed starts. An existing legacy copy is replaced by the symlink without changing earlier run archives. `vision stop` reports the immutable path as `receipts` and the latest-run path as `receipts_latest`; its successful stop receipt is present before either path is published.

Additive attempts note: receipts add `attempts_scope:"per_step_id"`; `attempts` counts actions for the receipt's caller-supplied `step` ID within its job run. A different ID has an independent counter; no job-wide retry budget is claimed.

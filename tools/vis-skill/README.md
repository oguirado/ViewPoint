# vision (formerly vis)

Run and inspect Linux desktop, Electron and browser interfaces with private displays,
painted frames, visible-effect checks and text receipts. Claude and Codex load the
same skill. Start with `python3 -I ./vision doctor` and inspect the surface's missing
capabilities before starting a job. See [the contract](docs/CONTRACT.md) and
[the orchestration guide](docs/ORCHESTRATOR_GUIDE.md).

## Install

From the checkout that will remain available to the installed skill:

```sh
./install.sh --dry-run
VISION_INSTALL_OK=1 ./install.sh --apply
python3 -I ./vision doctor --skill
```

Dry-run prints every planned `MKDIR`, `WRITE` and `DELETE` and writes nothing.
Apply syncs only `dist/claude/vision`, `dist/codex/vision`,
`~/.claude/skills/vision` and `~/.agents/skills/vision`; it removes orphans inside
those directories. An unchanged second apply performs no writes. `VIS_INSTALL_OK=1`
is still accepted. A home target inside the checkout or any target crossing a
symlink is refused before writes, with rc 5. Legacy vis skill directories are
never modified or deleted.

`doctor --skill` compares every packaged file with the rendered sources in all
four copies. A changed, missing or orphan file returns rc 3 with `drift:true`.
An exact install returns rc 0 with `drift:false` when the core is available.
`legacy_copies` lists old vis skill locations; `legacy_skill_copy_present` in
`warnings` leaves the return code unchanged. Other core capability failures
retain their existing return codes.

## Compatibility

The vis executable remains the implementation; `vision` is a relative symlink
to it. Both support `python3 -I`. Compatibility names remain: RAM path
`/dev/shm/vqa-cu/vis/`, state path `~/.local/state/vis/receipts/`, `VIS_*` variables,
`vis.json`, engine cache paths and schema ids (including `vis.receipt/v1`).
The pinned `skill/references/worker_block.md` stays byte-identical. It still
uses the vis command and mentions the old branch prerequisite; that commit is
already in the history. Generated legacy vis packages remain untouched.

## Verification

Run all tests through the campaign's `g1/safe_run.sh` with a fresh temporary
HOME. Never apply an installer test to the real HOME. The wrapper sets
`NEWTON_TEST_PIDNS=1` and supplies the resource cap and private PID namespace.

```sh
# Inside safe_run, with HOME set to a fresh temporary directory:
python3 -I tests/test_vision.py -v
python3 -I tests/installer_mutants.py
VIS_GATE_NO_INSTALL=1 ./gates/run_gates.sh --all
python3 -I gates/false_green/false_green.py --vis "$PWD/vision" --out /tmp/vision-fg-results.json
```

The full gate suite marks unavailable systemd scope checks as uncovered inside
the wrapper. The independent false-green suite is run separately as described
in [release notes](docs/RELEASE_NOTES.md). Its web fixtures can hit Chrome's root
sandbox refusal under the wrapper's root mapping; record that separately from
rename regressions.

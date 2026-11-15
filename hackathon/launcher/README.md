# Launcher — one-command take scripts

> The three take scripts described here are **not published**: they only run on the build machine (they drive the private ViewPoint fork, the private capsule binaries and a local package). This page documents what they do so the demo is reproducible in spirit.


Host-specific scripts that start the demo take, open/close the operator view, and stop cleanly. They are **not** portable: they drive the private ViewPoint app fork, the private OpenSuperficies capsule binaries, and the private vision/wender components on a prepared GB10 arm64 host. A fresh clone cannot run them. Logins stay manual; the scripts never read profiles, cookies, or credentials.

| Script | Role |
|---|---|
| `vp_demo_take.sh` | Operator entry point: `start`, `view`, `show`, `hide`, `stop` |
| `vp_demo_v1.sh` | Underlying start/stop for the app + capsules |
| `vp_arm64_env_v3.sh` | Generates the clean executable environment for the app |

## Commands

```bash
bash launcher/vp_demo_take.sh start   # fresh start: 4 capsules, 2x2, per-capsule preflight
bash launcher/vp_demo_take.sh view    # open the private screen on your desktop (VNC viewer)
bash launcher/vp_demo_take.sh show    # [ON HOLD / TARGET] native window via xpra — disabled
bash launcher/vp_demo_take.sh hide    # hide viewer/window; program keeps running
bash launcher/vp_demo_take.sh stop    # clean stop; identities and logins are kept
```

- `start` restarts fresh (so the proposal-witness and journal budgets are clean), opens the capsules, writes the MCP session file (`vp_mcp_session.json`, copied into the take dir), and runs preflight: a negative control (unknown identity must be refused) then one navigation per capsule, reading back the login page where relevant. Screenshotting is `DISPLAY=:98`/tiles on its own private display. Restart before each take costs ~3 min and keeps state clean; logins persist.
- `view` shows the `:98` private screen on your desktop (localhost-only VNC, input allowed for manual logins). Closing the viewer or running `hide` leaves the program running.
- `show` is **on hold**: the first desktop attach took no input and disturbed the desktop session, so it exits with an error unless it passes on a private test display first (`VP_SHOW_OK=1` only then). Use `view`. [ON HOLD / TARGET]
- `hide` detaches the window/viewer without stopping the program or its agents.
- `stop` stops the recorded processes and shelves the run dir; identities/logins in `VP_STORE` are kept.

## Environment variables

| Var | Meaning (defaults in brackets) |
|---|---|
| `VP_SCREEN` | Demo screen size, `WxH` [1920x1080] |
| `VP_IDS` | Comma-separated capsule identities, sorted unique [arxiv,chatgpt,gemini,wikipedia] |
| `CAND` | Candidate package under `scratch/` [cand10d] |
| `REPO` | Tools checkout (falls back to CAND7 worktree) |
| `SRF_BIN` | Capsule binary dir (defaults to the combined srf-bin) |
| `VP_STORE` | Persistent identity store (private, outside the repo) |
| `VP_RECORDINGS` | Recordings + screenshots dir |
| `VP_DOMAINS` | Allowed domains for new identities |
| `PREFLIGHT` | `0` skips the negative-control + navigation checks [on] |
| `OPERATOR_DISPLAY` | Your desktop display for `view`/`show` [`:0`] |
| `VP_SHOW_OK` | `1` only after `show` passes on a private test display [off / hold] |

Private parts the scripts need (not in this repo): ViewPoint app fork package, srf capsule binaries, identity tools + registry, clean-env launcher, live attachment (`current.env`), and the MCP server in `mcp/vp_mcp/`. Bring those with you; the scripts will not fetch them.

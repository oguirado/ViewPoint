# ViewPoint v0.3.2

The local release candidate combines the VP-MAIN-1 chain through AM with
the accepted REA slot-control change. Build the candidate from the
[source installation guide](INSTALL.md). The source version is `0.3.2`
[CITED: `VERSION`]. Published candidate assets are AUSENTE.

## What changed

- VP-MAIN-1 through AM adds VP Direct runtime, control and evidence contracts,
  the scoped Asset Factory import, and Blender Visual QA. The candidate
  preserves the public Vision regression fixes and the Video REA synthetic
  wide fixtures. [Component map](docs/COMPONENTS.md) · [Dependency pins](deps.lock)
- BASE-OBS verified the original AM tree
  `53a273ba7ed772bfcfda926174fa5fc386e0552e` with `12 PASS + RECEIPT`, a
  Carbon `PASS`, and a Sonnet API `PASS-WITH-FIXES` [CITED: BASE-OBS].
  The join reran selected frame, storage, privacy, T2 and native capture
  checks. The BASE-OBS receipt applies to its original tree.
- Blender Visual QA runs in an isolated process, renders from a scratch
  copy, and checks that output paths stay within a verified RAM workspace.
  [Blender Visual QA guide](docs/VISUAL_QA_3D.md)
- The owner routes and catalogue index cover the assembled source tree.
  [Component owners](OWNERSHIP.json) · [Router guide](route/README.md)
- REA slot control adds the `rea.slot.project` request and correlated reply,
  with schema, fixture and control coverage. [Control code](components/vp-direct/control/slot_requests.py)
- The authorship law permits crediting models and their contributions.
  Commit messages and changelog fragments still follow the attribution
  wording checks. [Authorship law](LAWS.md#l1-authorship)

## Known limitations

- The Toro line is on standby. Live H1 and VM qualification remain pending.
  The accepted REA slot-control change covers the control contract and
  local verification. Live REA runtime verification is pending.
- The public base has inherited privacy scanner findings. The candidate
  release review tracks worktree exceptions by file and line, and checks
  reachable history separately. [Privacy scanner exceptions](docs/PUBLIC_PRIVACY_ALLOWLIST.md)
- Scheduled Volume failed on the public base. The property job failed at
  its probe, the end-to-end job failed at dependency installation, and the
  mutant jobs were cancelled [CITED: hosted Volume]. [Hosted Volume run](https://github.com/oguirado/ViewPoint/actions/runs/45159260510)

See the [changelog](CHANGELOG.md) for the assembled changes.

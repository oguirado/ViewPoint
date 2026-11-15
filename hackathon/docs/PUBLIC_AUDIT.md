# Public release audit

Privacy audit of this repository before its first push, run on 13 Nov 2026 at HEAD `23ff69e`. The checks scan the working tree and every blob in local history. The gate is `tools/check_public.sh`. It must exit 0 before any push.

## Verdict

**Rerun after the fixes (13 Nov, fresh single-commit history): `check_public: 0 finding(s)`, rc 0, with `--allow PERSONAL:README.md:44` (public upstream URL). Ready to push once the operator confirms the author address.** First run, kept for the record: The checker found no secrets and no copied private source. The open findings are personal data and host paths: **32 findings in all, 29 PATH and 3 PERSONAL**. Three of the launcher files also stay in history even if they are deleted, so the first push needs a clean history (see "How to publish").

## What the gate checks

| Rule | Fails on |
|---|---|
| SECRET | Private key blocks; Google, AWS, GitHub, provider `sk-`, Slack and Google OAuth tokens; JWTs; any `key`, `secret`, `token` or `password` assignment of 20+ characters with entropy of at least 3.6 that is not an obvious placeholder |
| PRIVATE | Any 5 consecutive significant lines that also appear in a private checkout passed with `--private-root`. Windows that also appear in an already-public checkout (`--public-root`) are subtracted. Also any `.go`, `.rs`, `.vue`, `.ts` or `.tsx` file |
| PERSONAL | Emails outside placeholder domains (`example.*`, `.invalid`, `.test`, GitHub noreply, `gserviceaccount.com`); maintainer names, handles and host name, which the script stores only as SHA-256 so it never publishes them; git author and committer emails |
| PATH | Home directories other than placeholders (`example`, `user`, `you`, `me`); SSH, AWS, GnuPG, Docker, kube, netrc and gcloud stores; cloud credential files; browser cookie and login stores; the build machine's runtime directories |

Matched values are masked in the output. The exit code is 0 when clean, 1 with findings, and 2 on a usage error.

The run used these roots:

```
tools/check_public.sh \
  --private-root "ViewPoint-app=<ViewPoint checkout>!hackathon/" \
  --private-root "OpenSuperficies=<checkout>" \
  --private-root "vision-engine=<checkout>" \
  --private-root "wender-agent=<checkout>" \
  --public-root "<MetaViewer checkout>"
```

- The run took about 11 s and covered 4 private roots, about 9,700 tracked files.
- The ViewPoint `hackathon/` subtree is excluded, because it is the source of what this repository publishes on purpose.

### The gate fails when it should

Each control was injected into a throwaway clone. Each one was reported, and none was missed.

| Injected | Reported as |
|---|---|
| Google-style API key | SECRET |
| Random 32-character `api_key` | SECRET (high entropy) |
| Email at a real-looking domain | PERSONAL |
| Home path of another user | PATH |
| 40 lines from an OpenSuperficies test | PRIVATE, 6 windows |
| 40 lines from a wender-agent test | PRIVATE, 7 windows |
| 40 lines from a vision-engine eval | PRIVATE, 5 windows |
| 40 lines from a ViewPoint-only file | PRIVATE, 3 windows |
| `mcp/vp_mcp` checked against its own origin without the `!hackathon/` exclusion | PRIVATE on every README window; with the exclusion, 0 |

## Results by rule

| Rule | Findings | Notes |
|---|---|---|
| SECRET | 0 | Gemini keys are read from the environment or from Secret Manager; no value is committed |
| PRIVATE | 0 | No window matches the four private roots. No private-language source files. No code copied from MetaViewer either: 0 windows match when MetaViewer is used as a private root |
| PERSONAL | 3 | Listed below |
| PATH | 29 | 22 of them are in `launcher/` |

## What must change

| # | Where | Finding | Change |
|---|---|---|---|
| 1 | git metadata (all 3 commits) | Author and committer email is a personal address | Before the first push, commit as a GitHub noreply address and publish a fresh history (see "How to publish") |
| 2 | `README.md:44` | The MetaViewer link contains the maintainer's GitHub handle | **Operator decision.** It is the public URL of the AGPL upstream, and the push itself shows the same handle. If it stays, run the gate with `--allow PERSONAL:README.md:44` |
| 3 | `history:README.md@85a692c44a:44` | The same link, in the earlier README blob | Gone once the history is fresh; otherwise allow it as in #2 |
| 4 | `gateway/README.md:39` | `cd` into an absolute path under the maintainer's home | Use a path relative to the repository: `cd gateway` |
| 5 | `mcp/vp_mcp/README.md:31` | `"repo"` example is a host checkout path | Use a placeholder, for example `"/path/to/metaviewer"` |
| 6 | `mcp/vp_mcp/README.md:33` | `"attach_env"` example under the build machine's runtime directory | Use a placeholder, for example `"$XDG_RUNTIME_DIR/metaviewer/attach/current.env"` |
| 7 | `mcp/vp_mcp/README.md:100` | MCP client config points at a host path to `vp_mcp.py` | Use `"<repo>/mcp/vp_mcp/vp_mcp.py"` |
| 8 | `mcp/vp_mcp/README.md:119` | `cd` into a host work directory | Use `cd mcp/vp_mcp` |
| 9 | `mcp/vp_mcp/tests/test_vp_mcp.py:1348` (2 hits) | The refusal fixture is a `file://` URL to a real-looking home and SSH key path | Any `file://` URL proves the refusal. Use `file:///etc/hostname`. Rerun the tests |
| 10 | `launcher/vp_arm64_env_v3.sh:17,19` | Host checkout path; the location of the local identity registry | Remove the file (see the recommendation below) |
| 11 | `launcher/vp_demo_v1.sh:8,10,11,12,46,57` | Host checkout, candidate and runtime paths | Remove the file |
| 12 | `launcher/vp_demo_take.sh:11,12,13,15,22,27,40,51,73,74,76,83,90,92` | Host paths, runtime directory, local identity store location | Remove the file |

## Should `launcher/` stay?

**Recommendation: no. Remove the three scripts and keep only `launcher/README.md` as a description.**

- **They cannot run outside the build machine.** They drive the private ViewPoint fork, the private capsule binaries and a local candidate package. A reader gains no way to reproduce anything.
- **They expose the build machine.** That means its home layout, its private repository layout (candidate and worktree names), the location of the local identity registry and identity store, and the names of private binaries. These account for 22 of the 29 PATH findings.
- **Deleting them in a new commit is not enough.** Commit `41491b0` still holds them, and a push publishes the whole history. The fresh history below fixes that.
- **`launcher/README.md` should not depend on the scripts.** It should describe the take flow (`start`, `view`, `show`, `hide`, `stop`) and the safety rules (manual logins, no credential reads, PID-checked stops). The current text does not trip the gate.

## License compatibility

The operator chose the PolyForm Strict License 1.0.0 for this repository. This section records why that is compatible.

- **No MetaViewer code is copied here.** 0 windows match when MetaViewer is passed as a private root.
- **vp_mcp uses MetaViewer as a separate program.** It invokes MetaViewer's `mvctl` with `subprocess` and exchanges JSON over the command line. It never imports MetaViewer modules.
- **PolyForm Strict here is compatible with MetaViewer's AGPL-3.0-only.** No MetaViewer code is copied and vp_mcp calls it as a separate program, so MetaViewer's obligations stay with MetaViewer. The maintainer is also the sole copyright holder of both.
- **If MetaViewer code is ever copied into this repository**, for example a schema or `mvctl` itself, those files must carry AGPL-3.0-only. 
- **The gateway has no third-party runtime dependencies** (standard library only, per `gateway/requirements.txt`), so it adds no license obligations.
- **`LICENSE` holds PolyForm Strict 1.0.0.** Its Required Notice line names the maintainer's public GitHub handle, like `README.md:44`.

## The tests run standalone

The tests ran in a fresh clone of HEAD `23ff69e` with an empty `HOME`, a minimal `PATH`, and no access to any other checkout:

| Suite | Command (from the directory shown) | Result |
|---|---|---|
| gateway | `python3 -m unittest discover -s tests` (in `gateway/`) | 107 tests OK, 9.8 s |
| vp_mcp | `python3 tests/test_vp_mcp.py` (in `mcp/vp_mcp/`) | 91 tests OK |
| vp_mcp identity | `python3 tests/test_vp_mcp_identity.py` | 10 tests OK |
| vp_mcp negative controls | `python3 tests/test_negctl_vpmcp.py` | 15 tests OK |

Note: `discover -t .` from `gateway/` fails because `tests/` has no `__init__.py`. Use the command above.

## How to publish

1. Apply changes 4 to 9, remove the `launcher/*.sh` scripts, and add `LICENSE`.
2. Create a fresh history, so that no earlier blob or personal email is reachable:
   ```
   git checkout --orphan public
   git add -A
   git -c user.name="<name>" -c user.email="<id>+<handle>@users.noreply.github.com" commit -m "ViewPoint hackathon components"
   git branch -D main && git branch -m main
   ```
3. Run `tools/check_public.sh` with the same roots as above, plus any `--allow` the operator approves for #2. It must exit 0.
4. Rerun the four test suites.
5. Push only after both steps pass.

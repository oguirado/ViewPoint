# V0: how the hosts load skills [MEASURED 2026-11-02]

Skill name: vision (formerly vis). Install from the retained checkout with `VISION_INSTALL_OK=1 ./install.sh --apply`; preview with `./install.sh --dry-run` (no writes). See [README](../README.md) for compatibility names and installation guards.

Method: throwaway skills with marker descriptions under a temp HOME and a temp project. The model listed what it had. No install. Evidence is the model's self-report on one run per case: treat it as strong, not absolute.

| Question | Codex 0.162.1 | Claude Code |
|---|---|---|
| User location | `~/.agents/skills` and `$CODEX_HOME/skills` both load | `~/.claude/skills` loads (newton-routing listed) |
| Project location | `<cwd>/.agents/skills` loads | `<cwd>/.claude/skills` loads |
| Symlinked skill directory | Loads (in `.agents/skills`) | Loads (in project `.claude/skills`) |
| Duplicate name in both Codex locations | BOTH are listed, neither wins | n/a |
| Headless `-p` / `exec` | Loads | Loads |
| Worker flags | `--ignore-user-config --ignore-rules -c project_doc_max_bytes=0` (scr-obrero-sol): STILL loads user and project skills | `--setting-sources "" --strict-mcp-config` (scr-obrero-claude): does NOT load any |

Consequences:
1. Install in exactly ONE Codex location: `~/.agents/skills/vision`. A second copy would show twice.
2. Claude workers launched with empty setting sources never see the skill, so the worker block (`skill/references/worker_block.md`) is mandatory there.
3. Muse (`muse exec`) and OpenCode (`oc-*`): AUSENTE, not tested. Use the worker block.
4. Generated copies, not symlinks: the generated line holds the absolute `vision` path and the source sha. Symlinks would also work.

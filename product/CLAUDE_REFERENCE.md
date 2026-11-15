# Claude reference stack (branch `claude-reference`)

This branch is `main` plus `product/`: the reference stack written by Claude
(`claude-opus-5-5`, `claude-fable-5-2`, via Claude Code) during and after the hackathon,
used as comparison material for the Gemini-written swarm (`hackathon/swarm/`, Track H):
the `ref_*` units, the integrated Blender demo (`ref_demo/run_demo.sh`), the `vp`
operations CLI and its MCP server (`agent_interface/`).

None of it is Gemini work, and ViewPoint on `main` does not need it to run. The comparison
report is `hackathon/product/comparison/H_VS_REF_REPORT.md` on `main`. Licence:
`AGPL-3.0-only`. Setup and test commands for this stack are in the `INSTALL.md` of branch
`assembled`.

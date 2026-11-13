# ViewPoint

ViewPoint lets an AI run a browser capsule the operator owns, watches, and can take over — and no agent action counts until the core has admitted it and verified it against a fresh frame.

## Problem

AI agents that drive a browser are hard to trust. They act without a named target, without visible ownership, and without a checkable outcome. When a human cannot see what the agent saw, who owns the keyboard, and whether the action actually worked, the agent is a liability, not a tool.

ViewPoint starts from one rule: **explicit admission between an agent's proposed input and its native executor.**

## What ViewPoint does

- Each agent gets an **isolated browser capsule** on a **private display**. The operator keeps the desktop keyboard.
- The operator **watches through a viewer** and can **take over any capsule** in human mode, type, and hand it back.
- Every agent action runs an **observe -> propose -> admit -> execute -> verify** loop:
  1. **Observe** the surface and capture a frame.
  2. **Propose** an action bound to that frame (`sha256` + `generation`).
  3. **Admit** — the core alone decides admission.
  4. **Execute** natively through the adapter.
  5. **Verify** against a fresh frame; the receipt is required before the next step.
- **Failures are typed, never silently counted as success**: `INPUT_UNCERTAIN`, `STALE_FRAME`, `VERIFICATION_FAILED`. Navigation stops on any refusal — there is no blind replay.

Demo identities (operator-chosen names): `arxiv`, `chatgpt`, `gemini`, `wikipedia`. Logins are manual by the operator; nothing reads credentials. Gemini login status is AUSENTE — if Google refuses the browser, there is no bypass.

## Where Gemini fits

Gemini is the **planner in the Cloud Run gateway** — it turns a recorded task into a short plan with a stated rationale, shown next to the recorded actions. This is **[TARGET] until a deploy receipt exists**: no real Gemini call receipt yet, the key is not configured, and the model is not configured. Today's verified takes use the bounded browser runner instead. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## What is in this repo vs private

In this repo:

| Path | What it is |
|---|---|
| `gateway/` | Cloud Run gateway: replay page + server-side Gemini planner, tests |
| `mcp/vp_mcp/` | viewpoint-agent MCP stdio server: navigate, bounded browser tasks, read page, identity tools |
| `launcher/` | Host-specific take scripts (`vp_demo_take.sh` and helpers) |
| `docs/IDEA.md`, `docs/TEAM.md` | One-pagers |

Private, deliberately **not** in this repo: the **vision engine**, the **wender-agent** planner, the **OpenSuperficies capsule binaries**, and the **ViewPoint app fork itself**. The public parts above call into them, but they are not here — that is why the launcher and MCP server need a prepared host (see [launcher/README.md](launcher/README.md)).

## Relationship to MetaViewer

ViewPoint is the hackathon build of [MetaViewer](https://github.com/oguirado/MetaViewer) (AGPL-3.0-only), the open base. The UI still shows MetaViewer identifiers. ViewPoint adds the hackathon demo layer on top: the gateway, the MCP server, and the take scripts, backed by private engines listed above.

## Quick start

### Gateway (local run + tests)

```bash
cd gateway
python3 -m unittest discover -s tests -v
python3 app.py --host 127.0.0.1 --port 8080
```

Open `http://127.0.0.1:8080/`. Without `GEMINI_API_KEY` the replay still works and the plan panel shows `GEMINI_KEY_MISSING`. The page never calls `/api/plan` on load — Gemini is called only when a visitor clicks "Generate plan" (no retries). Full deploy notes: [gateway/README.md](gateway/README.md).

### MCP server

The server is `mcp/vp_mcp/vp_mcp.py` (stdlib only, no pip deps). It needs one session file, given by env `VP_MCP_SESSION` (absolute path, validated against `mcp/vp_mcp/session.schema.json`):

```bash
VP_MCP_SESSION=/abs/path/to/session.json python3 -I mcp/vp_mcp/vp_mcp.py
```

Tools: `vp_list_capsules`, `vp_navigate`, `vp_browser_task`, `vp_read_page`, `vp_identity_list`, `vp_identity_create`, `vp_identity_set_label`, `vp_identity_delete`, `vp_show`, `vp_hide`.

Behaviour: `vp_navigate` is `https://`-only; URLs with a non-empty query or fragment are refused before any input because the capsule reports URLs without query by privacy design. `vp_browser_task` runs bounded `mw_run` tasks and is serialised (`agent busy` instead of running concurrently). Identity tools refuse active capsules and wrong confirms. Tests:

```bash
cd mcp/vp_mcp && python3 -B -m unittest discover -s tests -v
```

## Measured results

Only the numbers below are claimed (all [MEASURED] on Fri 13 Nov 2026 evening). See [docs/RESULTS.md](docs/RESULTS.md) for method and the two degradation cliffs.

| Result | Number |
|---|---|
| Navigations verified, 1 capsule, after fixes (CAND9) | **156 / 158** (2 isolated `STALE_FRAME`) |
| 4-capsule demo flow, 5 rounds: CAND9 verified searches | **12 / 15** (HN excluded) |
| 4-capsule demo flow, 5 rounds: CAND7 verified searches | **11 / 15**, session end at round 5 |
| RC1 integrated: arXiv | **5 / 5** |
| RC1 integrated: Wikipedia | **3 / 3** attempted |
| URL with `?` / `#` | refused in **30–54 ms**, zero input |
| Bulk "Pass all to Humans" | **4 / 4** views ready in **51 s** |
| Fresh take (`start`) | 4 named capsules, 2x2, per-capsule preflight with negative control (unknown identity refused) |
| Restart before each take | **~3 min**, state clean; logins persist |

Measured under host load1 15–27 from other projects. Private and link-local addresses are refused or timed out (`169.254.169.254` timed out). Reddit is a reCAPTCHA, not bypassed.

## Honest limits

- **Cloud Run deploy + real Gemini call [TARGET]** — no receipt yet; the gateway is local-only and the plan panel shows `GEMINI_KEY_MISSING`.
- **Final recording, H07/H08 captures, submission [TARGET]** — not done. The shipped replay is a synthetic SAMPLE, labelled as such.
- **Native desktop window [ON HOLD]** — agents work hidden on a private display; the operator watches through a viewer. `show` is disabled until it passes on a private test display. Do not claim it.
- **Human-to-agent return** — the round-trip input bug is fixed in the current candidate (agents -> humans -> agents verified with 4/4 navigations plus 1 task), but we still restart after manual logins as the cheap, known-clean path; seamless return is not claimed as the default flow.
- **Load sensitivity** — round-5 failures under load1 15–27 were slow verifications from external host load, not a product limit. Mitigation: pause other projects while recording, takes of at most 4 rounds, restart per take.
- **One-command take** needs the private components on a prepared GB10 arm64 host — it does not run from a fresh clone.

## License

ViewPoint is licensed under the [PolyForm Strict License 1.0.0](LICENSE): you may read, run and evaluate it for noncommercial purposes; changes and redistribution are not licensed. The base [MetaViewer](https://github.com/oguirado/MetaViewer) keeps its own `AGPL-3.0-only` license. The private engines named above are not part of this repository and are not covered by either license.

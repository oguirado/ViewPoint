TEAM - ViewPoint (Hackathon One-Pager, Plain Text)

ViewPoint is the hackathon build of MetaViewer; the UI still shows MetaViewer identifiers.
Founder: solo founder (solopreneur). AI agent team supporting the solo founder: Claude conductor, Ember, Hydrogen, Sol, Fable.
Dev build on the GB10 host (arm64), candidate CAND2/CAND3 packages; not a public release. Open-network capsules run on image 0.2.1 (dev build) under operator approval b2-0030, pinned by immutable image ID sha256:20efe5e8; private and link-local addresses are refused or timed out (169.254.169.254 timed out) [MEASURED]; Reddit is a reCAPTCHA, not bypassed.

This is a single-founder team. All products below are the founder's own local repositories and documents, built and shipped by the founder alone unless a contributor is later added and named.

WHAT THE FOUNDER HAS SHIPPED AND VERIFIED LOCALLY

- MetaViewer v0.2.6 - Linux desktop wall of application capsules that a person and an AI agent operate under explicit control. License `AGPL-3.0-only`. The package gives agent input a named target, checks the frame the agent saw, and records decisions in a verifiable journal. Architecture: polyglot monorepo - Rust core `metaviewer-core`, Go daemon adapter, Electron + Vue wall, Python agent package. Control loop: see, propose, admit, act, verify; core alone admits and verifies receipts.
- OpenSuperficies - Go browser capsule binaries (srf-*) that run browser surfaces under the MetaViewer adapter. Separate program; MetaViewer calls it through a bounded CLI runner selected by `MV_SRF_BIN_DIR`.
- vision-engine - Python perception package that describes pixels (vcore / vadapt CLI) without owning admission. The agent perception adapter passes RAM-only frames and reads closed JSON replies.
- wender-agent - Pipe-only planner (python -m wender.plan.cli) that proposes plans but owns no admission.

ARM64 TRACK RECORD (GB10 host)

- CP-A00 (12 Nov 2026): arm64 package MetaViewer-linux-arm64.tar (328,376,320 bytes) [MEASURED] built and verified: BUILD OK, VERIFY OK, SMOKE OK; `METAVIEWER_MAIN_SMOKE_OK`, negative controls PASS. TASK NOT RUN at that receipt.
- CP-A01 (12 Nov 2026): non-degraded launch with OpenSuperficies binaries (41 binaries from tree `3cb86d7`): `METAVIEWER_MAIN_READY` and `METAVIEWER_RENDERER_READY`, no degraded marker, clean stop by recorded PID.

MEASURED TODAY (Fri 13 Nov 2026 evening)

- One-command take: ctl/vp_demo_take.sh start = fresh start, 4 named capsules, 2x2, preflight per capsule with a negative control (unknown identity refused) [MEASURED].
- Demo identities (operator-chosen names): arxiv, chatgpt, gemini, wikipedia [MEASURED]. Logins are manual by the operator; nothing reads credentials. Operator may drop ChatGPT from the Google video and use Google sites (deepmind.google) instead [TARGET]. Gemini login status AUSENTE; if Google refuses the browser, no bypass.
- Reliability fixes with tests plus independent review plus live A/B: core proposal-witness cap fixed and journal budget raised. CAND9 156/158 navigations verified on 1 capsule (2 isolated STALE_FRAME) [MEASURED]. 4-capsule demo flow, 5 rounds [MEASURED], fresh: CAND9 12/15 verified searches (HN excluded), CAND7 11/15 with a session end at round 5 [MEASURED]. RC1 integrated: arXiv 5/5, Wikipedia 3/3 attempted, under host load1 15-27 from other projects [MEASURED].
- URLs with ?/# are refused immediately (30-54 ms, zero input) [MEASURED] by privacy design (URLs reported without query).
- Human mode fixed: live view, typing reaches the page [MEASURED]. Bulk "Pass all to Humans" brought 4/4 views ready in 51 s [MEASURED]. A human-to-agent round-trip input bug is being fixed (restart after logins meanwhile); seamless return is not claimed. Restart before each take costs about 3 min [MEASURED], keeps state clean; logins persist.
- Recording and screenshot buttons per capsule are present in the UI. MCP identity tools (list, create, set_label, delete with confirm) work, with refusals for the active capsule and wrong confirm [MEASURED]. MCP navigation and bounded browser tasks exist (vp_navigate, vp_browser_task, vp_read_page).
- Agents work hidden on a private display; the operator watches through a viewer. Native desktop window is ON HOLD and is not claimed. Submission hard stop Sat 14 Nov 10:30 PT = 19:30 CET [MEASURED].

WHAT IS IN PROGRESS [TARGET]

- ViewPoint hackathon demo: Gemini planning call on the hosted page is [TARGET] (HOLD until a real Gemini call receipt exists; no real Gemini call receipt yet, key not configured). Today's verified takes use the bounded browser runner (rc 0, click, type, Enter all verified_move, evidence h05_agent_*). Local gateway dry-run only. Not yet deployed, not yet recorded. Latency, cost and traction numbers are NOT claimed here until measured and receipted. End-to-end configuration via MCP from Claude Desktop, Codex, Antigravity 2, or AI CLIs such as Claude Code is [TARGET]. Roadmap after the hackathon: native desktop window, Wayland support, per-capsule resolution [TARGET].

NOT CLAIMED

- No invented traction, users, revenue, market size or funding history.
- No additional human team members. Solo founder only, supported by the AI agent team named above (Claude conductor, Ember, Hydrogen, Sol, Fable).
- Network isolation not claimed as demonstrated beyond the refusal scope above.
- Full daily acceptance matrix cells not yet observed remain AUSENTE.

CONTACT - to be filled by the operator before submission (name, email, GitHub, AI Studio link, Cloud Run URL). The operator confirms identity and submission details; this one-pager does not invent them.

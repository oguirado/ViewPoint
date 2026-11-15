IDEA - ViewPoint (Hackathon One-Pager, Plain Text)

ViewPoint is the hackathon build of MetaViewer; the UI still shows MetaViewer identifiers.

PROBLEM

AI agents that drive a browser are hard to trust: they act without a named target, without visible ownership, and without a checkable outcome. When a human cannot see what the agent saw, who owns the keyboard, and whether the action actually worked, the agent is a liability, not a tool. ViewPoint starts from a simple rule: explicit admission between an agent's proposed input and its native executor.

SOLUTION

ViewPoint gives an AI its own browser capsule while the operator keeps the desktop keyboard. On the GB10 host the operator opens a capsule, signs in manually, and hands over control. Every action is recorded in a verifiable journal, and failures are typed (INPUT_UNCERTAIN, STALE_FRAME, VERIFICATION_FAILED), never silently counted as success. Demo identities are arxiv, chatgpt, gemini, wikipedia [MEASURED] (operator-chosen names). Submission hard stop is Sat 14 Nov 10:30 PT = 19:30 CET [MEASURED].

HOW IT WORKS

See, propose, admit, act, verify. The agent observes the surface, proposes an action tied to that observation, and waits for the core's admission and verified outcome before proceeding. The operator can reclaim control at any time. Agents work hidden on a private display; the operator watches through a viewer. Opening ViewPoint as a native window on the desktop is ON HOLD and is not claimed here.

WHAT IS MEASURED TODAY

Only the following numbers are claimed, all [MEASURED] on Fri 13 Nov 2026 evening:

- One-command take: ctl/vp_demo_take.sh start = fresh start, 4 named capsules [MEASURED], 2x2 grid [MEASURED], preflight per capsule with a negative control (unknown identity refused) [MEASURED].
- Reliability: after the core proposal-witness cap fix and the raised journal budget, CAND9 reached 156/158 navigations verified on 1 capsule [MEASURED] (2 isolated STALE_FRAME) [MEASURED]. The 4-capsule demo flow over 5 rounds [MEASURED] scored CAND9 12/15 verified searches (HN excluded) [MEASURED] and CAND7 11/15 with a session end at round 5 [MEASURED]. RC1 integrated run: arXiv 5/5, Wikipedia 3/3 attempted, under host load1 15-27 from other projects [MEASURED].
- URLs with ?/# are refused immediately (30-54 ms, zero input) [MEASURED] because the capsule reports URLs without query by privacy design.
- Human takeover: operator takes over a capsule with a live view and typing reaches the page [MEASURED]. Bulk "Pass all to Humans" brought 4/4 views ready in 51 s [MEASURED]. A human-to-agent round-trip input bug is still being fixed; seamless return is not claimed. Recording and screenshot buttons per capsule are present in the UI.
- MCP identity tools (list, create, set_label, delete with confirm) work, with refusals for the active capsule and wrong confirm [MEASURED]. MCP navigation and bounded browser tasks exist (vp_navigate, vp_browser_task, vp_read_page).
- Restart before each take costs about 3 min [MEASURED] and keeps state clean; logins persist in the store.

WHAT GEMINI DOES IN THE FLOW [TARGET]

The hosted demo will run in REPLAY mode [TARGET]. Architecture: Cloud Run gateway [TARGET] (HOLD until a real Gemini call receipt exists; no real Gemini call receipt yet, key not configured, model unconfigured) will call the Gemini API for planning only when a visitor clicks "Generate plan" [TARGET]; renders a concise plan, steps, execution state and scrubbed evidence. Credentials stay server-side, outside the recording. GB10 arm64 runs the ViewPoint UI, native adapter, capsule, observation/action/verification, and ownership interlocks. The public page will show the hosted plan beside verified capsule actions and outcomes, honestly labeled live/replay [TARGET]; a new plan never silently authorizes or reattributes an old trace. Frozen demo task [TARGET]: compare official Gemini API grounding documentation and OpenAI web search documentation and collect source links. No exact models or URLs are claimed until recorded. Today's verified takes use the bounded browser runner instead.

SAFETY

Logins are manual by the operator; nothing reads credentials. No captcha bypass: Reddit is a reCAPTCHA, not bypassed; private and link-local addresses are refused or timed out (169.254.169.254 timed out) [MEASURED]. Gemini login status is AUSENTE; if Google refuses the browser, there is no bypass. Operator may drop ChatGPT from the Google video and use Google sites (deepmind.google) instead [TARGET].

ROADMAP AFTER THE HACKATHON [TARGET]

Native desktop window, Wayland support, and per-capsule resolution. End-to-end configuration via MCP from Claude Desktop, Codex, Antigravity 2, or AI CLIs such as Claude Code [TARGET].

WHAT IS VERIFIED VS [TARGET]

Verified: capsule wall with named target, admission and journal; today''s measured runs above. [TARGET]: Cloud Run Gemini gateway and replay page; recorded end-to-end demo; any latency, cost or determinism claim beyond the refusal timing above. Nothing [TARGET] is shipped until its gate passes and a receipt exists.

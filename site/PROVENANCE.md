# Provenance

This site explains ViewPoint, its architecture, queues, automated videos and robot experiments, and shows 3D pieces built by Gemini agents in a live Blender during the Google DeepMind × Berkeley hackathon (14 Nov 2026). This file says who made what, for the content and for the site itself.

## The pieces (the content shown)

| Piece | Built by | What else touched it |
|---|---|---|
| GemDroid (Android × Gemini mascot) | Gemini 4 Hydrogen (xhigh), one agent via Antigravity CLI | Nothing. Unofficial fan concept. |
| MMSEV-AI rover | Gemini 4 Hydrogen (xhigh), one agent via Antigravity CLI | Nothing. |
| Orbital inference node | Gemini 4 Carbon (high), one agent via Antigravity CLI | Nothing. |
| AstroScout | Gemini 4 Hydrogen (high), one agent via Antigravity CLI | Nothing. Warm-up take made at 08:36–08:45 CET, before the hackathon window opened at 08:59. |
| Platform 37 area study | 4 × Gemini 4 Hydrogen (high), 17 sessions | Director (Claude): launched waves, added short notes between waves, injected one wrong edit on purpose, and made the three renders from the agents' final scene. |
| ViewPoint logo in 3D | 4 × Gemini 4 Hydrogen (high), TaskLight microtasks; design sheet by Gemini 4 Hydrogen (high) | Director (Claude): injected one wrong edit on purpose; exported the .glb and made the hero renders and turntable after the take. No geometry changes. |

The agents wrote the scene-building bpy scripts, the vision actions and the camera intents. The director's injected tests and fallback markers are Claude's, as declared above. Full transcripts, stream logs and verification output are kept per take in the ViewPoint workspace (`hackathon/evidence/<take>/`).

Tools used by the agents but written by Claude before the event, and declared as such: the Blender script bridge (`bpy.app.timers` inbox/outbox), the recorder, the read-only viewer, the vision skill, and the multi-agent broker, auction, judge and rewind.

## Every number

Every number on the site lives in `data/pieces.json` with the log it comes from (the `source` field, shown on hover/tap). Known traps that were avoided:

- GemDroid has 46 meshes in the logs, not 47. "121,744 polygons" appears in no log and is not used.
- AstroScout: 22,672 are triangles; the polygon count is 10,984.
- Renders and .glb exports run in background Blender processes and are not visible in the screen recordings.

## This site

| Part | Written by | How |
|---|---|---|
| Page structure, CSS, JavaScript, SVG diagram, data files, image conversion | Claude (Opus 5.5) | Directly, in a Claude Code session. |
| Copy of "How the Blender engine works" and "Four agents, one Blender" (`data/copy.json`) | Gemini 4 Hydrogen (high) | Swarm wave `w1_copy`, from a context pack with the verified facts. Claude edited for accuracy: removed "turntables" from the single-agent offline step, rewrote step 1, said "auction events" instead of "auctions", and removed "Cycles" from the director's renders, which the logs do not state. |
| Piece blurbs and labels (`data/pieces.json`) | Claude (Opus 5.5) | From the take transcripts and TAKE_README files. |
| Engineering sections: What, Architecture, Queues, Video, Robots (`data/sections.json`) | Gemini 4 Hydrogen (high) | Swarm waves `w3_arch`, `w3_queues`, `w3_robot`, from the repository docs, the turn-queue spec, the material index and the take notes. Each section keeps a `claims` list with source quotes. Claude edited the titles, ledes and the vision and rewind blocks, and removed internal markers such as "AUSENTE" and "[TARGET]". |
| Flow diagrams, verdict chips, take-2049 bars, benchmark table, contact (`index.html`) | Claude (Opus 5.5) | Drawn from the repository's architecture diagrams, the turn-queue spec, the take events and the benchmark metrics. |
| Fact audit of the engineering sections | GPT-6.1 Sol (`sol-read`) | Swarm wave `w4_facts2`: 155 claims checked, 21 findings, 19 applied. |
| Clarity, design and accessibility review | Gemini 4 Carbon (`carbon-read`) | Swarm wave `w4_design2`, reading as a DeepMind engineer: 12 findings, most applied. |
| Fact audit against the logs | GPT-6.1 Sol (`sol-read`) | Swarm wave `w2_facts`. See the review notes below. |
| Design and accessibility review | Gemini 4 Carbon (`carbon-read`) | Swarm wave `w2_design`. See the review notes below. |

## Visual-first edit (operator, 15 Nov)

The long prose was taken off the page at the operator's request: the page shows diagrams, numbers and one-line captions, and the deep technical detail lives in the repository. `data/sections.json` still keeps every drafted section with its `claims` list (source quotes). The short summaries, the "Not claimed" chips, the take cards and the 2049 timeline (built line by line from the take's `events.jsonl`) were written by Claude from the same sources.

## Review notes

- **Fact audit, GPT-6.1 Sol (`w2_facts`)**: verdict FIX, 385 claims checked. Applied:
  - the director's injected test (props-agent-90) counts in the verified totals, so the label is now "broker events verified", with a note on the props role;
  - the rover has twelve wheels on six twin-wheel bogies (it was called six-wheeled);
  - the rover renders were made with EEVEE, so the page now says "Cycles or EEVEE";
  - the .glb check differs between takes and is no longer described as one script;
  - AstroScout ran on a 1280×800 display and has no recording;
  - the logo has a clock motif rather than "telling the time";
  - the logo agents' listing of a folder in the CLI configuration is now declared;
  - the director's fallback markers are declared;
  - the orbital node's "7 pivots" are now "7 empties".

  Kept on purpose: the bridge's 0.4 s timer comes from the bridge source code (`return 0.4`), and the 08:59 start comes from the event rules quoted in the operator's playbook. The Cloud Run link is the public deployment.
- **Design and accessibility review, Gemini 4 Carbon (`w2_design`)**: verdict FIX, 13 findings. Applied:
  - the rover's 3D framing and exposure;
  - all four nav links visible at 390 px;
  - the provenance table stacked into cards on mobile;
  - a stacked HTML version of the diagram under 760 px;
  - touch targets of at least 44 px (thumbnails, facts, details);
  - tooltips wider and dismissed with Escape;
  - 3:1 borders on the ghost and close buttons;
  - descriptive alt text that follows the selected render;
  - a status message when the 3D model loads;
  - the hero button that opens the satellite in 3D;
  - AstroScout's square render shown on a blurred backdrop.

  Not applied: a link to the Cloud Run viewer on the multi-agent cards, because that viewer replays other takes.

- **Engineering sections, GPT-6.1 Sol (`w4_facts2`)**: verdict FIX, 155 claims checked, 21 findings. Applied:
  - "every action ends in a receipt or a typed outcome", which now includes uncertain outcomes;
  - only *non-empty* queries and fragments are refused;
  - literal private hosts are refused, while DNS names are left to the egress policy;
  - MicroVM rewind is a developer implementation, with live demonstrations maintainer-reported;
  - the Blender broker runs outside the ViewPoint program lane;
  - take 0146: each cube's first placement missed and was corrected (3 of 6 attempts succeeded, 3 of 3 cubes in);
  - take 0134: six pointing observations, one of them a repeat;
  - sync error is 0.5 s, 1.0 s or 1.5 s depending on the take;
  - the frame cross-check covers selected and random events, not every event;
  - sha256 manifests exist for the named takes only;
  - the HUD exposure in take 0134 is now stated.

  Kept on purpose:
  - the Cloud Run link: the replay viewer is deployed, and it is labelled as a viewer of recorded takes, with no Gemini planning gateway claimed;
  - the contact email, which the operator asked to publish.
- **Engineering sections, Gemini 4 Carbon (`w4_design2`)**: verdict FIX, 12 findings. Applied:
  - the overflowing diagram grid;
  - the order: Video before Robots, and the chips, the 2049 card and the benchmark table above the prose;
  - stronger `--faint` and `--muted` contrast;
  - a stacked benchmark table on mobile;
  - an explicit failure branch in the action loop;
  - the robot captions and alt text;
  - inline `<code>` for identifiers;
  - a focus ring on facts and tooltips placed by side;
  - the "Limits" heading outside the list;
  - an active link in the nav;
  - noscript fallbacks.

  Not done: a wire example of a proposal and receipt, and a latency breakdown per step. Both are listed as wanted by an engineer, but neither appears in the sources.

## Third-party

- 3D viewer: [`<model-viewer>`](https://modelviewer.dev/) 4.1.0 (Apache-2.0), loaded from jsDelivr only when a visitor opens a model.
- Fonts: Bricolage Grotesque, IBM Plex Sans and IBM Plex Mono from Google Fonts (SIL OFL).
- Videos: YouTube, embedded through `youtube-nocookie.com` only after the visitor presses play.
- The Android robot is reproduced or modified from work created and shared by Google and used according to terms described in the Creative Commons 3.0 Attribution License. Android and Gemini are trademarks of Google LLC. This project is not affiliated with or endorsed by Google.

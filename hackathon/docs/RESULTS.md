# Results

All numbers below are [MEASURED] on Fri 13 Nov 2026 evening. Nothing else is claimed.

## Method

- **Fresh take**: `start` = fresh start, 4 named capsules (`arxiv`, `chatgpt`, `gemini`, `wikipedia`), 2x2 grid, preflight per capsule with a **negative control** (unknown identity refused). Restart before each take costs about **3 min** and keeps state clean; logins persist in the store.
- **Rounds**: the 4-capsule demo flow ran 5 rounds, fresh. Verified searches counted only with receipts. Host load1 was **15–27** from other projects during RC1.
- **Negative controls**: unknown identity must be refused; wrong delete-confirm must be refused; active capsule must be refused for delete; tampered frames and path traversal must be rejected (gateway tests). A check that never fires its input is not evidence — every control above was exercised with the bad input present.

## Results

| Result | Number |
|---|---|
| Navigations verified, 1 capsule, after fixes (CAND9) | **156 / 158** (2 isolated `STALE_FRAME`) |
| 4-capsule demo flow, 5 rounds: CAND9 verified searches | **12 / 15** (HN excluded) |
| 4-capsule demo flow, 5 rounds: CAND7 verified searches | **11 / 15**, session end at round 5 |
| RC1 integrated: arXiv | **5 / 5** |
| RC1 integrated: Wikipedia | **3 / 3** attempted |
| URL with `?` / `#` | refused in **30–54 ms**, zero input (`URL_QUERY_UNOBSERVABLE` / `URL_FRAGMENT_UNOBSERVABLE`) |
| Bulk "Pass all to Humans" | **4 / 4** views ready in **51 s** |
| Round-trip Agents -> Humans -> Agents | **4 / 4** agent navigations + **1** task verified after return |
| Private / link-local | refused or timed out (`169.254.169.254` timed out) |
| Reddit | reCAPTCHA, not bypassed |

## The two degradation cliffs, found and fixed

### Cliff 1: core evicts the newest proposal witness after 128 proposals (BH04)

- **Before.** On a own 1-capsule instance (CAND7), navigations ~7 s apart verified for about navs 0–42, then **every** navigation failed from nav 43 on with `VERIFICATION_FAILED "Missing, stale or unmatched native/frame evidence"` in ~1.8–2.9 s; from nav 141 even `list` returned `schema_error`. Run 1 (25 min): **41 / 300**. Run 2 (fresh, 55 navs): **43 / 55**, collapsing again at nav 43 exactly.
- **Arithmetic.** Nav 0 starts on about:blank = 2 proposals; navs 1–42 = 3 each → 2 + 42·3 = **128 proposals**. Nav 43's first proposal is **#129**.
- **Mechanism.** The core keeps a bounded table of proposal witnesses (128 entries). When the table was full, the eviction rule picked the oldest witness *without* a receipt; in steady state that is the proposal that was just made, so from the 129th proposal on every new proposal lost its witness before verification. The broken assumption was "an unsettled witness is an abandoned one"; in steady state it is the live in-flight proposal.
- **Class.** Deterministic counter cliff per core session (all capsules and `mw_run` proposals share it). It **fails closed** — it never produces a false green.
- **Fix.** Evict the oldest **settled** witness first (receipt present), never the id being inserted; fall back to the oldest unsettled other than the new one. Unit test: 200 propose+receipt cycles keep verifying; negative control: current code fails at #129.
- **After.** CAND9: **156 / 158** navigations verified on 1 capsule (2 isolated `STALE_FRAME`) — no collapse at 129, no session end at round 5 in the 4-capsule flow for this cause.

### Cliff 2: journal budget too small for long sessions

- **Before.** The journal budget was sized for short sessions; long multi-round runs exhausted it and the session ended (the CAND7 session end at round 5 in the table above is in this family). With the witness cap also clipping at proposal 129, a 4-capsule flow across rounds degraded long before the demo task was done.
- **Fix.** Raise the journal budget. The budget now covers the full multi-round flow.
- **After.** The same 4-capsule, 5-round flow scores CAND9 **12 / 15** verified searches instead of the CAND7 **11 / 15** with a session end at round 5. RC1 integrated run: arXiv **5 / 5**, Wikipedia **3 / 3**, under host load1 15–27.

## What did not get better, and why it is not a product limit

Round-5 misses under heavy host load (load1 15–27 from other projects) were **slow verifications**, not rejections — the wall-clock budget expired while the host was busy. Mitigation, already in place: pause other projects while recording, takes of at most 4 rounds, restart per take. A human-to-agent round-trip input bug was fixed and verified (4/4 navigations + 1 task after the return); the default flow still restarts after manual logins because it is the known-cheap path (~3 min, state clean), and seamless return is not claimed as the default.

## Not done — [TARGET], not results

Cloud Run deploy + real Gemini call, AI Studio share link, H07/H08 captures, final recording, submission. No latency, cost, or determinism claim beyond the refusal timing above (30–54 ms) until it is measured and receipted.

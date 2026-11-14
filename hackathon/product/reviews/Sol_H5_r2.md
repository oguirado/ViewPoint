# Cross-Family Review: Sol 6.1 (`gpt-6.1-sol`) on `H5_r2` (`swarm/viewer/`)

- **Model**: `gpt-6.1-sol` (`seat=sol`, `wave_id=77367a51d8914d6c`)
- **Pack SHA-256**: `None`

**H5_r2 passes the requested local source/test review.** All four prior `LIVE_PATH_MAJOR` findings are resolved [CITED]. Remaining deployment documentation drift is `BACKLOG`.

All findings below derive exclusively from the supplied inline corpus. Execution results and hashes are supplied receipts [CITED], not independently reproduced measurements.

| Prior finding | Re-verification evidence | Result |
|---|---|---|
| Swarm integrity bypass with external `REPLAY_PINS` | `swarm/viewer/server.py:197–206` gives environment pins strict precedence; `:253–274` checks the embedded swarm digest and validates the paired bundle. `swarm/viewer/export_replay.py:1209–1235` verifies the replay pin before checking its `provenance.note` `swarm_sha256`. Negative cases cover a modified swarm with an updated or deleted manifest at `swarm/viewer/test_viewer.py:1582–1646`. | Resolved |
| Pre-cutoff frames admitted | `swarm/viewer/export_replay.py:768–776` filters unconditionally and rejects an empty selection; `:1408–1413` independently rejects an entirely pre-cutoff source set. Lone-frame, multiple-frame, discovery and exact-boundary contrasts appear at `swarm/viewer/test_viewer.py:1679–1763`. | Resolved |
| Output ancestor symlinks redirect writes | `swarm/viewer/export_replay.py:1258–1341` checks ancestors, descendant directories, destinations, temporary paths and stale-frame targets. Calls at `:1372` and `:2105–2110` precede mutation at `:2113`. External sentinel preservation is asserted at `swarm/viewer/test_viewer.py:1765–1827`. | Resolved for the demonstrated pre-existing symlink cases |
| Missing rendered `GATEWAY_URL` link | `swarm/viewer/server.py:103–132` preserves accepted URL fragments; `:339` exposes the URL. `swarm/viewer/static/index.html:19` supplies the link; `swarm/viewer/static/app.js:70–118` validates and populates it, invoked at `:841`. HTTP and conditional Node probes cover the destination and unsafe-value hiding at `swarm/viewer/test_viewer.py:1829–1914`. | Resolved |

| Check | Evidence and local verdict |
|---|---|
| R1 | **PASS:** strict line termination, UTF-8, object, version, timestamp and verdict checks with byte offsets at `swarm/viewer/export_replay.py:497–631`; malformed-input controls at `swarm/viewer/test_viewer.py:700–784`. |
| R2 | **PASS:** inclusive event cutoff at `swarm/viewer/export_replay.py:1382–1385`; frame cutoff as above. Exact-boundary assertions at `swarm/viewer/test_viewer.py:933–948` distinguish `>=` from `>`. A fresh mutant execution receipt is **AUSENTE — no inventar**. |
| R3 | **PASS:** dynamic text uses `textContent` at `swarm/viewer/static/app.js:57–62`; CSP at `swarm/viewer/server.py:57–66`; safe API encoding at `:92–100`. Controls at `swarm/viewer/test_viewer.py:962–1042`. |
| R4 | **PASS:** external replay pins bind the swarm through the pinned replay; co-tampering refusal at `swarm/viewer/test_viewer.py:492–569`. The container manifest is root-owned and read-only under the runtime user at `swarm/viewer/Dockerfile:13–20`. |
| R5 | **PASS:** replay/live invariants at `swarm/viewer/replay_store.py:194–218`, `swarm/viewer/export_replay.py:927–944`, and `swarm/viewer/static/app.js:679–689`; refusal controls at `swarm/viewer/test_viewer.py:1171–1260`. |
| R6 | **PASS:** vendored fallback at `swarm/viewer/export_replay.py:59–109`; byte-equality assertions at `swarm/viewer/test_viewer.py:580–592`; isolated standalone HTTP test at `:1378–1414`. |
| R7 | **PASS:** export/source counts, compacted `PASS` counts and raw `events.jsonl` SHA-256 disclosure at `swarm/viewer/export_replay.py:1952–1966`; assertions at `swarm/viewer/test_viewer.py:638–674`; receipt display at `swarm/viewer/static/app.js:697–713`. |
| R8 | **PASS:** `VERIFIED_WITH_SPILL` maps to `observed` at `swarm/viewer/export_replay.py:818–828`, with validator enforcement at `:1094–1098`. Amber styling at `swarm/viewer/static/app.js:120–165`; controls at `swarm/viewer/test_viewer.py:270–452`. |
| R9 | **PASS:** source-frame containment at `swarm/viewer/export_replay.py:700–701`; output checks as above; HTTP traversal, symlink and canonical-containment checks at `swarm/viewer/server.py:494–520` and `:582–618`. |

The supplied execution receipt reports **17 tests passing, rc=0, in 2.463 seconds** [CITED], ending `OK`. Conditional Node probes and conditional gateway/source-file comparisons do not individually report whether their prerequisites were present; the receipt therefore supports the suite outcome without proving every optional branch executed.

The original host-path `BACKLOG` is resolved: portable paths appear at `swarm/viewer/README.md:32–55` and `:82`; diagnostic redaction appears at `swarm/viewer/export_replay.py:385–405`. The exact supplied `DEPLOY_PLAN.md:201` command, run within `swarm/viewer/`, is `grep -rIl -e 'AIza' -e 'BEGIN .*PRIVATE KEY' -e '/home/' . || echo CLEAN`; its supplied output is **`CLEAN`** [CITED].

Local prerequisites for §6 Step 6 and §7.2 are present: `Dockerfile`, standalone startup validation, `/healthz`, `REPLAY` HTML, playback controls, agent/turn display, overlay disclosure, gateway fragment forwarding and event-hash display. Live Cloud Run deployment, private-window laptop/phone checks, gateway “Generate plan” success, deployed event-hash comparison and service-account verification remain **[TARGET]** for the human operator. Their absent runtime receipts are expected, not code defects.

**BACKLOG — conflicting deployment example.** `swarm/viewer/README.md:81–90` omits `--service-account` and `GATEWAY_URL`, and uses resource settings differing from `DEPLOY_PLAN.md:203–206`. Following that README example would leave the gateway link hidden and would not explicitly establish the account required by `DEPLOY_PLAN.md:247`. Align the example with the deployment plan.

The supplied `PINS.manifest:1–6` explicitly lists `take1-w1-2agents`, `take2-w2-4agents` and `take3-w2-guards`; all replay and swarm digests match their corresponding entries in the supplied directory SHA-256 receipt [CITED]. This review is scoped to that inline source and receipt corpus.

**Remaining classification: no `BLOCKER`; no `LIVE_PATH_MAJOR`; deployment-documentation `BACKLOG` as identified above.**

**VERDICT: APPROVED**

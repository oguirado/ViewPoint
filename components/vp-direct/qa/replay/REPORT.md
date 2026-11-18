# REP deterministic replay report

**Disposition.** The pinned unit baseline and replay suite passed. The separate transition-publication recovery gate failed with `REP-P4-P7-001`. The owning surface is `components/vp-direct/perception/lifecycle/`. The affected consumer is `components/vp-direct/perception/receipts/`.

The subject was branch `feat/vp-direct-comms` at `29783d5be359c53db880ac2d6cba3a1aacaa9d1f` [MEASURED]. REP initially ran on `03285c4867404b7b52a3ba84e43142607f0bdb25` [MEASURED] and was repeated after the lifecycle and stream integrations. Both revisions include `4da87c99` (P7 publication) and `e77b58a1` (U4c local replay) [MEASURED]. The runner extracts committed source into a private snapshot under `qa/replay/`. The replay Python processes import that snapshot read-only and write synthetic fixture state in private RAM. The frozen `agents/metaviewer_agents/mcp_server.py` and `contracts/v0.2/mv_tool_call.schema.json` matched their supplied pins [MEASURED].

The earlier R-INT2 recovery and sidecar receipts are `sh-9qz51q66.log`, `sh-dlw5isjz.log` and `sh-sg3vvudq.log` [CITED]. R-INT5 records the initial broker replay failure in `sh-0swe1mtd.log` and subsequent successful broker and handoff runs in `sh-fuwgq59n.log` and `sh-21vlhyq5.log` [CITED]. R-INT6's `sh-ih1xnfa8.log` established P7 refusal before publication [CITED]. Those names refer to `<spool>/`. The REP finding concerns recovery at the publication boundary added by `4da87c99`.

## Commands and results

Run from the repository root. Each test process runs in a capped scope with `MemoryHigh=6G` and `MemoryMax=8G` and inherits `taskset -c 16-19` [MEASURED]. The replay suite checks effective affinity. The findings entry point returns failure when it reproduces a product defect.

```sh
systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -- taskset -c 16-19 sh components/vp-direct/qa/replay/tests/safe_run.sh baseline 29783d5be359c53db880ac2d6cba3a1aacaa9d1f
sh components/vp-direct/qa/replay/tests/safe_run.sh suite 29783d5be359c53db880ac2d6cba3a1aacaa9d1f
sh components/vp-direct/qa/replay/tests/safe_run.sh findings 29783d5be359c53db880ac2d6cba3a1aacaa9d1f
```

| Run | Result [MEASURED] | Local Newton spool receipt |
|---|---|---|
| Committed unit wrappers | `rc=0`, 243 passed, 0 failed, 0 skipped | `<spool>/sh-3ae61y_s.log` |
| REP suite | `rc=0`, 14 passed, 0 failed, 0 skipped. All 7 executed mutants were killed. | `<spool>/sh-tovsv2b6.log` |
| Publication probe | `rc=1`, 2 control schedules passed, 1 failing seed. All observations matched the initial revision. | `<spool>/sh-v3neojqu.log` |

The same commands with revision `03285c4867404b7b52a3ba84e43142607f0bdb25` produced baseline `rc=0`, 240 passed (`sh-fjzle78m.log`), suite `rc=0`, 14 passed (`sh-5dw1sw52.log`), and the same publication failure with `rc=1` (`sh-wn_kdgdt.log`, byte-identical to `sh-1q_obske.log`) [MEASURED].

Each baseline command was `taskset -c 16-19 sh components/vp-direct/<surface>/tests/safe_run.sh` in the pinned snapshot. Counts and return codes are [MEASURED].

| Surface | Passed | rc |
|---|---:|---:|
| `perception/demand` | 17 | 0 |
| `perception/stream` | 17 | 0 |
| `perception/lifecycle` | 34 | 0 |
| `perception/receipts` | 16 | 0 |
| `bus/broker` | 30 | 0 |
| `bus/journal` | 21 | 0 |
| `rewind/machine` | 41 | 0 |
| `rewind/blender_rw1` | 23 | 0 |
| `rewind/microvm` | 16 | 0 |
| `loop` | 28 | 0 |

## Replay gates

| Invariant and negative contrast | Named test | Verdict |
|---|---|---|
| Work published during drain and after sleep survives owner replacement. Agent-first and external-first wakes share a single producer acquisition. Duplicate publication and settlement while borrowed are refused. Delivery is independently read back and settled once. Durable counters, retained charges, released charges and canonical transition receipts agree. | `test_publish_drain_wake_replays_counters_charges_and_delivery` | PASS |
| No new `ACTIVE` transition, ledger change, or receipt without published external work after reopen. The phantom wake mutant reaches `ACTIVE` and fails the same oracle. | `test_absent_external_work_after_reopen_and_phantom_wake_mutant` | PASS |
| Empty sleep spends its attempt. Suppressing the debit reaches the `NON_DESCENDING` gate before broker publication. | `test_empty_sleep_charge_mutant_is_rejected_by_durable_progress_gate` | PASS |
| An old producer witness cannot settle a new wake. The fault substitutes the old witness after acquisition of a fresh one. The wake gives `STALE_FRAME` and the broker returns to `ASLEEP`. | `test_wake_refuses_replayed_producer_witness_before_active` | PASS |
| A retired observation cannot authorize an effect. Substituting the current ID at admission lets the fixture effect through and kills the mutant. | `test_retired_observation_negative_and_admission_mutant` | PASS |
| CDP, X11 and AT-SPI aliases share target exclusion. Disabling canonical identity permits two acquisitions and fails the guard. | `test_alias_double_acquisition_negative_and_mutant` | PASS |
| A process exit after dispatch and external send leaves `UNKNOWN`. Duplicate and new requests remain fenced, and the external ledger contains a single effect after reconciliation. | `test_broker_process_death_after_external_send_keeps_request_fenced` | PASS |
| Exit after a durable append and before acknowledgement replays the same reference. A mutated request gives `REQUEST_ID_CONFLICT`. Torn tails give `JOURNAL_TORN` without changing the bytes. | `test_journal_restart_before_ack_and_torn_tail` | PASS |
| The independent line hash includes the final LF. Dropping LF fails reference verification with stored bytes unchanged. | `test_journal_reference_without_lf_mutant` | PASS |
| The original Blender checkpoint reopens. A changed acceptance sidecar with a recomputed digest gives `UNCERTAIN_OUTCOME` before native dispatch and leaves the broker journal unchanged. | `test_checkpoint_substitution_preserves_native_fence_after_reopen` | PASS |
| MicroVM replay refuses substituted checkpoint evidence and an unfenced reseed, with duration unavailable and both authorities fenced. Clean replay renews authority and preserves `UNREVERTED_HOST_NET_JOURNAL`. | `test_microvm_refuses_substituted_checkpoint_and_unfenced_reseed` | PASS |
| Bypassing the MicroVM reseed fence permits new authority and fails the oracle. | `test_microvm_unfenced_reseed_mutant_admits_new_authority` | PASS |
| Loop restart retains its verified prefix and spent counters. A positive effect witness reconciles the pending step. An absent witness gives `HUMAN_NEEDED` and fences subsequent execution. Both preserve the external ledger. | `test_loop_process_restart_preserves_verified_prefix_without_resend` | PASS |
| Resending a possible effect during loop reconciliation changes the external ledger and fails independent readback. | `test_loop_reconciliation_resend_mutant` | PASS |
| A durably recorded sleep must appear in lineage before another transition can be admitted, or the recovered owner must remain fenced. | `probe_findings.py` / `discontinuity` | FAIL before append. PASS with no interruption and with interruption after append. |

## Failing seed `REP-P4-P7-001`

Seed `0` [MEASURED] runs `wake → sleep with a fault before lineage append → replace durable owners → status → wake`. At the fault, the broker had committed `ASLEEP` at sequence `5`, stream generation `1` [MEASURED]. The first divergent lineage record is Lamport `2` [MEASURED]. Its expected destination is `ASLEEP`. Readback found `WAKING → ACTIVE`. The verified `lineage.jsonl` advanced from `[ACTIVE]` to `[ACTIVE, ACTIVE]` while recovered `status` returned `to: ASLEEP, complete: true`. Goal attempts remained `1` [MEASURED]. Both control schedules retained the sleep event. [Failing-seed record](failing-seeds.json)

The transition callback had never appended the sleep record. The lifecycle instance keeps `_pending_publication` in memory and initializes it to `None` for a replacement. `_recover` can accept the completed goal and broker rest records at that boundary. The persisted history alone therefore cannot recover the missing publication or keep the later wake fenced. [Lifecycle owner](../../perception/lifecycle/machine.py) · [Receipt consumer](../../perception/receipts/__init__.py)

## Evidence boundaries

- Fixture-owned Python processes exited at publisher, delivery, broker, journal and replay-loop cut points. The lineage defect uses lifecycle-owner replacement within a single Python process, with the same P3 fixture producer retained. Full lifecycle process-loss qualification is **NOT-DONE**.
- Blender uses an offline broker and backend fixture. MicroVM tests use local phase replay. Live app, guest boot, and production transition-lineage qualification are **NOT-DONE**.
- No new `SCHEMA_GAP` was found. A guarantee of durable P4 publication recovery before the P7 append is **AUSENTE — no inventar** on the tested cut.
- The repository route check printed `ROUTE_OK` before editing REP. At final review, working-tree edits to `docs/VP_DIRECT.md` and `docs/vp-direct/hosts/claude-desktop.md` made the documentation index stale. The pinned REP tests use committed source and fixtures directly.

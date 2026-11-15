# P2/VC process-isolation and pinned-source mutation audit

Task `p2-vc-isolation-audit.a1.chatgpt-f`. This area is a **diagnostic worker**, not a repair of production `vadapt` code, another owner's tests, or external application behavior. All specimens run offline in newly spawned Python interpreters; no X, network, Docker, credentials, application binary, media/log persistence or real transport is used.

## Source evidence and ownership

At `bridge/integration`, inspected as commit `f4e0b4a7457078d520336f012b0f42e7e7a6681d` through the ChatGPT GitHub connector:

- `vadapt/xfb.py`, SHA-256 `786d52b1d40a081b784dce81dbcf581b24f9171400b1ddd5cdd6f2104b320a05`: line 38 defines `XwdError`; lines 41–43 format `code + ' ' + msg`. `validate_frame(buf, width=None, height=None)` (line 70), `rgb24(buf, hd=None)` (line 80), `consistent_read(read_fn, tries=TORN_TRIES, *, max_bytes=MAX_FRAME_BYTES)` (line 114), and `read_screen_fd(fd, *, max_bytes, tries=TORN_TRIES, width=None, height=None, before_read=None)` (line 156) remain unchanged. `rgb24` rejects mismatched cached headers using `E_XWD_GEOMETRY` (line 84).
- `tests/test_jq3_r_r1_j23_mutkill_xfb_ramws_rtring_str_fda281.py`, SHA-256 `75b3899134744331a1f2784f59c8fc0fa11c8936175f97058aa11e7388f0c0f6`: line 80 begins `TestRealAssertions`. Its `test_m_vadapt_xfb_2_real` at lines 100–124 writes `xfb.os.open = mock_open`, which also changes process `os.open` because `xfb.os` is an alias, and its `finally` writes `xfb.os.open = os.open` after `os.open` is already replaced. The correct paired repair, **outside this worker's write grant**, preserves the real original before the patch or uses `patch.object` and restores that saved identity in `finally`.

Known poison test remains **read-only** here: running its original body is a diagnostic, not an authorized edit. Unrelated historical OOM and host/aarch64 gates are infrastructure/`AUSENTE`, never semantic kills. Task uses the current integration source; the inbox's recorded `ad12c0a...` commit is historical metadata, **not** the ref resolved in this attempt.

## Ordered interfaces and input domains

`eval.v2eval.p2_isolation_audit` exports `AST_SITE_SCHEMA='p2.ast_site/v1'` and `MUTATION_TRIAL_SCHEMA='p2.mutation_trial/v1'`.

`AstSite` is an immutable record with ordered fields `schema, module_path, source_sha256, qualified_ast_scope, containing_expression, original_expression, structural_selector, transformation, site_id`. `module_path` is a repository-relative `.py` path; digest is 64 lowercase hexadecimal characters; scope is a qualified AST function/method or `<module>`; selector is a named, field-based AST path, with optional wildcard **only if it resolves uniquely**. Bare numerical/line ordinal anchors are refused. Original expression and its containing expression must both match the parsed *pinned* source. A digest mismatch or zero/multiple matches yields exact code `ANCHOR_STALE`. Mutators change exactly one AST expression and compile-check the result: `OPERATOR_FLIP`, `BOUNDARY_OFF_BY_ONE` (`:+1` or `:-1`), `DROPPED_CHECK`, `SWAPPED_ARGS`. Unsupported syntax/operator mutation yields `E_AUDIT_MUTANT`, not a kill.

`MutationTrial` is an immutable record with ordered fields `schema, campaign_id, site, fixture_sha256, test_ids, seed, control_outcome, mutant_outcome, assertion_identity, observed_effect_digest, elapsed_ns, disposition, equivalence_proof_ref`. Dispositions are `KILLED`, `SURVIVED`, `EQUIVALENT`, `INVALID_MUTANT`, `INFRASTRUCTURE`, `BASELINE_RED`, `ANCHOR_STALE`, `DEFERRED_OWNER`. An `EQUIVALENT` exclusion needs a reviewed current-source proof reference tied to the pinned SHA-256, not an inferred absence of tests. All test IDs are distinct fully-qualified original `unittest.TestCase` method IDs, not empty/skipped suites.

`freeze_campaign(campaign_id, sources, fixture, seed=17, max_per_category=8)` freezes each source's digest and immutable fixture digest before specimen execution; per module/category it counts raw candidates, eligible sites, a deterministic selection of up to 8, and unselected candidates. `reduce_campaign(campaign, receipts)` requires one receipt **per frozen selected site**, rejecting missing/extra/duplicate receipts (`E_AUDIT_RECEIPT`) instead of shrinking denominators. Per-module/category counts are `raw_sites, eligible_sites, selected, not_selected, generated, invalid, executed, killed, survived, equivalent, infrastructure, baseline_red, stale, deferred`. Invariants:

```
selected = generated + stale + deferred
generated = invalid + executed
executed = killed + survived + equivalent + infrastructure + baseline_red
eligible_sites = selected + not_selected
raw_sites >= eligible_sites
```

Gross score is `killed/generated`; semantic score is `killed/(killed+survived)`. A denominator of zero returns `None` = **AUSENTE**, never a fabricated 0 or 100%.

## Isolation and execution boundary

`run_originals(test_ids, *, time_limit_s=60, memory_limit_bytes=2*1024**3)` validates all preconditions before constructing a child process. It uses `multiprocessing.spawn` for a clean namespace, Linux `RLIMIT_AS` for the 2 GiB maximum, and parent `join(timeout)` + `terminate` for the 60-second maximum. Budget values are **design targets, not measured host performance**. Child import/setup/syntax errors, nonzero exits, timeouts and missing receipts are `INFRASTRUCTURE`, not mutation kills. A test is run by the real `unittest.TestSuite.run` including `setUp`, test body, `tearDown` and registered cleanups. The captured result retains method IDs, assertion error frames, ordinary errors, skip, expected failure and unexpected success; `XPASS`, `XFAIL` and `SKIPPED` are never clean pass evidence.

The before/after process snapshot covers `os.open` **identity**, `builtins.open`, `os.environ`, `sys.modules` and selected module dictionaries, import search caches/path, weak reference and finalizer registries, live/locked thread locks, live threads, shared-memory directory entries and installed signal handlers. It takes a snapshot after loading the test module and compares it immediately after complete `TestCase.run`. The bounded child always enters `finally` restoration, including when an exception occurs, restoring the pre-run `os.open` callable and other revertible process globals. Live leaked threads, locks, and SHM cannot safely be force-reaped; they are recorded as `ISOLATION_FAILURE`, then the child exits. An externally created `/dev/shm` entry is only a **candidate snapshot drift**, not reliable owner attribution to this specimen; host SHM ownership proof is **AUSENTE** if no explicit matching owner evidence exists.

`run_orders(ids, seed=17, ...)` compares real original test method outcomes in solo fresh processes with coupled original, reverse and seeded-permutation suite orders. `agreement` requires every original method to pass **and** all solo/coupled process snapshots to remain clean. Import cache and `os.open` leaks are tracked independently from inner assertion correctness.

`execute_mutation_trial(campaign, site, ids, assertion_identity, source=...)` first runs the pristine original assertion in a fresh spawned interpreter; only then does it compile a uniquely anchored mutant and run the same IDs in another interpreter with the mutated target module injected **before** loading test bodies. A semantic `KILLED` requires the positive original pass, exactly the designated assertion method failing with a real `AssertionError` whose traceback contains the original method body, matching executed method IDs, and zero process-state leakage. Other exception types, setup/import errors, timeouts or isolation leaks are `INFRASTRUCTURE`; an original red is `BASELINE_RED` if it is an assertion/skip/xfail/xpass and is never promoted to a kill.

## Accepted controls and refusal codes

The worker's unit suite has scoped `patch.object(os, 'open', ...)` positive controls for ordinary return and for an intentionally raised exception. Negative specimens individually poison `os.open`, the environment, module cache or leave an unreaped thread; all must return `ISOLATION_FAILURE` while their inner test bodies report `PASS`. Weak reference and held-lock snapshot checks use barrier/identity evidence, not sleeps. A real unmodified poison original is read and run as evidence; the separately granted paired repair owns its correction. Unit checks also exercise `XPASS`, `XFAIL`, skip, real body `AssertionError`, runtime error, structural ambiguity, stale hashes, per-category mutation creation, population reconciliation, and proof requirements.

The explicit refusal codes are `E_AUDIT_SCHEMA`, `E_AUDIT_DIGEST`, `E_AUDIT_SOURCE`, `E_AUDIT_FIXTURE`, `E_AUDIT_MUTANT`, `E_AUDIT_TESTS`, `E_AUDIT_BUDGET`, `E_AUDIT_PROOF`, `E_AUDIT_RECEIPT`, `E_AUDIT_RECONCILIATION`, `ANCHOR_STALE`. A crash is `INFRASTRUCTURE`, never evidence of a correct refusal or kill. Invalid outer inputs must cause no child spawn, factory/send or disk effects. Tests use no application media; the only file reads during the real-poison fixture are Python module imports.

## Remaining AUSENTE

- Independent execution in the **actual full** `bridge/integration` checkout on Python 3.12 / aarch64, and authoritative conductor verdict (until CI/host rerun).
- Full source/fixture-specific production mutation score for every eligible site outside the frozen test specimens.
- Reliable ownership attribution for system-wide SHM directory differences (snapshot-only unless the owner is independently bound).
- Any resolution of the separately owned poisoned test, unrelated pinned vision tests, or prior host OOM.

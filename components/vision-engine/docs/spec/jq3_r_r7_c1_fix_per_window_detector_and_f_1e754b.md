# R7 detector/FL fixture repair and G-R7-11 evidence

Task: `jq3-r-r7-c1-fix-per-window-detector-and-f-1e754b`, attempt `a1`.

**Status: exact focused gate passed in the VM.** The repaired gate reports 13 outer tests and 43 clean original-method executions; both named catalogue mutations are killed after passing real assertions. This is collection evidence, not orchestrator acceptance.

## Source binding

The source was read with the GitHub connector, passing `ref=bridge/integration` explicitly. That ref resolved in this session to `5ac4fa8b9e924c4cfa6f440bf481a667621a0d3b`. The task metadata's `observed_source_commit`, `133f41d8df7cc0d173f2e2a1f3118b2686cfef2c`, is historical dispatch metadata, not the current observed source commit. The task card and task JSON were read before the bound sources. The uploaded instruction supplies `PROMPT-ID jq3-r-r7-c1-fix-per-window-detector-and-f-1e754b a1 888ca2ca2d9ebccf`.

The complete pre-edit file inventory below was created and original methods were executed before fixture edits. File digests cover the retrieved original bytes. Method digests cover the original source span, including decorators. Runtime method IDs are exact `unittest` IDs, including the dynamically attached J2 method.

### Requested source evidence

E0 — SHA-256 of `eval/v2x/per_window_detector.py`:

```text
34a526659b19e10d1ad2ccbbd6cc60d6d45261484aa2656f04ff8b8f9127d87e
```

E1 — line 260 of `eval/v2x/per_window_detector.py`, verbatim, with original indentation and without the line terminator:

```python
                            ops = None if ops is None or second is None else ops + second
```

## Concrete defect and repair boundary

Six original methods stopped at the detector's tile-input guard before their intended GM/FL assertion. Their raw integer matrices were not one-dimensional `visioncore.desc.TILE_DTYPE` descriptors covering the actual `Grid` in exact Morton order. Twelve more methods were expected failures because `eval.v2eval.multiwin_chain.MultiWindowChain` is absent. These are recorded baseline failures, not behavioral mutant kills.

Fixture repair uses real `Grid` and `desc.describe_padded(grid.pad(rgb), grid, lazy_xh=True)` output, real `Handle(kind, canonical_id, generation)` and `ChangeResult`, and merged detector injection interfaces. `per_window_detector.py` and `fl_v3.py` are protected and unchanged. Zero generation, epoch, sequence and valid fake-time values remain legal. Fixtures must not add authority, bypass constructors, guess a production class, or replace an actual detector result with a reference-only result.

`MultiWindowChain` remains absent. The permitted consumer fixture calls the real `PerWindowDetector` and existing `eval.v2eval.chain.compose_chain`, returns the exact detector `ChangeResult`, and checks composition against actual detector claims. Explicit fake-time freshness is a structural consumer seam for later owners. It does not establish a production wall-clock settlement oracle: this detector certifies adjacent-frame evidence and has no wall-clock input. Settlement positives establish the initial sample plus three adjacent clean transitions before the original assertions. Missing-evidence, gap, motion, epoch and reset assertions remain. The focused gate separately observes real positive FL, exact-return FLICKER preservation and P2 demotion: canvas seeds (0, 1, 0) yield class 4 and (0, 1, 2) yield class 2, changing only the final RGB evidence.

### Individually demonstrated stale fixtures and pins

| Legacy area | Original fixture or pin | Direct evidence and bounded repair |
|---|---|---|
| J1 interleaved owner test | `uint8` tiles with shapes `(1, 1)` and `(2, 2)` | Original line 339 raised the S2 dtype/grid/Morton error. Build descriptors per RGB/grid; retain interleaved-versus-solo handle, epoch, settlement and claims assertions. |
| J1 owner invariants | Raw `uint8` `(2, 2)` tiles | Original line 395 raised the same error. Build real descriptors; retain handle isolation. |
| J1 owner-record map | Thirteen nonowner schema pins | Each export listed separately below was observed absent. Check the actual `Handle` and `ChangeResult` with required dataclass/frozen checks and existing field-order assertion. All fifteen-record reference-schema assertions remain. |
| J2 accounting | Raw `(4, 4)` byte matrices; initialization compared to a later held frame | Original line 314 failed validation. Unchanged `fl_v3.update_with_ops` measured initialization/held counts 9/14 for one tile, 36/56 for four tiles. Build valid descriptors and compare equivalent temporal phases on independent solo and interleaved detectors, retaining both equality assertions. |
| J2 dynamic owner factory | Conditional expected-failure probe and fallback handle lookup | Owner is present; active conformance errored on malformed tiles. Keep the exact registered ID, use real records and remove conditional absence masking. The inactive branch is inventoried below. |
| J3 detector calls | Raw tile fixtures | Original lines 195 and 218 failed validation. Supply real descriptors, preserve real ABA FLICKER and compare solo versus interleaved production results; all nine original IDs pass. |
| J3/J5 absent class | `MultiWindowChain` imports and constructor guesses | All twelve original expected failures were the recorded `ModuleNotFoundError`. Use real detector/composition with the explicit structural time seam; do not introduce or certify a production `MultiWindowChain`. |
| J4 instantiation | `uint16` tiles, shape `(16, 4, 4)` | Original line 357 failed the tile guard. Replace the malformed fixture; preserve `ChangeResult`, handle and epoch assertions. Original shadow reference assertions remain; the focused gate separately checks production eager hashes, same-luma chroma, owned RGB history and GM/baseline-MOVED priority over FL. |

Each of these owner export probes returned false on the unchanged detector:

| Removed J1 owner-schema pin | `hasattr(per_window_detector, name)` |
|---|---|
| `TargetRecord` | `False` |
| `FrameRef` | `False` |
| `Observation` | `False` |
| `Receipt` | `False` |
| `ActionRequest` | `False` |
| `ActorLease` | `False` |
| `FocusLease` | `False` |
| `StreamSpec` | `False` |
| `StreamToken` | `False` |
| `Ticket` | `False` |
| `PerceptionResult` | `False` |
| `WindowFrames` | `False` |
| `WindowDescription` | `False` |

J1/J4 conditional decorators had no runtime expected-failure effect because the detector was present. Their removal makes absent/broken owners fail normally. An AST multiset comparison found zero original assertion expressions removed from J1/J4; their reference harnesses were not substituted for production results.

## Gate obligations

- **INV-R7-1:** new failing-before-repair regressions must demonstrate valid fixtures reaching the intended production guard before asserting behavior.
- **INV-R7-2:** remove only individually demonstrated stale pins; preserve original assertion objectives and observed outcomes.
- **INV-R7-3:** preserve canonical constructors, identities, exact dtype/grid/Morton validation and authority. Structural seams may consume real evidence, not grant authority.
- **INV-R7-4:** genuine protected production failures remain external blockers with exact assertion and contract; no deletion or decoration may hide them.
- **G-R7-11:** both named catalogue sites require unique qualified AST anchors, identical passing real assertions, failing mutant behavioral assertions and clean teardown. The GM site is nested `PerWindowDetector._step.gm_call` containing `ops + second`; the validation site is `PerWindowDetector.update`'s exact dtype/grid/Morton condition. No file-wide ordinal selection.
- Mutants compile only in fresh private modules in memory with preserved package imports/registration, `sys.dont_write_bytecode` and state restoration in `finally`. Only mutant-behavior `AssertionError` counts as a kill; positive failures, missing/nonunique anchors, constructor/import errors, unrelated exceptions, timeouts, survivors and equivalents stay separate.
- The focused gate must load every repaired original exact ID below with `unittest.TestLoader` and an inner `TestResult`, asserting empty failures, errors, skips, expected failures and unexpected successes. Compare changed methods/helpers against pre-edit inventory and run every method affected by shared helpers. Surrogate fixture assertions do not replace original-method execution.
- Property checks need deterministic seeds and replay/minimal-counterexample metadata. Threads need barriers, bounded joins, propagated exceptions and leak checks. All checks are offline with stdlib, NumPy and RAM frames.

## Pre-edit file inventory

| Original owned file | SHA-256 of exact original bytes | Original methods |
|---|---|---:|
| `tests/test_jq2a_m5_j1_per_window_gm_fl_state_isolation.py` | `4f58087231b12c9415f0fb276393df1d485ab79c0b626a4d690e902df6f7131d` | 9 |
| `tests/test_jq2a_m5_j2_fl_v3_contextvar_op_accounting.py` | `f7e04ee95899422f5bea3d9e5643d3d980fab39d730e2a6c6cf4821d48844a55` | 4 |
| `tests/test_jq2a_m5_j3_interleaved_window_flicker_veto.py` | `e071388333e3165eb92d253dd487651bacf60d098220c0d8cd0ad485a3186b16` | 9 |
| `tests/test_jq2a_m5_j4_def_a2_02_03_04_shadow_guards.py` | `892923ffec2f0258bdc429653a9b1fa94d5df2e5bff320630da8468da6dabd9b` | 6 |
| `tests/test_jq2a_m5_j5_multiwin_chain_settle_oracle.py` | `43792ed700f1dadcc174e5e9ab2a7d0240fb3756135ede7ca2d737f148980c61` | 15 |

Observed baseline: **43 methods: 25 PASS, 6 ERROR, 12 EXPECTED_FAILURE**. No assertion failures, skips or unexpected successes were recorded. Historical expected failures are not acceptable final outcomes.

### Exact original failure messages

`E-TILES` is:

```text
eval.v2x.per_window_detector.DetectorInputError: tiles must match the exact S2 grid, dtype and Morton order
```

It originates in unchanged `eval/v2x/per_window_detector.py:156`. The six original caller lines are in the method tables.

`E-MULTIWIN` is:

```text
ModuleNotFoundError: No module named 'eval.v2eval.multiwin_chain'
```

All twelve affected methods failed at their original imports, listed below, before behavior assertions; their decorators converted the exception to an expected failure.

### Original class and inherited decorator inventory

Every listed class had an empty source decorator list. Every runtime MRO entry had `__unittest_expecting_failure__ == False` and `__unittest_skip__ == False`. Each MRO is the named class, `unittest.case.TestCase`, `builtins.object`. There were no custom inherited test-method sources or class-level expected-failure/skip decorators.

| Exact original test class | Source decorators | MRO expected-failure flags | MRO skip flags |
|---|---|---|---|
| `tests.test_jq2a_m5_j1_per_window_gm_fl_state_isolation.TestPerWindowGMFLStateIsolation` | `[]` | `False / False / False` | `False / False / False` |
| `tests.test_jq2a_m5_j2_fl_v3_contextvar_op_accounting.TestFLV3ContextvarOpAccounting` | `[]` | `False / False / False` | `False / False / False` |
| `tests.test_jq2a_m5_j3_interleaved_window_flicker_veto.TestCanonicalDataclassesContract` | `[]` | `False / False / False` | `False / False / False` |
| `tests.test_jq2a_m5_j3_interleaved_window_flicker_veto.TestContractHarnessInterleavedBlinkVeto` | `[]` | `False / False / False` | `False / False / False` |
| `tests.test_jq2a_m5_j3_interleaved_window_flicker_veto.TestG_M5_3_PerWindowDetector` | `[]` | `False / False / False` | `False / False / False` |
| `tests.test_jq2a_m5_j3_interleaved_window_flicker_veto.TestMultiWindowChain` | `[]` | `False / False / False` | `False / False / False` |
| `tests.test_jq2a_m5_j4_def_a2_02_03_04_shadow_guards.TestOwnerPerWindowDetector` | `[]` | `False / False / False` | `False / False / False` |
| `tests.test_jq2a_m5_j4_def_a2_02_03_04_shadow_guards.TestShadowGuardsContract` | `[]` | `False / False / False` | `False / False / False` |
| `tests.test_jq2a_m5_j5_multiwin_chain_settle_oracle.TestMultiWindowChainSettleOracleOwner` | `[]` | `False / False / False` | `False / False / False` |
| `tests.test_jq2a_m5_j5_multiwin_chain_settle_oracle.TestSettleContractHarness` | `[]` | `False / False / False` | `False / False / False` |

Method decorator codes:

| Code | Exact original source decorator | Observed runtime effect |
|---|---|---|
| D1 | `require_owner_symbol('eval.v2x.per_window_detector', 'PerWindowDetector')` | Inactive expected-failure wrapper; owner present. |
| D2 | `_expected_failure_if_owner_absent('eval.v2x.per_window_detector', 'PerWindowDetector')` | Inactive expected-failure wrapper; owner present. |
| D3 | `_expected_failure_if_owner_absent('eval.v2eval.multiwin_chain', 'MultiWindowChain')` | Method expected-failure flag true. |
| D4 | `conditional_expected_failure('eval.v2x.per_window_detector', 'PerWindowDetector')` | Inactive expected-failure wrapper; owner present. |
| D5 | `expected_failure_if_missing('eval.v2eval.multiwin_chain', 'MultiWindowChain')` | Method expected-failure flag true. |
| none | No decorator on the active original function. | No method expected-failure or skip flag. |

All 43 runtime method skip flags were false. D3/D5 account for all twelve true method expected-failure flags. D1/D2/D4 are recorded despite no effect on observed flags.

### Exact original method IDs, source digests and outcomes

Source spans refer to original source including decorators. Every ID is fully qualified for `unittest.TestLoader.loadTestsFromName`.

#### J1

| Exact original method ID | Original source SHA-256 | Original source lines | Decorator | Pre-edit outcome / original caller line |
|---|---|---|---|---|
| `tests.test_jq2a_m5_j1_per_window_gm_fl_state_isolation.TestPerWindowGMFLStateIsolation.test_canonical_dataclasses_and_field_orders_owner` | `8db46789ae8cd828053df251e7a221c1f5f17b30e7f1ffcbab4119f1aa4c4f07` | 404–435 | D1 | PASS |
| `tests.test_jq2a_m5_j1_per_window_gm_fl_state_isolation.TestPerWindowGMFLStateIsolation.test_dataclass_schemas_and_field_order_reference` | `b7c061af03169f406a8be1ecaff9a6bc1521b205f576f254efe162b0ea797edc` | 529–556 | none | PASS |
| `tests.test_jq2a_m5_j1_per_window_gm_fl_state_isolation.TestPerWindowGMFLStateIsolation.test_frame_ref_readonly_rgb_and_window_frames_sequence` | `7cb5f95c34478042c8244469ead8044f3caadbba898adacc8508c1947f637722` | 567–595 | none | PASS |
| `tests.test_jq2a_m5_j1_per_window_gm_fl_state_isolation.TestPerWindowGMFLStateIsolation.test_gate_g_m5_1_interleaved_histories_equal_solo_histories_owner` | `846680bf6f253ff65b8882ca3efa73375520930d12d66e1abfbd38f240f99e5c` | 319–380 | D1 | ERROR E-TILES, line 339 |
| `tests.test_jq2a_m5_j1_per_window_gm_fl_state_isolation.TestPerWindowGMFLStateIsolation.test_invariants_m5_1_m5_2_m5_3_owner` | `bb293d3ce436a5c7917a9b2dc6f3419d7a2428b4a520156632540e3877202c5e` | 382–402 | D1 | ERROR E-TILES, line 395 |
| `tests.test_jq2a_m5_j1_per_window_gm_fl_state_isolation.TestPerWindowGMFLStateIsolation.test_negative_mutant_shared_state_fails_isolation_harness` | `34b950e2de98e472d52bce2355ca89886e0b06fc1cd44874a5db94aeec498ff5` | 493–527 | none | PASS |
| `tests.test_jq2a_m5_j1_per_window_gm_fl_state_isolation.TestPerWindowGMFLStateIsolation.test_owner_module_exports_and_signatures` | `fc5ef7b0e3453357c3e6a4109ff42c5bba9ef721288d11fd5aaaf772668eef9b` | 301–317 | D1 | PASS |
| `tests.test_jq2a_m5_j1_per_window_gm_fl_state_isolation.TestPerWindowGMFLStateIsolation.test_positive_control_reference_isolation_harness` | `a6a652e72457734e7684e3e2e5c497c780a134759f6b760a1bfcd3d9c91c63d9` | 441–491 | none | PASS |
| `tests.test_jq2a_m5_j1_per_window_gm_fl_state_isolation.TestPerWindowGMFLStateIsolation.test_supported_target_kinds_and_handle_validation` | `9139ef3155e060acf64e8283445eeac6eaeb96b859b15dfa2c42e8b7deb88aaa` | 558–565 | none | PASS |

#### J2

| Exact original method ID | Original source SHA-256 | Original source lines | Decorator | Pre-edit outcome / original caller line |
|---|---|---|---|---|
| `tests.test_jq2a_m5_j2_fl_v3_contextvar_op_accounting.TestFLV3ContextvarOpAccounting.test_negative_mutant_shared_global_fails_isolation` | `ddda84c88c6655c6229b7819fc99ed1064de3f148ccd7462396e741d3e825bd8` | 276–288 | none | PASS |
| `tests.test_jq2a_m5_j2_fl_v3_contextvar_op_accounting.TestFLV3ContextvarOpAccounting.test_positive_control_exception_and_nested_contexts` | `13a540796f51a0cc95175d4ee38115e983733eac4e1423d74e3dc18d653f3ff9` | 266–274 | none | PASS |
| `tests.test_jq2a_m5_j2_fl_v3_contextvar_op_accounting.TestFLV3ContextvarOpAccounting.test_positive_control_interleaved_updates_equal_isolated_totals` | `918fcd88ea0b43e78a09b646a8f6b81ba1322b09847bfe8f1f571fbea41c58a6` | 256–264 | none | PASS |
| `tests.test_jq2a_m5_j2_fl_v3_contextvar_op_accounting.TestFLV3ContextvarOpAccounting.test_production_owner_fl_v3_contextvar_op_accounting` | `ad27baad0f53aef6310f35538ad691b2356823cfa9495a9c49558db6a8384f71` | 298–329 | none | ERROR E-TILES, line 314 |

#### J3

| Exact original method ID | Original source SHA-256 | Original source lines | Decorator | Pre-edit outcome / original caller line |
|---|---|---|---|---|
| `tests.test_jq2a_m5_j3_interleaved_window_flicker_veto.TestCanonicalDataclassesContract.test_proposed_dataclasses_when_owner_present` | `bb365cb4481c570e8b178f7ca8f347cf9a04dcc827275aecc052e6631932d249` | 263–280 | none | PASS |
| `tests.test_jq2a_m5_j3_interleaved_window_flicker_veto.TestCanonicalDataclassesContract.test_target_supported_kinds` | `392b3077f529da9166b966d71dc58daed7212d3884ed9214567c136f6c504c4b` | 259–261 | none | PASS |
| `tests.test_jq2a_m5_j3_interleaved_window_flicker_veto.TestContractHarnessInterleavedBlinkVeto.test_gate_g_m5_3_local_consecutive_blink_survives_sibling_interleaving` | `2d53c16c8fa14b83cae561577eb1bfc23f4c8b968705a7b02a183b4ae55f34c0` | 102–130 | none | PASS |
| `tests.test_jq2a_m5_j3_interleaved_window_flicker_veto.TestContractHarnessInterleavedBlinkVeto.test_negative_mutant_global_frame_sequence_fails_interleaved_veto` | `4fede98fc770e7217e90e199fc440626767fe74c7497f3191cd3e6559f8a2da1` | 132–151 | none | PASS |
| `tests.test_jq2a_m5_j3_interleaved_window_flicker_veto.TestContractHarnessInterleavedBlinkVeto.test_target_epoch_navigation_and_local_gap_resets` | `d1d95f3421fbbb34c65a2341ecc69c227c93a966d99bef2df0cd20f3e3525dfc` | 153–174 | none | PASS |
| `tests.test_jq2a_m5_j3_interleaved_window_flicker_veto.TestG_M5_3_PerWindowDetector.test_per_window_detector_interleaved_blink_veto` | `3e4ac5e66b37ccd4abd0a3fc9e53b245df90455df4e9faf29d37d4505d6c2740` | 180–206 | D2 | ERROR E-TILES, line 195 |
| `tests.test_jq2a_m5_j3_interleaved_window_flicker_veto.TestG_M5_3_PerWindowDetector.test_per_window_detector_reset_window_and_isolation` | `7e5cc18492b8c92485d28f96445d39af9b39e8c4d4df28d92676edfd171dfb27` | 208–219 | D2 | ERROR E-TILES, line 218 |
| `tests.test_jq2a_m5_j3_interleaved_window_flicker_veto.TestMultiWindowChain.test_multi_window_chain_interleaved_observations_and_settle` | `ec88021432ecdf1a93105c4bf465a640c678e1effa9c409c7a63832bca11e7b0` | 225–241 | D3 | EXPECTED_FAILURE E-MULTIWIN, line 228 |
| `tests.test_jq2a_m5_j3_interleaved_window_flicker_veto.TestMultiWindowChain.test_multi_window_chain_missing_time_never_settles` | `5579ad42ea0ed5bc3e436b249ab32abba1567c4f2c2bec897359ec560a22eb28` | 243–253 | D3 | EXPECTED_FAILURE E-MULTIWIN, line 246 |

#### J4

| Exact original method ID | Original source SHA-256 | Original source lines | Decorator | Pre-edit outcome / original caller line |
|---|---|---|---|---|
| `tests.test_jq2a_m5_j4_def_a2_02_03_04_shadow_guards.TestOwnerPerWindowDetector.test_owner_detector_import_and_instantiation` | `47517260fea92c48311bdbd2c6e6635db39af614906e079b3072938abf23a9f3` | 343–360 | D4 | ERROR E-TILES, line 357 |
| `tests.test_jq2a_m5_j4_def_a2_02_03_04_shadow_guards.TestOwnerPerWindowDetector.test_owner_detector_reset_window` | `47e38ee316cf402f0d072868c4e351c5466757ea5128dc134296550cc2149bd9` | 362–371 | D4 | PASS |
| `tests.test_jq2a_m5_j4_def_a2_02_03_04_shadow_guards.TestShadowGuardsContract.test_dataclass_contract_fields` | `79e7bdaa57d4785e046f3a20b0103e0f975a3eb14cdea6c477ff592ba41b1f07` | 322–337 | none | PASS |
| `tests.test_jq2a_m5_j4_def_a2_02_03_04_shadow_guards.TestShadowGuardsContract.test_negative_mutant_lazy_zero_hash_suppression` | `e5cb3d83fda7061ce1ba26326490c777d5b72821c935f02d7b43eaeaa1d45e5a` | 289–309 | none | PASS |
| `tests.test_jq2a_m5_j4_def_a2_02_03_04_shadow_guards.TestShadowGuardsContract.test_negative_mutant_mask_all_motion` | `452848140f291345d88b036e524963f904598d0fdb0fa89bbb1cc646e69a64ce` | 311–320 | none | PASS |
| `tests.test_jq2a_m5_j4_def_a2_02_03_04_shadow_guards.TestShadowGuardsContract.test_positive_control_motion_and_chroma_preserved_under_blink_veto` | `2b5d8c2eb86cde426d149f185806193dbc69f5483d7a1fdfdbb5c710b509c83f` | 262–287 | none | PASS |

#### J5

| Exact original method ID | Original source SHA-256 | Original source lines | Decorator | Pre-edit outcome / original caller line |
|---|---|---|---|---|
| `tests.test_jq2a_m5_j5_multiwin_chain_settle_oracle.TestMultiWindowChainSettleOracleOwner.test_capsule_and_child_screen_canonical_destination` | `66c279e92d77af307bc30b89281ba006beaf792a7cc037b763999e3aeb62d585` | 565–578 | D5 | EXPECTED_FAILURE E-MULTIWIN, line 568 |
| `tests.test_jq2a_m5_j5_multiwin_chain_settle_oracle.TestMultiWindowChainSettleOracleOwner.test_gate_m5_5_missing_evidence_unsettled` | `b74a39472886bbbb1b5916ec05af051370f4b1dac89595eb1041959f950dfa4c` | 418–427 | D5 | EXPECTED_FAILURE E-MULTIWIN, line 421 |
| `tests.test_jq2a_m5_j5_multiwin_chain_settle_oracle.TestMultiWindowChainSettleOracleOwner.test_gate_m5_5_regressing_time_or_none_time_fails_settle` | `4e573f2bb6c8082e296f2aed3d7019abdc749c4b03319423d8d604dd3c5d2690` | 489–504 | D5 | EXPECTED_FAILURE E-MULTIWIN, line 492 |
| `tests.test_jq2a_m5_j5_multiwin_chain_settle_oracle.TestMultiWindowChainSettleOracleOwner.test_gate_m5_5_settle_fails_after_motion` | `8c16cb445173568f90272233a87b13563af0abd7b4a56677668b6863b26ba701` | 446–468 | D5 | EXPECTED_FAILURE E-MULTIWIN, line 449 |
| `tests.test_jq2a_m5_j5_multiwin_chain_settle_oracle.TestMultiWindowChainSettleOracleOwner.test_gate_m5_5_settle_fails_after_sequence_gap` | `6bf85dfbea1b3a7f332e8958123054a671e0075165902def4a6020e5754e469c` | 429–444 | D5 | EXPECTED_FAILURE E-MULTIWIN, line 432 |
| `tests.test_jq2a_m5_j5_multiwin_chain_settle_oracle.TestMultiWindowChainSettleOracleOwner.test_gate_m5_5_settle_fails_after_stale_frame` | `38c872d408db19a407b452a245b3838ac0a9a6dfc0bdc2cd4d9a0072543e2afe` | 470–487 | D5 | EXPECTED_FAILURE E-MULTIWIN, line 473 |
| `tests.test_jq2a_m5_j5_multiwin_chain_settle_oracle.TestMultiWindowChainSettleOracleOwner.test_gate_m5_5_valid_configured_quiet_run_settles` | `3448c09ed00ab94359eeda6d05c0f4f301b4e175c993169aa9806e6ede5345a8` | 401–416 | D5 | EXPECTED_FAILURE E-MULTIWIN, line 404 |
| `tests.test_jq2a_m5_j5_multiwin_chain_settle_oracle.TestMultiWindowChainSettleOracleOwner.test_inv_m5_1_handle_isolation_and_epoch_reset` | `7e25f61794e473ce928211ef46918df2be1813ae47b50de8f4f9f2e6933d3b58` | 523–549 | D5 | EXPECTED_FAILURE E-MULTIWIN, line 526 |
| `tests.test_jq2a_m5_j5_multiwin_chain_settle_oracle.TestMultiWindowChainSettleOracleOwner.test_settled_never_grants_action_permission` | `d50deb7c0700ee4cd878ab351388a2518a042f140998f3dbdd0c71658310deea` | 506–521 | D5 | EXPECTED_FAILURE E-MULTIWIN, line 509 |
| `tests.test_jq2a_m5_j5_multiwin_chain_settle_oracle.TestMultiWindowChainSettleOracleOwner.test_supported_target_kinds` | `fc555ed1773a6282685cb785a743239c8d147b391f1f34780ea3b16723e4a1bc` | 551–563 | D5 | EXPECTED_FAILURE E-MULTIWIN, line 554 |
| `tests.test_jq2a_m5_j5_multiwin_chain_settle_oracle.TestSettleContractHarness.test_negative_mutant_settled_after_gap_rejected` | `9a3ffd72f29363cc20bb1113366038fb391733f24ddb9ef4994d952a4163058a` | 370–375 | none | PASS |
| `tests.test_jq2a_m5_j5_multiwin_chain_settle_oracle.TestSettleContractHarness.test_negative_mutant_settled_on_missing_evidence_rejected` | `8da128ef15fdea93d464aa277f88ecfd58e1e68db43f0ff72bd4659012ce3b2f` | 363–368 | none | PASS |
| `tests.test_jq2a_m5_j5_multiwin_chain_settle_oracle.TestSettleContractHarness.test_negative_mutant_settled_on_motion_rejected` | `a8832c1128c9b3e39ebfd09e46ecdebc4c3f807c7cfd16c3667a25aa0673b5d4` | 377–382 | none | PASS |
| `tests.test_jq2a_m5_j5_multiwin_chain_settle_oracle.TestSettleContractHarness.test_negative_mutant_settled_when_stale_rejected` | `281e7c1079bda82dab2982952a87bbb24b6294fb7de0ff3c0a30393b57ac2e0f` | 384–389 | none | PASS |
| `tests.test_jq2a_m5_j5_multiwin_chain_settle_oracle.TestSettleContractHarness.test_positive_control_valid_quiet_run_settles` | `9178aae406b2fd997f63b43274665cfb0a694193a5e27029b5d292427588ca16` | 358–361 | none | PASS |

### J2 dynamic registration branch

Original registration was `setattr(TestFLV3ContextvarOpAccounting, "test_production_owner_fl_v3_contextvar_op_accounting", _make_owner_test_method(get_owner_symbol()))`. The table's runtime ID resolves to `_make_owner_test_method.<locals>.test_owner_conformance`, digest `ad27baad0f53aef6310f35538ad691b2356823cfa9495a9c49558db6a8384f71`. This owner-present branch was active, had no expected-failure/skip flags, and produced `E-TILES` before either equality assertion.

The inactive alternative `_make_owner_test_method.test_owner_probe` occupied original lines 293–295, had `@unittest.expectedFailure`, and called `self.fail("eval.v2x.per_window_detector.PerWindowDetector absent")`. Its source digest is `813ac160985bad0bdabef30eddae1bf3991e2683acceae820153b6e781f434c0`. It was not independently discovered or executed. The entire original factory digest is `03d45084d947c23093eb85d83bdb1b432d9a7e78f97e953c601d6ec90f1f2e05`. Repair preserves the exact registered ID without conditional absence masking; the inactive branch is not an additional repaired method.

## Verification outcome and limitations

The final source ref was re-resolved before the gate and still identified `5ac4fa8b9e924c4cfa6f440bf481a667621a0d3b`. The exact gate command was executed in the VM with `PYTHONDONTWRITEBYTECODE=1` in its environment:

```sh
python3 -m unittest tests.test_jq3_r_r7_c1_fix_per_window_detector_and_f_1e754b
```

Observed final output (stdout and stderr captured, concatenated):

```text
R7 catalogue M-eval-v2x-per-window-detector-1: positive=PASS mutant=KILLED line=260 replay={"consensus_ops": 11, "estimator_ops": 7, "height": 16, "seed": 730711, "values": [20, 21], "width": 16}
R7 catalogue M-eval-v2x-per-window-detector-2: positive=PASS mutant=KILLED line=154 replay={"height": 16, "invalid_condition": "Morton order only", "morton": [1, 0], "seed": 730711, "width": 32}
R7 mutation teardown: source/module/package/context/bytecode state restored
R7 inner outcomes: {"errors": 0, "expectedFailures": 0, "failures": 0, "run": 43, "skipped": 0, "unexpectedSuccesses": 0}
R7 original-method replay: 43 passed; failures/errors/skips/xfails/xpasses=0
.............
----------------------------------------------------------------------
Ran 13 tests in 0.574s

OK
```

The identical final new-gate source was also run against the original, unrepaired legacy files. Its original-method replay fails on the six real tile errors and exposes the twelve original expected failures:

```text
R7 inner outcomes: {"errors": 6, "expectedFailures": 12, "failures": 0, "run": 43, "skipped": 0, "unexpectedSuccesses": 0}
Ran 13 tests in 0.463s
FAILED (failures=1)
```

The failing gate assertion was `self.assertEqual(getattr(result, field), [], field)` with `field == "errors"`; its first recorded error is the original J1 interleaving method at line 339 raising `DetectorInputError` from production line 156. Thus the new regression fails because original methods do not complete their behavior, not because their source files have not changed. Original method-level error tracebacks and exact locations are preserved above.

### Mutation evidence and outcome separation

| Catalogue | Qualified scope / structural selector | Independent behavioral assertion | Observed contrast |
|---|---|---|---|
| `M-eval-v2x-per-window-detector-1` | `PerWindowDetector._step.gm_call`, unique full `ops` assignment, `Assign.value.orelse.op: Add -> Sub`, line 260 | Estimator charge 7 plus consensus charge 11 must produce 18, with real MOVED class/vector and correct calls | Real PASS; private mutant KILLED (produces -4). |
| `M-eval-v2x-per-window-detector-2` | `PerWindowDetector.update`, unique complete tile-input expression, `If.test.op: Or -> And`, line 154 | Correct dtype/shape with only reordered Morton values must raise the exact input guard before any veto factory instance | Real PASS; private mutant KILLED (reaches a different downstream evidence exception). |

Arrangement and production execution finish in the preparation phase. Behavioral assertion functions only compare observations; even an `AssertionError` during construction or production execution is a setup error, never a kill. Negative harness controls independently verify positive-failure short-circuiting, constructor `ValueError` and `AssertionError`, unrelated runtime exceptions, timeouts, missing and nonunique anchors, import errors, survivors and structurally identical equivalents. Only the two named behavioral failures enter the killed count. No broader mutation score is claimed.

Each contrast compiles an AST in a fresh private module with package registration preserved, then restores registration and bytecode flags in `finally`. The gate checks unchanged source bytes and original module identity, restored package/context state, and no private source or bytecode artifacts. Seeded interleaving uses seed `730711`, records replay parameters and retains the smallest observed failing case if any; both named minimal witnesses print their replay metadata. Context-counter threads use barriers, bounded joins, propagated exceptions and explicit leak detection. No frame files, live binaries, network or model calls occur in tests.

### Original assertion and changed-definition audit

All 43 exact original IDs remain. All original assertion calls remain AST-equivalent, including J2's dynamic-to-direct method mapping; no assertion was removed or changed. The original reference/mutant harness implementations remain separately intact. New fixture checks add 16 assertion calls:

| Legacy module | Original IDs replayed | Original assertions retained | Current assertions |
|---|---:|---:|---:|
| `tests/test_jq2a_m5_j1_per_window_gm_fl_state_isolation.py` | 9 | 31 / 31 | 33 |
| `tests/test_jq2a_m5_j2_fl_v3_contextvar_op_accounting.py` | 4 | 10 / 10 | 14 |
| `tests/test_jq2a_m5_j3_interleaved_window_flicker_veto.py` | 9 | 21 / 21 | 27 |
| `tests/test_jq2a_m5_j4_def_a2_02_03_04_shadow_guards.py` | 6 | 16 / 16 | 16 |
| `tests/test_jq2a_m5_j5_multiwin_chain_settle_oracle.py` | 15 | 28 / 28 | 32 |
| **Total** | **43** | **106 / 106** | **122** |

The gate embeds pre-edit qualified definition digests and exact original method IDs. It computes changed, added and removed method/helper definitions, verifies the current original ID set, and executes the entire corresponding module's original IDs whenever shared helpers are affected. All five modules are replayed, covering 22 changed original methods and every method affected by their helpers. Runtime skip/expected-failure flags are checked across methods and class MROs; the inner result requires empty failures, errors, skips, expected failures and unexpected successes.

Changed fixture/helper boundaries: J1/J4 add `_baseline_tiles` and remove the demonstrated stale owner probes; J2 adds `_baseline` and preserves its registered production ID as a direct method while removing the absence factory; J3 adds `baseline` and `DetectorEvidenceFixture` while removing absence probes; J5 replaces the absent-class/fallback-import fixtures with canonical records, owned RGB/digest observations and `DetectorChainFixture`. Original reference models are not the source of any production detector result.

### Limits

Both protected production files remain byte-identical to the fetched source; their constructors and validation were not patched. No unresolved protected production failure was observed by these scoped checks. Production `MultiWindowChain` and a production wall-clock freshness oracle remain **ABSENT** as anticipated by the task; fake-time checks are structural consumer coverage. Host measurements are **AUSENTE**. All reported test timings are VM measurements. This focused gate is not a full repository test-suite run. Acceptance belongs to the external orchestrator.

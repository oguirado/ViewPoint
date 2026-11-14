# R7 C3 — individually verified FX2 stale-marker repairs

## Defect and repair boundary

The seven owned legacy modules contain 37 original test methods and 24 original
expected-failure markers. Current production APIs make several markers stale;
other fixtures fail before reaching the intended assertion. This change removes
22 markers only after real assertions pass, runs 35 exact original IDs inside
the new gate, and preserves two unresolved original methods byte-for-byte.
No production source or constructor admission rule is changed.

Repairs are limited to valid equal-sum glyph construction, locating the actual
selection helpers, current claim-aware chain seams, explicit mock-FL veto
evidence, supported bounded XWD readers, two-read controls, meaningful fstat/pread
checks, and a faithful offline special-node fixture. FL scenes and chrome/competing
motion assertions remain intact. Chain composition still executes real
run_chain/compose_chain/apply_claims with the original class/vector expectations.

The original symmetric linear-gradient scene is **not** replaced by an easier
positive. Fixing only its 64x64-to-60x60 clipping error exposes None where its
original assertion requires (4,4). That method and its marker remain unchanged.
The blanket underscore-helper documentation scan is corrected to the real
select_features/constellation_candidates API and strengthened with independent
seven-origin, integer-gradient and exact-signature assertions.

## Source and verification

Source: GitHub MCP reads with explicit ref `bridge/integration`, resolved and
rechecked at commit `5ac4fa8b9e924c4cfa6f440bf481a667621a0d3b`. The task metadata's older dispatch
commit is not substituted for the observed current source. All 56 retrieved files
were checked against their Git blob identities; only the seven allowed legacy
test files differ. The gate, this note and the two outbox deliverables are new.

The exact command is:

```text
python3 -m unittest tests.test_jq3_r_r7_c3_unpin_vision_fx2_xpass
..........................................................
----------------------------------------------------------------------
Ran 58 tests in 3.190s

OK
```

Timing is VM, not host. The same gate over pristine original legacy bytes with
the same current production modules reports 58 tests and 24 failures: 22 stale
markers are rejected, the old chain fixture fails, and the old mixed-socket
fixture encounters sandbox EPERM. This establishes a failing-before-repair gate.

The gate checks each original through TestLoader and an inner TestResult,
rejecting failures, errors, skips, expected failures and unexpected successes.
It additionally rejects method/class decorators and inherited failure/skip flags.
Every changed method and every method affected by shared chain/RAM fixture edits
is included. The two blocked originals are excluded from the repaired set and
protected by exact original method hashes.

Fourteen named contrasts each have one qualified AST source anchor, a passing
real assertion, a failing private-mutant behavioral assertion and clean teardown.
Only BehavioralAssertion (an AssertionError subclass from an explicit oracle)
counts; constructor/import errors, failed positives, timeouts, missing/nonunique
anchors, equivalent syntax and survivors remain separate. Compilation is in
memory with package imports/registration preserved; bytecode and context/module
state are restored in finally. No sibling mutation harness is imported.

The required projection mutation changes OPS from the independent expected 7296
to 8064; the returned pair alone cannot detect it. The private _tile_sums contrast
passes 80 seeded integer-oracle cases on real code (seed 730203); the smallest
tested mutant counterexample is mask [[1]], side 1, shape 1x1 versus 1x3. This is
explicitly a private-helper test, with no claim that it lies on the public GM path.

## Preserved blockers and scope limits

1. **BLOCKED — XWD D11-BOUNDS color-table/header-offset budget.** The original
   tests/test_fx2_f8_j1_xwd_header_and_byte_order_parser.py:69-70 refusal expects
   XwdError; parse_header in vadapt/xfb.py:46 accepts the count. A 129-byte local
   fixture changing only ncolors to 16777216 gives pixels_offset 201326697.
   validate_frame still rejects the missing table with E_XWD_SIZE. The original
   method and make_xwd_fixture helper are unchanged. Owner:
   fx2-f8-d11-x11-framebuffer-ram-capture.
2. **BLOCKED — original symmetric-gradient recovery.** The original baseline
   tests/test_fx2_f5_j3_gm_constellation_voter_adversarial.py:129 raises ValueError
   before its assertIsNotNone at :143. Correcting only clipping leaves the original
   linear scene and (4,4) expectation, but the current gm_constellation API at :221
   returns None. The original method/marker are unchanged; a separate tiny diagnostic
   records this gap without claiming a repair. Owner:
   fx2-f5-gm-bands-hv-v21-and-constellation.

Both stale-second-sequence ring assertions now pass individually on the observed
source: RingReader._try already reconfirms the (sequence,index) tuple. Their pins
are removed only after exact original assertions pass; flipping that reconfirmation
fails the independent gate. This proves the Python protocol model, not ARM hardware
acquire/release fences. Ring file allocation is replaced only at the I/O seam by
RAM arrays; constructors, parsing and protocol methods remain real.

Original UNIX socket binds produced PermissionError [Errno 1]. The repaired
special-node fixture uses an inert pathname with socket-mode stat/lstat metadata;
isfile/isdir/islink all remain false, and an unlink-omission mutant fails. Real
open_run, verified tmpfs admission, quotas and release logic remain intact.
No live socket, capture binary, network/model call or frame file is used by the gate.
Empty metadata/sentinel files are cleaned up. No threads are launched. Host timing,
live socket integration and ARM hardware measurements: **AUSENTE**.

## Pre-edit inventory and outcomes

Inventory was captured before any edits. All seven test classes directly inherit
unittest.TestCase; no class-level or inherited failure/skip decorator was present.
`EF` below means the original method had unittest.expectedFailure. Original
source hashes include the decorator through the last body line, with exact bytes.
`XPASS` and `XFAIL` are historical pre-repair outcomes, never accepted gate outcomes.
The fstat baseline ran under an explicitly recorded RAM-only file-I/O seam; the
ring baselines used the same virtual allocation seam as their final exact-ID runs.
The large overflow baseline was isolated and memory-bounded; its actual assertion
was reached and reported XwdError not raised.

### `tests/test_fx2_f5_j2_gm_chrome_clock_and_competing_motions.py`

Original file SHA-256: `7b42b3386d069151c125c1706f0fd949bbbdf8c76870823865139ebf4b6da392`

| Exact original method ID | Original method SHA-256 | Pin | Original | Bare assertion | Final |
|---|---|---|---|---|---|
| `tests.test_fx2_f5_j2_gm_chrome_clock_and_competing_motions.TestChromeClockAndCompetingMotions.test_top_and_bottom_chrome_clock_symmetry` | `7da76d8e2968db5e695e93b94afe00792eee433278e55c4c28b99d2f199164bf` | EF | XPASS | PASS | PASS |
| `tests.test_fx2_f5_j2_gm_chrome_clock_and_competing_motions.TestChromeClockAndCompetingMotions.test_sequential_state_leak_independence` | `525440f4656fe88df6a286e09a67b528bb57412623d23ec494e1b7756201a7dc` | EF | XPASS | PASS | PASS |
| `tests.test_fx2_f5_j2_gm_chrome_clock_and_competing_motions.TestChromeClockAndCompetingMotions.test_clock_span_and_separation_sweep` | `df696fa4bfe88097d3544e6f153fe83c9ebecbab1605c5838bafebbc4a1f17c7` | EF | XPASS | PASS | PASS |
| `tests.test_fx2_f5_j2_gm_chrome_clock_and_competing_motions.TestChromeClockAndCompetingMotions.test_competing_motions_capped_tile_support_vetoes` | `766f7e2b08c62ce6b3b23459a8e12edfb1f21dd16f33638f42e51c8106304962` | none | PASS | — | PASS |

### `tests/test_fx2_f5_j3_gm_constellation_voter_adversarial.py`

Original file SHA-256: `c57ef5caf6665669b1b16176480c5504a4effb76d2c4c4bebf0f52f0a102a5bb`

| Exact original method ID | Original method SHA-256 | Pin | Original | Bare assertion | Final |
|---|---|---|---|---|---|
| `tests.test_fx2_f5_j3_gm_constellation_voter_adversarial.TestFx2F5J3GmConstellationVoterAdversarial.test_equal_sum_impostor_glyphs_no_false_correspondence` | `9597c024b3cedc73323101556f1d72ed7f7f4d7f32c2510961ad1f74d1fe29c2` | EF | XFAIL | ERROR | PASS |
| `tests.test_fx2_f5_j3_gm_constellation_voter_adversarial.TestFx2F5J3GmConstellationVoterAdversarial.test_repeated_identical_glyphs_competing_modes_veto_refusal` | `4e8432b3972b20c7301df7d6535ff95f000f2870b6fba882becbe7de4d71b755` | EF | XPASS | PASS | PASS |
| `tests.test_fx2_f5_j3_gm_constellation_voter_adversarial.TestFx2F5J3GmConstellationVoterAdversarial.test_symmetric_gradient_read_only_and_strided_views` | `5eac4072252a74fb8ec29ecd162ba171bcae144456efb8347ec361c8bcc1a9f3` | EF | XFAIL | ERROR | BLOCKED; unchanged |
| `tests.test_fx2_f5_j3_gm_constellation_voter_adversarial.TestFx2F5J3GmConstellationVoterAdversarial.test_production_ast_no_floats_or_true_division` | `1a72ef9d1cf3945bf90ae395e22221425a4d47ebf975125305e4f1b8567fb6df` | EF | XPASS | PASS | PASS |
| `tests.test_fx2_f5_j3_gm_constellation_voter_adversarial.TestFx2F5J3GmConstellationVoterAdversarial.test_internal_selection_check_located_and_contract_documented` | `838860657b390c1dce24fb373b1c42abee5e0a7c587db849aa96ae9d5a1cf79f` | EF | XFAIL | FAIL | PASS |

### `tests/test_fx2_f6_j1_fl_v3_unit_scenes_s1_s2_s3.py`

Original file SHA-256: `21039c7495f64b25dd7b2937741da64a020bcf344aa9a1c694092c23881021ae`

| Exact original method ID | Original method SHA-256 | Pin | Original | Bare assertion | Final |
|---|---|---|---|---|---|
| `tests.test_fx2_f6_j1_fl_v3_unit_scenes_s1_s2_s3.TestFLV3UnitScenesS1S2S3.test_fixture_preconditions_and_eager_hashes` | `9d0ba4cdd1713441eb5e60a5024e7a9b8f30017ffd1c288cf3a7599b6d85269f` | none | PASS | PASS | PASS |
| `tests.test_fx2_f6_j1_fl_v3_unit_scenes_s1_s2_s3.TestFLV3UnitScenesS1S2S3.test_fl_v3_simultaneous_multi_stream_vector_tracking` | `0b72ac3ed78271a09ffeaeb92fe19caaa3d92a0f841c73d1090ab2045c6c2727` | EF | XPASS | PASS | PASS |
| `tests.test_fx2_f6_j1_fl_v3_unit_scenes_s1_s2_s3.TestFLV3UnitScenesS1S2S3.test_fl_v3_independent_tracker_slot_equivalence` | `cdf898ad1e8e69a65915ca35fd1dc36e983e2b7bf59447780ecb259ea73e191f` | EF | XPASS | PASS | PASS |
| `tests.test_fx2_f6_j1_fl_v3_unit_scenes_s1_s2_s3.TestFLV3UnitScenesS1S2S3.test_fl_v3_slot_permutation_invariance` | `e21e3aa4d2b20072457305df3a9f526f811889e0c31d97be8b5afc48774accbb` | EF | XPASS | PASS | PASS |
| `tests.test_fx2_f6_j1_fl_v3_unit_scenes_s1_s2_s3.TestFLV3UnitScenesS1S2S3.test_fl_v3_reset_isolation_on_slot_replacement` | `4f4ea17177fb7ceee6239e16774c03c28a1feaeeaac53b232a9b5c2361e9835a` | EF | XPASS | PASS | PASS |

### `tests/test_fx2_f6_j4_chain_veto_and_probe_accounting.py`

Original file SHA-256: `8e7d0a7898d6452dec05afc42b2489e89b02aa03124e67d75606177990659cee`

| Exact original method ID | Original method SHA-256 | Pin | Original | Bare assertion | Final |
|---|---|---|---|---|---|
| `tests.test_fx2_f6_j4_chain_veto_and_probe_accounting.TestChainVetoAndProbeAccounting.test_chain_execution_preconditions_and_immutability` | `5cd3b7dc5fce501f5302a9bbc3af6546c6b850cbd98bd95edc780fe0b1ae240d` | none | FAIL | FAIL | PASS |
| `tests.test_fx2_f6_j4_chain_veto_and_probe_accounting.TestChainVetoAndProbeAccounting.test_chain_veto_desired_demotion_contract` | `b8f91186de9ff81d7da231e25b0f70e843a50ca93d2f6105bfac6aa4279249e0` | EF | XFAIL | FAIL | PASS |
| `tests.test_fx2_f6_j4_chain_veto_and_probe_accounting.TestChainVetoAndProbeAccounting.test_probe_accounting_invariants_and_format_equivalence` | `6593592bf8c899a50ca6e8dbf5d1ca5368a7abb79b543a4e48856b4f91d47ff2` | none | PASS | PASS | PASS |
| `tests.test_fx2_f6_j4_chain_veto_and_probe_accounting.TestChainVetoAndProbeAccounting.test_probe_accounting_reordering_determinism` | `aa719cb106e40d97eef2ace293e88a9a2a3ca01ace4d14425aad0abda87a6b8b` | none | PASS | PASS | PASS |
| `tests.test_fx2_f6_j4_chain_veto_and_probe_accounting.TestChainVetoAndProbeAccounting.test_probe_tp_exclusion_per_kind_records_and_truth_class` | `44dabfc4ee0da6aad4d131c00cb7e003720f735ff1f50e3742c7889f42864734` | EF | XPASS | PASS | PASS |
| `tests.test_fx2_f6_j4_chain_veto_and_probe_accounting.TestChainVetoAndProbeAccounting.test_probe_tp_exclusion_cause_bucket_other_accounting` | `0e704fb596b7f75666f209a94d14c6b2c3f46c75caeaef679c4dbc4cc60e20c4` | EF | XPASS | PASS | PASS |
| `tests.test_fx2_f6_j4_chain_veto_and_probe_accounting.TestChainVetoAndProbeAccounting.test_probe_tp_exclusion_summary_exact_fp_totals` | `8c67b0cb58130fc4fb075966b0424dbb51afa396723b6f7740fba980bddbf32c` | EF | XPASS | PASS | PASS |

### `tests/test_fx2_f7_j3_rtring_seqlock_reorder_and_wrap.py`

Original file SHA-256: `ff0495e001081745906d32e89662f745607b7bf92ca2e76da56aab1c74df8c22`

| Exact original method ID | Original method SHA-256 | Pin | Original | Bare assertion | Final |
|---|---|---|---|---|---|
| `tests.test_fx2_f7_j3_rtring_seqlock_reorder_and_wrap.TestFx2F7J3RtringSeqlockReorderAndWrap.test_recovery_from_finite_stale_observation_prefix_followed_by_coherent_commit` | `496feba50ad6e0467e4fa61afc6edbdcca31df9ec33278afea4c1386c799d97e` | none | PASS | — | PASS |
| `tests.test_fx2_f7_j3_rtring_seqlock_reorder_and_wrap.TestFx2F7J3RtringSeqlockReorderAndWrap.test_f02_stale_second_seq_load_reordering_lapped` | `7404690db3c6218a47423909ab0443cd118abb760ceec21c3f7a1cc69f90afd8` | EF | XPASS | PASS | PASS |
| `tests.test_fx2_f7_j3_rtring_seqlock_reorder_and_wrap.TestFx2F7J3RtringSeqlockReorderAndWrap.test_f02_stale_second_seq_load_reordering_absent` | `a09ae826e296791a7a0eef0e50f5d7b57f581ddf1c10ce5145caa0f4b3472889` | EF | XPASS | PASS | PASS |
| `tests.test_fx2_f7_j3_rtring_seqlock_reorder_and_wrap.TestFx2F7J3RtringSeqlockReorderAndWrap.test_stable_different_render_index_controls` | `5b0f0e6d65ec8bbd04d51dded52b9af00971ff69bba0cdfd6d05beb69e3a8514` | none | PASS | — | PASS |
| `tests.test_fx2_f7_j3_rtring_seqlock_reorder_and_wrap.TestFx2F7J3RtringSeqlockReorderAndWrap.test_slot_reuse_across_ring_laps_and_exact_accounting` | `846a90d34becc439fee46364610a9495e525e0b06ea0682c12bb7174847fe654` | none | PASS | — | PASS |
| `tests.test_fx2_f7_j3_rtring_seqlock_reorder_and_wrap.TestFx2F7J3RtringSeqlockReorderAndWrap.test_combined_callback_overwrite_post_use_rejection_recovery_and_crc_corruption` | `e3eb804aa486ba43c384be4640b0505d115eb41419812cedb14850bc5ecc021e` | none | PASS | — | PASS |

### `tests/test_fx2_f7_j4_ramws_special_nodes_and_quota_sweep.py`

Original file SHA-256: `c1eb786afd568e6331b93ec5f91d1490f80bbb6cb2c55963ddae648fef1881e2`

| Exact original method ID | Original method SHA-256 | Pin | Original | Bare assertion | Final |
|---|---|---|---|---|---|
| `tests.test_fx2_f7_j4_ramws_special_nodes_and_quota_sweep.TestRamwsSpecialNodesAndQuotaSweep.test_release_bound_socket_node_removal_defect` | `fc5e810d54b61264b84af268da35556bb1ede997c7715f7f341d04e6fb04ceb8` | EF | XFAIL | ERROR | PASS |
| `tests.test_fx2_f7_j4_ramws_special_nodes_and_quota_sweep.TestRamwsSpecialNodesAndQuotaSweep.test_release_mixed_special_node_directory_and_sentinel_safety` | `40dca090097d4b3fe4a66bca72777a3e3a8a37289078453a8077d62a83bfdd6b` | none | ERROR | — | PASS |
| `tests.test_fx2_f7_j4_ramws_special_nodes_and_quota_sweep.TestRamwsSpecialNodesAndQuotaSweep.test_quota_refusal_release_and_successful_retry` | `9472c788358cd4577aefa50f73c72e7fb0c148469526166c54fa0f7e736a0190` | none | PASS | — | PASS |
| `tests.test_fx2_f7_j4_ramws_special_nodes_and_quota_sweep.TestRamwsSpecialNodesAndQuotaSweep.test_keep_frames_capped_sorted_prefix_and_inert_metadata` | `db42d3929bf4a0c81cf5b5e7196005d4f2962127ebd8f33ebf1ca2f4c85225ad` | none | PASS | — | PASS |
| `tests.test_fx2_f7_j4_ramws_special_nodes_and_quota_sweep.TestRamwsSpecialNodesAndQuotaSweep.test_sweep_kept_expiry_equality_and_immediately_afterward` | `e25a1a5c5250b2c6ce7fb390fe1f69cc8444230318c088a430364e12ce8c6e76` | none | PASS | — | PASS |

### `tests/test_fx2_f8_j1_xwd_header_and_byte_order_parser.py`

Original file SHA-256: `9cd1d80c62831daa9422686c53b0742a3489ea9b7bac36ffad0347cbb41e52d3`

| Exact original method ID | Original method SHA-256 | Pin | Original | Bare assertion | Final |
|---|---|---|---|---|---|
| `tests.test_fx2_f8_j1_xwd_header_and_byte_order_parser.TestXwdHeaderAndByteOrderParserContract.test_invalid_byte_order_rejection` | `d9014dca1772e26f893055b3a221fef10cedf15d57fcbf1b5d7e2e5d4ab1c1dc` | EF | XPASS | PASS | PASS |
| `tests.test_fx2_f8_j1_xwd_header_and_byte_order_parser.TestXwdHeaderAndByteOrderParserContract.test_header_size_and_ncolors_arithmetic_overflow` | `d15dd90a670e9dadb72439c63ddc69a12a995b3df39689fb259f0a9ec0afe27e` | EF | XFAIL | FAIL | BLOCKED; unchanged |
| `tests.test_fx2_f8_j1_xwd_header_and_byte_order_parser.TestXwdHeaderAndByteOrderParserContract.test_max_bytes_preallocation_budget_bounds` | `9e2f963fa8a85d74b462d25932fbc4e3db048c77665cf92ddd2ee5cbdb00c090` | EF | XFAIL | ERROR | PASS |
| `tests.test_fx2_f8_j1_xwd_header_and_byte_order_parser.TestXwdHeaderAndByteOrderParserContract.test_read_screen_refuses_over_budget_fstat_before_mmap` | `aa22408ee68a2ce659e44ce31541d3c8b685db6c256b4fc280993f8d777b75db` | EF | XPASS | PASS | PASS |
| `tests.test_fx2_f8_j1_xwd_header_and_byte_order_parser.TestXwdHeaderAndByteOrderParserContract.test_retry_count_tries_rejected_inputs` | `97010611cae3af42f24cb7333240e5d68a2e06970111b544f0e28c0ca9c5dada` | EF | XFAIL | ERROR | PASS |


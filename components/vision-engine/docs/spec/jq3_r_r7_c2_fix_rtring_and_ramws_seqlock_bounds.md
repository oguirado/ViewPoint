# R7-C2 fixture conformance and bounded-read gate

Bound repository: `oguirado/vision-engine`, explicit ref `bridge/integration`, observed commit `5ac4fa8b9e924c4cfa6f440bf481a667621a0d3b`. Branch: `bridge/jq3-r-r7-c2-fix-rtring-and-ramws-seqlock-bounds/a1`. The ref was resolved again before the final VM gate and was unchanged.

## Concrete defect and repair boundary

The owned M4 fixtures still encoded provisional records, hook arity, capture provenance, quota accounting, and refusal results. Before editing, all 30 original methods were enumerated and run individually: **11 PASS, 18 ERROR, 1 FAILURE**. The repairs use the merged constructors and real RAM buffers; all original test methods and safety assertions remain, with only demonstrated API-equivalent refusal/phase/epoch assertions adapted.

| Family | Original result | Concrete repair |
| --- | --- | --- |
| J1 lifecycle | 6 errors | Remove nonexistent shared-record imports; closed reads raise `StaleStreamError`; actual ring-byte quotas; bound/exported canonical guard; one-phase hooks. |
| J2 crop/seqlock | 5 errors | Trusted `resolve(handle)` returns `TrustedScreen` over owned CRC v2 rings; successful `xvfb_screen` requests; `invalid_crop`; distinct render index and slot seqlock; in-bounds quota rollback. |
| J4 zero torn reads | 5 pass, 2 errors, 1 failure | Real shared types; `hook(phase)`; fixture-owned `ring_factory` bytes replace nonexistent `_corrupt_slot`; change one sequence field at a time; physical-byte quota ledger. |
| J6 canonical lock order | 6 pass, 5 errors | Real constructors and raising order guard; exact per-ring and shared budgets; one-phase hooks; initial epoch zero; bounded, exception-propagating thread teardown. |

`vadapt/rtring.py`, `vadapt/ramws.py`, `vadapt/window_stream_bank.py`, and `vadapt/multi_x11_ramcap.py` are read-only. The fixtures do not expose `_staging` as a public API or fabricate a copy-revalidation method. Revalidation is exercised in the real read body through the supported step hook or the restored NumPy-copy seam. No protected production failure was encountered by the repaired original methods. Any unresolved protected-path defects remain external-owner blockers; this fixture gate does not resolve or certify them.

## Pre-edit inventory and decorators

File SHA-256 values bind exact original bytes. Method SHA-256 values below bind the original physical source span from its first decorator (if any) through its final line, including the final newline. Every listed ID existed before editing and is replayed through `unittest.TestLoader.loadTestsFromName` and its own inner `TestResult`.

| Original file | SHA-256 |
| --- | --- |
| `tests/test_jq2a_m4_j1_window_stream_bank_lifecycle.py` | `a7c3a2af47947e1c94f366f3a0f96304c76e19aadbf3bb5d99e9a7aa61b23e43` |
| `tests/test_jq2a_m4_j2_multi_x11_per_window_crop_and_seqlock.py` | `0fe44ee3b0ec3b76113b99d4515e31b3291647b41308ba18f8f9bd0a332b09a5` |
| `tests/test_jq2a_m4_j4_zero_torn_concurrent_window_reads.py` | `74fdc036d9728fb818d3a145856d37902f57a544a53602832e8767f0fc0d64fa` |
| `tests/test_jq2a_m4_j6_stream_bank_canonical_lock_order.py` | `bd97f2e681045cc52ae77c0f2c37bf1d948bbd52125b08b4440225f96d7a08b9` |

All original runtime method/class/inherited skip and expected-failure flags were false. No test class or inherited TestCase base carried a failure decorator. J1 methods had `expected_failure_if_owner_absent`; J2 had `expected_failure_if_absent`; J4 had `owner_expected_failure`; the six J6 production methods had `expected_failure_if_owner_absent`, while the five J6 harness methods had none. These 25 conditional pins were each inventoried and inactive with the merged owner. They were removed individually; the new gate rejects effective method, class, or inherited failure/skip flags.

| Exact original method ID | Original source SHA-256 | Pre-repair outcome |
| --- | --- | --- |
| `tests.test_jq2a_m4_j1_window_stream_bank_lifecycle.TestWindowStreamBankLifecycle.test_canonical_handle_ordering_for_multi_stream_lock` | `bc176512ed9323f88de00687977b9b6f90496f8cb801f57aac378d02b42e657b` | ERROR: ImportError: cannot import name 'TargetRecord' from 'vadapt.window_stream_bank' (/workspace/scratch/7dd7b9f895d5/vision-engine/vadapt/window_stream_bank.py) |
| `tests.test_jq2a_m4_j1_window_stream_bank_lifecycle.TestWindowStreamBankLifecycle.test_close_and_respawn_target_and_resize_sibling` | `1903bafee357a3533be217acd2d8717213977c2f187961228f3078e22271cfaa` | ERROR: ImportError: cannot import name 'TargetRecord' from 'vadapt.window_stream_bank' (/workspace/scratch/7dd7b9f895d5/vision-engine/vadapt/window_stream_bank.py) |
| `tests.test_jq2a_m4_j1_window_stream_bank_lifecycle.TestWindowStreamBankLifecycle.test_gate_g_m4_1_closed_incarnation_negative_mutant_vs_positive_control` | `1d68e1467b0fb07a168c7fb4a32bb3709187686f52bf478b3ccc51853ffdccbd` | ERROR: ImportError: cannot import name 'TargetRecord' from 'vadapt.window_stream_bank' (/workspace/scratch/7dd7b9f895d5/vision-engine/vadapt/window_stream_bank.py) |
| `tests.test_jq2a_m4_j1_window_stream_bank_lifecycle.TestWindowStreamBankLifecycle.test_identical_geometry_different_handles_separate_rings` | `4731f4ffe0ab0729d5b79d3fced1c0fe75b04e44e154bfeda1fc36de2eea618c` | ERROR: ImportError: cannot import name 'TargetRecord' from 'vadapt.window_stream_bank' (/workspace/scratch/7dd7b9f895d5/vision-engine/vadapt/window_stream_bank.py) |
| `tests.test_jq2a_m4_j1_window_stream_bank_lifecycle.TestWindowStreamBankLifecycle.test_step_hook_invocation_and_copy_revalidation` | `e291346d718aaed319f9dc78f3103c75039176547ec16ed9f0f88a9f0b9ba1f2` | ERROR: ImportError: cannot import name 'TargetRecord' from 'vadapt.window_stream_bank' (/workspace/scratch/7dd7b9f895d5/vision-engine/vadapt/window_stream_bank.py) |
| `tests.test_jq2a_m4_j1_window_stream_bank_lifecycle.TestWindowStreamBankLifecycle.test_transactional_quota_admission_and_rollback` | `38e271310012ba7b402197a3308b173a0ea694e8f4b79c5ce538ba722c2ba769` | ERROR: ImportError: cannot import name 'TargetRecord' from 'vadapt.window_stream_bank' (/workspace/scratch/7dd7b9f895d5/vision-engine/vadapt/window_stream_bank.py) |
| `tests.test_jq2a_m4_j2_multi_x11_per_window_crop_and_seqlock.TestJQ2AM4J2MultiX11PerWindowCropAndSeqlock.test_negative_and_overflowing_crop_bounds_refused` | `81d1cdcfad9827c5e1b96b18603b45b66535ec7ccfd714936c42a389477997f4` | ERROR: TypeError: source must expose trusted resolve(handle) |
| `tests.test_jq2a_m4_j2_multi_x11_per_window_crop_and_seqlock.TestJQ2AM4J2MultiX11PerWindowCropAndSeqlock.test_odd_and_changing_seqlock_sequence_refused` | `2283c55ec1747fefcecf6007ee2c92045781ba5d491f6f12a9600ab13e110719` | ERROR: TypeError: source must expose trusted resolve(handle) |
| `tests.test_jq2a_m4_j2_multi_x11_per_window_crop_and_seqlock.TestJQ2AM4J2MultiX11PerWindowCropAndSeqlock.test_quota_admission_transactional_rollback` | `7f40908e4063db516298d3737a7f09b9b8445642497883413f36ae3485ba1c95` | ERROR: TypeError: source must expose trusted resolve(handle) |
| `tests.test_jq2a_m4_j2_multi_x11_per_window_crop_and_seqlock.TestJQ2AM4J2MultiX11PerWindowCropAndSeqlock.test_unsupported_source_mode_and_target_kinds_refused` | `382fdb68830adccc5b92e18e65564c99a5e916655418629d7e1d25af3b39d8fe` | ERROR: vadapt.window_stream_bank.StreamError: unsupported target kind or empty canonical_id |
| `tests.test_jq2a_m4_j2_multi_x11_per_window_crop_and_seqlock.TestJQ2AM4J2MultiX11PerWindowCropAndSeqlock.test_valid_crop_byte_exact_per_window_signatures` | `26c1cbb8534fabac5ae1ca634f402e2c183d46417fd502c9a8718c80312c5081` | ERROR: TypeError: source must expose trusted resolve(handle) |
| `tests.test_jq2a_m4_j4_zero_torn_concurrent_window_reads.TestZeroTornConcurrentWindowReads.test_canonical_handle_order_and_lock_order_checks` | `07d92d5a87c02f83246e64b5bc4b186835de7e02d6afc34593d9285eb5f89a9f` | PASS |
| `tests.test_jq2a_m4_j4_zero_torn_concurrent_window_reads.TestZeroTornConcurrentWindowReads.test_gate_g_m4_4_step_hook_torn_frame_detected_and_dropped` | `cd6a4c34804d38c492280e06094ae237dad9d2556b0c2dd82ce72c7c0bfcb5cf` | ERROR: TypeError: TestZeroTornConcurrentWindowReads.test_gate_g_m4_4_step_hook_torn_frame_detected_and_dropped.<locals>.corrupting_hook() missing 2 required positional arguments: 'token' and 'slot_idx' |
| `tests.test_jq2a_m4_j4_zero_torn_concurrent_window_reads.TestZeroTornConcurrentWindowReads.test_inv_m4_1_generation_change_advances_epoch` | `6acbae8c41a661f32f73972c254a407ac38d69eceae0517b2b3dcd27c7945312` | PASS |
| `tests.test_jq2a_m4_j4_zero_torn_concurrent_window_reads.TestZeroTornConcurrentWindowReads.test_inv_m4_1_separate_single_writer_rings_and_supported_kinds` | `d3256acd1fd0db4a0e6d63e4dc51e63bfda636da349c957e97a85350c96e9ec0` | PASS |
| `tests.test_jq2a_m4_j4_zero_torn_concurrent_window_reads.TestZeroTornConcurrentWindowReads.test_inv_m4_2_transactional_quota_admission_exact_accounting` | `9693443d0738f5f7085e3932a7ae3006a194edf82a21676cbc935035557c40fc` | FAILURE: AssertionError: (<class 'ValueError'>, <class 'RuntimeError'>, <class 'MemoryError'>) not raised |
| `tests.test_jq2a_m4_j4_zero_torn_concurrent_window_reads.TestZeroTornConcurrentWindowReads.test_inv_m4_3_bounded_retries_yield_explicit_drops_no_sleep` | `9bf5f8bb85ac08c2d4375fc4635193c4616467dd2e0d689447d7f39e80b3f301` | ERROR: TypeError: TestZeroTornConcurrentWindowReads.test_inv_m4_3_bounded_retries_yield_explicit_drops_no_sleep.<locals>.infinite_lapped_hook() missing 2 required positional arguments: 'tok' and 'slot_idx' |
| `tests.test_jq2a_m4_j4_zero_torn_concurrent_window_reads.TestZeroTornConcurrentWindowReads.test_positive_control_stable_ram_slots_delivered` | `9f2065095cf2aa58739679fcc2bb7bd31b8f4325361938dc27700535305aa7ac` | PASS |
| `tests.test_jq2a_m4_j4_zero_torn_concurrent_window_reads.TestZeroTornConcurrentWindowReads.test_record_classes_field_order_and_immutability` | `4dbe972ba834e562de7a76f01a414a5093a95e74dea82bd8ba87d1802ed7710b` | PASS |
| `tests.test_jq2a_m4_j6_stream_bank_canonical_lock_order.TestCanonicalLockOrderHarness.test_check_lock_order_predicate_canonical_cases` | `6d11c7c6f70def7c0e4f720bc0362df0cd0ad588fd8398f03e695f450107df83` | PASS |
| `tests.test_jq2a_m4_j6_stream_bank_canonical_lock_order.TestCanonicalLockOrderHarness.test_dataclass_field_order_and_immutability` | `aca709e02641ea8ac73e7e0ba99cfb2ec2758c6307eff8b6abd638b18f2ad8a4` | PASS |
| `tests.test_jq2a_m4_j6_stream_bank_canonical_lock_order.TestCanonicalLockOrderHarness.test_negative_mutant_deadlock_on_reversed_caller_order` | `256fa0db7773ae40f9456dbfa34d6da1e0c329b0b4e83e620395f95dfe84a932` | PASS |
| `tests.test_jq2a_m4_j6_stream_bank_canonical_lock_order.TestCanonicalLockOrderHarness.test_positive_control_concurrent_canonical_order_safety` | `7d489d9bc67a3e5adcad8cbc1845c43a480862a0faf79834dbf8d0ff2e3a6cd6` | PASS |
| `tests.test_jq2a_m4_j6_stream_bank_canonical_lock_order.TestCanonicalLockOrderHarness.test_positive_control_rejection_of_reversed_handle_order` | `7974d2d0d805ff3b996ea3648e10e51467bf4ada9988e740f3722fbb7e8bb04a` | PASS |
| `tests.test_jq2a_m4_j6_stream_bank_canonical_lock_order.TestProductionStreamBankConformance.test_prod_check_lock_order_predicate` | `4d1d6b15033109e7857e95783ae08df52ec09094ab2cbb76b334abb7b36eb597` | ERROR: vadapt.window_stream_bank.StreamError: stream handles must be unique and in canonical order |
| `tests.test_jq2a_m4_j6_stream_bank_canonical_lock_order.TestProductionStreamBankConformance.test_prod_no_actor_or_focus_locks_acquired` | `7f73083ce1b35db4289f7ea4e2769d087f0897731689ffb18e920b2cb7f742c3` | ERROR: vadapt.window_stream_bank.QuotaError: ring_bytes exceeds StreamSpec.quota_bytes |
| `tests.test_jq2a_m4_j6_stream_bank_canonical_lock_order.TestProductionStreamBankConformance.test_prod_owner_module_presence` | `aa92a7bdbc9b70a9c872e0ab78f694d8033d486a0ad3f39cc2bd848a59f1e89f` | PASS |
| `tests.test_jq2a_m4_j6_stream_bank_canonical_lock_order.TestProductionStreamBankConformance.test_prod_resize_epoch_advancement_and_stale_token_rejection` | `ae6ca3b094e5459fb42cb311452354dc9bb46363b8fc919d0ed847a66756e7f9` | ERROR: vadapt.window_stream_bank.QuotaError: ring_bytes exceeds StreamSpec.quota_bytes |
| `tests.test_jq2a_m4_j6_stream_bank_canonical_lock_order.TestProductionStreamBankConformance.test_prod_step_hook_invocation_before_and_after_copy` | `890713269e5adfd1f65a3304a1c3e0eea050a8321a8ad09d1fd7298fa4d70aa0` | ERROR: vadapt.window_stream_bank.QuotaError: ring_bytes exceeds StreamSpec.quota_bytes |
| `tests.test_jq2a_m4_j6_stream_bank_canonical_lock_order.TestProductionStreamBankConformance.test_prod_stream_bank_group_operations_lock_order` | `7521446d1597e811bde9a4b6c1253f4d13a14004920c3ac4b58ac63ee08e7f05` | ERROR: vadapt.window_stream_bank.QuotaError: ring_bytes exceeds StreamSpec.quota_bytes |

## Inventory comparison and exact-method coverage

The pre-edit/current AST member comparison found **no removed or renamed original test ID**. All 30 original IDs exactly equal the new gate inventory: J1=6, J2=5, J4=8, J6=11, with no missing, extra, or duplicate ID. Shared import/helper edits affect every method in those files, so the gate runs all 30 rather than selecting only the initially red methods. Every inner result must have empty failures, errors, skips, expected failures, and unexpected successes. The original-method runner also checks the current discovered method set against the pre-edit inventory and detects leaked threads.

Changed/removed original fixture helpers are listed below; new cleanup, trusted-source, ring, and thread helpers affect the same complete family. Removed shadow record classes and fallback bank constructors were also included in the AST comparison; their complete original bytes are bound by the file digests above.

| Original qualified helper | Original source SHA-256 | Change | All affected methods rerun |
| --- | --- | --- | --- |
| `tests.test_jq2a_m4_j1_window_stream_bank_lifecycle.expected_failure_if_owner_absent` | `2fc3ebd69acee336e8c70f95ee5155f26b1f70d4e7dab75bcb68c4a78aeb9eba` | removed | 6 |
| `tests.test_jq2a_m4_j1_window_stream_bank_lifecycle._import_owner` | `1daab31311adb4f1e0d19a6a7079f1359164136750c5721065243cb535f5599f` | changed | 6 |
| `tests.test_jq2a_m4_j2_multi_x11_per_window_crop_and_seqlock.expected_failure_if_absent` | `b52e1b1b73c7395fe6497fcac2604fe25c1c42107a7f3be5db46f4b57ed4a125` | removed | 5 |
| `tests.test_jq2a_m4_j2_multi_x11_per_window_crop_and_seqlock.FakeTrustedSource.__init__` | `3d52ffa621f453601b5af9346daf5477b5259d924a9e5f0e505ab8413edb0939` | changed | 5 |
| `tests.test_jq2a_m4_j2_multi_x11_per_window_crop_and_seqlock.FakeTrustedSource.add_window` | `b39bff6ee835b605477d0c096c4453d7ed0850c2dbe85c8327e70d2d77421f9b` | changed | 5 |
| `tests.test_jq2a_m4_j2_multi_x11_per_window_crop_and_seqlock.FakeTrustedSource.get_geometry` | `fc0e3ef9760d0b846baf8a2ec04aca4245d002322042fa89d4729872a8c886b8` | removed | 5 |
| `tests.test_jq2a_m4_j2_multi_x11_per_window_crop_and_seqlock.TestJQ2AM4J2MultiX11PerWindowCropAndSeqlock._require_owner` | `369109ce54c70c1f856ab8cc39bb3e00ddfac5917f8d5db3964df3043c423ffd` | changed | 5 |
| `tests.test_jq2a_m4_j4_zero_torn_concurrent_window_reads._get_owner_module` | `dea38e141222220f028b78d0dc7f2c594dac158012741b9143fa1eef6aebfb68` | changed | 8 |
| `tests.test_jq2a_m4_j4_zero_torn_concurrent_window_reads.owner_expected_failure` | `097250de8079e8683d46463af62713effe80237aafd17a4c98709e3cce940b3c` | removed | 8 |
| `tests.test_jq2a_m4_j4_zero_torn_concurrent_window_reads.TestZeroTornConcurrentWindowReads._get_wsb` | `ccaf34001f64ffe6050204b78de83204c1cfa4ecf8000697ecacc9ea8d4de5f8` | changed | 8 |
| `tests.test_jq2a_m4_j6_stream_bank_canonical_lock_order.expected_failure_if_owner_absent` | `9a9d7a12254b7e092e813bd29be935de24e6a09bff7fd55540769daf3ea5bd01` | removed | 11 |
| `tests.test_jq2a_m4_j6_stream_bank_canonical_lock_order.WindowStreamBank.__init__` | `bfc86fe11ac09a8dc5e5fdc211a52de7621880ce15fdec672d4a9c4180cec265` | removed | 11 |
| `tests.test_jq2a_m4_j6_stream_bank_canonical_lock_order.WindowStreamBank.open` | `0288b4e0a646232547081619c96a914c7761d55da56bc6da8b4652f0e3e0ab6c` | removed | 11 |
| `tests.test_jq2a_m4_j6_stream_bank_canonical_lock_order.WindowStreamBank.publish` | `87fc4cf65fd01c01b596fe394f302faa00182ec76c433d30d2112dfa90126930` | removed | 11 |
| `tests.test_jq2a_m4_j6_stream_bank_canonical_lock_order.WindowStreamBank.read` | `98b7ff1c92b45e65b7731beabc2de6ebd0d618546b61519bd2e6467e43f4f81f` | removed | 11 |
| `tests.test_jq2a_m4_j6_stream_bank_canonical_lock_order.WindowStreamBank.resize` | `b05f7548aaa0a2bab7af26dca94b598619f17090c17b15ee14032142300b6851` | removed | 11 |
| `tests.test_jq2a_m4_j6_stream_bank_canonical_lock_order.WindowStreamBank.close` | `97113f8fd3550d0dd1515edb28c912a5a37ea7e11e3e1ba79881bf1f7db30c64` | removed | 11 |
| `tests.test_jq2a_m4_j6_stream_bank_canonical_lock_order.LockOrderHarness._get_lock` | `78d0277fdee65b571592af08e76b88f5d098df67dca876a4e622c21e565cba87` | changed | 11 |

Prerequisite-only in-memory probes exposed failures hidden by the first stale import/quota: closed reads raised `StaleStreamError`, quota fixtures admitted a second small ring, the unbound instance order guard raised `TypeError`, three-argument hooks raised `TypeError`, and J4 had no `_corrupt_slot` method. J6 probes additionally demonstrated `0 != 1` at the stale initial-epoch assertion and shared-budget overflow after only per-stream budgets were fixed. The preserved original assertions now exercise these paths, with real production validation still active.

## Verification outcome and mutation evidence

Exact command in the VM:

```text
python3 -m unittest tests.test_jq3_r_r7_c2_fix_rtring_and_ramws_seqlock_bounds
```

- Before repair, with only the new gate overlaid on the preserved integration snapshot: `Ran 6 tests in 0.276s`, `FAILED (failures=23)`, exit 1. These are the 19 original failing/error results plus four aggregate family-completion failures; both real/mutant contrasts passed. This establishes a failing-before-repair regression for each family.
- After repair: `Ran 6 tests in 0.295s`, `OK`, exit 0. The four family tests execute all **30 original methods**, and two tests exercise the named mutation sites. No skips, expected failures, or unexpected successes.
- Both mutation probes use 133 deterministic cases, replay seed `1352042835` (`0x50968953`), and independent expectations built from allocated KiB. Counterexamples are the smallest sampled cases, not a global minimality claim.

| Named site | Qualified AST anchor / structural selector | Real assertion | Mutant assertion | Smallest sampled counterexample | Teardown |
| --- | --- | --- | --- | --- | --- |
| `M-vadapt-ramws-1` | Unique `_swap_used` return `(t - f) * 1024`; `.value.left.op`, `Sub → Add` | PASS | KILLED by behavioral `AssertionError` | total=1 KiB, free=1 KiB: expected 0 bytes, observed 2048 | clean |
| `M-vadapt-ramws-2` | Same unique return; `.value.right`, `1024 → 1025` | PASS | KILLED by behavioral `AssertionError` | total=1 KiB, free=0 KiB: expected 1024 bytes, observed 1025 | clean |

Only the pure behavioral assertion can produce a kill; observation/construction/import errors, timeouts, unrelated exceptions, missing/nonunique anchors, survivors, and equivalent ASTs have separate statuses. A failed real assertion never runs the mutant. Mutants compile solely into fresh private registered modules with package metadata. `sys.dont_write_bytecode`, module registration, parent-package attributes, meminfo fakes, and context state are restored in `finally`. Source bytes, RAM workspace/signal state, and private-module cleanup are checked. No mutated source or bytecode is written.

Focused fault probes verified failed-positive exclusion, constructor/observation `AssertionError` exclusion, import failure cleanup, and missing/nonunique anchors. Review found that two timeout paths initially shared other error categories; these were corrected, and final probes confirmed distinct `timeout` status with restored context for private entry, mutant assertion, and real assertion.

## Evidence requests and limits

E0, exact `vadapt/ramws.py` SHA-256: `09b857162f058df15664bed102aea7a44e763625939c94508c063f19da508559`.

E1, exact line 103 (the code line starts with four spaces; its line terminator is excluded):

```python
    return (t - f) * 1024
```

All timings above are VM measurements. Host measurements: **AUSENTE**. Live X11/native binaries, network/model calls, frame files, broader red suites, and host integration acceptance are outside this offline gate. No rtring mutation score or protected production fix is claimed. Orchestrator acceptance remains unestablished; the PR is for collection.

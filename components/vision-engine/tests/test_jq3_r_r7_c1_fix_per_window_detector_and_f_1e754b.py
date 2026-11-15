"""G-R7-11: real M5 fixtures, exact legacy replay and private AST contrasts.

Everything runs offline with RAM arrays. Mutation sites are selected by qualified
scope, the complete containing expression and an explicit structural selector.
No sibling mutation harness or reference detector supplies production outcomes.
"""
import ast
from collections import Counter
from contextlib import contextmanager
from contextvars import Context, ContextVar, copy_context
from dataclasses import dataclass
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
from queue import Queue
import sys
from threading import Barrier, Thread
from types import ModuleType, SimpleNamespace
import unittest

_import_flag = sys.dont_write_bytecode
try:
    sys.dont_write_bytecode = True
    import numpy as np
    from visioncore import desc
    from visioncore.grid import Grid
    from eval.v2x import fl_blinkprior, fl_v3, per_window_detector as real_detector
    from eval.v2x.per_window_detector import Handle, ChangeResult
finally:
    sys.dont_write_bytecode = _import_flag

REPLAY_SEED = 730711
SOURCE = Path(real_detector.__file__)
PRIVATE_NAME = "eval.v2x._r7_private_detector"
PRIVATE_ATTR = PRIVATE_NAME.rsplit(".", 1)[1]
_MISSING = object()

# PRE_EDIT_MANIFEST is generated from the untouched integration sources before
# any fixture edit. Every original ID is replayed, so changes to a shared helper
# cannot silently reduce coverage. Definition hashes include original decorators.
PRE_EDIT_MANIFEST = {'tests.test_jq2a_m5_j1_per_window_gm_fl_state_isolation': {'definitions': {'RefActionRequest': 'a2a3b39084a0205a83fea11583d466b6279d36b5646e634fb271ccbfd407ea56',
                                                                            'RefActorLease': '1f6b53fbe7f97a0bcea32827a60406b4d6814c7f10c28c3fd5f9c3c36a8a3c49',
                                                                            'RefChangeResult': '5a83652d764e0a0e8d4c06365f87b0d3411f3c18c628d3795aac61923e14109e',
                                                                            'RefFocusLease': '02333e6e94bcbd6f04d1fb6180084710e83f805a6d73102c45a5490cc40afb8a',
                                                                            'RefFrameRef': '76e9fdc4407fcf9707c008802c0ca757119d7097d466b70f047d70be2b195489',
                                                                            'RefFrameRef.__post_init__': '2e3131eb5c2c86886de4a45753c3ad68d8efa004b413b0fe9981a79bf8ef2141',
                                                                            'RefHandle': '623600b3806582f4a4d83665bd845532d64f8c23ae022497c22d1c180b5d28c5',
                                                                            'RefHandle.__post_init__': 'de88867966227d05997b8fb93402f7f7639d55ab1345c7c6006562e44b452bbc',
                                                                            'RefObservation': '5516c270625f4ea0e701fb77e377116574b1b74bac2a7f226471599544a82cc2',
                                                                            'RefObservation.__post_init__': 'ab9ef6ee6e41fd49a965f2a24ec4ef0789e08554ece02098bf664b530ed72c52',
                                                                            'RefPerceptionResult': '29aa06ecf61edb38fb852927c1fa631e26cb912e2623d939c73ff17168bc8308',
                                                                            'RefReceipt': '0f1971b9ceb3af13d71e0ba3ad6f0cb5b65128b4ef6552e90c8175e22cf36f78',
                                                                            'RefStreamSpec': '065039ebbc4adb31a12d2c9b910724b0f565d3a74e9ca74e378799cf600b5ffb',
                                                                            'RefStreamToken':
                                                                                'abb883facafdf342bdba9152c492511ea33880c605eb75db52541ce50fc8a02c',
                                                                            'RefTargetRecord': '22549bbc0e3c5e570d5e7472f49017350550c902fe2c7d9fb5ab74629dd83503',
                                                                            'RefTicket': '5ca7ef5b27477d735f1612c800cd5fa784218c59e024ff3c3eb3055d4c5e6c4f',
                                                                            'RefWindowDescription': '455cf47be70aabb86d748cf85548e62f52b43f58b1177972671f79c801725f25',
                                                                            'RefWindowFrames': '0953da3d47b3623fe20d7712c95cccd8b7576fdd03cb2f8aebca312175cf0880',
                                                                            'RefWindowFrames.__post_init__': 'f3db750b7f621e31e2ebd98e94a8c4367349109fefbaa231ae0e778d6c1d5b47',
                                                                            'ReferencePerWindowDetector': 'aa728b264f4e3f84b922e7f0a8d622caa47d3960e43a2ada4bfa5ebd1bf6925d',
                                                                            'ReferencePerWindowDetector.__init__': 'b453fa956ee5300777746e509f98b5dcb0a8c8c7d912b9f491a8b17ffb9eb5de',
                                                                            'ReferencePerWindowDetector.reset_window': '5c3a1ec6a2e9042cd515ed9d9dfea71e19e79a15a93d1bb1cba97dccf1600660',
                                                                            'ReferencePerWindowDetector.update': 'eb0dcebc5f7e79f4d7970678e0031ef731efcb0a0f2243fafb62d106114c5d10',
                                                                            'SharedStatePerWindowDetectorMutant': 'e6fde8245bc7a1323cc59e416bc9f8a3558c807385e2af57733bcedd7a6723ba',
                                                                            'SharedStatePerWindowDetectorMutant.__init__': '662233cc61d97f80c092481cd90c531fc4628ec8011b23389b7639caf2209fa7',
                                                                            'SharedStatePerWindowDetectorMutant.reset_window': '3ed3c349b3bcf50fcd01f216649c246a1c9d9d55022949cb2841a590c8cf2daa',
                                                                            'SharedStatePerWindowDetectorMutant.update': 'a0b6e52a2f9185ab9792174312510cc1adeef0fab8a3d1cea286eb380b3c87c9',
                                                                            'TestPerWindowGMFLStateIsolation': '49daa9c7665b55f6e508f1e8bd6b59ad1ee91790da98651d680b0d418de5dcaf',
                                                                            'TestPerWindowGMFLStateIsolation.test_canonical_dataclasses_and_field_orders_owner': '8db46789ae8cd828053df251e7a221c1f5f17b30e7f1ffcbab4119f1aa4c4f07',
                                                                            'TestPerWindowGMFLStateIsolation.test_dataclass_schemas_and_field_order_reference': 'b7c061af03169f406a8be1ecaff9a6bc1521b205f576f254efe162b0ea797edc',
                                                                            'TestPerWindowGMFLStateIsolation.test_frame_ref_readonly_rgb_and_window_frames_sequence': '7cb5f95c34478042c8244469ead8044f3caadbba898adacc8508c1947f637722',
                                                                            'TestPerWindowGMFLStateIsolation.test_gate_g_m5_1_interleaved_histories_equal_solo_histories_owner': '846680bf6f253ff65b8882ca3efa73375520930d12d66e1abfbd38f240f99e5c',
                                                                            'TestPerWindowGMFLStateIsolation.test_invariants_m5_1_m5_2_m5_3_owner': 'bb293d3ce436a5c7917a9b2dc6f3419d7a2428b4a520156632540e3877202c5e',
                                                                            'TestPerWindowGMFLStateIsolation.test_negative_mutant_shared_state_fails_isolation_harness': '34b950e2de98e472d52bce2355ca89886e0b06fc1cd44874a5db94aeec498ff5',
                                                                            'TestPerWindowGMFLStateIsolation.test_owner_module_exports_and_signatures': 'fc5ef7b0e3453357c3e6a4109ff42c5bba9ef721288d11fd5aaaf772668eef9b',
                                                                            'TestPerWindowGMFLStateIsolation.test_positive_control_reference_isolation_harness': 'a6a652e72457734e7684e3e2e5c497c780a134759f6b760a1bfcd3d9c91c63d9',
                                                                            'TestPerWindowGMFLStateIsolation.test_supported_target_kinds_and_handle_validation': '9139ef3155e060acf64e8283445eeac6eaeb96b859b15dfa2c42e8b7deb88aaa',
                                                                            'probe_owner_symbol': 'c79a8964e2309acc0f7d650ce59048d4e81c127156bdff6128fa97b1609c731b',
                                                                            'require_owner_symbol': 'fe51b9dd94af7d073d258440af22f4ccae5f5ee70a6b4a24add5d184a39061e0',
                                                                            'require_owner_symbol.decorator': 'f13816915f9e5eb033de3a701c435e0d59e0ff5729400272abf54ab67141c66a'},
                                                            'file_sha256': '4f58087231b12c9415f0fb276393df1d485ab79c0b626a4d690e902df6f7131d',
                                                            'methods': ('tests.test_jq2a_m5_j1_per_window_gm_fl_state_isolation.TestPerWindowGMFLStateIsolation.test_canonical_dataclasses_and_field_orders_owner',
                                                                        'tests.test_jq2a_m5_j1_per_window_gm_fl_state_isolation.TestPerWindowGMFLStateIsolation.test_dataclass_schemas_and_field_order_reference',
                                                                        'tests.test_jq2a_m5_j1_per_window_gm_fl_state_isolation.TestPerWindowGMFLStateIsolation.test_frame_ref_readonly_rgb_and_window_frames_sequence',
                                                                        'tests.test_jq2a_m5_j1_per_window_gm_fl_state_isolation.TestPerWindowGMFLStateIsolation.test_gate_g_m5_1_interleaved_histories_equal_solo_histories_owner',
                                                                        'tests.test_jq2a_m5_j1_per_window_gm_fl_state_isolation.TestPerWindowGMFLStateIsolation.test_invariants_m5_1_m5_2_m5_3_owner',
                                                                        'tests.test_jq2a_m5_j1_per_window_gm_fl_state_isolation.TestPerWindowGMFLStateIsolation.test_negative_mutant_shared_state_fails_isolation_harness',
                                                                        'tests.test_jq2a_m5_j1_per_window_gm_fl_state_isolation.TestPerWindowGMFLStateIsolation.test_owner_module_exports_and_signatures',
                                                                        'tests.test_jq2a_m5_j1_per_window_gm_fl_state_isolation.TestPerWindowGMFLStateIsolation.test_positive_control_reference_isolation_harness',
                                                                        'tests.test_jq2a_m5_j1_per_window_gm_fl_state_isolation.TestPerWindowGMFLStateIsolation.test_supported_target_kinds_and_handle_validation')},
 'tests.test_jq2a_m5_j2_fl_v3_contextvar_op_accounting': {'definitions': {'ActionRequest': '8d1d43489d20e443ff8fe50fe8b803800a6ff1732519c09429d701a293c73def',
                                                                          'ActorLease': '3f5913e7f63895fc1253c7a4d1ad15df356a2cae7b36f80e27e7acb15e9e9d91',
                                                                          'ChangeResult': 'd75642bd8ac9692f96cc2bc9bccaba3c8d637b9f223abb9994f27e6f055d0408',
                                                                          'FocusLease': '12516976d1f43623e063e1cba17ee3c00a30d5d1d547ac091ddc62e2dc46fba7',
                                                                          'FrameRef': '63a085dd256350148a492dc01a905ecfae72a793e03ab77d1545a1bab77ccdad',
                                                                          'Handle': '0519bf741d2334dca35c24f453cf86c3299451d149e23f9f0241bd173f77faa8',
                                                                          'Observation': 'd747e756718d70bb77837897b8cad01d7212d4917219c7b7021db475bf4a6706',
                                                                          'PerceptionResult': '3fa0f5265d0007713b5113acf1c80700ea00838bb0ab63d49bb2489aa6e0f19e',
                                                                          'Receipt': 'eedbd9a35222eb8514bc7dd987c30fb6b5fa0e0a27634b9d47bac15ac4ef2bbd',
                                                                          'StreamSpec': 'c5515206551ca6f128507fd4aa817e7a7b5bef2a418fc5365839e8ebffe41ba0',
                                                                          'StreamToken':
                                                                              '0080e1f342896e35f79c2412b357a588ba8dd9b5c01490cf55465d8b4d5d3cca',
                                                                          'TargetRecord': '6e6b196c6b938a49eac706b05f989d562854426637c83798be28dd505c073dfb',
                                                                          'TestFLV3ContextvarOpAccounting': 'b666cc779a5e5b509a8c601eb329021ab1b1ad27d8711af9f10d5ea25cf1349a',
                                                                          'TestFLV3ContextvarOpAccounting.test_negative_mutant_shared_global_fails_isolation': 'ddda84c88c6655c6229b7819fc99ed1064de3f148ccd7462396e741d3e825bd8',
                                                                          'TestFLV3ContextvarOpAccounting.test_positive_control_exception_and_nested_contexts': '13a540796f51a0cc95175d4ee38115e983733eac4e1423d74e3dc18d653f3ff9',
                                                                          'TestFLV3ContextvarOpAccounting.test_positive_control_interleaved_updates_equal_isolated_totals': '918fcd88ea0b43e78a09b646a8f6b81ba1322b09847bfe8f1f571fbea41c58a6',
                                                                          'Ticket': '62c9d79be6543cc5c58543da7bfd328b66898797b19802a3211cb8250d13eb57',
                                                                          'WindowDescription': 'f2036d9c45a8ff3672d72b38ea82f2eb671245666d651b3579b8f0cd7c662037',
                                                                          'WindowFrames': '09ae156c7b99e148467bf0aa754b02c43ac37d476bce13dda23abe29e3631f17',
                                                                          '_make_owner_test_method': '03d45084d947c23093eb85d83bdb1b432d9a7e78f97e953c601d6ec90f1f2e05',
                                                                          '_make_owner_test_method.test_owner_conformance': 'ad27baad0f53aef6310f35538ad691b2356823cfa9495a9c49558db6a8384f71',
                                                                          '_make_owner_test_method.test_owner_probe': '813ac160985bad0bdabef30eddae1bf3991e2683acceae820153b6e781f434c0',
                                                                          '_negative_fl_op_shared_global': 'b48f5e535d8365cdfd0610ba8ff1b55930afa2e63d86d9ee2bbb676fbf078deb',
                                                                          '_positive_fl_op': 'a35f121d897b683f4af58be486952ad3d0ca296a1aa91f5bbd28f6bda43b079e',
                                                                          'get_owner_symbol': '1c72333ca7c74134d14e01d082cb96118926658c85435a2acf3c32a80c99eb67',
                                                                          'run_negative_shared_global_harness': '285c7b27b830bdb5521a533fedff3ce2c20383785d41c2d8da511901a1e3967d',
                                                                          'run_positive_interleaved_contextvar_harness': 'e97e8d799120e023f1f86b54c9dbe52711b33e251d276bc83ae9a64dde12fb95',
                                                                          'run_positive_interleaved_contextvar_harness.step1_a': 'a96612b5361db19cb8ebdb1f64170c3333882b13a29caed65371a5ca73758ced',
                                                                          'run_positive_interleaved_contextvar_harness.step1_b': 'b2b97ef9df663cb66c45c6da0eabe82c4765eb13d014a3a963b466fba57724bc',
                                                                          'run_positive_interleaved_contextvar_harness.step2_a': '66b2291648fbd364b42c6fb7aeccf5c9a753f2d8c62f84636c421e00d0f958e4'},
                                                          'file_sha256': 'f7e04ee95899422f5bea3d9e5643d3d980fab39d730e2a6c6cf4821d48844a55',
                                                          'methods': ('tests.test_jq2a_m5_j2_fl_v3_contextvar_op_accounting.TestFLV3ContextvarOpAccounting.test_negative_mutant_shared_global_fails_isolation',
                                                                      'tests.test_jq2a_m5_j2_fl_v3_contextvar_op_accounting.TestFLV3ContextvarOpAccounting.test_positive_control_exception_and_nested_contexts',
                                                                      'tests.test_jq2a_m5_j2_fl_v3_contextvar_op_accounting.TestFLV3ContextvarOpAccounting.test_positive_control_interleaved_updates_equal_isolated_totals',
                                                                      'tests.test_jq2a_m5_j2_fl_v3_contextvar_op_accounting.TestFLV3ContextvarOpAccounting.test_production_owner_fl_v3_contextvar_op_accounting')},
 'tests.test_jq2a_m5_j3_interleaved_window_flicker_veto': {'definitions': {'InterleavedBlinkTrackerHarness': 'b0a651c2c774eb30939ba5cf276088c94f2419229512b04f90a7596a7d05e98d',
                                                                           'InterleavedBlinkTrackerHarness.__init__': 'b25566022201e013171c83737f04f1cb195bf6e27a42bdc49eb47cba9c7f7987',
                                                                           'InterleavedBlinkTrackerHarness.reset_target': 'b57b9f685ac01014f8fd519f3684efaa333a1714aaa08a22b48682d3ee6a0924',
                                                                           'InterleavedBlinkTrackerHarness.update': '9adbc23ad1950ad5d0b984d643f97de6f6a7d9179ce787fadd074fad7bfb0893',
                                                                           'TestCanonicalDataclassesContract': '777010d0c373c54833af2d99d4831f3d036708f57acbd4ed39ea0b166c253d4a',
                                                                           'TestCanonicalDataclassesContract.test_proposed_dataclasses_when_owner_present': 'bb365cb4481c570e8b178f7ca8f347cf9a04dcc827275aecc052e6631932d249',
                                                                           'TestCanonicalDataclassesContract.test_target_supported_kinds': '392b3077f529da9166b966d71dc58daed7212d3884ed9214567c136f6c504c4b',
                                                                           'TestContractHarnessInterleavedBlinkVeto': '7b41da2d2e56214d375896c4e85dae96b17cd3e84a2a60afd1ff652abcdb0b90',
                                                                           'TestContractHarnessInterleavedBlinkVeto.test_gate_g_m5_3_local_consecutive_blink_survives_sibling_interleaving': '2d53c16c8fa14b83cae561577eb1bfc23f4c8b968705a7b02a183b4ae55f34c0',
                                                                           'TestContractHarnessInterleavedBlinkVeto.test_negative_mutant_global_frame_sequence_fails_interleaved_veto': '4fede98fc770e7217e90e199fc440626767fe74c7497f3191cd3e6559f8a2da1',
                                                                           'TestContractHarnessInterleavedBlinkVeto.test_target_epoch_navigation_and_local_gap_resets': 'd1d95f3421fbbb34c65a2341ecc69c227c93a966d99bef2df0cd20f3e3525dfc',
                                                                           'TestG_M5_3_PerWindowDetector': '32d65bbbec212a0f0910c8cbb1d62cbff46e0dfd85c2c6b58c9d752686ba1e14',
                                                                           'TestG_M5_3_PerWindowDetector.test_per_window_detector_interleaved_blink_veto': '3e4ac5e66b37ccd4abd0a3fc9e53b245df90455df4e9faf29d37d4505d6c2740',
                                                                           'TestG_M5_3_PerWindowDetector.test_per_window_detector_reset_window_and_isolation': '7e5cc18492b8c92485d28f96445d39af9b39e8c4d4df28d92676edfd171dfb27',
                                                                           'TestMultiWindowChain': '0779088c669e9d0b01c9cc8ab8156687190323d8a7321e0341a3c83562fcdcc4',
                                                                           'TestMultiWindowChain.test_multi_window_chain_interleaved_observations_and_settle': 'ec88021432ecdf1a93105c4bf465a640c678e1effa9c409c7a63832bca11e7b0',
                                                                           'TestMultiWindowChain.test_multi_window_chain_missing_time_never_settles': '5579ad42ea0ed5bc3e436b249ab32abba1567c4f2c2bec897359ec560a22eb28',
                                                                           '_expected_failure_if_owner_absent': '1ee6183db48a62637376ef0d3f57b97485fb5935c005afa0bcb0e192234f4f49'},
                                                           'file_sha256': 'e071388333e3165eb92d253dd487651bacf60d098220c0d8cd0ad485a3186b16',
                                                           'methods': ('tests.test_jq2a_m5_j3_interleaved_window_flicker_veto.TestCanonicalDataclassesContract.test_proposed_dataclasses_when_owner_present',
                                                                       'tests.test_jq2a_m5_j3_interleaved_window_flicker_veto.TestCanonicalDataclassesContract.test_target_supported_kinds',
                                                                       'tests.test_jq2a_m5_j3_interleaved_window_flicker_veto.TestContractHarnessInterleavedBlinkVeto.test_gate_g_m5_3_local_consecutive_blink_survives_sibling_interleaving',
                                                                       'tests.test_jq2a_m5_j3_interleaved_window_flicker_veto.TestContractHarnessInterleavedBlinkVeto.test_negative_mutant_global_frame_sequence_fails_interleaved_veto',
                                                                       'tests.test_jq2a_m5_j3_interleaved_window_flicker_veto.TestContractHarnessInterleavedBlinkVeto.test_target_epoch_navigation_and_local_gap_resets',
                                                                       'tests.test_jq2a_m5_j3_interleaved_window_flicker_veto.TestG_M5_3_PerWindowDetector.test_per_window_detector_interleaved_blink_veto',
                                                                       'tests.test_jq2a_m5_j3_interleaved_window_flicker_veto.TestG_M5_3_PerWindowDetector.test_per_window_detector_reset_window_and_isolation',
                                                                       'tests.test_jq2a_m5_j3_interleaved_window_flicker_veto.TestMultiWindowChain.test_multi_window_chain_interleaved_observations_and_settle',
                                                                       'tests.test_jq2a_m5_j3_interleaved_window_flicker_veto.TestMultiWindowChain.test_multi_window_chain_missing_time_never_settles')},
 'tests.test_jq2a_m5_j4_def_a2_02_03_04_shadow_guards': {'definitions': {'RefActionRequest': 'f03d2b3d55094aaf3e9a2241df8be4e091c778325f835f4f312797c75ad5194f',
                                                                         'RefActorLease': '5a826c66cd11f3b6e63ac4156b26ca491fbf9bfaf7f1776f9e0f53f1beb0a61c',
                                                                         'RefChangeResult': '71c297ad5342fd1f001b89e17b652e82e5875dd217af7ed17125589238108982',
                                                                         'RefFocusLease': '1be7bec198e38356917582c94cb1d13d0747fb8b732428b74e9ce554c4438e75',
                                                                         'RefFrameRef': '150606bbecaa7d297fe4e9a7550bef5c7c316f88b48f949c21bdccbed030437a',
                                                                         'RefHandle': 'a9dcc2b9550a50440ef6a1f0e2fcb5d259fbda1e7f7baebe6308c9dd655ccb70',
                                                                         'RefObservation': 'acd3ac28a21b657fc7214b57249c1f8d094a122a218bf59426b4675d1f904eb0',
                                                                         'RefPerceptionResult': '17faa93e2d908655b1e2054e9ff4d38113899bd52f7dcd452722e399a0be07e6',
                                                                         'RefReceipt': '81a1bcb35d647dd21fc48a5232ecb41e5ed536514afe9a4411fb9294a71731ea',
                                                                         'RefStreamSpec': 'd20e31208344d3be83b65f09cc880662325a797361a32029dbd66d90aac5fd64',
                                                                         'RefStreamToken':
                                                                             '8435781b02b23f53a797a3ba656f1283cc94b1294b403eafda2168d72332f797',
                                                                         'RefTargetRecord': '87191f0e36dc989cac088bf00dfec2642b969de2e9ff94178b067d962e612cbf',
                                                                         'RefTicket': '62469925341c225534838b99009afd61f20bbd652cda43df0fd852f76e5d3c79',
                                                                         'RefWindowDescription': 'a33a7d578aa1f8014683aac063be6b22a7661974a026d81a6a312b8e0db1c6dd',
                                                                         'RefWindowFrames': 'dd27d187fdde54eb000d8831588b7f12b353d7d72d9e3344a61398a331998032',
                                                                         'TestOwnerPerWindowDetector': '28b8eedc46976154aaa55107c281cc7d8b0794a2166b66b56ffeb99714c4d161',
                                                                         'TestOwnerPerWindowDetector.test_owner_detector_import_and_instantiation': '47517260fea92c48311bdbd2c6e6635db39af614906e079b3072938abf23a9f3',
                                                                         'TestOwnerPerWindowDetector.test_owner_detector_reset_window': '47e38ee316cf402f0d072868c4e351c5466757ea5128dc134296550cc2149bd9',
                                                                         'TestShadowGuardsContract': 'c25ceff5d7925383aa0163c5f2b223203ecaf7bc499b73a448e0fed8c01cc6a2',
                                                                         'TestShadowGuardsContract.test_dataclass_contract_fields': '79e7bdaa57d4785e046f3a20b0103e0f975a3eb14cdea6c477ff592ba41b1f07',
                                                                         'TestShadowGuardsContract.test_negative_mutant_lazy_zero_hash_suppression': 'e5cb3d83fda7061ce1ba26326490c777d5b72821c935f02d7b43eaeaa1d45e5a',
                                                                         'TestShadowGuardsContract.test_negative_mutant_mask_all_motion': '452848140f291345d88b036e524963f904598d0fdb0fa89bbb1cc646e69a64ce',
                                                                         'TestShadowGuardsContract.test_positive_control_motion_and_chroma_preserved_under_blink_veto': '2b5d8c2eb86cde426d149f185806193dbc69f5483d7a1fdfdbb5c710b509c83f',
                                                                         'conditional_expected_failure': '59c628c6ac96ddd9fa6f9335eb6b30f9cc4b242e4bd506f71959df52c4aa6fdf',
                                                                         'owner_symbol_exists': 'e6aaca76047549dd3bf11b81d6e17ac623560fd666a26dcc607867719d94578c',
                                                                         'simulate_detector_pipeline': '72ee904233451780b55784b6ca0c8f6a242927715f9e8e4bbe5c07f01da5f421'},
                                                         'file_sha256': '892923ffec2f0258bdc429653a9b1fa94d5df2e5bff320630da8468da6dabd9b',
                                                         'methods': ('tests.test_jq2a_m5_j4_def_a2_02_03_04_shadow_guards.TestOwnerPerWindowDetector.test_owner_detector_import_and_instantiation',
                                                                     'tests.test_jq2a_m5_j4_def_a2_02_03_04_shadow_guards.TestOwnerPerWindowDetector.test_owner_detector_reset_window',
                                                                     'tests.test_jq2a_m5_j4_def_a2_02_03_04_shadow_guards.TestShadowGuardsContract.test_dataclass_contract_fields',
                                                                     'tests.test_jq2a_m5_j4_def_a2_02_03_04_shadow_guards.TestShadowGuardsContract.test_negative_mutant_lazy_zero_hash_suppression',
                                                                     'tests.test_jq2a_m5_j4_def_a2_02_03_04_shadow_guards.TestShadowGuardsContract.test_negative_mutant_mask_all_motion',
                                                                     'tests.test_jq2a_m5_j4_def_a2_02_03_04_shadow_guards.TestShadowGuardsContract.test_positive_control_motion_and_chroma_preserved_under_blink_veto')},
 'tests.test_jq2a_m5_j5_multiwin_chain_settle_oracle': {'definitions': {'ActionRequest': '7e2f4eb5ed6ed3e6f0b7b759a53233a95d67825bf319cb1e0eeab46294c92337',
                                                                        'ActorLease': '277946706bf33a821193bfd6d1b7afbe5bd8b150ef1bd531460fdaef6e694e9a',
                                                                        'ChangeResult': '575f31fe168748696f6a9bddba6e01070ec9a9e260bb93f67d5c28ccb02f6f36',
                                                                        'FocusLease': 'cb5e3aa9a8656828da2cbff61a3b591bb1e0aaf8ceaaab4b4eb4487e99a89435',
                                                                        'FrameRef': '23fd1d7fb1793a9cad122fcfe7fe544e79bc593a5adfb557faf8d8f7ed62d550',
                                                                        'Handle': 'd1cba6a8d57191095061ea0e020dae87862c6c9c5555d989f6b562eab2764632',
                                                                        'NegativeMutantSettledAfterGap': '29bfeb3a50c2219d7bb25a4e46fd53d0990c4e22630ee7e944a32b88efb7e4e1',
                                                                        'NegativeMutantSettledAfterGap.update': '762f3a4da52c1b1334922f2c2e55dcf80df525a4047721dde453ae1b477f802d',
                                                                        'NegativeMutantSettledOnMissingEvidence': 'd011b4cc0944349320365dadaeeb596825037ab8844e1239c20cc76d537df1d1',
                                                                        'NegativeMutantSettledOnMissingEvidence.is_settled': 'f350544bc08ea5af8664d094235b44ff3ca22b1781169e1f540dcab14e626508',
                                                                        'NegativeMutantSettledOnMotion': 'b02611b5f320b946fb3f78daed0cccdb6ec0c1925e1b1c0ad8783fb6f6e03fc0',
                                                                        'NegativeMutantSettledOnMotion.update': '4889142789c8c486d9188b7edd67a7d45977fc2a06f016e6231d4c43c6c45456',
                                                                        'NegativeMutantSettledWhenStale': 'bb695a254fde9ebdd8cae88e586e9e5541dc218f8e340e6d664351576c8eab82',
                                                                        'NegativeMutantSettledWhenStale.is_settled': 'a7a2a6717e58adde63e7303dc03202aa9977f7ca1d1540599254013927e4f09e',
                                                                        'Observation': 'f6e346eadf91e5c84a10389a5549401074106b8c30da8be5c4857560c014b4f0',
                                                                        'PerceptionResult': 'f2c53b935e12144c54123d5c53cb5a9587fc6429fd9893a455e41a04c10ee697',
                                                                        'Receipt': '3aa66466e1e6fef2ddaa1e51665c9f04f362ae3a3ee8bddbafb943b85c433ad9',
                                                                        'ReferenceSettleChain': '7541e3f45c91bd4e0ca5e7323764b2f806a892dc3537d74551e752cad85452df',
                                                                        'ReferenceSettleChain.__init__': '1ca7b74a7f29a559f85adad1260acd5506f726f356a09f64e9515633b1947c42',
                                                                        'ReferenceSettleChain.is_settled': '90d93f1ab8e044fe0043194558d20fb72dd2faa295990b81a3cee4bd749c729b',
                                                                        'ReferenceSettleChain.update': '68bf5c14db69dcfe7f918781d968b5dfd2eac1912ad5098d7967b787d4d2333a',
                                                                        'StreamSpec': 'f8efc9466a50bfa2b05b049b3bd53a46408addfae6f7a92035aca43eac80a42c',
                                                                        'StreamToken':
                                                                            '65ab0d7501b4aaee8c2eb708aa6cf20eb3450b25a3e007f5e6df483dbd355a5b',
                                                                        'TargetRecord': '87266c258ea7c8e2353dbec030e306bb8d4bbce38d38d92391e82b0255020349',
                                                                        'TestMultiWindowChainSettleOracleOwner': 'f77211d3f3db03b9e811364b0d2116f8fadc92a8b74dc380474d0874a7dcc6f6',
                                                                        'TestMultiWindowChainSettleOracleOwner.test_capsule_and_child_screen_canonical_destination': '66c279e92d77af307bc30b89281ba006beaf792a7cc037b763999e3aeb62d585',
                                                                        'TestMultiWindowChainSettleOracleOwner.test_gate_m5_5_missing_evidence_unsettled': 'b74a39472886bbbb1b5916ec05af051370f4b1dac89595eb1041959f950dfa4c',
                                                                        'TestMultiWindowChainSettleOracleOwner.test_gate_m5_5_regressing_time_or_none_time_fails_settle': '4e573f2bb6c8082e296f2aed3d7019abdc749c4b03319423d8d604dd3c5d2690',
                                                                        'TestMultiWindowChainSettleOracleOwner.test_gate_m5_5_settle_fails_after_motion': '8c16cb445173568f90272233a87b13563af0abd7b4a56677668b6863b26ba701',
                                                                        'TestMultiWindowChainSettleOracleOwner.test_gate_m5_5_settle_fails_after_sequence_gap': '6bf85dfbea1b3a7f332e8958123054a671e0075165902def4a6020e5754e469c',
                                                                        'TestMultiWindowChainSettleOracleOwner.test_gate_m5_5_settle_fails_after_stale_frame': '38c872d408db19a407b452a245b3838ac0a9a6dfc0bdc2cd4d9a0072543e2afe',
                                                                        'TestMultiWindowChainSettleOracleOwner.test_gate_m5_5_valid_configured_quiet_run_settles': '3448c09ed00ab94359eeda6d05c0f4f301b4e175c993169aa9806e6ede5345a8',
                                                                        'TestMultiWindowChainSettleOracleOwner.test_inv_m5_1_handle_isolation_and_epoch_reset': '7e25f61794e473ce928211ef46918df2be1813ae47b50de8f4f9f2e6933d3b58',
                                                                        'TestMultiWindowChainSettleOracleOwner.test_settled_never_grants_action_permission': 'd50deb7c0700ee4cd878ab351388a2518a042f140998f3dbdd0c71658310deea',
                                                                        'TestMultiWindowChainSettleOracleOwner.test_supported_target_kinds': 'fc555ed1773a6282685cb785a743239c8d147b391f1f34780ea3b16723e4a1bc',
                                                                        'TestSettleContractHarness': '9d033f0aaf1c2e8e017a0be0398b7f347411a8c35498ad4d5b4c8a7a802a38c7',
                                                                        'TestSettleContractHarness.test_negative_mutant_settled_after_gap_rejected': '9a3ffd72f29363cc20bb1113366038fb391733f24ddb9ef4994d952a4163058a',
                                                                        'TestSettleContractHarness.test_negative_mutant_settled_on_missing_evidence_rejected': '8da128ef15fdea93d464aa277f88ecfd58e1e68db43f0ff72bd4659012ce3b2f',
                                                                        'TestSettleContractHarness.test_negative_mutant_settled_on_motion_rejected': 'a8832c1128c9b3e39ebfd09e46ecdebc4c3f807c7cfd16c3667a25aa0673b5d4',
                                                                        'TestSettleContractHarness.test_negative_mutant_settled_when_stale_rejected': '281e7c1079bda82dab2982952a87bbb24b6294fb7de0ff3c0a30393b57ac2e0f',
                                                                        'TestSettleContractHarness.test_positive_control_valid_quiet_run_settles': '9178aae406b2fd997f63b43274665cfb0a694193a5e27029b5d292427588ca16',
                                                                        'Ticket': '06c26d2cb9d168e0d5e48f186e97d07b846ed5f8c2a791fb41009ed7bebad2a4',
                                                                        'WindowDescription': '4a771b5777732cb78f7afbcb6a9ca9ca19bbce47abb50c37ae0fa904fcaa0322',
                                                                        'WindowFrames': 'a812ad3815ada1a1b64529309d22f5ddef956b01d359f84539a099e74e54cdba',
                                                                        '_has_owner_symbol': 'afaa92240c702727966ff17ea2a1780b9da88cd6884cecfd6f51c675ac09b14c',
                                                                        'expected_failure_if_missing': 'c85155204d5d20743e260f73aa862c75aac654c515ac54c95a4cc684ea3bbd63',
                                                                        'expected_failure_if_missing.decorator': 'fdfa4ba263ccfd659606a3eaf8382bfd6c276e44245ed85657d903941b3d4f9f',
                                                                        'make_dummy_frame': 'e538935b0a19574442259ca91a1d60706e847d4cbb9221807036045af2f8a220',
                                                                        'make_dummy_observation': 'ff39552dbbda8dcbdcbe6469faf12e123f15cf7d5457751cdf0aa736f0f0a176',
                                                                        'verify_gate_m5_5_contract': '55bd68f2c748f2b5b30c8c0fb78d6e534b445a8ef78b9bae417d0969bee55bfa'},
                                                        'file_sha256': '43792ed700f1dadcc174e5e9ab2a7d0240fb3756135ede7ca2d737f148980c61',
                                                        'methods': ('tests.test_jq2a_m5_j5_multiwin_chain_settle_oracle.TestMultiWindowChainSettleOracleOwner.test_capsule_and_child_screen_canonical_destination',
                                                                    'tests.test_jq2a_m5_j5_multiwin_chain_settle_oracle.TestMultiWindowChainSettleOracleOwner.test_gate_m5_5_missing_evidence_unsettled',
                                                                    'tests.test_jq2a_m5_j5_multiwin_chain_settle_oracle.TestMultiWindowChainSettleOracleOwner.test_gate_m5_5_regressing_time_or_none_time_fails_settle',
                                                                    'tests.test_jq2a_m5_j5_multiwin_chain_settle_oracle.TestMultiWindowChainSettleOracleOwner.test_gate_m5_5_settle_fails_after_motion',
                                                                    'tests.test_jq2a_m5_j5_multiwin_chain_settle_oracle.TestMultiWindowChainSettleOracleOwner.test_gate_m5_5_settle_fails_after_sequence_gap',
                                                                    'tests.test_jq2a_m5_j5_multiwin_chain_settle_oracle.TestMultiWindowChainSettleOracleOwner.test_gate_m5_5_settle_fails_after_stale_frame',
                                                                    'tests.test_jq2a_m5_j5_multiwin_chain_settle_oracle.TestMultiWindowChainSettleOracleOwner.test_gate_m5_5_valid_configured_quiet_run_settles',
                                                                    'tests.test_jq2a_m5_j5_multiwin_chain_settle_oracle.TestMultiWindowChainSettleOracleOwner.test_inv_m5_1_handle_isolation_and_epoch_reset',
                                                                    'tests.test_jq2a_m5_j5_multiwin_chain_settle_oracle.TestMultiWindowChainSettleOracleOwner.test_settled_never_grants_action_permission',
                                                                    'tests.test_jq2a_m5_j5_multiwin_chain_settle_oracle.TestMultiWindowChainSettleOracleOwner.test_supported_target_kinds',
                                                                    'tests.test_jq2a_m5_j5_multiwin_chain_settle_oracle.TestSettleContractHarness.test_negative_mutant_settled_after_gap_rejected',
                                                                    'tests.test_jq2a_m5_j5_multiwin_chain_settle_oracle.TestSettleContractHarness.test_negative_mutant_settled_on_missing_evidence_rejected',
                                                                    'tests.test_jq2a_m5_j5_multiwin_chain_settle_oracle.TestSettleContractHarness.test_negative_mutant_settled_on_motion_rejected',
                                                                    'tests.test_jq2a_m5_j5_multiwin_chain_settle_oracle.TestSettleContractHarness.test_negative_mutant_settled_when_stale_rejected',
                                                                    'tests.test_jq2a_m5_j5_multiwin_chain_settle_oracle.TestSettleContractHarness.test_positive_control_valid_quiet_run_settles')}}


def setUpModule():
    global _gate_bytecode_flag
    _gate_bytecode_flag = sys.dont_write_bytecode
    sys.dont_write_bytecode = True


def tearDownModule():
    sys.dont_write_bytecode = _gate_bytecode_flag


def rgb(value=40, width=16, height=16):
    return np.full((height, width, 3), value, dtype=np.uint8)


def tiles_for(frame, cls=0):
    grid = Grid(frame.shape[1], frame.shape[0])
    tiles, _ = desc.describe_padded(grid.pad(frame), grid, lazy_xh=True)
    tiles["cls"] = cls
    return tiles


def canvas_rgb(seed):
    """Small deterministic three-state pixel witness; no image files involved."""
    x = np.arange(16, dtype=np.uint32)[None, :]
    y = np.arange(16, dtype=np.uint32)[:, None]
    z = (x * np.uint32(374761393)) ^ (y * np.uint32(668265263)) ^ np.uint32(seed)
    z = (z ^ (z >> np.uint32(13))) * np.uint32(1274126177)
    z ^= z >> np.uint32(16)
    grey = (z & np.uint32(31)).astype(np.uint8)
    bands = ((x // 8) % 3).astype(np.uint8) + ((y // 13) % 3).astype(np.uint8)
    np.subtract(bands, 3, out=bands, where=bands >= 3)
    grey += bands * np.uint8(23)
    grey += np.uint8(45)
    return np.stack((grey, grey + np.uint8(12), grey + np.uint8(24)), axis=2)


class CountedGM:
    """Two separately charged GM calls with an independent known sum (7+11)."""
    def __init__(self, malformed=False):
        self.counter = ContextVar("r7_gm_ops", default=0)
        self.calls = []
        self.malformed = malformed

    @property
    def OPS(self):
        return self.counter.get()

    def estimate_global_motion(self, previous, current, max_d):
        self.calls.append(("estimate", max_d))
        self.counter.set(7)
        return (1, 0)

    def consensus_tiles(self, previous, current, side, dx, dy):
        self.calls.append(("consensus", side, dx, dy))
        self.counter.set(11)
        shape = (current.shape[0] // side, current.shape[1] // side)
        return np.ones((shape[0] * shape[1],) if self.malformed else shape,
                       dtype=np.bool_)


class RecordingVeto:
    def __init__(self):
        self.instances = []
        self.calls = []

    def __call__(self, grid):
        owner = self
        self.instances.append(grid)

        class StreamVeto:
            def update(self, tiles, frame, *, frame_id):
                owner.calls.append((frame_id, tiles.dtype, tiles.shape))
                return fl_v3.VetoDecision(np.zeros(grid.N, dtype=np.bool_),
                                         np.zeros(grid.N, dtype=np.uint8))
        return StreamVeto()


class NoMotion:
    OPS = 0

    @staticmethod
    def estimate_global_motion(previous, current, max_d):
        return None

    @staticmethod
    def consensus_tiles(previous, current, side, dx, dy):
        raise AssertionError("zero-motion fixture must not request consensus")


class AllFlicker:
    class FlickerTracker:
        def __init__(self, n_tiles, max_half=64):
            self.n_tiles = n_tiles

        def update(self, hashes):
            return (np.full(self.n_tiles, 4, dtype=np.int64),
                    np.zeros(self.n_tiles, dtype=np.int64))


class AnchorError(Exception):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = status


def ast_key(node):
    return ast.dump(node, include_attributes=False)


def one(nodes, description):
    if len(nodes) != 1:
        status = "ANCHOR_MISSING" if not nodes else "ANCHOR_NONUNIQUE"
        raise AnchorError(status, description + ": " + str(len(nodes)))
    return nodes[0]


def child_scopes(node):
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            yield child
        else:
            yield from child_scopes(child)


def qualified_scope(tree, names):
    scope = tree
    for name in names:
        scope = one([node for node in child_scopes(scope) if node.name == name],
                    ".".join(names) + " / " + name)
    return scope


def scope_nodes(node):
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        yield child
        yield from scope_nodes(child)


@dataclass(frozen=True)
class Site:
    catalogue: str
    scope: tuple
    expression: str
    selector: str
    line: int


GM_SITE = Site(
    "M-eval-v2x-per-window-detector-1",
    ("PerWindowDetector", "_step", "gm_call"),
    "None if ops is None or second is None else ops + second",
    "Assign.value.orelse.op:Add->Sub", 260,
)
TILE_SITE = Site(
    "M-eval-v2x-per-window-detector-2",
    ("PerWindowDetector", "update"),
    'not isinstance(tiles, np.ndarray) or tiles.dtype != desc.TILE_DTYPE '
    'or tiles.shape != (grid.N,) or not np.array_equal(tiles["morton"], grid.mortons)',
    "If.test.op:Or->And", 154,
)


def mutated_tree(source, site, *, identity=False):
    tree = ast.parse(source)
    scope = qualified_scope(tree, site.scope)
    expression = ast_key(ast.parse(site.expression, mode="eval").body)
    if site.catalogue == GM_SITE.catalogue:
        matches = [n for n in scope_nodes(scope)
                   if isinstance(n, ast.Assign) and len(n.targets) == 1
                   and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "ops"
                   and ast_key(n.value) == expression]
        node = one(matches, site.catalogue + " complete assignment expression")
        operation = node.value.orelse
        if not isinstance(operation, ast.BinOp) or not isinstance(operation.op, ast.Add):
            raise AnchorError("ANCHOR_MISSING", site.selector)
        if not identity:
            operation.op = ast.Sub()
    elif site.catalogue == TILE_SITE.catalogue:
        node = one([n for n in scope_nodes(scope)
                    if isinstance(n, ast.If) and ast_key(n.test) == expression],
                   site.catalogue + " complete input guard")
        if not isinstance(node.test, ast.BoolOp) or not isinstance(node.test.op, ast.Or):
            raise AnchorError("ANCHOR_MISSING", site.selector)
        if not identity:
            node.test.op = ast.And()
    else:
        raise AnchorError("ANCHOR_MISSING", "unknown catalogue site")
    return ast.fix_missing_locations(tree), node.lineno


@contextmanager
def private_module(tree):
    """Compile in RAM; preserve package registration, bytecode flag and modules."""
    parent = importlib.import_module("eval.v2x")
    old_module = sys.modules.get(PRIVATE_NAME, _MISSING)
    old_attribute = getattr(parent, PRIVATE_ATTR, _MISSING)
    old_flag = sys.dont_write_bytecode
    module = ModuleType(PRIVATE_NAME)
    module.__file__ = str(SOURCE)
    module.__package__ = "eval.v2x"
    module.__spec__ = importlib.util.spec_from_loader(PRIVATE_NAME, loader=None,
                                                     origin=str(SOURCE))
    module.__cached__ = None
    try:
        sys.dont_write_bytecode = True
        sys.modules[PRIVATE_NAME] = module
        setattr(parent, PRIVATE_ATTR, module)
        exec(compile(tree, str(SOURCE), "exec"), module.__dict__)
        yield module
    finally:
        if old_module is _MISSING:
            sys.modules.pop(PRIVATE_NAME, None)
        else:
            sys.modules[PRIVATE_NAME] = old_module
        if old_attribute is _MISSING:
            if hasattr(parent, PRIVATE_ATTR):
                delattr(parent, PRIVATE_ATTR)
        else:
            setattr(parent, PRIVATE_ATTR, old_attribute)
        sys.dont_write_bytecode = old_flag


@dataclass(frozen=True)
class Outcome:
    status: str
    detail: str = ""
    anchor_line: int | None = None
    counterexample: dict | None = None


def exercise(module, prepare, assertion, *, positive):
    """Constructor/setup exceptions can never be behavioral mutation kills."""
    try:
        case = prepare(module)
    except TimeoutError as error:
        return Outcome("POSITIVE_TIMEOUT" if positive else "TIMEOUT", str(error))
    except Exception as error:
        return Outcome("POSITIVE_SETUP_ERROR" if positive else "SETUP_ERROR",
                       type(error).__name__ + ": " + str(error))
    try:
        assertion(module, case)
    except AssertionError as error:
        return Outcome("POSITIVE_FAILED" if positive else "KILLED",
                       str(error), counterexample=case.get("replay"))
    except TimeoutError as error:
        return Outcome("POSITIVE_TIMEOUT" if positive else "TIMEOUT", str(error))
    except Exception as error:
        return Outcome("POSITIVE_ERROR" if positive else "UNRELATED_EXCEPTION",
                       type(error).__name__ + ": " + str(error))
    return Outcome("POSITIVE_PASSED" if positive else "SURVIVED")


def contrast(site, prepare, assertion, *, source=None, identity=False):
    """Identical fixture and assertion on real first, then on one private mutant."""
    positive = Context().run(exercise, real_detector, prepare, assertion, positive=True)
    if positive.status != "POSITIVE_PASSED":
        return positive
    source = SOURCE.read_text(encoding="utf-8") if source is None else source
    try:
        tree, line = mutated_tree(source, site, identity=identity)
    except AnchorError as error:
        return Outcome(error.status, str(error))
    if ast_key(tree) == ast_key(ast.parse(source)):
        return Outcome("EQUIVALENT", "structurally identical AST", line)
    try:
        with private_module(tree) as module:
            result = Context().run(exercise, module, prepare, assertion, positive=False)
    except Exception as error:
        return Outcome("IMPORT_OR_COMPILE_ERROR", type(error).__name__ + ": " + str(error), line)
    return Outcome(result.status, result.detail, line, result.counterexample)


def gm_case(module):
    motion = CountedGM()
    factory = RecordingVeto()
    detector = module.PerWindowDetector(gm=motion, veto=factory)
    handle = module.Handle("cdp_tab", "gm:sum", 0)
    before, after = rgb(20), rgb(21)
    first = detector.update(handle, tiles_for(before), before, epoch=0, frame_seq=0)
    result = detector.update(handle, tiles_for(after), after, epoch=0, frame_seq=1)
    return {"first": first, "result": result, "motion": motion, "factory": factory,
            "replay": {"seed": REPLAY_SEED, "width": 16, "height": 16,
                       "values": [20, 21], "estimator_ops": 7, "consensus_ops": 11}}


def assert_gm_sum(module, case):
    # Pure observations only: lazy constructors and production execution have
    # already completed in gm_case, where AssertionError is a SETUP_ERROR.
    first, result = case["first"], case["result"]
    assert isinstance(first, module.ChangeResult)
    assert isinstance(result, module.ChangeResult)
    assert case["motion"].calls == [("estimate", 15), ("consensus", 16, 1, 0)]
    assert result.ops["gm"] == 18, "independent charge: 7 + 11 must equal 18"
    assert case["motion"].OPS == 0, "copied plugin context must not leak"
    assert result.claims["cls"].tolist() == [3]
    assert result.claims["mv_dx"].tolist() == [1]
    assert result.settled is False


def morton_case(module):
    frame = rgb(40, width=32)
    tiles = tiles_for(frame)
    tiles["morton"] = tiles["morton"][::-1]
    factory = RecordingVeto()
    detector = module.PerWindowDetector(veto=factory)
    handle = module.Handle("godot_window", "tiles:order", 0)
    caught = None
    try:
        detector.update(handle, tiles, frame, epoch=0, frame_seq=0)
    except module.DetectorInputError as error:
        caught = error
    return {"caught": caught, "tiles": tiles, "factory": factory,
            "replay": {"seed": REPLAY_SEED, "width": 32, "height": 16,
                       "morton": [1, 0], "invalid_condition": "Morton order only"}}


def assert_morton_guard(module, case):
    tiles, caught = case["tiles"], case["caught"]
    assert isinstance(tiles, np.ndarray)
    assert tiles.dtype == desc.TILE_DTYPE
    assert tiles.shape == (2,)
    assert tiles["morton"].tolist() == [1, 0]
    assert type(caught) is module.DetectorInputError, "exact tile guard must reject before downstream evidence"
    assert str(caught) == "tiles must match the exact S2 grid, dtype and Morton order"
    assert case["factory"].instances == [], "invalid tiles must precede any stream/plugin construction"


def definitions(source):
    """Match the pre-edit inventory's qualified definitions and exact digests."""
    lines = source.splitlines(keepends=True)
    found = {}

    def visit(node, prefix=""):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + child.name
                start = min([child.lineno] + [d.lineno for d in child.decorator_list]) - 1
                exact = "".join(lines[start:child.end_lineno])
                found[name] = hashlib.sha256(exact.encode()).hexdigest()
                visit(child, name + ".")
            else:
                visit(child, prefix)
    visit(ast.parse(source))
    return found


def flattened(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flattened(item)
        else:
            yield item


class TestOriginalMethods(unittest.TestCase):
    def test_every_original_id_and_affected_helper_has_clean_inner_result(self):
        all_ids = []
        changed = {}
        failure_decorators = []
        for module_name, before in PRE_EDIT_MANIFEST.items():
            module = importlib.import_module(module_name)
            current = definitions(Path(module.__file__).read_text(encoding="utf-8"))
            delta = sorted(name for name in set(current) | set(before["definitions"])
                           if current.get(name) != before["definitions"].get(name))
            changed[module_name] = delta
            current_suite = unittest.TestLoader().loadTestsFromModule(module)
            current_ids = [case.id() for case in flattened(current_suite)]
            original_ids = list(before["methods"])
            self.assertCountEqual(current_ids, original_ids,
                                  "no original ID can disappear behind a helper/factory edit")
            for case in flattened(unittest.TestLoader().loadTestsFromModule(module)):
                fn = getattr(type(case), case._testMethodName)
                for obj in (fn,) + type(case).__mro__:
                    if (getattr(obj, "__unittest_skip__", False)
                            or getattr(obj, "__unittest_expecting_failure__", False)):
                        failure_decorators.append(case.id())
            # Any definition delta (including a shared setUp/helper) requires the
            # entire original module's exact ID set, not a guessed call graph.
            all_ids.extend(original_ids)

        self.assertEqual(len(all_ids), 43)
        self.assertEqual(len(set(all_ids)), 43)
        result = unittest.TestResult()
        loader = unittest.TestLoader()
        executed = []
        for test_id in all_ids:
            suite = loader.loadTestsFromName(test_id)
            loaded_ids = [case.id() for case in flattened(suite)]
            self.assertEqual(loaded_ids, [test_id], "exact original ID resolution")
            executed.extend(loaded_ids)
            suite.run(result)
        self.assertEqual(loader.errors, [])
        self.assertEqual(result.testsRun, len(all_ids))
        print("R7 inner outcomes: " + json.dumps({
            "run": result.testsRun,
            **{name: len(getattr(result, name)) for name in
               ("failures", "errors", "skipped", "expectedFailures", "unexpectedSuccesses")},
        }, sort_keys=True))
        for field in ("failures", "errors", "skipped", "expectedFailures", "unexpectedSuccesses"):
            self.assertEqual(getattr(result, field), [], field)
        self.assertEqual(failure_decorators, [], "no active failure/skip decorators")
        self.assertCountEqual(executed, all_ids)
        for module_name, delta in changed.items():
            self.assertTrue(set(PRE_EDIT_MANIFEST[module_name]["methods"]).issubset(executed),
                            "uncovered changed definitions: " + repr(delta))
        print("R7 original-method replay: 43 passed; failures/errors/skips/xfails/xpasses=0")


class TestRealFixtures(unittest.TestCase):
    def test_canonical_handles_zero_epoch_and_single_condition_refusals(self):
        for kind in ("cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window"):
            for generation in (0, 2**85):
                with self.subTest(kind=kind, generation=generation):
                    handle = Handle(kind, "same:destination", generation)
                    frame = rgb()
                    result = real_detector.PerWindowDetector().update(
                        handle, tiles_for(frame), frame, epoch=0, frame_seq=0)
                    self.assertIsInstance(result, ChangeResult)
                    self.assertEqual(result.handle, handle)
                    self.assertEqual(result.epoch, 0)
        for args in (("unknown", "id", 0), ("cdp_tab", "", 0),
                     ("cdp_tab", "id", -1), ("cdp_tab", "id", True)):
            with self.subTest(invalid=args):
                with self.assertRaises(real_detector.DetectorInputError):
                    Handle(*args)
        with self.assertRaisesRegex(real_detector.DetectorInputError,
                                    "veto must be an independent per-stream factory"):
            real_detector.PerWindowDetector(veto=object())
        frame = rgb()
        for epoch in (-1, True):
            with self.assertRaisesRegex(real_detector.DetectorInputError, "epoch must be a nonnegative integer"):
                real_detector.PerWindowDetector().update(
                    Handle("cdp_tab", "epoch", 0), tiles_for(frame), frame,
                    epoch=epoch, frame_seq=0)

    def test_repaired_descriptor_reaches_gm_consensus_guard_and_cleans_lane(self):
        handle = Handle("cdp_tab", "fixture:guard", 0)
        first, second = rgb(10, width=32), rgb(30, width=32)
        gm = CountedGM(malformed=True)
        detector = real_detector.PerWindowDetector(gm=gm)
        with self.assertRaisesRegex(real_detector.DetectorInputError,
                                    "tiles must match the exact S2 grid, dtype and Morton order"):
            detector.update(handle, np.zeros((4, 4), np.uint8), first, epoch=0, frame_seq=0)
        self.assertEqual(gm.calls, [])
        self.assertEqual(detector._lanes, {})
        valid = tiles_for(first)
        grid = Grid(32, 16)
        self.assertEqual(valid.dtype, desc.TILE_DTYPE)
        self.assertEqual(valid.shape, (grid.N,))
        np.testing.assert_array_equal(valid["morton"], grid.mortons)
        detector.update(handle, valid, first, epoch=0, frame_seq=0)
        with self.assertRaisesRegex(real_detector.MissingEvidenceError, "^GM consensus must match grid$"):
            detector.update(handle, tiles_for(second), second, epoch=0, frame_seq=1)
        self.assertEqual(gm.calls, [("estimate", 31), ("consensus", 16, 1, 0)])
        self.assertIsNone(detector._lanes[handle].stream)
        recovered = detector.update(handle, tiles_for(second), second, epoch=0, frame_seq=2)
        self.assertFalse(recovered.settled)
        self.assertEqual(recovered.ops["gm"], 0)

    def test_tile_negatives_change_one_condition_and_reject_before_plugins(self):
        frame = rgb(width=32)
        valid = tiles_for(frame)

        class NonArray:
            dtype = valid.dtype
            shape = valid.shape

            def __getitem__(self, name):
                return valid[name]

        cases = {"ndarray_type": NonArray(),
                 "dtype": valid.astype([(name, 'i8') for name in valid.dtype.names]),
                 "shape": valid.reshape(1, -1),
                 "morton": valid.copy()}
        cases["morton"]["morton"] = valid["morton"][::-1]
        for name, invalid in cases.items():
            with self.subTest(condition=name):
                factory = RecordingVeto()
                detector = real_detector.PerWindowDetector(veto=factory)
                with self.assertRaisesRegex(real_detector.DetectorInputError,
                                            "tiles must match the exact S2 grid, dtype and Morton order"):
                    detector.update(Handle("cdp_tab", "tiles", 0), invalid, frame,
                                    epoch=0, frame_seq=0)
                self.assertEqual(factory.instances, [])

    def test_real_fl_reaches_missing_history_after_dtype_repair(self):
        frame = rgb(80)
        tiles = tiles_for(frame, cls=4)
        grid = Grid(16, 16)
        words = np.zeros(grid.N, dtype=np.bool_)
        with self.assertRaisesRegex(TypeError, "s2_tiles must be one-dimensional TILE_DT"):
            fl_v3.veto_flicker(np.zeros(grid.N, np.uint8), None, None, words,
                              (grid.s, grid.gw, grid.gh))
        result = fl_v3.veto_flicker(tiles, None, None, words, (grid.s, grid.gw, grid.gh))
        self.assertEqual(result.mask.tolist(), [False])
        self.assertEqual(result.reasons.tolist(), [int(fl_v3.Reason.MISSING_HISTORY)])
        np.testing.assert_array_equal(tiles["cls"], [4])

    def test_eager_hashes_preserve_chroma_and_owned_rgb_history(self):
        seen = []

        class CaptureTracker(fl_v3.FlickerTracker):
            def update(self, hashes):
                seen.append(hashes.copy())
                return super().update(hashes)

        seam = SimpleNamespace(FlickerTracker=CaptureTracker, update_with_ops=fl_v3.update_with_ops)
        detector = real_detector.PerWindowDetector(fl=seam)
        handle = Handle("x11_window", "shadow:chroma", 0)
        original = rgb(100)
        chroma = original.copy()
        chroma[:, :, 0] = 110
        chroma[:, :, 2] = 90
        np.testing.assert_array_equal(desc.luma_of_padded(original), desc.luma_of_padded(chroma))
        lazy = tiles_for(original)
        self.assertEqual(lazy["xh64"].tolist(), [0])
        detector.update(handle, lazy, original, epoch=0, frame_seq=0)
        original[:] = 240
        lazy["xh64"] = 999
        held = rgb(100)
        result = detector.update(handle, tiles_for(held), held, epoch=0, frame_seq=1)
        self.assertEqual(result.ops["gm"], 0)
        np.testing.assert_array_equal(seen[0], seen[1])
        detector.update(handle, tiles_for(chroma), chroma, epoch=0, frame_seq=2)
        self.assertTrue(np.all(seen[0] != 0))
        self.assertTrue(np.all(seen[2] != seen[1]), "same luma must not erase chroma evidence")

    def test_gm_and_baseline_motion_keep_priority_over_positive_fl(self):
        handle = Handle("godot_window", "shadow:priority", 0)
        first, second = rgb(11), rgb(22)
        d = real_detector.PerWindowDetector(gm=CountedGM(), fl=AllFlicker)
        initial = d.update(handle, tiles_for(first), first, epoch=0, frame_seq=0)
        self.assertEqual(initial.claims["cls"].tolist(), [4], "FL seam must actually assert flicker")
        final = d.update(handle, tiles_for(second), second, epoch=0, frame_seq=1)
        self.assertEqual(final.claims["cls"].tolist(), [3])
        self.assertEqual(final.claims["mv_dx"].tolist(), [1])
        self.assertFalse(final.settled)
        for baseline_class, expected in ((0, 4), (3, 3)):
            tiles = tiles_for(first, cls=baseline_class)
            tiles["mv_dx"] = 7
            result = real_detector.PerWindowDetector(gm=NoMotion(), fl=AllFlicker).update(
                handle, tiles, first, epoch=0, frame_seq=0)
            self.assertEqual(result.claims["cls"].tolist(), [expected])
            self.assertEqual(result.claims["mv_dx"].tolist(), [7 if baseline_class == 3 else 0])

    def test_interleaved_exact_return_and_three_state_veto_use_real_results(self):
        a = Handle("cdp_tab", "veto:a", 0)
        b = Handle("cdp_tab", "veto:b", 0)
        # One condition distinguishes the exact-return preservation from the
        # three-distinct-frame P2 veto; all input descriptors have real provenance.
        for seeds, expected in (((0, 1, 0), 4), ((0, 1, 2), 2)):
            frames = tuple(canvas_rgb(seed) for seed in seeds)
            with self.subTest(expected_class=expected, replay_seeds=seeds):
                shared = real_detector.PerWindowDetector()
                solo = real_detector.PerWindowDetector()
                for seq, frame in enumerate(frames):
                    cls = 4 if seq == 2 else 2
                    actual = shared.update(a, tiles_for(frame, cls), frame, epoch=0, frame_seq=seq)
                    other = rgb(60 + seq)
                    shared.update(b, tiles_for(other), other, epoch=0, frame_seq=seq)
                    isolated = solo.update(a, tiles_for(frame, cls), frame, epoch=0, frame_seq=seq)
                self.assertEqual(actual.claims["cls"].tolist(), [expected])
                np.testing.assert_array_equal(actual.claims, isolated.claims)
                self.assertEqual(actual.ops, isolated.ops)
                self.assertFalse(actual.settled)

    def test_seeded_interleaving_matches_same_phase_replay(self):
        rng = np.random.default_rng(REPLAY_SEED)
        a = Handle("cdp_tab", "random:shared", 0)
        b = Handle("godot_window", "random:shared", 2**85)
        failures = []
        for width in (16, 32):
            stream_a = [rgb(v, width=width) for v in ([37] * 8 + [189] * 8) * 2]
            stream_b = [rgb(int(v), width=width) for v in rng.integers(1, 255, size=32)]
            serial = []
            for handle, frames in ((a, stream_a), (b, stream_b)):
                detector = real_detector.PerWindowDetector()
                serial.append([detector.update(handle, tiles_for(frame), frame, epoch=0, frame_seq=i)
                               for i, frame in enumerate(frames)])
            shared = real_detector.PerWindowDetector()
            for i, frames in enumerate(zip(stream_a, stream_b)):
                order = (0, 1) if rng.integers(0, 2) == 0 else (1, 0)
                for j in order:
                    frame, handle = frames[j], (a, b)[j]
                    result = shared.update(handle, tiles_for(frame), frame, epoch=0, frame_seq=i)
                    wanted = serial[j][i]
                    if (not np.array_equal(result.claims, wanted.claims)
                            or result.ops != wanted.ops or result.settled != wanted.settled):
                        failures.append({"seed": REPLAY_SEED, "width": width, "height": 16,
                                         "frame_seq": i, "stream": j,
                                         "value": int(frame[0, 0, 0]), "order": order})
            self.assertEqual(serial[0][24].claims["cls"].tolist(), [4] * (width // 16))
        smallest = min(failures, key=lambda x: (x["width"], x["frame_seq"], x["stream"])) if failures else None
        self.assertEqual(failures, [], "seed=" + str(REPLAY_SEED) + "; smallest_counterexample=" + repr(smallest))

    def test_settle_reset_guards_change_one_target_condition(self):
        a = Handle("capsule", "same:resource", 0)
        b = Handle("xvfb_screen", "same:resource", 0)
        frame = rgb(41)
        detector = real_detector.PerWindowDetector()
        a_states, b_states = [], []
        for seq in range(4):
            a_states.append(detector.update(a, tiles_for(frame), frame, epoch=0, frame_seq=seq).settled)
            b_states.append(detector.update(b, tiles_for(frame), frame, epoch=0, frame_seq=seq).settled)
        self.assertEqual(a_states, [False, False, False, True])
        self.assertEqual(b_states, [False, False, False, True])
        self.assertFalse(detector.update(a, tiles_for(frame), frame, epoch=0, frame_seq=5).settled)
        self.assertTrue(detector.update(b, tiles_for(frame), frame, epoch=0, frame_seq=4).settled)
        self.assertFalse(detector.update(b, tiles_for(frame), frame, epoch=1, frame_seq=5).settled)
        detector.reset_window(a)
        self.assertFalse(detector.update(a, tiles_for(frame), frame, epoch=0, frame_seq=0).settled)
        with self.assertRaisesRegex(real_detector.DetectorInputError, "non-increasing local frame_seq"):
            detector.update(a, tiles_for(frame), frame, epoch=0, frame_seq=0)


class TestContextCounters(unittest.TestCase):
    def test_real_context_counters_nested_exception_and_barrier_threads(self):
        def isolated():
            fl_blinkprior.reset_ops()
            fl_blinkprior.FlickerTracker(2).update([5, 7])
            parent_count = fl_blinkprior.OPS
            self.assertEqual(parent_count, 18)
            barrier = Barrier(2, timeout=3)
            results, errors = Queue(), Queue()

            class Racing(fl_v3.FlickerTracker):
                def update(self, hashes):
                    barrier.wait(timeout=3)
                    result = super().update(hashes)
                    barrier.wait(timeout=3)
                    return result

            def worker(n):
                try:
                    tracker = Racing(n)
                    first = fl_v3.update_with_ops(tracker, [3] * n)[1]
                    second = fl_v3.update_with_ops(tracker, [3] * n)[1]
                    results.put((n, first, second, fl_blinkprior.OPS))
                except BaseException as error:
                    errors.put(error)

            threads = [Thread(target=worker, args=(n,), name="r7-fl-" + str(n), daemon=True)
                       for n in (1, 5)]
            try:
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=5)
            finally:
                barrier.abort()
                for thread in threads:
                    thread.join(timeout=5)
            self.assertEqual([t.name for t in threads if t.is_alive()], [], "worker leak")
            if not errors.empty():
                raise errors.get()
            got = sorted(results.get_nowait() for _ in range(results.qsize()))
            self.assertEqual(got, [(1, 9, 14, 0), (5, 45, 70, 0)])
            self.assertEqual(fl_blinkprior.OPS, parent_count)
            with fl_v3.ops_scope() as outer:
                fl_v3.FlickerTracker(1).update([11])
                self.assertEqual(outer(), 9)
                with fl_v3.ops_scope() as inner:
                    fl_v3.FlickerTracker(3).update([1, 2, 3])
                    self.assertEqual(inner(), 27)
                self.assertEqual(outer(), 9)
            self.assertEqual(fl_blinkprior.OPS, parent_count)

            class Explodes(fl_v3.FlickerTracker):
                def update(self, hashes):
                    super().update(hashes)
                    raise RuntimeError("intentional tracker exception")
            with self.assertRaisesRegex(RuntimeError, "intentional tracker exception"):
                fl_v3.update_with_ops(Explodes(1), [1])
            self.assertEqual(fl_blinkprior.OPS, parent_count)
        Context().run(isolated)


class TestCatalogueContrasts(unittest.TestCase):
    def test_named_catalogue_positives_kills_and_clean_teardown(self):
        source_before = SOURCE.read_bytes()
        module_before = sys.modules[real_detector.__name__]
        parent = importlib.import_module("eval.v2x")
        attribute_before = getattr(parent, PRIVATE_ATTR, _MISSING)
        private_before = sys.modules.get(PRIVATE_NAME, _MISSING)
        flag_before = sys.dont_write_bytecode
        context_before = dict(copy_context().items())
        disk_before = set(SOURCE.parent.rglob("*r7_private*"))
        outcomes = []
        for site, prepare, assertion in ((GM_SITE, gm_case, assert_gm_sum),
                                          (TILE_SITE, morton_case, assert_morton_guard)):
            outcome = contrast(site, prepare, assertion)
            outcomes.append(outcome)
            self.assertEqual(outcome.status, "KILLED", repr(outcome))
            self.assertEqual(outcome.anchor_line, site.line)
            self.assertIsNotNone(outcome.counterexample)
            print("R7 catalogue " + site.catalogue + ": positive=PASS mutant=KILLED "
                  + "line=" + str(outcome.anchor_line) + " replay="
                  + json.dumps(outcome.counterexample, sort_keys=True))
        self.assertEqual(Counter(x.status for x in outcomes), {"KILLED": 2})
        self.assertEqual(SOURCE.read_bytes(), source_before)
        self.assertIs(sys.modules[real_detector.__name__], module_before)
        self.assertIs(getattr(parent, PRIVATE_ATTR, _MISSING), attribute_before)
        self.assertIs(sys.modules.get(PRIVATE_NAME, _MISSING), private_before)
        self.assertEqual(sys.dont_write_bytecode, flag_before)
        self.assertEqual(dict(copy_context().items()), context_before)
        self.assertEqual(set(SOURCE.parent.rglob("*r7_private*")), disk_before)
        print("R7 mutation teardown: source/module/package/context/bytecode state restored")

    def test_non_kill_outcomes_are_separate_and_positive_failure_stops(self):
        calls = []

        def prepare(module):
            calls.append(module.__name__)
            return {"replay": {"seed": REPLAY_SEED}}

        def fail_positive(module, case):
            raise AssertionError("positive did not pass")
        outcome = contrast(GM_SITE, prepare, fail_positive)
        self.assertEqual(outcome.status, "POSITIVE_FAILED")
        self.assertEqual(calls, [real_detector.__name__])
        self.assertEqual(contrast(GM_SITE, prepare, lambda m, c: None).status, "SURVIVED")
        self.assertEqual(contrast(GM_SITE, prepare, lambda m, c: None, identity=True).status, "EQUIVALENT")

        def bad_constructor(module):
            if module.__name__ == PRIVATE_NAME:
                raise ValueError("constructor failure")
            return {}
        self.assertEqual(contrast(GM_SITE, bad_constructor, lambda m, c: None).status, "SETUP_ERROR")

        def asserting_constructor(module):
            if module.__name__ == PRIVATE_NAME:
                raise AssertionError("lazy constructor assertion is not a behavioral kill")
            return {}
        self.assertEqual(contrast(GM_SITE, asserting_constructor, lambda m, c: None).status, "SETUP_ERROR")

        def unrelated(module, case):
            if module.__name__ == PRIVATE_NAME:
                raise RuntimeError("unrelated failure")
        self.assertEqual(contrast(GM_SITE, prepare, unrelated).status, "UNRELATED_EXCEPTION")

        def timed_out(module, case):
            if module.__name__ == PRIVATE_NAME:
                raise TimeoutError("bounded fixture timeout")
        self.assertEqual(contrast(GM_SITE, prepare, timed_out).status, "TIMEOUT")
        source = SOURCE.read_text(encoding="utf-8")
        missing = source.replace("else ops + second", "else ops * second")
        self.assertEqual(contrast(GM_SITE, prepare, lambda m, c: None, source=missing).status, "ANCHOR_MISSING")
        line = "                            ops = None if ops is None or second is None else ops + second"
        duplicate = source.replace(line, line + "\n" + line)
        self.assertEqual(contrast(GM_SITE, prepare, lambda m, c: None, source=duplicate).status, "ANCHOR_NONUNIQUE")
        bad_import = "import _r7_deliberately_absent_module\n" + source
        self.assertEqual(contrast(GM_SITE, prepare, lambda m, c: None, source=bad_import).status,
                         "IMPORT_OR_COMPILE_ERROR")
        self.assertNotIn(PRIVATE_NAME, sys.modules)


if __name__ == "__main__":
    unittest.main()

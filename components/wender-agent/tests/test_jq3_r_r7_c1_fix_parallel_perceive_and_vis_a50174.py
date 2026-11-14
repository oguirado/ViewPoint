"""R7 C1: sealed fixture repairs, bound runner receipts and catalogue proofs.

All frames and mutant modules stay in memory.  The legacy inventory below is
from integration 9c418c4ab2cb4cc43af302a4c4d62639e4ce9bf7, before any repair.
Every original method is executed by its exact ID, including methods whose
shared fixture helpers changed.  The local FakePersistentRunner exercised by
the original J3 methods is not a production worker-pool API.
"""

import ast
from contextlib import contextmanager
import copy
from dataclasses import dataclass
import hashlib
import importlib
from pathlib import Path
import random
import signal
import sys
import threading
import types
import unittest

import numpy as np

from wender.perceive import parallel_perceive as parallel
from wender.perceive import vision_adapter as vision
from wender.perceive.session_registry import (
    FrameRef, Observation, PerceptionResult, SessionRegistry, WindowDescription, WindowFrames,
)
from wender.perceive.vision_engine_real import RealVisionEngine


ROOT = Path(__file__).resolve().parents[1]
SEED = 0xA50174
VALID_CASES = 64
SHRINK_LIMIT = 32
SCHEDULES = 32
WAIT_SECONDS = 2

# Populated from the pre-edit AST inventory, not from the repaired methods.
LEGACY_INVENTORY = {'tests/test_jq2a_m3_c2_parallel_perceive_implementation.py': {'sha256': '89e6b1caeccf551d5df6b2c61fae88c1f7418421063d0b82b711bb041c8216bd',
                                                               'methods': ['tests.test_jq2a_m3_c2_parallel_perceive_implementation.TestParallelPerceive.test_completion_reversal_preserves_input_order_and_serial_bytes',
                                                                           'tests.test_jq2a_m3_c2_parallel_perceive_implementation.TestParallelPerceive.test_sibling_window_cannot_supply_predecessor',
                                                                           'tests.test_jq2a_m3_c2_parallel_perceive_implementation.TestParallelPerceive.test_epoch_and_generation_split_temporal_sequences',
                                                                           'tests.test_jq2a_m3_c2_parallel_perceive_implementation.TestParallelPerceive.test_partial_runner_failure_keeps_completed_indices',
                                                                           'tests.test_jq2a_m3_c2_parallel_perceive_implementation.TestParallelPerceive.test_invalid_pixels_dpr_digest_and_sequences_are_typed_partial_errors',
                                                                           'tests.test_jq2a_m3_c2_parallel_perceive_implementation.TestParallelPerceive.test_pixels_are_owned_before_runner_executes',
                                                                           'tests.test_jq2a_m3_c2_parallel_perceive_implementation.TestParallelPerceive.test_same_target_fusion_and_sibling_ax_namespace',
                                                                           'tests.test_jq2a_m3_c2_parallel_perceive_implementation.TestParallelPerceive.test_direct_fusion_fails_closed_on_target_epoch_and_dpr_mismatch',
                                                                           'tests.test_jq2a_m3_c2_parallel_perceive_implementation.TestParallelPerceive.test_runner_and_adapter_errors_are_per_item_without_disk_io'],
                                                               'functions': {'DescriptorEngine.screen_fingerprint': 'fc26f95892251c715d32d83d918ccbd1f90623d2d861bc57d03290836b6a36f7',
                                                                             'ReverseRunner.map': 'c88b8607f5e6fcd0772f44d4be07c317d2ef22169e0ac0027020ea6de6085a1e',
                                                                             'MutatingRunner.__init__': '78e28f92b59885d1ae1dc7256cbb1b8282a12951c812a6df8feace125cf5947e',
                                                                             'MutatingRunner.map': 'e6a93824ff6b25ba2b908dd4820e5eca7d88b6c16281ef963cd74fd38799ed47',
                                                                             'PartialDeathRunner.map': '1cc7f2d0f34b072d895d8896a0a0322011bb127946fff8bcf72094beac1d6006',
                                                                             'observation': '98ea224bb92cd371e397529550033f6580c07c5b3d97dbc17945aee4e6194f97',
                                                                             'view': 'a26f8e3d89346521c7cc60c12fcb51044b23182723a9d4f67ca3bf623f034d66',
                                                                             'TestParallelPerceive.test_completion_reversal_preserves_input_order_and_serial_bytes': '7e45d83ee4ac3eada1a2766efa472ef3fcb075c1c25130813076df49fd659f15',
                                                                             'TestParallelPerceive.test_sibling_window_cannot_supply_predecessor': '32f5d83c3aa0f53786242fed8d2fbdb9ef0b52fdbaa683b1f32cb3be852f3847',
                                                                             'TestParallelPerceive.test_epoch_and_generation_split_temporal_sequences': '3e980f6c8ea3687286f88b455a06fa7e8bacd1afc58b6a0513966db7966588c0',
                                                                             'TestParallelPerceive.test_partial_runner_failure_keeps_completed_indices': 'ab37e9a4a616d05430b3c48f5a6d4d17b983c27f7ca9a4f9974e2595944f6f93',
                                                                             'TestParallelPerceive.test_invalid_pixels_dpr_digest_and_sequences_are_typed_partial_errors': 'd90bd309780bd388bf3875004cca09e83a24c28569c8a6c94b2c99be8cc5c8c1',
                                                                             'TestParallelPerceive.test_pixels_are_owned_before_runner_executes': '470b31f1486ce757f0fec69d7977d3c042328b8761adb19a2bc02f0d159f3292',
                                                                             'TestParallelPerceive.test_same_target_fusion_and_sibling_ax_namespace': '27e4438780c9f37353a7c473adbd59e7484058cb6bc9a449a333da512bc54754',
                                                                             'TestParallelPerceive.test_direct_fusion_fails_closed_on_target_epoch_and_dpr_mismatch': '22ef0dbd8b90e7ad3b15ba2a123d0c32c9e1b65ca9f46db8fb5193c504a7dcba',
                                                                             'TestParallelPerceive.test_runner_and_adapter_errors_are_per_item_without_disk_io': 'f0e6d30a14d00cf42978528d7c3d15459c944d53b0a3adc24e86d9d8b4ed3342',
                                                                             'TestParallelPerceive.test_runner_and_adapter_errors_are_per_item_without_disk_io.OneBroken.screen_identity': '2dcab51a2eb909b29960f5a0fb867ff37b5bb4dd662ed5102eacd1196b9b2892'}},
 'tests/test_jq2a_m3_j1_describe_many_windows_batch.py': {'sha256': '53918538266eb700bb4abcd4298a0561e4726b71a1d25ebe2e2884673ef6f0d3',
                                                          'methods': ['tests.test_jq2a_m3_j1_describe_many_windows_batch.TestJq2aM3J1DescribeManyWindowsBatch.test_healthy_control_dataclass_field_order_and_immutability',
                                                                      'tests.test_jq2a_m3_j1_describe_many_windows_batch.TestJq2aM3J1DescribeManyWindowsBatch.test_healthy_control_reverse_runner_mechanics',
                                                                      'tests.test_jq2a_m3_j1_describe_many_windows_batch.TestJq2aM3J1DescribeManyWindowsBatch.test_healthy_control_negative_mutant_vs_positive_control',
                                                                      'tests.test_jq2a_m3_j1_describe_many_windows_batch.TestJq2aM3J1DescribeManyWindowsBatch.test_perceive_many_reverse_completion_mixed_success',
                                                                      'tests.test_jq2a_m3_j1_describe_many_windows_batch.TestJq2aM3J1DescribeManyWindowsBatch.test_describe_windows_batch_identities_and_serial_bytes_agree',
                                                                      'tests.test_jq2a_m3_j1_describe_many_windows_batch.TestJq2aM3J1DescribeManyWindowsBatch.test_real_vision_engine_describe_many',
                                                                      'tests.test_jq2a_m3_j1_describe_many_windows_batch.TestJq2aM3J1DescribeManyWindowsBatch.test_vision_bridge_screen_identities_delegates',
                                                                      'tests.test_jq2a_m3_j1_describe_many_windows_batch.TestJq2aM3J1DescribeManyWindowsBatch.test_inv_m3_1_validate_contiguous_uint8_rgb_and_positive_dpr',
                                                                      'tests.test_jq2a_m3_j1_describe_many_windows_batch.TestJq2aM3J1DescribeManyWindowsBatch.test_inv_m3_2_temporal_sequence_target_isolation',
                                                                      'tests.test_jq2a_m3_j1_describe_many_windows_batch.TestJq2aM3J1DescribeManyWindowsBatch.test_inv_m3_3_worker_count_variation_preserves_descriptors'],
                                                          'functions': {'expected_failure_if_absent': '278d595a8f9d33c1c8dd65f7793a35b86f2ef0d15b597bdad0b0669a5625c03b',
                                                                        'expected_failure_if_absent.decorator': '343488b2f18a3220dfd27765d883406b008a893089470244ca5bd18552eb6a26',
                                                                        'ReverseCompletionRunner.map': '486311efea046dd91a89cdd013ed81a54dbd315f9bd607cf5dfa40de42939b32',
                                                                        'AssembleCompletionRunner.map': '005737d5741da5d2f8bd50dc0a2c5b06af67f420c8f6da02f0d345555a933ace',
                                                                        'create_valid_rgb': 'a2b906ccf3d1b3f70f56194c519905a88292a777403ee1d1bcfb8254fb5589e3',
                                                                        'TestJq2aM3J1DescribeManyWindowsBatch.test_healthy_control_dataclass_field_order_and_immutability': '4d7472e30db888d9c6b13f495758c27b3dabca5d314e633e8658e865626a72d9',
                                                                        'TestJq2aM3J1DescribeManyWindowsBatch.test_healthy_control_reverse_runner_mechanics': '0fb3cc814d04538e8883b20976208b60329bd94b5724686be18cbf4ff3fa91ab',
                                                                        'TestJq2aM3J1DescribeManyWindowsBatch.test_healthy_control_reverse_runner_mechanics.worker': 'e1fce28c624d289f5a48f2f386915176ec515633dfc39a891c7803fec5677823',
                                                                        'TestJq2aM3J1DescribeManyWindowsBatch.test_healthy_control_negative_mutant_vs_positive_control': '61cfbc962aa40b9391c0ecf1c4b0a06a83f42f6809e6054bf9893f9d800a9894',
                                                                        'TestJq2aM3J1DescribeManyWindowsBatch.test_healthy_control_negative_mutant_vs_positive_control.worker_pos': '26c3536412704bf95f93e5a464974b35879f6748c04f47da74ef3aedb419d61d',
                                                                        'TestJq2aM3J1DescribeManyWindowsBatch.test_healthy_control_negative_mutant_vs_positive_control.worker_neg': '2a46c6b2d841a40352ed00a17948036e970acbbb1307a9b3d758b8509f698ed2',
                                                                        'TestJq2aM3J1DescribeManyWindowsBatch.test_perceive_many_reverse_completion_mixed_success': 'cae793b52388cfbd94c41ca798b2cffc382a8438fcca03a8c038a7246923f6c7',
                                                                        'TestJq2aM3J1DescribeManyWindowsBatch.test_perceive_many_reverse_completion_mixed_success.mock_perceive': '2b1776414622c25ae48b4ed175e8e265abf1550f0ec885efb49eddb1d4a22ca4',
                                                                        'TestJq2aM3J1DescribeManyWindowsBatch.test_describe_windows_batch_identities_and_serial_bytes_agree': '5bbed2f72dd3e51405e023568785e4024969684e06fd07d0cb9a099c59bed921',
                                                                        'TestJq2aM3J1DescribeManyWindowsBatch.test_real_vision_engine_describe_many': '27318080e1e42de9bbd196b8639b240469fc2694953185de9fb423997a22f078',
                                                                        'TestJq2aM3J1DescribeManyWindowsBatch.test_vision_bridge_screen_identities_delegates': 'e18eeea04f247d61fdb978d5be91c5dbe85d5dfcd327147d8206cf6c9580df3d',
                                                                        'TestJq2aM3J1DescribeManyWindowsBatch.test_inv_m3_1_validate_contiguous_uint8_rgb_and_positive_dpr': '522ff166aa2105c26d49a5be3c581cde1a00ec49fbc57f34c7b50a8e1ad43c95',
                                                                        'TestJq2aM3J1DescribeManyWindowsBatch.test_inv_m3_2_temporal_sequence_target_isolation': 'b0c431c36409ce39745cf3beeee3bd73d2ad7a08b1a0eadd8ebbe78909349842',
                                                                        'TestJq2aM3J1DescribeManyWindowsBatch.test_inv_m3_2_temporal_sequence_target_isolation.side_effect': 'fd868476a32570ee5a72f8a55f385e0e5acadb697f801809cf98e177fab990c3',
                                                                        'TestJq2aM3J1DescribeManyWindowsBatch.test_inv_m3_3_worker_count_variation_preserves_descriptors': '73ddcd79a5b45bd367e9157e76f3c2f1d0956a34ab5440f3326b6a3f4549de6c'}},
 'tests/test_jq2a_m3_j3_inprocess_vision_worker_pool.py': {'sha256': '862f2cc37dbf8000ac3ab1ce1a72a490f39ab0a003385279873dcfe5fd05aba1',
                                                           'methods': ['tests.test_jq2a_m3_j3_inprocess_vision_worker_pool.TestInprocessVisionWorkerPool.test_record_classes_and_field_signatures',
                                                                       'tests.test_jq2a_m3_j3_inprocess_vision_worker_pool.TestInprocessVisionWorkerPool.test_supported_target_kinds_validation',
                                                                       'tests.test_jq2a_m3_j3_inprocess_vision_worker_pool.TestInprocessVisionWorkerPool.test_gate_g_m3_3_runner_reused_and_item_failure_isolated',
                                                                       'tests.test_jq2a_m3_j3_inprocess_vision_worker_pool.TestInprocessVisionWorkerPool.test_runner_death_marks_unfinished_items_as_errors',
                                                                       'tests.test_jq2a_m3_j3_inprocess_vision_worker_pool.TestInprocessVisionWorkerPool.test_negative_mutant_spawn_per_frame',
                                                                       'tests.test_jq2a_m3_j3_inprocess_vision_worker_pool.TestInprocessVisionWorkerPool.test_inv_m3_1_validation_contiguous_uint8_rgb_and_dpr',
                                                                       'tests.test_jq2a_m3_j3_inprocess_vision_worker_pool.TestInprocessVisionWorkerPool.test_inv_m3_2_temporal_sequence_epoch_isolation',
                                                                       'tests.test_jq2a_m3_j3_inprocess_vision_worker_pool.TestInprocessVisionWorkerPool.test_inv_m3_3_worker_count_variation_preserves_output_identity',
                                                                       'tests.test_jq2a_m3_j3_inprocess_vision_worker_pool.TestInprocessVisionWorkerPool.test_production_session_registry_export',
                                                                       'tests.test_jq2a_m3_j3_inprocess_vision_worker_pool.TestInprocessVisionWorkerPool.test_production_real_vision_engine_describe_many'],
                                                           'functions': {'expected_if_no_session_registry': 'f5fff9f01827f916098044d97a32c79048dd14832a6a8272fcba631b1f50719b',
                                                                         'expected_if_no_describe_many': '8f5ebe77268444517b9482f6ca81f738dba1fc97a7967d8e1ab17299cb6e7c60',
                                                                         'make_valid_rgb': 'a2e4966b3763638268914ab29f22c1d31218ef94cc5b5fe96acef32018e81ec8',
                                                                         'FakePersistentRunner.__init__': '711127c60aa48466d332929f966fa166aac24d88057c12f47842d0763c48ae8d',
                                                                         'FakePersistentRunner.is_alive': '7a80cf19d078d9af775eb9dc52215b7be3a0c40bbec3cbcd6a5f34f4c51163ad',
                                                                         'FakePersistentRunner.kill': 'bc78006857a355fb206e7723df17b5c1a1d980e5824251d3a9133e12f3212c9a',
                                                                         'FakePersistentRunner.describe_windows': '5ca326e4112547068800ff3bf481ca9aa8ce0df716fb4ab796dada3ddba7caae',
                                                                         'FakePersistentRunner.perceive_many': '1c28ce3f1540002a4d90476cd1f4964f3bf0ba40b3c9390f4ab139b0e29c8800',
                                                                         'TestInprocessVisionWorkerPool.setUp': 'c46610573be832a2443377e95b53517c9f418226a54fc78996fc853b7aa3b4ba',
                                                                         'TestInprocessVisionWorkerPool.test_record_classes_and_field_signatures': '9be709fad58f5ace37d09c96d2d117ddf93a27080aae58470f744bc41d85976c',
                                                                         'TestInprocessVisionWorkerPool.test_supported_target_kinds_validation': '1bd0213e888ec4d92d99ca4bfd2191f3372c32fb3ee144fa9400b285b9624ac0',
                                                                         'TestInprocessVisionWorkerPool.test_gate_g_m3_3_runner_reused_and_item_failure_isolated': '599c8576838b8e7031b71b40b619cf9458e7266350d4e3d66055ab22e6fbc9e2',
                                                                         'TestInprocessVisionWorkerPool.test_runner_death_marks_unfinished_items_as_errors': '1e2deb1372e6ee39136c9d45f9f5ec5f96b28113dfbe41a7cbe8d1c8b73d0f44',
                                                                         'TestInprocessVisionWorkerPool.test_negative_mutant_spawn_per_frame': 'cfcd88b773a89e42cbfdb93a05bb0c786c7a223494bbaf9470189048b5471d8d',
                                                                         'TestInprocessVisionWorkerPool.test_inv_m3_1_validation_contiguous_uint8_rgb_and_dpr': '96e66d0d3ce822af0eab600a40f2250a5b2c48989ab1b57c4c449ef7435f0eb2',
                                                                         'TestInprocessVisionWorkerPool.test_inv_m3_2_temporal_sequence_epoch_isolation': 'd55f3ad398a2473b7b45948089820e7082fb8e1d4594b4a6e30929d2a54a1c41',
                                                                         'TestInprocessVisionWorkerPool.test_inv_m3_3_worker_count_variation_preserves_output_identity': '60421f10a53be1e9b3c0b36b931044cb5e1ce6a0a5655dd2edd8b18e701d8e53',
                                                                         'TestInprocessVisionWorkerPool.test_production_session_registry_export': '9c50702d2537d6d17fcab091aee1082261fcf6c88b9ab66cd6c1a154c867f4bd',
                                                                         'TestInprocessVisionWorkerPool.test_production_real_vision_engine_describe_many': 'be45c8e7b2ed16b3b706995fe9d9e41a28bcd889921e687179a892f1c4b096eb'}},
 'tests/test_jq2a_m3_j4_per_window_dpr_and_affordance_align.py': {'sha256': '4f802b4ff0e6db5c5ffbba252e692e3bc854e8920c02eab50ef9cfa805ee5577',
                                                                  'methods': ['tests.test_jq2a_m3_j4_per_window_dpr_and_affordance_align.TestPerWindowDprAndAffordanceAlign.test_healthy_control_per_target_dpr_fusion_coordinate_alignment',
                                                                              'tests.test_jq2a_m3_j4_per_window_dpr_and_affordance_align.TestPerWindowDprAndAffordanceAlign.test_healthy_control_negative_mutant_global_dpr_misalignment',
                                                                              'tests.test_jq2a_m3_j4_per_window_dpr_and_affordance_align.TestPerWindowDprAndAffordanceAlign.test_healthy_control_positive_dpr_validation',
                                                                              'tests.test_jq2a_m3_j4_per_window_dpr_and_affordance_align.TestPerWindowDprAndAffordanceAlign.test_healthy_control_resize_and_geometry_epoch_transition',
                                                                              'tests.test_jq2a_m3_j4_per_window_dpr_and_affordance_align.TestPerWindowDprAndAffordanceAlign.test_contract_perceive_many_per_target_dpr_and_affordance_alignment',
                                                                              'tests.test_jq2a_m3_j4_per_window_dpr_and_affordance_align.TestPerWindowDprAndAffordanceAlign.test_contract_vision_bridge_screen_identities_delegation',
                                                                              'tests.test_jq2a_m3_j4_per_window_dpr_and_affordance_align.TestPerWindowDprAndAffordanceAlign.test_contract_fuse_regions_handle_epoch_and_stale_refusal',
                                                                              'tests.test_jq2a_m3_j4_per_window_dpr_and_affordance_align.TestPerWindowDprAndAffordanceAlign.test_contract_real_vision_engine_describe_many_invariants'],
                                                                  'functions': {'expected_failure_if_absent': '5d0c6f65b3d50656a6647ec9cf5d891770d43ea1785d1219b650cfca0c895f52',
                                                                                'make_test_frame': '483190163a4f1829ce00972fc85e1df943a01044c88da06df40437da807c94f4',
                                                                                'make_test_state': 'b6c9d69ca3b414a41ede742805d9ec10bc7f5e496c779a47fdd273facb0e6fdf',
                                                                                'TestPerWindowDprAndAffordanceAlign.setUp': '17c0dea98777622ca119a1f883904bc27fd8f08a414bacb96d36aa66d0ae1cba',
                                                                                'TestPerWindowDprAndAffordanceAlign.test_healthy_control_per_target_dpr_fusion_coordinate_alignment': '311b24d1e83067fb071861b155610ad2b345eddf2b4fbb5381eb92a93e364fe8',
                                                                                'TestPerWindowDprAndAffordanceAlign.test_healthy_control_negative_mutant_global_dpr_misalignment': '57b1fdb4ac5d7858ef719853ad576edd37a53ebdbe20f18f929e363e69732623',
                                                                                'TestPerWindowDprAndAffordanceAlign.test_healthy_control_positive_dpr_validation': '59c98a6ebc386f15ceed94ac2fe1ff375bcf6b2b6c30382c4414d92cacb04bfb',
                                                                                'TestPerWindowDprAndAffordanceAlign.test_healthy_control_resize_and_geometry_epoch_transition': '51e43e4d253a3b74c332e279d987def9973604646d16ce1b7859dd848dee7f1b',
                                                                                'TestPerWindowDprAndAffordanceAlign.test_contract_perceive_many_per_target_dpr_and_affordance_alignment': '804893572e781aa137d50d990e738d54245e819ebad56e8957ee6f714b4dcf44',
                                                                                'TestPerWindowDprAndAffordanceAlign.test_contract_vision_bridge_screen_identities_delegation': '178fdbd61d3ac40e78a5d4ed67e6181e3f35bf240a200eaa85e2083bc0cc0464',
                                                                                'TestPerWindowDprAndAffordanceAlign.test_contract_fuse_regions_handle_epoch_and_stale_refusal': '3fdc144b64c7a95c6717732cbc4c717c5fe51953eb6778deee0ee112d8848e42',
                                                                                'TestPerWindowDprAndAffordanceAlign.test_contract_real_vision_engine_describe_many_invariants': '2a91f8c902b8ea1ee0c433549a83fa63ece494c6d67bec8d9924ee3cbc9da756'}}}


@dataclass(frozen=True)
class Mutation:
    name: str
    path: str
    qualified: str
    anchor: str
    node_type: str
    selector: str
    operation: str


@dataclass(frozen=True)
class Proof:
    disposition: str
    detail: str


class AnchorError(ValueError):
    pass


class ProofTimeout(BaseException):
    """A source-level ``except Exception`` must never consume proof deadlines."""
    pass


def _definitions(tree):
    """Qualified functions, including helpers inside conditional class blocks."""
    found = {}

    def visit(node, prefix=()):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            prefix = (*prefix, node.name)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.setdefault(".".join(prefix), []).append(node)
        for child in ast.iter_child_nodes(node):
            visit(child, prefix)

    visit(tree)
    return found


def _scope_nodes(node):
    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        yield from _scope_nodes(child)


def _selected(tree, source, spec):
    scopes = [tree] if spec.qualified == "<module>" else _definitions(tree).get(spec.qualified, [])
    if len(scopes) != 1:
        raise AnchorError("qualified scope missing or ambiguous: " + spec.qualified)
    matches = [node for node in _scope_nodes(scopes[0])
               if type(node).__name__ == spec.node_type
               and ast.get_source_segment(source, node) == spec.anchor]
    if len(matches) != 1:
        raise AnchorError("exact source anchor missing or ambiguous: %s (%d matches)" %
                          (spec.name, len(matches)))
    return matches[0]


def _mutate(tree, source, spec):
    """Apply precisely one catalogue-selected O/B/D/S edit; never a fallback."""
    result = copy.deepcopy(tree)
    node = _selected(result, source, spec)
    opposite = {
        ast.Lt: ast.Gt, ast.Gt: ast.Lt, ast.LtE: ast.GtE, ast.GtE: ast.LtE,
        ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Is: ast.IsNot,
        ast.IsNot: ast.Is, ast.In: ast.NotIn, ast.NotIn: ast.In,
        ast.And: ast.Or, ast.Or: ast.And, ast.Add: ast.Sub, ast.Sub: ast.Add,
        ast.Mult: ast.FloorDiv, ast.FloorDiv: ast.Mult,
    }
    boundary = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt}
    if spec.operation in ("O", "B") and isinstance(node, ast.Compare):
        if not (spec.selector.startswith("ops[") and spec.selector.endswith("]")):
            raise AnchorError("comparison requires its exact ops[index] selector")
        index = int(spec.selector[4:-1])
        if not 0 <= index < len(node.ops):
            raise AnchorError("comparison operator index out of range")
        mapping = opposite if spec.operation == "O" else boundary
        if type(node.ops[index]) not in mapping:
            raise AnchorError("unsupported comparison operator for " + spec.operation)
        node.ops[index] = mapping[type(node.ops[index])]()
    elif spec.operation == "O" and isinstance(node, (ast.BoolOp, ast.BinOp)):
        if spec.selector != "op" or type(node.op) not in opposite:
            raise AnchorError("unsupported exact operator selector")
        node.op = opposite[type(node.op)]()
    elif spec.operation == "B" and isinstance(node, ast.Constant):
        if spec.selector != "value" or type(node.value) is not int:
            raise AnchorError("B constant requires an exact integer, excluding bool")
        node.value += 1
    elif spec.operation == "D" and isinstance(node, ast.If):
        if spec.selector != "test" or not any(
                isinstance(child, ast.Raise) for statement in node.body for child in ast.walk(statement)):
            raise AnchorError("D requires a selected raising If")
        node.test = ast.copy_location(ast.Constant(False), node.test)
    elif spec.operation == "D" and isinstance(node, ast.Assert):
        if spec.selector != "test":
            raise AnchorError("D Assert requires test selector")
        node.test = ast.copy_location(ast.Constant(True), node.test)
    elif spec.operation == "S" and isinstance(node, ast.Call):
        if spec.selector != "first_two_distinct_nonstarred_args":
            raise AnchorError("S requires the positional-argument selector")
        positional = [(index, arg) for index, arg in enumerate(node.args)
                      if not isinstance(arg, ast.Starred)]
        if not positional:
            raise AnchorError("S requires two distinct non-starred positional arguments")
        first, first_arg = positional[0]
        different = [index for index, arg in positional[1:]
                     if ast.dump(arg, include_attributes=False) != ast.dump(first_arg, include_attributes=False)]
        if not different:
            raise AnchorError("S requires two distinct non-starred positional arguments")
        second = different[0]
        node.args[first], node.args[second] = node.args[second], node.args[first]
    else:
        raise AnchorError("operation, node and selector do not match")
    return ast.fix_missing_locations(result)


@contextmanager
def _deadline():
    """Bound proof execution on the unittest main thread, restoring the alarm."""
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("proof execution requires the unittest main thread")
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)

    def expired(_signum, _frame):
        raise ProofTimeout("VM proof exceeded two seconds")

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, WAIT_SECONDS)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)


@contextmanager
def _module(tree, filename):
    """Compile in RAM; register before exec so private dataclasses work."""
    code = compile(tree, filename, "exec", dont_inherit=True)
    name = "wender.perceive._r7_c1_private_proof"
    previous_modules = dict(sys.modules)
    previous_bytecode = sys.dont_write_bytecode
    module = types.ModuleType(name)
    module.__file__ = filename
    module.__package__ = "wender.perceive"
    sys.dont_write_bytecode = True
    sys.modules[name] = module
    try:
        exec(code, module.__dict__)
        yield module
    finally:
        for key in set(sys.modules).difference(previous_modules):
            del sys.modules[key]
        for key, value in previous_modules.items():
            if sys.modules.get(key) is not value:
                sys.modules[key] = value
        sys.dont_write_bytecode = previous_bytecode


def _exercise(tree, path, assertion):
    try:
        code = compile(tree, path, "exec", dont_inherit=True)
        del code
    except Exception as exc:
        return Proof("COMPILE_ERROR", repr(exc))
    entered = False
    try:
        with _deadline(), _module(tree, path) as module:
            entered = True
            assertion(module)
    except ProofTimeout as exc:
        return Proof("TIMEOUT", str(exc))
    except AssertionError as exc:
        if not entered:
            return Proof("IMPORT_ERROR", repr(exc))
        traceback = exc.__traceback__
        while traceback is not None:
            if traceback.tb_frame.f_globals is module.__dict__:
                return Proof("RUNTIME_ERROR", "source raised AssertionError: " + repr(exc))
            traceback = traceback.tb_next
        return Proof("ASSERTION", str(exc))
    except Exception as exc:
        return Proof("RUNTIME_ERROR" if entered else "IMPORT_ERROR", repr(exc))
    return Proof("PASS", "semantic assertion passed")


def _prove(spec, assertion, *, source=None, equivalence=None):
    source = (ROOT / spec.path).read_text(encoding="utf-8") if source is None else source
    try:
        tree = ast.parse(source, filename=spec.path)
        _selected(tree, source, spec)
    except AnchorError as exc:
        return Proof("ANCHOR_ERROR", str(exc))
    except Exception as exc:
        return Proof("COMPILE_ERROR", repr(exc))
    # The identical semantic assertion must pass on untouched real source first.
    control = _exercise(tree, spec.path, assertion)
    if control.disposition != "PASS":
        return Proof("FAILED_CONTROL", control.disposition + ": " + control.detail)
    try:
        mutant = _mutate(tree, source, spec)
    except Exception as exc:
        return Proof("ANCHOR_ERROR", repr(exc))
    result = _exercise(mutant, spec.path, assertion)
    if result.disposition == "ASSERTION":
        return Proof("KILLED", result.detail)
    if result.disposition != "PASS":
        return result
    if equivalence is not None:
        try:
            reason = equivalence(tree, mutant)
        except Exception as exc:
            return Proof("EQUIVALENCE_ERROR", repr(exc))
        if not isinstance(reason, str) or not reason:
            return Proof("EQUIVALENCE_ERROR", "no algebraic proof supplied")
        return Proof("EQUIVALENT", reason)
    return Proof("SURVIVED", "control and mutant both passed")


CATALOGUE = (
    Mutation("M-wender-perceive-parallel-perceive-1", "wender/perceive/parallel_perceive.py",
             "_prepare", "not isinstance(digest, str) or len(digest) != 64\n            or digest != actual_digest",
             "BoolOp", "op", "O"),
    Mutation("M-wender-perceive-parallel-perceive-2", "wender/perceive/parallel_perceive.py",
             "_prepare", '_handle_key(_structural_handle(state["handle"])) != key',
             "Compare", "ops[0]", "O"),
)


class InlineRunner:
    def map(self, function, jobs):
        return [function(job) for job in jobs]


class ReverseRunner:
    def map(self, function, jobs):
        return [function(job) for job in reversed(list(jobs))]


def _issued_observation(registry, handle, seq, pixels, dpr):
    epoch = registry.resolve(handle).epoch
    frame = registry.admit_frame(handle, seq, pixels, epoch=epoch)
    state = {"nodes": [{"id": "node-" + handle.canonical_id,
                        "box": [0, 0, 100, 100], "actionable": True}],
             "handle": {"kind": handle.kind, "canonical_id": handle.canonical_id,
                        "generation": handle.generation},
             "epoch": epoch, "viewport": {"dpr_ppm": dpr}}
    return registry.admit_observation(handle, frame, state=state, dpr_ppm=dpr)


def _fixture(parameters, name="gate"):
    registry = SessionRegistry()
    handle = registry.register(parameters["kind"], name, object(), owned=False)
    for _ in range(parameters["epoch"]):
        registry.advance_epoch(handle)
    pixels = np.full((parameters["height"], parameters["width"], 3),
                     parameters["fill"], dtype=np.uint8)
    observation = _issued_observation(registry, handle, 0, pixels, parameters["dpr"])
    return registry, observation, pixels


def _structural(observation):
    """Invalid-input probes deliberately bypass strict value-record constructors."""
    frame = types.SimpleNamespace(**vars(observation.frame))
    fields = dict(vars(observation), frame=frame, state=copy.deepcopy(observation.state))
    return types.SimpleNamespace(**fields)


def _case(rng):
    return {"kind": rng.choice(sorted(parallel._TARGET_KINDS)),
            "width": rng.randint(1, 8), "height": rng.randint(1, 8),
            "fill": rng.randint(0, 255), "delta": rng.randint(1, 255),
            "epoch": rng.randint(0, 3),
            "dpr": rng.choice((500000, 1000000, 1500000, 2000000))}


def _shrink(parameters, assertion, *, limit=SHRINK_LIMIT):
    """Find a fixed point under valid minima reductions, or report incomplete."""
    if type(limit) is not int or not 0 <= limit <= SHRINK_LIMIT:
        raise ValueError("shrink budget must be an integer between zero and 32")
    minimal = dict(parameters)
    minima = {"width": 1, "height": 1, "fill": 0, "delta": 1, "epoch": 0, "dpr": 500000}
    attempts = 0
    while True:
        reduced = False
        for key, minimum in minima.items():
            if minimal[key] == minimum:
                continue
            if attempts == limit:
                return minimal, attempts, True
            candidate = dict(minimal, **{key: minimum})
            attempts += 1
            try:
                assertion(candidate)
            except AssertionError:
                minimal = candidate
                reduced = True
                break  # Revisit earlier dimensions after every accepted change.
            except Exception:
                # A runtime error is not a preserved semantic counterexample.
                continue
        if not reduced:
            return minimal, attempts, False


def _random_cases(assertion, domain):
    rng = random.Random(SEED + domain)
    valid = 0
    for index in range(VALID_CASES):
        fixture = _case(rng)
        try:
            assertion(fixture)
        except AssertionError as exc:
            minimal, attempts, exhausted = _shrink(fixture, assertion)
            raise AssertionError("seed=%d case=%d minimal_fixture=%r shrink_attempts=%d incomplete=%r: %s" %
                                 (SEED + domain, index, minimal, attempts, exhausted, exc)) from exc
        except Exception as exc:
            raise RuntimeError("seed=%d case=%d fixture=%r: %s" %
                               (SEED + domain, index, fixture, exc)) from exc
        valid += 1
    if valid != VALID_CASES:
        raise AssertionError("incomplete valid-case budget: %d/%d" % (valid, VALID_CASES))


class ControlledRunner:
    """Real map jobs, two threads, replayable Events, no invented pool API."""
    def __init__(self, order, fail_after_first):
        self.order = order
        self.fail_after_first = fail_after_first

    def map(self, function, jobs):
        jobs = list(jobs)
        if len(jobs) != 2:
            raise AssertionError("schedule requires exactly two target groups")
        barrier = threading.Barrier(3)
        release = [threading.Event() for _ in jobs]
        finished = [threading.Event() for _ in jobs]
        outputs, errors = [None, None], [None, None]

        def worker(index):
            try:
                barrier.wait(WAIT_SECONDS)
                if not release[index].wait(WAIT_SECONDS):
                    raise TimeoutError("scheduled worker was not released")
                outputs[index] = function(jobs[index])
            except BaseException as exc:
                errors[index] = exc
            finally:
                finished[index].set()

        threads = [threading.Thread(target=worker, args=(index,), daemon=True)
                   for index in range(2)]
        for thread in threads:
            thread.start()
        try:
            barrier.wait(WAIT_SECONDS)
            for index in self.order:
                release[index].set()
                if not finished[index].wait(WAIT_SECONDS):
                    raise TimeoutError("scheduled completion exceeded two seconds")
        finally:
            for event in release:
                event.set()
            for thread in threads:
                thread.join(WAIT_SECONDS)
            if any(thread.is_alive() for thread in threads):
                raise AssertionError("leaked controlled-runner thread")
        if any(error is not None for error in errors):
            raise AssertionError("scheduled worker failed: %r" % errors)
        for position, index in enumerate(self.order):
            yield outputs[index]
            if self.fail_after_first and position == 0:
                raise RuntimeError("controlled runner death after one delivery")


class TestR7C1ProofHarness(unittest.TestCase):
    def test_all_operator_mappings_and_structural_selectors(self):
        comparisons = (("<", ">"), (">", "<"), ("<=", ">="), (">=", "<="),
                       ("==", "!="), ("!=", "=="), ("is", "is not"),
                       ("is not", "is"), ("in", "not in"), ("not in", "in"))
        for before, after in comparisons:
            source = "def probe(a, b):\n    return a %s b\n" % before
            spec = Mutation("operator", "<memory>", "probe", "a %s b" % before,
                            "Compare", "ops[0]", "O")
            changed = _selected(_mutate(ast.parse(source), source, spec), source, spec)
            self.assertEqual(ast.unparse(changed), "a %s b" % after)
        for before, after, node_type in (("and", "or", "BoolOp"), ("or", "and", "BoolOp"),
                                        ("+", "-", "BinOp"), ("-", "+", "BinOp"),
                                        ("*", "//", "BinOp"), ("//", "*", "BinOp")):
            source = "def probe(a, b):\n    return a %s b\n" % before
            spec = Mutation("operator", "<memory>", "probe", "a %s b" % before, node_type, "op", "O")
            node = _selected(_mutate(ast.parse(source), source, spec), source, spec)
            self.assertEqual(ast.unparse(node), "a %s b" % after)
        for before, after in (("<", "<="), ("<=", "<"), (">", ">="), (">=", ">")):
            source = "def probe(a, b):\n    return a %s b\n" % before
            spec = Mutation("boundary", "<memory>", "probe", "a %s b" % before, "Compare", "ops[0]", "B")
            node = _selected(_mutate(ast.parse(source), source, spec), source, spec)
            self.assertEqual(ast.unparse(node), "a %s b" % after)
        source = "def probe():\n    return 4\n"
        spec = Mutation("constant", "<memory>", "probe", "4", "Constant", "value", "B")
        self.assertEqual(_selected(_mutate(ast.parse(source), source, spec), source, spec).value, 5)
        with self.assertRaises(AnchorError):
            _mutate(ast.parse(source), source, Mutation("wrong-selector", "<memory>", "probe", "4", "Constant", "ops[0]", "B"))
        for source, anchor, node_type, expected in (
                ("def probe(x):\n    if x < 0:\n        raise ValueError('x')\n", "if x < 0:\n        raise ValueError('x')", "If", False),
                ("def probe(x):\n    assert x > 0\n", "assert x > 0", "Assert", True)):
            spec = Mutation("delete", "<memory>", "probe", anchor, node_type, "test", "D")
            self.assertIs(_selected(_mutate(ast.parse(source), source, spec), source, spec).test.value, expected)
        source = "def probe(f, a, b, rest):\n    return f(*rest, a, a, b, key=1)\n"
        spec = Mutation("swap", "<memory>", "probe", "f(*rest, a, a, b, key=1)", "Call", "first_two_distinct_nonstarred_args", "S")
        self.assertEqual(ast.unparse(_selected(_mutate(ast.parse(source), source, spec), source, spec)),
                         "f(*rest, b, a, a, key=1)")

    def test_control_dispositions_anchors_and_private_module_cleanup(self):
        source = "from dataclasses import dataclass\n@dataclass\nclass Record:\n    number: int\ndef probe():\n    return Record(1 + 2).number\n"
        spec = Mutation("self-check", "<memory>", "probe", "1 + 2", "BinOp", "op", "O")
        modules = dict(sys.modules)
        bytecode = sys.dont_write_bytecode
        self.assertEqual(_prove(spec, lambda module: self.assertEqual(module.probe(), 3), source=source).disposition, "KILLED")
        self.assertEqual(_prove(spec, lambda module: None, source=source).disposition, "SURVIVED")
        self.assertEqual(_prove(spec, lambda module: self.fail("bad control"), source=source).disposition, "FAILED_CONTROL")
        source_assert = "def probe():\n    assert 1 + 2 > 0\n"
        self.assertEqual(_prove(spec, lambda module: module.probe(), source=source_assert).disposition,
                         "RUNTIME_ERROR", "a mutant's own assert is not a semantic-oracle kill")
        missing = Mutation("missing", "<memory>", "probe", "1 + 3", "BinOp", "op", "O")
        self.assertEqual(_prove(missing, lambda module: None, source=source).disposition, "ANCHOR_ERROR")
        ambiguous = "def probe():\n    return (1 + 2, 1 + 2)\n"
        self.assertEqual(_prove(spec, lambda module: None, source=ambiguous).disposition, "ANCHOR_ERROR")
        self.assertEqual(_exercise(ast.parse("import __r7_nonexistent_module__"), "<memory>", lambda module: None).disposition, "IMPORT_ERROR")
        self.assertEqual(_exercise(ast.parse("pass"), "<memory>", lambda module: 1 / 0).disposition, "RUNTIME_ERROR")
        self.assertEqual(_exercise(ast.parse("pass"), "<memory>", lambda module: (_ for _ in ()).throw(ProofTimeout("bounded"))).disposition, "TIMEOUT")
        catches_errors = ("import signal\ndef probe():\n    try:\n        if 1 + 2 < 0:\n"
                          "            signal.raise_signal(signal.SIGALRM)\n        return 1\n"
                          "    except Exception:\n        return 0\n")
        self.assertEqual(_prove(spec, lambda module: self.assertEqual(module.probe(), 1), source=catches_errors).disposition,
                         "TIMEOUT", "source error isolation must not turn a timeout into an oracle kill")
        malformed_ast = ast.Module(body=[ast.Return(value=ast.Constant(1))], type_ignores=[])
        self.assertEqual(_exercise(ast.fix_missing_locations(malformed_ast), "<memory>", lambda module: None).disposition, "COMPILE_ERROR")
        self.assertEqual(set(sys.modules), set(modules))
        self.assertTrue(all(sys.modules[key] is value for key, value in modules.items()))
        self.assertIs(sys.dont_write_bytecode, bytecode)

    def test_shrinking_revisits_interactions_and_labels_budget_exhaustion(self):
        fixture = {"kind": "cdp_tab", "width": 2, "height": 2, "fill": 0,
                   "delta": 1, "epoch": 0, "dpr": 500000}

        def controlled_failure(candidate):
            if candidate["width"] == 2 or candidate["height"] == 1:
                raise AssertionError("synthetic dimensional interaction")

        minimal, attempts, incomplete = _shrink(fixture, controlled_failure)
        self.assertEqual((minimal["width"], minimal["height"]), (1, 1))
        self.assertEqual(attempts, 3)
        self.assertFalse(incomplete)
        partial, attempts, incomplete = _shrink(fixture, controlled_failure, limit=1)
        self.assertEqual(partial, fixture)
        self.assertEqual(attempts, 1)
        self.assertTrue(incomplete)
        with self.assertRaises(ValueError):
            _shrink(fixture, controlled_failure, limit=33)

    def test_both_catalogue_obligations_kill_after_real_control(self):
        parameters = {"kind": "cdp_tab", "width": 1, "height": 1, "fill": 7,
                      "delta": 1, "epoch": 0, "dpr": 1000000}
        registry, observation, _ = _fixture(parameters)
        digest_probe = _structural(observation)
        digest_probe.frame.digest = "0" * 64
        handle_probe = _structural(observation)
        handle_probe.state["handle"] = registry.register("cdp_tab", "sibling", object(), owned=False)
        for spec, probe, message in (
                (CATALOGUE[0], digest_probe, "frame content SHA-256 mismatch"),
                (CATALOGUE[1], handle_probe, "state target handle mismatch")):
            with self.subTest(mutation=spec.name, seed=SEED, case=0, minimal_fixture=parameters):
                def assertion(module):
                    try:
                        module._prepare(probe)
                    except module.ObservationError as exc:
                        self.assertEqual(str(exc), message)
                    else:
                        self.fail("invalid binding accepted: " + message)
                proof = _prove(spec, assertion)
                self.assertEqual(proof.disposition, "KILLED", proof)

    def test_pixel_argument_swap_has_algebraic_equivalence_not_a_kill(self):
        spec = Mutation("S-symmetric-pixel-pair", "wender/perceive/vision_adapter.py",
                        "FakeVisionEngine.changed_regions", "zip(prev_data[start:end], cur_data[start:end])",
                        "Call", "first_two_distinct_nonstarred_args", "S")

        def assertion(module):
            rng = random.Random(SEED + 4)
            for index in range(VALID_CASES):
                width, height = rng.randint(1, 8), rng.randint(1, 8)
                before = bytes(rng.randrange(256) for _ in range(width * height * 3))
                after = bytes(rng.randrange(256) for _ in range(width * height * 3))
                tile, threshold = rng.randint(1, 4), rng.randrange(256)
                engine = module.FakeVisionEngine(tile=tile, threshold=threshold)
                left, right = module.Frame(width, height, before), module.Frame(width, height, after)
                expected = vision.FakeVisionEngine(tile=tile, threshold=threshold).changed_regions(
                    vision.Frame(width, height, before), vision.Frame(width, height, after))
                self.assertEqual(engine.changed_regions(left, right), expected, (SEED + 4, index))

        def equivalence(original, mutated):
            function = _definitions(original)[spec.qualified][0]
            abs_calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)
                         and isinstance(node.func, ast.Name) and node.func.id == "abs"
                         and ast.unparse(node) == "abs(a - b)"]
            self.assertEqual(len(abs_calls), 1)
            self.assertNotEqual(ast.dump(original), ast.dump(mutated))
            return ("For every RGB channel a,b, abs(a-b)=abs(b-a). Swapping the two zip inputs "
                    "leaves every tile total, threshold decision, connected component and region unchanged.")

        self.assertEqual(_prove(spec, assertion, equivalence=equivalence).disposition, "EQUIVALENT")


class TestR7C1BindingRegressions(unittest.TestCase):
    def test_unbound_perception_receipt_requires_error_and_no_observation(self):
        fixture = {"kind": "cdp_tab", "width": 1, "height": 1, "fill": 0,
                   "delta": 1, "epoch": 0, "dpr": 1000000}
        _, observation, _ = _fixture(fixture)
        rejected = PerceptionResult(None, None, "VALIDATION_ERROR:invalid input")
        self.assertIsNone(rejected.handle)
        self.assertIsNone(rejected.observation)
        self.assertEqual(rejected.error, "VALIDATION_ERROR:invalid input")
        for payload, error in ((None, None), (None, ""), (None, 1),
                               (observation, None), (observation, "bad input")):
            with self.subTest(observation_present=payload is not None, error=error):
                with self.assertRaises(ValueError):
                    PerceptionResult(None, payload, error)
        # Existing bound metadata receipts stay constructible; runner publication
        # separately requires either an observation or a nonempty error.
        self.assertEqual(PerceptionResult(observation.handle, None, None).handle, observation.handle)

    def test_real_window_runner_full_digests_and_foreign_result_isolation(self):
        registry = SessionRegistry()
        windows = []
        for index, name in enumerate(("before", "subject", "after")):
            handle = registry.register("cdp_tab", name, object(), owned=False)
            epoch = registry.advance_epoch(handle)
            initial = _issued_observation(registry, handle, 0, np.full((2, 3, 3), index, dtype=np.uint8), 1000000)
            final = _issued_observation(registry, handle, 1, np.full((2, 3, 3), index + 30, dtype=np.uint8), 1000000)
            windows.append(WindowFrames(handle, epoch, (initial.frame.rgb, final.frame.rgb), (0, 1), 1000000))
        for mode in ("description", "dict", "omitted", "foreign_handle", "epoch", "frame_seqs"):
            with self.subTest(mode=mode):
                class WindowRunner:
                    def describe_window(self, item):
                        descriptors = tuple(hashlib.sha256(frame.tobytes()).digest() for frame in item.frames)
                        if mode == "dict":
                            return {"descriptors": descriptors, "regions": ()}
                        if item.handle.canonical_id == "subject":
                            if mode == "omitted":
                                return None
                            handle = windows[0].handle if mode == "foreign_handle" else item.handle
                            epoch = item.epoch + 1 if mode == "epoch" else item.epoch
                            seqs = (0, 2) if mode == "frame_seqs" else item.frame_seqs
                        else:
                            handle, epoch, seqs = item.handle, item.epoch, item.frame_seqs
                        return WindowDescription(handle, epoch, seqs, descriptors, (), None)

                engine = RealVisionEngine(runner=WindowRunner())
                results = engine.describe_many(windows)
                self.assertEqual([result.handle for result in results], [item.handle for item in windows])
                for index, (result, item) in enumerate(zip(results, windows)):
                    if index == 1 and mode not in ("description", "dict"):
                        self.assertIsNone(result.observation)
                        self.assertTrue(result.error.startswith("FrameValidationError:"), result.error)
                    else:
                        self.assertIsNone(result.error, result.error)
                        self.assertEqual(result.observation.epoch, item.epoch)
                        self.assertEqual(result.observation.frame_seq, item.frame_seqs[-1])
                        self.assertEqual(result.observation.dpr_ppm, item.dpr_ppm)
                        self.assertEqual(result.observation.frame.digest,
                                         hashlib.sha256(item.frames[-1].tobytes()).hexdigest())
                        self.assertEqual(len(result.observation.frame.digest), 64)
                        self.assertFalse(result.observation.frame.rgb.flags.writeable)

    def test_64_issued_immutable_digest_and_snapshot_cases(self):
        def assertion(parameters):
            _, observation, original_pixels = _fixture(parameters)
            prepared = parallel._prepare(observation)
            original_pixels[:] = (parameters["fill"] + parameters["delta"]) % 256
            observation.state["nodes"][0]["id"] = "caller-mutation"
            self.assertEqual(prepared.frame.rgb.dtype, np.uint8)
            self.assertTrue(prepared.frame.rgb.flags.c_contiguous)
            self.assertFalse(prepared.frame.rgb.flags.writeable)
            self.assertEqual(prepared.frame.digest, hashlib.sha256(prepared.frame.rgb.tobytes()).hexdigest())
            self.assertTrue(np.all(prepared.frame.rgb == parameters["fill"]))
            self.assertEqual(prepared.state["nodes"][0]["id"], "node-gate")
            with self.assertRaises(ValueError):
                prepared.frame.rgb.flags.writeable = True
        _random_cases(assertion, 0)

    def test_64_changed_frame_per_window_dpr_and_temporal_cases(self):
        def assertion(parameters):
            registry, first, _ = _fixture(parameters, "left")
            handle = registry.register("x11_window", "right", object(), owned=False)
            second_dpr = 2000000 if parameters["dpr"] != 2000000 else 1000000
            other = _issued_observation(registry, handle, 0, np.zeros_like(first.frame.rgb), second_dpr)
            next_pixels = np.full_like(first.frame.rgb, (parameters["fill"] + parameters["delta"]) % 256)
            following = _issued_observation(registry, first.handle, 1, next_pixels, parameters["dpr"])
            rows = [first, other, following]
            serial = parallel.perceive_many(rows, adapter=vision.FakeVisionEngine(tile=1, threshold=0), runner=InlineRunner())
            reverse = parallel.perceive_many(rows, adapter=vision.FakeVisionEngine(tile=1, threshold=0), runner=ReverseRunner())
            self.assertTrue(all(row.error is None for row in serial + reverse), [row.error for row in serial + reverse])
            self.assertEqual([row.observation.state for row in serial], [row.observation.state for row in reverse])
            self.assertEqual([row.handle for row in reverse], [row.handle for row in rows])
            self.assertEqual([row.observation.state["_perception"]["regions"] for row in reverse[:2]], [[], []])
            region = reverse[2].observation.state["_perception"]
            self.assertTrue(region["regions"])
            self.assertEqual(region["dpr_ppm"], parameters["dpr"])
            self.assertEqual(reverse[1].observation.dpr_ppm, second_dpr)
            # Floor conversion may collapse a one-pixel extent at high DPR.
            expected_fusion = vision.fuse_regions(first.state, region["regions"], parameters["dpr"])
            self.assertEqual(region["fusion"], expected_fusion)
            self.assertTrue(all(hit["node"] == "node-left" for hit in region["fusion"]["hits"]))
        _random_cases(assertion, 1)

    def test_64_invalid_structural_inputs_keep_typed_receipts_and_sibling_success(self):
        def assertion(parameters):
            _, observation, _ = _fixture(parameters)
            digest = _structural(observation)
            digest.frame.digest = "0" * 64
            dpr = _structural(observation)
            dpr.dpr_ppm = 0
            handle = _structural(observation)
            handle.handle = types.SimpleNamespace(kind="cdp_tab", canonical_id="bad", generation=0)
            state = _structural(observation)
            state.state = {"nodes": "invalid"}
            results = parallel.perceive_many([None, digest, dpr, handle, state, observation],
                                            adapter=vision.FakeVisionEngine(), runner=InlineRunner())
            self.assertEqual(len(results), 6)
            self.assertTrue(all(isinstance(result, PerceptionResult) for result in results))
            self.assertTrue(all(result.error.startswith("VALIDATION_ERROR:") and result.observation is None
                                for result in results[:-1]))
            self.assertIsNone(results[0].handle)
            self.assertIsNone(results[3].handle)
            self.assertEqual(results[1].handle, observation.handle)
            self.assertIsNone(results[-1].error)
            self.assertEqual(results[-1].handle, observation.handle)
        _random_cases(assertion, 2)

    def test_foreign_omitted_duplicate_and_mismatched_runner_results_are_fenced(self):
        parameters = {"kind": "cdp_tab", "width": 2, "height": 2, "fill": 5,
                      "delta": 1, "epoch": 0, "dpr": 1000000}
        registry, first, _ = _fixture(parameters, "first")
        handle = registry.register("x11_window", "second", object(), owned=False)
        second = _issued_observation(registry, handle, 0, np.zeros((2, 2, 3), dtype=np.uint8), 2000000)

        def altered(result, field):
            observation = result.observation
            state = copy.deepcopy(observation.state)
            epoch, seq, dpr = observation.epoch, observation.frame_seq, observation.dpr_ppm
            pixels = observation.frame.rgb
            if field == "epoch":
                epoch += 1
                state["epoch"] = epoch
            elif field == "frame_seq":
                seq += 1
            elif field == "dpr":
                dpr += 1
                state["viewport"]["dpr_ppm"] = dpr
            elif field == "geometry":
                pixels = np.zeros((3, 2, 3), dtype=np.uint8)
            elif field == "digest":
                pixels = np.full_like(pixels, 255)
            state["_perception"].update(epoch=epoch, frame_seq=seq, dpr_ppm=dpr)
            frame = FrameRef.from_rgb(observation.handle, epoch, seq, pixels)
            changed = Observation(observation.handle, epoch, seq, dpr, state, frame)
            return PerceptionResult(observation.handle, changed, None)

        for mode in ("omitted", "foreign_index", "duplicate", "wrong_handle", "wrong_type",
                     "epoch", "frame_seq", "dpr", "geometry", "digest"):
            with self.subTest(mode=mode):
                class CorruptRunner:
                    def map(self, function, jobs):
                        groups = [function(job) for job in jobs]
                        yield groups[0]
                        index, result = groups[1][0]
                        if mode == "omitted":
                            return
                        if mode == "foreign_index":
                            yield [(99, result)]
                        elif mode == "duplicate":
                            yield groups[0]
                        elif mode == "wrong_handle":
                            yield [(index, groups[0][0][1])]
                        elif mode == "wrong_type":
                            yield [(index, types.SimpleNamespace(**vars(result)))]
                        else:
                            yield [(index, altered(result, mode))]
                results = parallel.perceive_many([first, second], adapter=vision.FakeVisionEngine(), runner=CorruptRunner())
                self.assertIsNone(results[0].error)
                self.assertEqual(results[0].handle, first.handle)
                self.assertTrue(results[1].error.startswith("RUNNER_ERROR:"), results[1])
                self.assertIsNone(results[1].observation)
                self.assertEqual(results[1].handle, second.handle)

    def test_32_replayable_completion_and_partial_failure_schedules(self):
        for schedule in range(SCHEDULES):
            with self.subTest(seed=SEED + 3, schedule=schedule):
                rng = random.Random(SEED + 3 + schedule)
                parameters = _case(rng)
                registry, first, _ = _fixture(parameters, "scheduled-left")
                second_handle = registry.register("x11_window", "scheduled-right", object(), owned=False)
                second = _issued_observation(registry, second_handle, 0, np.zeros_like(first.frame.rgb), 1000000)
                following = _issued_observation(registry, first.handle, 1, np.full_like(first.frame.rgb, 255), parameters["dpr"])
                last = _issued_observation(registry, second_handle, 1, np.full_like(first.frame.rgb, 20), 1000000)
                order = [0, 1] if schedule % 2 == 0 else [1, 0]
                fail = schedule % 4 in (2, 3)
                results = parallel.perceive_many([first, second, following, last],
                                                adapter=vision.FakeVisionEngine(),
                                                runner=ControlledRunner(order, fail))
                self.assertEqual([result.handle for result in results],
                                 [first.handle, second.handle, first.handle, second.handle])
                for index, result in enumerate(results):
                    if fail and index % 2 != order[0]:
                        self.assertTrue(result.error.startswith("RUNNER_ERROR:"), result)
                        self.assertIsNone(result.observation)
                    else:
                        self.assertIsNone(result.error, result)
                        self.assertEqual(result.observation.frame_seq, index // 2)


class TestR7C1OriginalMethodGate(unittest.TestCase):
    def test_every_original_exact_id_and_every_changed_helper_is_covered(self):
        loader = unittest.TestLoader()
        original_ids = []
        changed_methods, changed_helpers = set(), set()
        affected_by_helpers = set()
        for path, baseline in LEGACY_INVENTORY.items():
            source = (ROOT / path).read_text(encoding="utf-8")
            current = _definitions(ast.parse(source))
            module_name = path[:-3].replace("/", ".")
            module = importlib.import_module(module_name)
            expected = set(baseline["methods"])
            current_ids = set()
            for name, value in vars(module).items():
                if isinstance(value, type) and issubclass(value, unittest.TestCase) and value.__module__ == module_name:
                    self.assertFalse(getattr(value, "__unittest_skip__", False), name)
                    self.assertFalse(getattr(value, "__unittest_expecting_failure__", False), name)
                    current_ids.update(module_name + "." + name + "." + method
                                       for method in loader.getTestCaseNames(value))
            self.assertEqual(current_ids, expected, "original test IDs removed, renamed or hidden: " + path)
            original_ids.extend(sorted(expected))
            # Compare every original function/helper, not just changed test bodies.
            helper_changed = False
            for qualified, digest in baseline["functions"].items():
                nodes = current.get(qualified, [])
                if len(nodes) != 1:
                    changed = True  # Removed legacy fallback/decorator helpers count too.
                else:
                    node = nodes[0]
                    start = min([node.lineno] + [decorator.lineno for decorator in node.decorator_list])
                    lines = source.splitlines(keepends=True)
                    text = "".join(lines[start - 1:node.end_lineno])
                    changed = hashlib.sha256(text.encode()).hexdigest() != digest
                if changed:
                    full_id = module_name + "." + qualified
                    (changed_methods if full_id in expected else changed_helpers).add(full_id)
                    helper_changed |= full_id not in expected
            for qualified in set(current).difference(baseline["functions"]):
                changed_helpers.add(module_name + "." + qualified)
                helper_changed = True
            # Conservatively run every original consumer of any changed/new helper.
            if helper_changed:
                affected_by_helpers.update(expected)
        self.assertEqual(len(original_ids), 37)
        self.assertEqual(len(set(original_ids)), 37)
        exercised = set()
        for exact_id in original_ids:
            with self.subTest(original_method=exact_id):
                suite = loader.loadTestsFromName(exact_id)
                self.assertEqual(suite.countTestCases(), 1, exact_id)
                inner = unittest.TestResult()
                suite.run(inner)
                self.assertEqual(inner.testsRun, 1, exact_id)
                self.assertEqual(inner.failures, [], exact_id)
                self.assertEqual(inner.errors, [], exact_id)
                self.assertEqual(inner.skipped, [], exact_id)
                self.assertEqual(inner.expectedFailures, [], exact_id)
                self.assertEqual(inner.unexpectedSuccesses, [], exact_id)
                exercised.add(exact_id)
        self.assertEqual(exercised, set(original_ids))
        self.assertTrue(changed_methods.issubset(exercised), changed_methods - exercised)
        self.assertTrue(affected_by_helpers.issubset(exercised), affected_by_helpers - exercised)
        self.assertTrue(changed_helpers, "fixture repair inventory unexpectedly unchanged")
        self.assertEqual(loader.errors, [])


if __name__ == "__main__":
    unittest.main()

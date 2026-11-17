"""Temporarily break production rules in memory; their unchanged tests must fail."""
from asset_factory import proc as af_proc
import copy
import inspect
import textwrap
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_f2u4_glb as witnesses
from tools.blender import glb_io as io
from tools.blender import glb_capture as capture
from tools.blender import helpers_contracts as contracts


def mutate_rule(module, name, old, new):
    """Compile one actual production predicate mutation, restoring it after use."""
    source = textwrap.dedent(inspect.getsource(getattr(module, name)))
    if source.count(old) != 1: raise AssertionError('mutation anchor drift: ' + name)
    namespace = dict(getattr(module, name).__globals__)
    exec(compile(source.replace(old, new), '<internal-rule-mutant>', 'exec'), namespace)
    return mock.patch.object(module, name, namespace[name])


class RuleMutants(unittest.TestCase):
    def kill(self, case, name, patch):
        outcome = unittest.TestResult()
        with patch:
            case(name).run(outcome)
        self.assertEqual(outcome.testsRun, 1)
        self.assertEqual(outcome.errors, [], outcome.errors)
        self.assertGreater(len(outcome.failures), 0, 'SURVIVED: ' + name)

    def test_identity_rewrite_mutant_goes_red(self):
        # Same document and BIN, different whitespace: semantic checks still pass
        # but the raw byte-preservation assertion must detect the rewrite.
        def rewrite(doc, binary, data, table):
            text = witnesses.json.dumps(doc, indent=2).encode()
            text += b' ' * (-len(text) % 4)
            chunks = witnesses.struct.pack('<II', len(text), io.JSON_CHUNK) + text
            chunks += witnesses.struct.pack('<II', len(binary), io.BIN_CHUNK) + binary
            return witnesses.struct.pack('<4sII', b'glTF', 2, 12+len(chunks)) + chunks
        self.kill(witnesses.GLBTests, 'test_u4_identity_keeps_raw_bytes',
                  mock.patch.object(io, '_visual_product', side_effect=rewrite))

    def test_helper_leak_mutant_goes_red(self):
        original = io._partition
        def leak(doc, table, visual):
            return copy.deepcopy(doc) if visual else original(doc, table, visual)
        self.kill(witnesses.GLBTests, 'test_u4_separates_declared_helpers',
                  mock.patch.object(io, '_partition', side_effect=leak))

    def test_suffix_authority_mutant_goes_red(self):
        original = io.role_table
        def suffix(doc, roles):
            table = original(doc, roles)
            return {i: 'collision' if doc['nodes'][i].get('name','').endswith('_col') else role
                    for i, role in table.items()}
        self.kill(witnesses.GLBTests, 'test_u4_suffix_is_not_role_authority',
                  mock.patch.object(io, 'role_table', side_effect=suffix))

    def test_feature_loss_blind_gate_mutant_goes_red(self):
        self.kill(witnesses.GLBTests, 'test_u4_roundtrip_preserves_scene_and_pbr',
                  mutate_rule(io, 'verify_partition', 'if binary != source_binary:', 'if False:'))

    def test_role_coverage_mutant_goes_red(self):
        def ignore(doc, roles):
            return {i: 'visual' for i, n in enumerate(doc['nodes']) if 'mesh' in n}
        self.kill(witnesses.GLBTests, 'test_role_coverage_and_invalid_types_refuse',
                  mock.patch.object(io, 'role_table', side_effect=ignore))

    def test_missing_metadata_mutant_goes_red(self):
        self.kill(witnesses.GLBTests, 'test_u4_missing_or_stale_sidecar_refuses',
                  mutate_rule(io, '_metadata', "if metadata is None: raise GlbError('metadata_missing', 3)",
                              "if metadata is None: raise GlbError('metadata_missing', 2)"))

    def test_missing_lod_mutant_goes_red(self):
        self.kill(witnesses.GLBTests, 'test_u4_missing_lod_is_not_renumbered',
                  mutate_rule(io, '_check_evidence', "if not path.is_file(): raise GlbError('required_lod_missing', 5)",
                              "if not path.is_file(): raise GlbError('required_lod_missing', 3)"))

    def test_unsupported_feature_mutant_goes_red(self):
        self.kill(witnesses.GLBTests, 'test_u4_unsupported_skin_and_external_texture_refuse',
                  mock.patch.object(io, 'validate_document', return_value=None))

    def test_caller_preservation_mutant_goes_red(self):
        self.kill(witnesses.GLBTests, 'test_u4_refusal_preserves_outputs',
                  mutate_rule(io, 'separate_glb', "raise GlbError('destination_exists')",
                              "shutil.rmtree(target)"))

    def test_normalization_default_mutant_goes_red(self):
        self.kill(witnesses.GLBTests, 'test_u4_missing_or_stale_sidecar_refuses',
                  mutate_rule(io, 'separate_glb', 'if type(normalize) is not bool or normalize:',
                              'if type(normalize) is not bool:'))

    def test_replace_race_mutant_goes_red(self):
        # The test injects a caller directory immediately before our primitive.
        # os.replace would erase it, even though it passed an earlier precheck.
        self.kill(witnesses.GLBTests, 'test_atomic_commit_does_not_replace_racing_empty_directory',
                  mock.patch.object(io, 'publish_directory', side_effect=witnesses.os.replace))

    def test_atomic_unsupported_fallback_mutant_goes_red(self):
        self.kill(witnesses.GLBTests, 'test_atomic_publish_unavailable_refuses_without_fallback',
                  mock.patch.object(io, 'publish_directory', side_effect=witnesses.os.replace))

    def test_closed_record_validator_mutant_goes_red(self):
        self.kill(witnesses.GLBTests, 'test_closed_primitive_rejects_unknown_fields',
                  mutate_rule(contracts, 'closed', 'if not isinstance(obj, dict) or set(obj) != set(fields):', 'if False:'))

    def test_upstream_failure_mutant_goes_red(self):
        self.kill(witnesses.GLBTests, 'test_bound_upstream_gate_and_paths',
                  mutate_rule(io, '_check_evidence', 'if any(rcs):', 'if False:'))

    def test_budget_mutant_goes_red(self):
        self.kill(witnesses.GLBTests, 'test_stale_source_and_budget_preserve_bytes',
                  mutate_rule(io, 'separate_glb', 'if _monotonic() >= deadline:', 'if False:'))

    def test_tamper_blind_receipt_mutant_goes_red(self):
        self.kill(witnesses.GLBTests, 'test_receipt_and_artifact_tampering_are_stale',
                  mutate_rule(io, 'verify_bundle', "if digest(content) != artifact['sha256']:", 'if False:'))

    def test_missing_capture_mutant_goes_red(self):
        self.kill(witnesses.GLBTests, 'test_u4_missing_capture_cannot_pass',
                  mutate_rule(capture, 'evaluate_captures', "if captures is None: return result(3, 'capture_missing')", "if captures is None: return result(0)"))

    def test_timeout_swallowed_mutant_goes_red(self):
        self.kill(witnesses.GLBTests, 'test_u4_timeout_reaps_owned_processes',
                  mutate_rule(capture, 'run_owned', 'af_proc.cleanup(job)', '[]'))

    def test_opaque_collision_gate_mutant_goes_red(self):
        self.kill(witnesses.GLBRenderTests, 'test_u4_opaque_col_blocks_visual_fails',
                  mutate_rule(capture, 'compare_visuals', "if partition['rc']: return result(5, *partition['reason_codes'])", "if partition['rc']: return result(0)"))

    def test_opaque_occluder_gate_mutant_goes_red(self):
        self.kill(witnesses.GLBRenderTests, 'test_u4_opaque_occ_blocks_visual_fails',
                  mutate_rule(capture, 'compare_visuals', "if partition['rc']: return result(5, *partition['reason_codes'])", "if partition['rc']: return result(0)"))


if __name__ == '__main__': unittest.main()

"""Actually disable contract rules and require independent tests to turn red.

Runtime mutants execute in isolated module dictionaries; schemas live in scratch.
The source files are never edited and their byte identities must remain stable.
"""
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from asset_factory import core, contracts
import test_f0u1_contracts as consumers


def _run(test_name):
    suite = unittest.defaultTestLoader.loadTestsFromName(test_name)
    output = io.StringIO()
    result = unittest.TextTestRunner(stream=output, verbosity=0).run(suite)
    return result, output.getvalue()


class MutationTests(unittest.TestCase):
    def test_independent_oracles_kill_runtime_and_schema_mutants(self):
        c = 'test_f0u1_contracts.ContractTests.'
        k = 'test_f0u1_kernel.KernelTests.'
        b = 'test_f0u1_kernel.ContractBoundaryTests.'
        paths = [ROOT/'asset_factory'/'core.py', ROOT/'asset_factory'/'contracts.py',
                 *sorted((ROOT/'slots').glob('*.schema.json'))]
        before = {path:hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
        baseline = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name) for name in
                    ('test_f0u1_contracts.ContractTests', 'test_f0u1_kernel.KernelTests',
                     'test_f0u1_kernel.ContractBoundaryTests'))
        output = io.StringIO()
        result = unittest.TextTestRunner(stream=output, verbosity=0).run(baseline)
        self.assertGreater(result.testsRun, 0)
        self.assertTrue(result.wasSuccessful(), output.getvalue())
        print('F0-U1 mutation baseline: GREEN tests=' + str(result.testsRun))
        # Each tuple names the disabled rule, source, unique edit and independent oracle.
        mutations = [
            ('canonical_json_digest', core, 'return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()', 'return "0" * 64', c+'test_schema_refs_and_canonical_roundtrip'),
            ('binary_file_digest', core, 'return digest.hexdigest()', 'return "0" * 64', k+'test_canonical_types_cycles_and_exact_digests'),
            ('canonical_sort', core, 'sort_keys=True', 'sort_keys=False', c+'test_schema_refs_and_canonical_roundtrip'),
            ('canonical_compact', core, 'separators=(",", ":")', 'separators=(", ", ": ")', c+'test_schema_refs_and_canonical_roundtrip'),
            ('canonical_unicode', core, 'ensure_ascii=False', 'ensure_ascii=True', c+'test_schema_refs_and_canonical_roundtrip'),
            ('canonical_newline', core, 'allow_nan=False) + "\\n"', 'allow_nan=False)', c+'test_schema_refs_and_canonical_roundtrip'),
            ('canonical_float', core, 'if obj is None or type(obj) in (str, int, bool):', 'if obj is None or type(obj) in (str, int, bool, float):', c+'test_schema_refs_and_canonical_roundtrip'),
            ('json_duplicate', core, 'if key in obj:', 'if False:', k+'test_strict_load_nested_duplicates_constants_and_limits'),
            ('json_float_parser', core, 'parse_float=_reject_number', 'parse_float=lambda value: int(float(value))', k+'test_strict_load_nested_duplicates_constants_and_limits'),
            ('json_constant_parser', core, 'parse_constant=_reject_number', 'parse_constant=lambda value: 0', k+'test_strict_load_nested_duplicates_constants_and_limits'),
            ('json_size', core, 'if len(raw) > max_bytes:', 'if False:', k+'test_strict_load_nested_duplicates_constants_and_limits'),
            ('json_budget_type', core, 'if type(max_bytes) is not int or max_bytes < 1:', 'if type(max_bytes) not in (int, bool) or max_bytes < 1:', k+'test_strict_load_nested_duplicates_constants_and_limits'),
            ('schema_preflight', core, '_preflight(schema, schema, set())', 'pass', k+'test_schema_preflight_covers_unvisited_branches'),
            ('unknown_keyword', core, 'if set(node) - _KEYWORDS:', 'if False:', k+'test_schema_preflight_covers_unvisited_branches'),
            ('schema_type', core, 'if not any(type(value) is _TYPES[t] for t in types):', 'if False:', k+'test_type_const_enum_and_union_identity'),
            ('schema_const', core, 'if "const" in node and canonical_json(value) != canonical_json(node["const"]):', 'if False:', k+'test_type_const_enum_and_union_identity'),
            ('schema_enum', core, 'if "enum" in node and not any(canonical_json(value) == canonical_json(v) for v in node["enum"]):', 'if False:', k+'test_type_const_enum_and_union_identity'),
            ('const_bool_alias', core, 'canonical_json(value) != canonical_json(node["const"])', 'value != node["const"]', k+'test_type_const_enum_and_union_identity'),
            ('enum_bool_alias', core, 'canonical_json(value) == canonical_json(v)', 'value == v', k+'test_type_const_enum_and_union_identity'),
            ('oneof_overlap', core, 'if matches != 1:', 'if matches == 0:', k+'test_type_const_enum_and_union_identity'),
            ('schema_required', core, 'for key in node.get("required", []):', 'for key in []:', k+'test_object_and_array_assertions'),
            ('schema_properties', core, '_matches(child, properties[key], root, _pointer(path, key), active, depth + 1)', 'pass', k+'test_object_and_array_assertions'),
            ('schema_closed', core, 'if extra is False:', 'if False:', k+'test_object_and_array_assertions'),
            ('schema_extra_type', core, 'if type(extra) is dict:', 'if False:', k+'test_object_and_array_assertions'),
            ('schema_minitems', core, 'if len(value) < node.get("minItems", 0):', 'if False:', k+'test_object_and_array_assertions'),
            ('schema_items', core, '_matches(child, node["items"], root, _pointer(path, index), active, depth + 1)', 'pass', k+'test_object_and_array_assertions'),
            ('schema_ref', core, '_matches(value, _resolve_ref(root, node["$ref"]), root, path, active, depth + 1)', 'pass', k+'test_local_refs_siblings_and_recursive_bounds'),
            ('recursive_bound', core, '_MAX_DEPTH = 128', '_MAX_DEPTH = 1024', k+'test_local_refs_siblings_and_recursive_bounds'),
            ('aggregate_error', core, 'if 2 in values:', 'if False:', k+'test_aggregation_all_pairs_and_order'),
            ('aggregate_fail', core, 'if 5 in values:', 'if False:', k+'test_aggregation_all_pairs_and_order'),
            ('aggregate_unknown', core, 'if not values or any(rc in (3, 4, 6) for rc in values):', 'if not values:', k+'test_aggregation_all_pairs_and_order'),
            ('aggregate_empty', core, 'if not values or any(rc in (3, 4, 6) for rc in values):', 'if any(rc in (3, 4, 6) for rc in values):', c+'test_worst_rc_non_numeric_precedence'),
            ('aggregate_invalid', core, 'if any(type(rc) is not int or rc not in RC for rc in values):', 'if False:', k+'test_aggregation_all_pairs_and_order'),
            ('publication_fsync', core, 'os.fsync(stream.fileno())', 'pass', k+'test_atomic_publication_order_and_failure_cleanup'),
            ('publication_replace', core, 'os.replace(staged, target)', 'target.write_bytes(content)', k+'test_atomic_publication_order_and_failure_cleanup'),
            ('publication_cleanup', core, 'staged.unlink(missing_ok=True)', 'pass', k+'test_atomic_publication_order_and_failure_cleanup'),
            ('emit_rc', core, 'if type(obj) is not dict or type(obj.get("rc")) is not int or obj["rc"] not in RC:', 'if type(obj) is not dict or "rc" not in obj:', k+'test_emit_and_cli_parse_refusals'),
            ('slot_selector', contracts, 'if payload["kind"] != kind:', 'if False:', b+'test_selector_binding_and_scalar_params'),
            ('slot_unknown', contracts, 'if type(slot) is not str or slot not in core.SLOTS:', 'if False:', c+'test_slot_contracts_validate_each_stage'),
            ('kind_unknown', contracts, 'if type(kind) is not str or kind not in ("request", "result"):', 'if False:', c+'test_slot_contracts_validate_each_stage'),
            ('input_exclusive', contracts, 'if len(roles) != len(set(roles)) or set(roles) not in _INPUTS[slot]:', 'if False:', c+'test_generate_and_motion_input_exclusivity'),
            ('input_multiplicity', contracts, 'if len(roles) != len(set(roles)) or set(roles) not in _INPUTS[slot]:', 'if False:', c+'test_all_role_multiplicities'),
            ('result_status', contracts, 'if type(rc) is not int or rc not in core.RC or payload["status"] != core.STATUS[rc]:', 'if False:', c+'test_result_requires_receipt_for_ok'),
            ('result_receipts', contracts, 'if rc == 0 and not payload["receipts"]:', 'if False:', c+'test_result_requires_receipt_for_ok'),
            ('result_outputs', contracts, 'if not complete:', 'if False:', c+'test_all_role_multiplicities'),
            ('result_duplicates', contracts, 'if len(roles) != len(set(roles)):', 'if False:', b+'test_strings_digests_and_partial_refusal_roles'),
            ('metric_absence', contracts, 'if type(reason) is not str or not reason.strip():', 'if False:', c+'test_metrics_unknown_is_not_zero'),
            ('metric_negative', contracts, 'elif value < 0:', 'elif False:', c+'test_metrics_unknown_is_not_zero'),
            ('revision_pin', contracts, 'if type(value) is not str or not value.strip() or value in _MUTABLE_REVISIONS:', 'if False:', c+'test_provider_rejects_unpinned_weights'),
            ('digest_grammar', contracts, 'if type(value) is not str or _DIGEST.fullmatch(value) is None:', 'if False:', c+'test_provider_rejects_unpinned_weights'),
            ('string_nonempty', contracts, 'if type(value) is not str or not value.strip():', 'if False:', c+'test_context_params_and_artifact_types'),
            ('timeout_positive', contracts, 'if context["timeout_s"] <= 0:', 'if False:', c+'test_context_params_and_artifact_types'),
            ('peak_provenance', contracts, 'if peak is not None and (peak < 0 or reference is None):', 'if False:', c+'test_metrics_unknown_is_not_zero'),
            ('cli_exclusive', contracts, 'if any(v is not None for v in (args.slot, args.kind, args.input_path)):', 'if False:', k+'test_emit_and_cli_parse_refusals'),
        ]
        killed = 0
        for name, target, old, new, oracle in mutations:
            with self.subTest(mutant=name):
                source = Path(target.__file__).read_text()
                expected_count = 2 if name in ('input_exclusive','input_multiplicity') else 1
                self.assertEqual(source.count(old), expected_count, name)
                mutant = types.ModuleType('af_mutant_' + name)
                mutant.__file__ = target.__file__
                exec(compile(source.replace(old,new), target.__file__, 'exec'), mutant.__dict__)
                with mock.patch.dict(target.__dict__, mutant.__dict__):
                    result, transcript = _run(oracle)
                self.assertGreater(result.testsRun, 0)
                self.assertFalse(result.wasSuccessful(), name + ' SURVIVED\n' + transcript)
                self.assertTrue(result.failures or result.errors, transcript)
                killed += 1
                print('F0-U1 mutant:', name, 'RED', 'tests=' + str(result.testsRun),
                      'failures=' + str(len(result.failures)), 'errors=' + str(len(result.errors)))
        # Schema branch rules also need observable mutants, rather than static shape checks alone.
        docs = {path.name:json.loads(path.read_text()) for path in (ROOT/'slots').glob('*.schema.json')}
        for name in ('provider_licence','model_weights','model_files','tool_weights','blocked_reason',
                     'request_closed','request_identity','receipt_descriptor','scalar_params'):
            with self.subTest(mutant=name), tempfile.TemporaryDirectory() as directory:
                scratch = Path(directory); (scratch/'slots').mkdir()
                changed = copy.deepcopy(docs)
                p = changed['provider.schema.json']
                g = changed['generate.schema.json']['$defs']
                if name == 'provider_licence':
                    p['required'].remove('licence'); oracle = c+'test_gate_rejects_planted_schema_failure'
                elif name == 'model_weights':
                    p['oneOf'][0]['properties']['weights']['minItems'] = 0
                    oracle = c+'test_provider_rejects_unpinned_weights'
                elif name == 'model_files':
                    p['oneOf'][0]['properties']['weights']['items']['properties']['files']['minItems'] = 0
                    p['properties']['weights']['items']['properties']['files']['minItems'] = 0
                    oracle = c+'test_provider_rejects_unpinned_weights'
                elif name == 'tool_weights':
                    p['oneOf'][1]['properties']['weights'] = {}
                    oracle = c+'test_provider_rejects_unpinned_weights'
                elif name == 'blocked_reason':
                    p['oneOf'][2]['required'] = []
                    oracle = k+'test_schema_branch_rules_directly'
                elif name == 'request_closed':
                    g['request']['additionalProperties'] = True
                    oracle = c+'test_slot_contracts_validate_each_stage'
                elif name == 'request_identity':
                    g['request']['properties']['schema'] = {'type':'string'}
                    oracle = c+'test_slot_contracts_validate_each_stage'
                elif name == 'receipt_descriptor':
                    g['receipt']['required'].remove('schema')
                    oracle = k+'test_schema_branch_rules_directly'
                else:
                    g['request']['properties']['params']['additionalProperties'] = True
                    oracle = c+'test_context_params_and_artifact_types'
                for filename, doc in changed.items():
                    (scratch/'slots'/filename).write_text(json.dumps(doc))
                with mock.patch.object(contracts, '_SCHEMAS', scratch/'slots'), \
                     mock.patch.object(consumers, 'ROOT', scratch), \
                     mock.patch('test_f0u1_kernel.ROOT', scratch):
                    result, transcript = _run(oracle)
                self.assertGreater(result.testsRun, 0)
                self.assertFalse(result.wasSuccessful(), name + ' SURVIVED\n' + transcript)
                self.assertTrue(result.failures or result.errors)
                killed += 1
                print('F0-U1 mutant:', name, 'RED', 'tests=' + str(result.testsRun),
                      'failures=' + str(len(result.failures)), 'errors=' + str(len(result.errors)))
        self.assertEqual(killed, len(mutations) + 9)
        self.assertEqual(before, {path:hashlib.sha256(path.read_bytes()).hexdigest() for path in paths})
        print('F0-U1 mutation summary:', killed, 'killed; 0 survived; source and schemas unchanged')

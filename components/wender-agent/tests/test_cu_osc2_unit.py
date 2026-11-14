"""OSC2 live-loop grounding: independently verified offsets and negative controls."""
import copy
import io
import json
import unittest

from tests import test_cu_loop_unit as fixtures
from tests.test_cu_loop_unit import Describer, proposal
from wender.learn.cu_loop import ReplayDecider, run_loop
from wender.learn.model_client import ActionValidationError, validate_action, _prompt
from wender.perceive.osc2 import join_osc2


class TestOsc2(unittest.TestCase):
    # @anchor:wa.tests.test_cu_osc2_unit.TestOsc2.setUp
    def setUp(self):
        self.nodes = [dict(id=str(i), name=name, role='button', actionable=True,
                           parent=None, states=dict(enabled='true', visible='true'),
                           box=[x, y, w, h]) for i, (name, x, y, w, h) in enumerate([
                               ('Products', 10, 10, 20, 10), ('Equity', 60, 30, 30, 12),
                               ('Production', 100, 70, 40, 14)])]
        self.frame = dict(generation='episode', width=200, height=200,
                          lane=dict(elements=[dict(id='e'+n['id'], name=n['name'], role=n['role'],
                                                   state_node_id=n['id'], actionable=True)
                                              for n in self.nodes], state=dict(nodes=self.nodes)))
        self.desc = dict(marks=[dict(id=i+1, x=n['box'][0], y=n['box'][1]+46,
                                    w=n['box'][2], h=n['box'][3]) for i, n in enumerate(self.nodes)])

    # @anchor:wa.tests.test_cu_osc2_unit.TestOsc2.test_verified_translation_and_center
    def test_verified_translation_and_center(self):
        join_osc2(self.frame, self.desc, {})
        self.assertEqual(self.frame['osc2_transform']['dy'], 46)
        result = validate_action(proposal('click', element_id='e0'), 200, 200, self.frame['lane']['elements'])
        self.assertEqual((result['action']['x'], result['action']['y']), (20, 61))

    # @anchor:wa.tests.test_cu_osc2_unit.TestOsc2.test_gap_next_to_named_element_must_fail
    def test_gap_next_to_named_element_must_fail(self):
        join_osc2(self.frame, self.desc, {})
        click = proposal('click', x=31, y=61)
        click['observation'] = 'Click Products button'
        with self.assertRaisesRegex(ActionValidationError, 'outside elements names an element'):
            validate_action(click, 200, 200, self.frame['lane']['elements'])

    # @anchor:wa.tests.test_cu_osc2_unit.TestOsc2.test_covered_raw_click_must_fail_but_uncovered_is_allowed
    def test_covered_raw_click_must_fail_but_uncovered_is_allowed(self):
        join_osc2(self.frame, self.desc, {})
        with self.assertRaisesRegex(ActionValidationError, 'covers a target'):
            validate_action(proposal('click', x=20, y=61), 200, 200, self.frame['lane']['elements'])
        validate_action(proposal('click', x=180, y=180), 200, 200, self.frame['lane']['elements'])

    # @anchor:wa.tests.test_cu_osc2_unit.TestOsc2.test_no_default_transform_and_no_cross_episode_cache
    def test_no_default_transform_and_no_cross_episode_cache(self):
        cache = {}
        with self.assertRaisesRegex(ValueError, 'unproven'):
            join_osc2(copy.deepcopy(self.frame), {}, cache)
        join_osc2(copy.deepcopy(self.frame), self.desc, cache)
        join_osc2(copy.deepcopy(self.frame), {}, cache)
        self.frame['generation'] = 'another_episode'
        with self.assertRaisesRegex(ValueError, 'unproven'):
            join_osc2(self.frame, {}, cache)

    # @anchor:wa.tests.test_cu_osc2_unit.TestOsc2.test_collapsed_disclosure_children_hidden_and_expansion_prompted
    def test_collapsed_disclosure_children_hidden_and_expansion_prompted(self):
        self.nodes[0]['states']['expanded'] = 'false'
        self.nodes[0]['name'] = 'More screens'
        self.nodes[1]['parent'] = '0'
        cache = {}
        join_osc2(self.frame, self.desc, cache)
        self.assertEqual([e['id'] for e in self.frame['lane']['elements']], ['e0', 'e2'])
        text = _prompt(dict(goal='open menu', elements=self.frame['lane']['elements']))
        self.assertIn('"expanded": false', text)
        self.assertNotIn('Equity', text)
        self.setUp()
        self.nodes[0]['states']['expanded'] = 'true'
        self.nodes[1]['parent'] = '0'
        join_osc2(self.frame, self.desc, cache)
        self.assertIn('e1', [e['id'] for e in self.frame['lane']['elements']])

    # @anchor:wa.tests.test_cu_osc2_unit.TestOsc2.test_current_boxes_joined_again_with_episode_transform
    def test_current_boxes_joined_again_with_episode_transform(self):
        cache = {}
        join_osc2(copy.deepcopy(self.frame), self.desc, cache)
        self.nodes[0]['box'][0] += 5
        join_osc2(self.frame, {}, cache)
        self.assertEqual(self.frame['lane']['elements'][0]['bbox']['x'], 15)

    # @anchor:wa.tests.test_cu_osc2_unit.TestOsc2.test_disabled_target_cannot_click
    def test_disabled_target_cannot_click(self):
        cache = {}
        join_osc2(copy.deepcopy(self.frame), self.desc, cache)
        self.nodes[0]['states']['enabled'] = 'false'
        join_osc2(self.frame, {}, cache)
        with self.assertRaises(ActionValidationError):
            validate_action(proposal('click', element_id='e0'), 200, 200,
                            self.frame['lane']['elements'])

    # @anchor:wa.tests.test_cu_osc2_unit.TestOsc2.test_model_receives_table_target_mapping_and_expansion_state
    def test_model_receives_table_target_mapping_and_expansion_state(self):
        from pathlib import Path
        import tempfile
        from wender.learn.cu_loop import _encode_png
        from wender.learn.model_client import ModelClient
        self.nodes[0]['states']['expanded'] = 'false'
        join_osc2(self.frame, self.desc, {})
        class Backend:
            def generate(inner, request):
                inner.request = request
                return proposal('click', element_id='e0')
        backend = Backend()
        with tempfile.TemporaryDirectory(dir='/dev/shm') as root:
            path = Path(root) / 'frame.png'
            path.write_bytes(_encode_png(bytes(200*200*3), 200, 200))
            result = ModelClient(backend).act('Products', str(path), dict(schema="ve.cu_describe/v1", width=200, height=200),
                                               elements=self.frame['lane']['elements'])
        self.assertEqual(result['action']['y'], 61)
        self.assertIn({'element_id': 'e0'}, backend.request['element_table']['targets'].values())
        self.assertIn('"expanded": false', _prompt(backend.request))

    # @anchor:wa.tests.test_cu_osc2_unit.TestOsc2.test_allowed_raw_click_flagged_in_trace
    def test_allowed_raw_click_flagged_in_trace(self):
        fixture = fixtures.TestCuLoop()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        _, rows = fixture.run_case(fixture.capsule(['a', 'b']),
                                   [proposal('click', x=15, y=15)], max_steps=1)
        self.assertTrue(rows[0]['grounding']['raw_coordinates'])

    # @anchor:wa.tests.test_cu_osc2_unit.TestOsc2.test_duplicate_nodes_rejected
    def test_duplicate_nodes_rejected(self):
        self.nodes.append(copy.deepcopy(self.nodes[0]))
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            join_osc2(self.frame, self.desc, {})

    # @anchor:wa.tests.test_cu_osc2_unit.TestOsc2.test_live_loop_passes_enriched_targets_and_executes_center
    def test_live_loop_passes_enriched_targets_and_executes_center(self):
        fixture = fixtures.TestCuLoop()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        cap = fixture.capsule(['before', 'after'])
        # Use the existing fake capsule transport with current OSC2 client boxes.
        for n in self.nodes:
            n['box'] = [n['box'][0]//10, n['box'][1]//10, 2, 1]
        self.frame.update(width=16, height=16)
        self.desc['marks'] = [dict(id=i+1, x=n['box'][0], y=n['box'][1]+4, w=2, h=1)
                              for i, n in enumerate(self.nodes)]
        original = cap.frame
        def frame(**kwargs):
            row = original(**kwargs)
            row['lane'].update(copy.deepcopy(self.frame['lane']))
            return row
        cap.frame = frame
        desc = Describer()
        original_describe = desc.describe
        def describe(*args, **kwargs):
            return dict(original_describe(*args, **kwargs), **self.desc)
        desc.describe = describe
        trace = io.StringIO()
        run_loop(cap, desc, ReplayDecider([proposal('click', element_id='e0')]),
                 goal='Products', trace=trace, max_steps=1, clock=fixture.clock, sleep=fixture.clock.sleep)
        self.assertEqual((cap.inputs[0]['x'], cap.inputs[0]['y']), (2, 5))
        row = json.loads(trace.getvalue().splitlines()[0])
        self.assertFalse(row['grounding']['raw_coordinates'])
        self.assertEqual(row['grounding']['osc2_transform']['dy'], 4)

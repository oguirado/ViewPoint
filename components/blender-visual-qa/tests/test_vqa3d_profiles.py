"""Profile contracts and discriminating mutants; optional runtimes are never loaded."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from blender_visual_qa.drive.png import encode
from vqa3d import profile as p
from vqa3d.canon import canonical_json, sha256_canonical
from vqa3d.contract import Observation
from vqa3d.receipt import make_receipt


class Scene:
    id, version, status = 'blender', '1', 'implemented'

    def __init__(self, *, capture_engine='cycles', capture_samples=48, width=96, height=72):
        self.state = {
            'frame_start':1, 'frame_end':3, 'frame_current':1, 'source_camera':'Camera',
            'render':copy.deepcopy(p.load('film-animation')['render']),
            'objects': {
                'foot.L': {'type':'MESH', 'world_bbox_min_um':[0,0,2500], 'parent_type':'BONE', 'parent':'Rig.a',
                           'parent_bone':'foot.L', 'world_bbox_max_um':[100000,100000,100000], 'tris':100},
                'Floor': {'type':'MESH', 'world_bbox_min_um':[-900000,-900000,-2000],
                          'world_bbox_max_um':[900000,900000,0], 'tris':2},
                'Camera': {'type':'CAMERA'},
                'Rig.a': {'type':'ARMATURE', 'bones':{
                    'hand.R':{'rotation_mdeg':[0,0,0], 'head_world_um':[0,0,100000], 'tail_world_um':[0,0,0],
                              'quat_local_ppm':[1000000,0,0,0], 'quat_world_ppm':[1000000,0,0,0]},
                    'foot.L':{'rotation_mdeg':[0,0,0], 'head_world_um':[0,0,80000], 'tail_world_um':[0,-150000,10000],
                              'quat_local_ppm':[1000000,0,0,0], 'quat_world_ppm':[1000000,0,0,0]}}},
                'prop/x~': {'type':'MESH', 'parent_type':'BONE', 'parent':'Rig.a', 'parent_bone':'hand.R',
                            'world_location_um':[0,0,50000], 'attach':'grip', 'tris':20,
                            'world_bbox_min_um':[0,0,0], 'world_bbox_max_um':[10000,10000,10000]},
                'Key': {'type':'LIGHT','energy_mw':1000,'color_ppm':[1000000,1000000,1000000]}},
            'images': {'image/a':{'size_px':[1024,1024]}}, 'interpenetration_pairs':[]}
        self.capabilities = dict(capture_available=True, scene_state=True, act=True)
        self.actions = []
        self.samples = []
        self.offset = 0

    def probe(self, render=False):
        return dict(available=True, capabilities=self.capabilities, engines=['CYCLES','BLENDER_WORKBENCH'],
                    cycles_devices={'CPU':['CPU']}, denoisers=['OPENIMAGEDENOISE'], view_transforms=['AgX'],
                    looks={'AgX':['AgX - Medium High Contrast']})

    def scene_state(self):
        return copy.deepcopy(self.state)

    def sample(self, frames, paths):
        self.samples.append((frames, paths))
        return {str(f): {path:p.lookup(self.state, path) for path in paths} for f in frames}

    def act(self, action):
        self.actions.append(action)
        if action['op'] == 'set_frame':
            self.state['frame_current'] = action['frame']
        return {'ok':True}

    def capture(self, tag):
        rgb = bytes([10,20,30,40,50,60,70,80,90,100,110,120])
        return Observation.create(self.id, tag, self.scene_state(), encode(2,2,rgb), camera_source='scene', camera_name='Camera')

    def close(self):
        pass


class Sim(Scene):
    id = 'mujoco'

    def __init__(self, offset=0):
        super().__init__()
        self.state = dict(time_us=0, ncon=0, contact_geom_pairs=[], qpos=[0])
        self.offset = offset

    def act(self, action):
        if action['op'] == 'step':
            self.state.update(time_us=self.state['time_us']+action['n']*2000,
                              ncon=1, contact_geom_pairs=[[0,1]], qpos=[self.state['qpos'][0]+action['n']])
        return {'ok':True}

    def capture(self, tag):
        v = (self.state['qpos'][0] + self.offset) % 256
        return Observation.create(self.id, tag, self.scene_state(), encode(2,2,bytes([v,20,30])*4))


def selected(profile, *ids):
    result = copy.deepcopy(profile)
    result['checks'] = [c for c in result['checks'] if c['id'] in ids]
    result['provenance'] = {path:'target' for path in p.numeric_paths(result)}
    return result


class Profiles(unittest.TestCase):
    def test_all_shipped_profiles_canonical_and_provenance_complete(self):
        for identifier in ('film-animation','game-realtime','hyperreal-still','simulation'):
            with self.subTest(profile=identifier):
                profile = p.load(identifier)
                self.assertEqual((p.DIRECTORY/(identifier+'.json')).read_text(), canonical_json(profile)+'\n')
                self.assertTrue(set(p.numeric_paths(profile)) <= set(profile['provenance']))

    def test_schema_refuses_float_bool_unknown_nested_and_missing_provenance(self):
        cases = [('float',lambda d:d['capture'].update(samples=1.5)),
                 ('bool',lambda d:d['render'].update(motion_blur=True)),
                 ('unknown',lambda d:d.update(extra='x')),
                 ('nested',lambda d:d['checks'][0]['select'].update(typo='x')),
                 ('parameter',lambda d:d['defaults']['feet_floor'].update(floor=0)),
                 ('no-provenance',lambda d:d['provenance'].pop('/capture/samples')),
                 ('bad-origin',lambda d:d['provenance'].update({'/capture/samples':'measured:'})),
                 ('bad-frames',lambda d:d['checks'][0].update(frames=[1,3,0]))]
        for name, mutate in cases:
            with self.subTest(case=name):
                profile = p.load('film-animation')
                mutate(profile)
                with self.assertRaises(ValueError):
                    p.validate(profile)

    def test_cycle_missing_parent_and_path_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, parent in [('a','b'),('b','a')]:
                (Path(tmp)/(name+'.json')).write_text(json.dumps({'schema':p.SCHEMA,'id':name,'extends':parent}))
            with self.assertRaisesRegex(ValueError,'profile_extends_cycle'):
                p.load('a',tmp)
            with self.assertRaises(FileNotFoundError):
                p.load('missing',tmp)
            with self.assertRaisesRegex(ValueError,'invalid_id'):
                p.load('../film-animation',tmp)

    def test_inheritance_resolves_before_identity_and_child_needs_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = p.load('film-animation')
            (Path(tmp)/'film-animation.json').write_text(canonical_json(base))
            child = dict(schema=p.SCHEMA,id='child',extends='film-animation',capture={'samples':64},
                         provenance={'/capture/samples':'target'})
            (Path(tmp)/'child.json').write_text(canonical_json(child))
            resolved = p.resolve('child',tmp)
            self.assertNotIn('extends',resolved)
            self.assertEqual(resolved['capture']['samples'],64)
            self.assertEqual(resolved['render'],base['render'])
            self.assertEqual(p.identity(resolved)['sha256'],sha256_canonical(resolved))
            child.pop('provenance')
            (Path(tmp)/'child.json').write_text(canonical_json(child))
            with self.assertRaisesRegex(ValueError,'number_without_provenance'):
                p.resolve('child',tmp)

    def test_duplicate_json_key_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp)/'a.json').write_text('{"schema":"vqa3d.profile/v1","id":"a","id":"b"}')
            with self.assertRaisesRegex(ValueError,'duplicate_json_key'):
                p.load('a',tmp)

    def test_expansion_is_deterministic_sorted_and_escapes_names(self):
        profile = p.load('film-animation')
        state = Scene().scene_state()
        first = p.expand(profile,state)
        state['objects'] = dict(reversed(list(state['objects'].items())))
        self.assertEqual(first,p.expand(profile,state))
        attachment = next(s for s in first if s['check_id'].startswith('attachments:'))
        self.assertEqual(attachment['check_id'],'attachments:prop/x~')
        self.assertEqual(attachment['obligation']['a'],'/objects/prop~1x~0/world_location_um')
        self.assertEqual(attachment['obligation']['tail'],'/objects/Rig.a/bones/hand.R/tail_world_um')
        self.assertEqual(attachment['obligation']['head'],'/objects/Rig.a/bones/hand.R/head_world_um')
        self.assertEqual(attachment['obligation']['frames'],[1,3,1])
        self.assertEqual([s['check_id'] for s in first if s['check_id'].startswith('bone-continuity')],
                         ['bone-continuity:Rig.a:foot.L','bone-continuity:Rig.a:hand.R'])

    def test_empty_gate_selector_never_passes(self):
        profile = selected(p.load('film-animation'),'feet-floor')
        adapter = Scene()
        adapter.state['objects'].pop('foot.L')
        receipts,result,_ = p.run(profile,adapter)
        self.assertEqual(result['final_verdict'],'CANNOT_SEE')
        # A foot bone without sole geometry can prove penetration, never clearance.
        self.assertTrue(receipts[-1]['error'].startswith('sole_geometry_unresolved:foot_bone_joints_clear:Rig.a:foot.L'))
        adapter.state['objects']['Rig.a']['bones'].pop('foot.L')
        receipts,result,_ = p.run(profile,adapter)
        self.assertEqual(result['final_verdict'],'CANNOT_SEE')
        self.assertEqual(receipts[-1]['error'],'selector_empty:feet-floor')

    def test_defaults_explicit_precedence_without_mutating_input(self):
        profile = p.load('film-animation')
        original = {'kind':'frame_change','pixel_tolerance':0}
        actual = p.apply_defaults(original,profile)
        self.assertEqual(actual,{'kind':'frame_change','pixel_tolerance':0,'min_changed_ppm':1000})
        self.assertEqual(original,{'kind':'frame_change','pixel_tolerance':0})

    def test_receipt_identity_is_covered_by_hash_and_schema_unchanged(self):
        profile, adapter = p.load('film-animation'), Scene()
        adapter.profile_identity = p.identity(profile)
        receipt = make_receipt(adapter,{'kind':'no_change'},None,None,[{'oracle':'test','verdict':'CANNOT_SEE','reason':'absent'}])
        digest = receipt.pop('receipt_sha256')
        self.assertEqual(receipt['schema'],'vqa3d.receipt/v1')
        self.assertEqual(receipt['profile'],p.identity(profile))
        self.assertEqual(digest,sha256_canonical(receipt))
        receipt['profile']['id'] = 'mutant'
        self.assertNotEqual(digest,sha256_canonical(receipt))

    def test_missing_capture_constructor_is_typed_and_state_checks_run(self):
        class Legacy:
            def __init__(self, **kwargs): pass
        profile = selected(p.load('film-animation'),'feet-floor','nonblank')
        with self.assertRaisesRegex(p.CannotSee,'capture_engine_unsupported'):
            p.adapter_kwargs(profile,Legacy)
        self.assertEqual(p.adapter_kwargs(profile,Scene)['capture_samples'],48)
        receipts,result,_ = p.run(profile,Scene(),capture_reason='capture_engine_unsupported')
        self.assertEqual(next(r for r in receipts if r['check_id'].startswith('feet-floor'))['final_verdict'],'PASS')
        self.assertEqual(receipts[-1]['error'],'capture_engine_unsupported')
        self.assertEqual(result['final_verdict'],'CANNOT_SEE')

    def test_film_mutant_budget_refused_and_penetration_fails(self):
        profile = p.load('film-animation')
        profile['budgets'] = {'max_object_tris':1}
        profile['provenance']['/budgets/max_object_tris'] = 'target'
        with self.assertRaisesRegex(ValueError,'no_asset_budgets'):
            p.validate(profile)
        profile = selected(p.load('film-animation'),'feet-floor')
        adapter = Scene()
        adapter.state['objects']['foot.L']['world_bbox_min_um'][2] = -38098
        receipts,result,_ = p.run(profile,adapter)
        self.assertEqual(result['final_verdict'],'FAIL')
        self.assertIn('frame=1',receipts[-1]['error'])
        self.assertIn('foot_below_floor:foot.L',receipts[-1]['error'])
        self.assertIn('clearance_um=-38098',receipts[-1]['error'])

    def test_game_mutant_missing_tris_never_zero_and_budget_is_consumed(self):
        profile = selected(p.load('game-realtime'),'triangles')
        adapter = Scene()
        adapter.state['objects']['foot.L'].pop('tris')
        receipts,result,_ = p.run(profile,adapter)
        self.assertEqual(result['final_verdict'],'CANNOT_SEE')
        self.assertEqual(receipts[-1]['error'],'state_path_absent:/objects/foot.L/tris')
        adapter.state['objects']['foot.L']['tris'] = 100001
        self.assertEqual(p.run(profile,adapter)[1]['final_verdict'],'FAIL')

    def test_hyper_mutant_missing_light_not_presence_pass(self):
        profile = selected(p.load('hyperreal-still'),'lights')
        adapter = Scene()
        adapter.state['objects']['Key'].pop('color_ppm')
        receipts,result,_ = p.run(profile,adapter)
        self.assertEqual(result['final_verdict'],'CANNOT_SEE')
        self.assertEqual(receipts[-1]['error'],'state_path_absent:/objects/Key/color_ppm')
        adapter.state['objects']['Key']['color_ppm'] = [1000000]*3
        adapter.state['objects']['Key']['energy_mw'] = 0
        self.assertEqual(p.run(profile,adapter)[1]['final_verdict'],'FAIL')

    def test_simulation_mutant_unequal_receipts_fails(self):
        profile = selected(p.load('simulation'),'identical-receipts')
        self.assertEqual(p.run(profile,Sim(),factory=Sim)[1]['final_verdict'],'PASS')
        offsets = iter([0,1])
        receipts,result,_ = p.run(profile,Sim(),factory=lambda:Sim(next(offsets)))
        self.assertEqual(result['final_verdict'],'FAIL')
        self.assertIn('nondeterministic_replay',receipts[-1]['error'])

    def test_failed_advisory_cannot_fail_gate(self):
        profile = selected(p.load('film-animation'),'feet-floor','interpenetration')
        adapter = Scene()
        adapter.state['interpenetration_pairs'] = [['a','b']]
        receipts,result,_ = p.run(profile,adapter)
        self.assertEqual(receipts[-1]['final_verdict'],'FAIL')
        self.assertEqual(result['final_verdict'],'ADVISORY')

    def test_bone_missing_rotation_typed_and_shortest_arc_continuity(self):
        # The Euler shortest-arc path stays available to profiles that still declare it.
        profile = selected(p.load('film-animation'),'bone-continuity')
        profile['checks'][0].update(kind='max_delta_over_frames',
                                    params={'path':'/objects/{object}/bones/{bone}/rotation_mdeg','max_delta':45000})
        profile['provenance'] = {path:'target' for path in p.numeric_paths(profile)}
        adapter = Scene()
        adapter.state['objects']['Rig.a']['bones'].pop('foot.L')
        adapter.state['objects']['Rig.a']['bones']['hand.R'].pop('rotation_mdeg')
        receipts,_,_ = p.run(profile,adapter)
        self.assertEqual(receipts[-1]['error'],'state_path_absent:/objects/Rig.a/bones/hand.R/rotation_mdeg')
        adapter.state['objects']['Rig.a']['bones']['hand.R']['rotation_mdeg'] = [0,0,0]
        with patch.object(adapter,'sample',return_value={str(f):{'/objects/Rig.a/bones/hand.R/rotation_mdeg':[v,0,0]}
                          for f,v in [(1,179000),(2,-179000),(3,-178000)]}):
            self.assertEqual(p.run(profile,adapter)[1]['final_verdict'],'PASS')
        with patch.object(adapter,'sample',return_value={str(f):{'/objects/Rig.a/bones/hand.R/rotation_mdeg':[v,0,0]}
                          for f,v in [(1,0),(2,90000),(3,0)]}):
            self.assertEqual(p.run(profile,adapter)[1]['final_verdict'],'FAIL')

    def test_missing_frames_never_defaults_to_current_frame(self):
        profile = selected(p.load('film-animation'),'feet-floor')
        adapter = Scene()
        adapter.state.pop('frame_start')
        receipts,result,_ = p.run(profile,adapter)
        self.assertEqual(result['final_verdict'],'CANNOT_SEE')
        self.assertEqual(receipts[-1]['error'],'state_path_absent:/frame_start')
        self.assertEqual(p.run(profile,adapter,frames=[1,3,1])[1]['final_verdict'],'PASS')

    def test_capability_first_and_failure_does_not_suppress_state(self):
        profile = selected(p.load('film-animation'),'feet-floor','nonblank')
        adapter = Scene()
        with patch.object(adapter,'probe',return_value={'engines':[]}):
            receipts,result,_ = p.run(profile,adapter)
        self.assertEqual(receipts[0]['check_id'],'requires')
        self.assertEqual(receipts[1]['final_verdict'],'PASS')
        self.assertEqual(receipts[2]['final_verdict'],'CANNOT_SEE')
        self.assertEqual(result['final_verdict'],'CANNOT_SEE')

    def test_empty_light_selector_and_absent_images_are_unobservable(self):
        adapter = Scene()
        adapter.state['objects'].pop('Key')
        result = p.run(selected(p.load('hyperreal-still'),'lights'),adapter)
        self.assertEqual(result[1]['final_verdict'],'CANNOT_SEE')
        adapter.state.pop('images')
        receipts,_,_ = p.run(selected(p.load('game-realtime'),'textures'),adapter)
        self.assertEqual(receipts[-1]['error'],'state_path_absent:/images')

    def test_trace_retains_temporal_samples_and_receipts_are_sealed(self):
        profile = selected(p.load('film-animation'),'feet-floor')
        receipts,_,trace = p.run(profile,Scene())
        self.assertTrue(any(e['kind']=='sample' for e in trace['events']))
        digest = trace.pop('trace_sha256')
        self.assertEqual(digest,sha256_canonical(trace))
        for receipt in receipts:
            payload = dict(receipt)
            digest = payload.pop('receipt_sha256')
            self.assertEqual(digest,sha256_canonical(payload))

    def test_independent_simulation_traces_replay_without_runtime(self):
        from vqa3d.fakes import ReplayAdapter
        from vqa3d.check import verify_step
        profile = selected(p.load('simulation'),'identical-receipts')
        _,_,trace = p.run(profile,Sim(),factory=Sim)
        for recorded in trace['replay_runs']:
            replay = ReplayAdapter(recorded)
            replay.profile_identity = p.identity(profile)
            actual = [verify_step(replay,s['action'],s['obligation']) for s in recorded['scenario']]
            self.assertEqual(actual,recorded['receipts'])

    def test_normal_cli_applies_defaults_and_explicit_parameter_wins(self):
        import contextlib
        import io
        from vqa3d.__main__ import main
        from vqa3d import registry
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'scenario.json'
            path.write_text(json.dumps([{'action':{'op':'step','n':120},'obligation':{'kind':'frame_change','pixel_tolerance':0}}]))
            stream = io.StringIO()
            with patch.dict(registry.ADAPTERS,{'mujoco':Sim}), patch('vqa3d.__main__.create_adapter',return_value=Sim()), contextlib.redirect_stdout(stream):
                code = main(['check','--adapter','mujoco','--profile','simulation','--scenario',str(path)])
            receipt = json.loads(stream.getvalue())
            self.assertEqual(code,0)
            self.assertEqual(receipt['obligation']['pixel_tolerance'],0)
            self.assertEqual(receipt['obligation']['min_changed_ppm'],1000)
            self.assertEqual(receipt['profile'],p.identity(p.load('simulation')))

    def test_schema_requires_effective_parameters_and_coherent_budget(self):
        profile = p.load('game-realtime')
        profile['checks'][0]['params']['max_object_tris'] = 1
        profile['provenance']['/checks/0/params/max_object_tris'] = 'target'
        with self.assertRaisesRegex(ValueError,'conflicts_with_budget'):
            p.validate(profile)
        profile = p.load('film-animation')
        self.assertEqual(profile['checks'][2]['id'],'bone-continuity')
        profile['checks'][2]['params'].pop('path')
        with self.assertRaisesRegex(ValueError,'required_parameter_missing:bone_rotation_continuity'):
            p.validate(profile)

    def test_pixels_use_requested_frame_coverage_and_restore_current(self):
        profile = selected(p.load('film-animation'),'nonblank','noise')
        adapter = Scene()
        adapter.state['frame_current'] = 2
        with patch.object(adapter,'capture',wraps=adapter.capture) as captures:
            _,result,trace = p.run(profile,adapter)
        self.assertEqual(result['final_verdict'],'ADVISORY')
        self.assertEqual(captures.call_count,3)
        self.assertEqual(adapter.state['frame_current'],2)
        self.assertEqual(len(trace['observations']),3)

    def test_advisory_error_cannot_worsen_an_existing_gate_failure(self):
        profile = selected(p.load('film-animation'),'feet-floor','interpenetration')
        adapter = Scene()
        adapter.state['objects']['foot.L']['world_bbox_min_um'][2] = -1
        adapter.state.pop('interpenetration_pairs')
        receipts,result,_ = p.run(profile,adapter)
        self.assertEqual(receipts[-1]['final_verdict'],'CANNOT_SEE')
        self.assertEqual(result['final_verdict'],'FAIL')

    def test_source_settings_allow_higher_sample_quality(self):
        adapter = Scene()
        adapter.state['render']['samples'] = 128
        profile = selected(p.load('film-animation'),'render-settings')
        self.assertEqual(p.run(profile,adapter)[1]['final_verdict'],'PASS')
        adapter.state['render']['samples'] = 8
        self.assertEqual(p.run(profile,adapter)[1]['final_verdict'],'FAIL')

    def test_refusal_preserves_existing_trace(self):
        import contextlib
        import io
        from vqa3d.__main__ import main
        with tempfile.TemporaryDirectory() as tmp:
            trace = Path(tmp)/'trace.json'
            trace.write_bytes(b'sentinel')
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(['profile','check','--profile','absent-profile','--record-trace',str(trace)])
            self.assertEqual(code,2)
            self.assertEqual(trace.read_bytes(),b'sentinel')


if __name__ == '__main__':
    unittest.main()

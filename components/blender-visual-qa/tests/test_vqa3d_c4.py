"""Contact, sampling, child lifecycle and declaration regressions."""
import copy
import json
import os
from pathlib import Path
import signal
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from vqa3d import profile as p
from vqa3d.adapters import blender as driver, blender_side as side
from vqa3d.adapters.blender_live import BlenderLiveAdapter
from vqa3d.check import evaluate, validate_obligation, verify_step
from vqa3d.contract import Observation
from vqa3d.fakes import RecordingAdapter
from tests.test_vqa3d_film import Shot, one, reason, mutated, HEAD, TAIL
from tests.test_vqa3d_profiles import selected
from blender_visual_qa.drive.png import encode


class Contact(unittest.TestCase):
    def test_declared_floor_uses_xy_height_and_declaration_not_name(self):
        objects = {
            'street': {'role':'floor', 'world_bbox_min_um':[-10,-10,-1], 'world_bbox_max_um':[10,10,0]},
            'LOBBY_Floor': {'role':'floor', 'world_bbox_min_um':[20,20,0], 'world_bbox_max_um':[30,30,300000]},
            'Floor_bad': {'world_bbox_min_um':[-10,-10,0], 'world_bbox_max_um':[10,10,900000]}}
        self.assertEqual(side.declared_floor_at(objects,[0,0,100000],None)['object'],'street')
        self.assertEqual(side.declared_floor_at(objects,[25,25,400000],None)['object'],'LOBBY_Floor')
        self.assertNotIn('z_um',side.declared_floor_at(objects,[100,100,100000],None))
        # B01 mutant: selecting a floor-shaped name cannot satisfy the XY oracle.
        broken = mutated(side, 'all(low[i] <= point[i] <= high[i] for i in (0, 1))', 'True', 'declared_floor_at')
        self.assertEqual(broken(objects,[0,0,400000],None)['object'],'LOBBY_Floor')
        self.assertEqual(side.declared_floor_at({},[0,0,10],{'z_um':3})['z_um'],3)
        for bad in ({}, {'object':'x','z_um':0}, {'z_um':True}, {'collection':4}):
            with self.assertRaises(ValueError): p.validate_floor(bad)

    def test_downward_raycast_skips_character_meshes(self):
        class Vec(list):
            @property
            def z(self): return self[2]
            @z.setter
            def z(self,value): self[2]=value
            def __sub__(self,other): return Vec(a-b for a,b in zip(self,other))
        rig=NS(name='Rig',parent=None,modifiers=[])
        sole=NS(name='sole',parent=rig,modifiers=[])
        street=NS(name='Street',parent=None,modifiers=[])
        hits=[(True,Vec([.05,.05,.02]),None,0,sole,None),
              (True,Vec([.05,.05,0]),None,0,street,None)]
        from unittest.mock import Mock
        scene=NS(objects=[rig,sole,street],ray_cast=Mock(side_effect=hits*9))
        bpy=NS(context=NS(scene=scene,evaluated_depsgraph_get=lambda:None))
        state={'objects':{'sole':{'world_bbox_min_um':[0,0,3000],'world_bbox_max_um':[100000,100000,50000]},
                          'Rig':{'bones':{'foot':{'head_world_um':[0,0,80000]}}}}}
        foot={'label':'sole','z':['/objects/sole/world_bbox_min_um/2'],'bones':[['Rig','foot']]}
        with patch.dict('sys.modules',{'mathutils':NS(Vector=Vec)}):
            self.assertEqual(side.contact_floor(bpy,state,foot)['object'],'Street')
        self.assertEqual(scene.ray_cast.call_count,18)  # 3 x 3 footprint rays, each skipping the sole once

    def test_scenario_contact_records_declared_floor(self):
        adapter=Shot();adapter.contact_feet=[]
        adapter.state['contacts']={'foot.L':{'object':'declared_z','z_um':0,'method':'declared'}}
        obligation={'kind':'feet_floor','frames':[1,3,1],'floor':{'z_um':0}}
        receipt=verify_step(adapter,{'op':'set_frame','frame':1},obligation)
        self.assertEqual(receipt['final_verdict'],'PASS')
        self.assertEqual(len(receipt['floors_used']),3)
        self.assertEqual(receipt['floors_used'][0]['object'],'declared_z')


    def test_unresolved_contact_cannot_pass_and_receipt_preserves_floor(self):
        adapter = Shot()
        policy = selected(p.load('film-animation'),'feet-floor')
        state = adapter.scene_state(); state['contact_sampling']=1
        step = p.expand(policy,state)[0]
        contact = step['obligation']['contact_path']
        adapter.state['contacts']={'foot.L':{'object':'street','z_um':0,'method':'ray_cast'}}
        verdict, detail = p._feet_floor(adapter,step['obligation'])
        self.assertEqual(verdict,'PASS')
        self.assertEqual(step['obligation']['floors_used'][0]['object'],'street')
        adapter.state['contacts']['foot.L']={'object':'','method':'unresolved'}
        with self.assertRaisesRegex(p.CannotSee,'floor_unresolved'):
            p._feet_floor(adapter,step['obligation'])

    def test_contact_trace_replays_the_same_floor_receipt(self):
        from vqa3d.fakes import ReplayAdapter
        adapter=Shot();adapter.contact_feet=[]
        adapter.state['contacts']={'foot.L':{'object':'declared_z','z_um':0,'method':'declared'}}
        recording=RecordingAdapter(adapter)
        step={'action':{'op':'set_frame','frame':1},'obligation':{'kind':'feet_floor','frames':[1,3,1],'floor':{'z_um':0}}}
        receipt=verify_step(recording,**step)
        replay=ReplayAdapter(recording.trace([step],[receipt],{}))
        again=verify_step(replay,**step)
        self.assertEqual(again,receipt)


    def test_velocity_filter_mutant_fast_ground_travel_is_not_a_plant(self):
        adapter = Shot((1,6))
        adapter.state['objects']['foot.L']['world_bbox_min_um'][2]=3000
        adapter.timeline[HEAD]={f:[200000*f,0,80000] for f in range(1,7)}
        adapter.timeline[TAIL]={f:[200000*f,-150000,10000] for f in range(1,7)}
        self.assertIn('foot_never_planted',one('film-animation','foot-slide',adapter)[0][0]['error'])
        broken = mutated(p, 'planted = value <= obligation["plant_max_um"] and slow',
                         'planted = value <= obligation["plant_max_um"]', '_foot_slide')
        with patch.object(p,'_foot_slide',broken):
            self.assertEqual(one('film-animation','foot-slide',adapter)[1]['final_verdict'],'FAIL')


class Declarations(unittest.TestCase):
    def test_rigid_strap_and_grip_offset_and_slip(self):
        adapter = Shot()
        prop=adapter.state['objects']['prop/x~']
        prop.pop('attach'); prop['bone_relative_um']=[400000,0,0]
        prop['world_location_um']=[400000,0,0]
        # Hung from the chest like the nightclub speaker: no grip bone, no declaration.
        adapter.state['objects']['Rig.a']['bones']['chest']=copy.deepcopy(adapter.state['objects']['Rig.a']['bones']['hand.R'])
        prop['parent_bone']='chest'
        receipts,result=one('film-animation','attachments',adapter)
        self.assertEqual(result['final_verdict'],'PASS')
        self.assertEqual(receipts[0]['attachment_mode'],'rigid_follow')
        prop['attach']='grip'
        self.assertEqual(one('film-animation','attachments',adapter)[1]['final_verdict'],'FAIL')
        prop['grip_point_um']=[400000,0,0]
        self.assertEqual(one('film-animation','attachments',adapter)[1]['final_verdict'],'PASS')
        adapter.timeline['/objects/prop~1x~0/bone_relative_um']={2:[410000,0,0]}
        self.assertEqual(one('film-animation','attachments',adapter)[1]['final_verdict'],'FAIL')

    def test_custom_attach_and_mapping_select_props_without_name_hints(self):
        adapter=Shot()
        prop=adapter.state['objects'].pop('prop/x~')
        adapter.state['objects']['Handset']=prop
        rows,_=one('film-animation','attachments',adapter)
        self.assertEqual([r['check_id'] for r in rows],['attachments:Handset'])
        prop.pop('attach')
        policy=selected(p.load('film-animation'),'attachments')
        policy['defaults']['attachment']['mapping']={'Handset':'in_grip'}
        rows,_,_=p.run(policy,adapter)
        self.assertEqual(rows[-1]['check_id'],'attachments:Handset')
        self.assertEqual(rows[-1]['final_verdict'],'PASS')


    def test_environment_hashes_hide_values_and_cli_controls(self):
        values={'CYCLES_KERNEL_PATH':'/tmp/kernels','RENDER_OPTION':'private-value'}
        adapter=driver.BlenderAdapter(blender_env=values,nice=10,cpus='0-15')
        self.assertEqual(adapter.environment_receipt['CYCLES_KERNEL_PATH']['path'],'/tmp/kernels')
        self.assertNotIn('private-value',json.dumps(adapter.environment_receipt))
        self.assertEqual(len(adapter.environment_receipt['RENDER_OPTION']['sha256']),64)
        policy=p.load('film-animation');policy['render']['env']=values
        p.validate(policy)
        self.assertEqual(p.adapter_kwargs(policy,driver.BlenderAdapter)['blender_env'],values)
        for values in ({'PYTHONPATH':'x'},{'LD_PRELOAD':'x'},{'HOME':'x'},{'DISPLAY':':0'},{'DISPLAY':':0.0'},{'WAYLAND_DISPLAY':'wayland-0'},{'XAUTHORITY':'/tmp/xauth'},{'XDG_CONFIG_HOME':'/tmp/cfg'},{'XDG_CACHE_HOME':'/tmp/cache'},{'XDG_RUNTIME_DIR':'/tmp/xdg'},{'XDG_DATA_DIRS':'/tmp/xdg-data'},{'DBUS_SESSION_BUS_ADDRESS':'unix:path=/tmp/dbus'},{'BLENDER_USER_SCRIPTS':'/tmp/scripts'},{'bad key':'x'}):
            with self.assertRaises(ValueError): driver.BlenderAdapter(blender_env=values)
        adapter.close()

    def test_allow_derived_keeps_other_objects_and_local_fields_strict(self):
        state={'frame_current':1,'objects':{'A':{'location_um':[0,0,0],'world_location_um':[0,0,0],
                'vertex_groups':{'foot':{'world_bbox_min_um':[0,0,0]}}},'B':{'world_location_um':[0,0,0]}}}
        after=copy.deepcopy(state); after['frame_current']=2
        after['objects']['A']['location_um'][0]=1
        after['objects']['A']['world_location_um'][0]=1
        after['objects']['A']['vertex_groups']['foot']['world_bbox_min_um'][0]=1
        a=Observation.create('fake','before',state); b=Observation.create('fake','after',after)
        o={'kind':'state_change','keys':['/objects/A/location_um']}
        self.assertEqual(evaluate(a,b,o)[0],'FAIL')
        o['allow_derived']=True; validate_obligation(o)
        self.assertEqual(evaluate(a,b,o)[0],'PASS')
        after['objects']['B']['world_location_um'][0]=1
        self.assertEqual(evaluate(a,Observation.create('fake','after',after),o)[0],'FAIL')
        with self.assertRaises(ValueError): validate_obligation(dict(o,allow_derived=1))

    def test_no_change_allows_only_scoped_derived_fields_and_equal_pixels(self):
        a={'frame_current':1,'objects':{'A':{'location_um':[0,0,0],'world_location_um':[0,0,0]}}}
        b=copy.deepcopy(a);b['frame_current']=2;b['objects']['A']['world_location_um'][0]=1
        png=encode(2,2,bytes(range(12)))
        before=Observation.create('fake','before',a,png);after=Observation.create('fake','after',b,png)
        o={'kind':'no_change','keys':['/objects/A/location_um'],'allow_derived':True}
        validate_obligation(o);self.assertEqual(evaluate(before,after,o)[0],'PASS')
        b['objects']['A']['location_um'][0]=2
        self.assertEqual(evaluate(before,Observation.create('fake','after',b,png),o)[0],'FAIL')


    def test_preview_noise_advisory_and_production_noise_gate(self):
        policy=selected(p.load('film-animation'),'nonblank','noise')
        records,result,_=p.run(policy,Shot())
        self.assertEqual(records[-1]['final_verdict'],'ADVISORY')
        self.assertIn('frame_outlier_ppm',reason(records[-1]))
        self.assertEqual(result['suggested_actions'],{records[-1]['check_id']:records[-1]['suggested_action']})
        self.assertIn('--frame <png>',records[-1]['suggested_action'])
        records,result,_=p.run(policy,Shot(),frame_png=encode(2,2,bytes(range(12))))
        # Round 9: a clean frame off the calibrated size is not trusted as PASS.
        self.assertEqual(records[-1]['final_verdict'],'ADVISORY')
        self.assertIn('uncalibrated_size:frame_px=4',reason(records[-1]))
        self.assertEqual(records[-1]['severity'],'gate')

    def test_noise_action_follows_probed_denoisers(self):
        self.assertTrue(p.noise_action({'engines':['CYCLES'],'denoisers':[]},False).startswith('no denoiser in this build'))
        # Review finding A: a failed or skipped render probe must not claim the build lacks a denoiser.
        for unknown in ({}, None, {'engines':[],'denoisers':[]}):
            self.assertTrue(p.noise_action(unknown,False).startswith('denoisers not probed'))
        self.assertIn('OPENIMAGEDENOISE',p.noise_action({'denoisers':['OPENIMAGEDENOISE']},False))
        self.assertNotIn('--frame',p.noise_action({'denoisers':['OPTIX']},False))
        # 0705-B02: a PASS carries no advice, and a summary without warnings keeps its old keys.
        self.assertNotIn('suggested_actions',p.summary([{'check_id':'noise','severity':'advisory','final_verdict':'PASS'}],p.load('film-animation')))


FAKE_DRIVER='''#!/usr/bin/python3
import json,sys,os,time
req,res=sys.argv[sys.argv.index('--')+1:]
x=json.load(open(req)); session=x.get('session')
while True:
    result={'ok':True,'runtime_version':'fake','state':{'frame_current':1,'objects':{}}}
    if x['mode']=='sample': result['samples']={str(f):{p:[1,2,3] for p in x['paths']} for f in x['frames']}
    if x['mode']=='capture': time.sleep(5)
    with open(res+'.tmp','w') as out: json.dump(result,out)
    os.replace(res+'.tmp',res)
    if not session: break
    line=sys.stdin.readline()
    if not line: break
    x=json.loads(line)
'''


class Lifecycle(unittest.TestCase):
    def make_driver(self,root,session=True,timeout=3):
        exe=root/'fake-blender'; exe.write_text(FAKE_DRIVER); exe.chmod(0o700)
        source=root/'source.blend'; source.write_bytes(b'fixture')
        return exe,driver.BlenderAdapter(source,profile_session=session,timeout=timeout,probe_timeout=1,capture_timeout=1)

    def test_one_child_serves_state_and_samples_spawn_mutant(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for session,expected in ((True,1),(False,3)):
                exe,adapter=self.make_driver(root,session)
                with patch.object(driver,'BLENDER',exe):
                    try:
                        adapter.scene_state(); adapter.sample([1,2],['/objects/X/location_um'])
                        self.assertEqual(len(adapter.process_logs),expected)
                    finally: adapter.close()
                self.assertEqual((root/'source.blend').read_bytes(),b'fixture')

    def test_kill_and_timeout_reasons_are_typed(self):
        with tempfile.TemporaryDirectory() as temp:
            exe,adapter=self.make_driver(Path(temp))
            with patch.object(driver,'BLENDER',exe):
                try:
                    adapter.scene_state()
                    adapter._session.terminate()
                    adapter._session.wait(timeout=2)
                    with self.assertRaisesRegex(RuntimeError,'driver_killed:SIGTERM'):
                        adapter._session_invoke([],{}, {'mode':'state'},adapter.scratch/'missing.json')
                except BrokenPipeError:
                    self.fail('dead driver leaked BrokenPipeError')
                finally: adapter.close()
            exe,adapter=self.make_driver(Path(temp))
            with patch.object(driver,'BLENDER',exe):
                try:
                    adapter.scene_state()
                    with self.assertRaisesRegex(TimeoutError,'driver_timeout:1'):
                        adapter._invoke('capture')
                finally: adapter.close()

    def test_sampling_defect_keyframe_and_neighbor_mutant(self):
        frames=p.sample_frames([1,250,1],{'stride':6},[94,98])
        self.assertTrue({1,93,94,95,97,98,99,250} <= set(frames))
        broken=mutated(p,' | {f for f in keyframes if start <= f <= end}', '', 'sample_frames')
        self.assertNotIn(94,broken([1,250,1],{'stride':6},[94,98]))

    def test_gates_only_does_not_run_advisories(self):
        policy=selected(p.load('film-animation'),'scene-camera','noise','gpu-render')
        receipts,_,_=p.run(policy,Shot(),gates_only=True)
        self.assertEqual([r['check_id'] for r in receipts],['requires','scene-camera:scene'])

    def test_dead_pid_and_silent_fake_bridge_are_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); (root/'inbox').mkdir(); (root/'outbox').mkdir()
            adapter=BlenderLiveAdapter(root,timeout=.08,frame_timeout=.08,poll_interval=.01)
            start=time.monotonic()
            self.assertIn('bridge_no_answer',adapter.probe()['reason'])
            self.assertLess(time.monotonic()-start,.5)
            # An owned subprocess is reaped before recording its dead PID.
            import subprocess
            child=subprocess.Popen(['/usr/bin/true']); child.wait()
            (root/'bridge.json').write_text(json.dumps({'pid':child.pid}))
            self.assertIn('live_target_dead',adapter.probe()['reason'])
            adapter.close()

    def test_eevee_capture_timeout_is_cannot_see(self):
        adapter=driver.BlenderAdapter(capture_engine='eevee')
        with patch.object(adapter,'_prepare'), patch.object(adapter,'_invoke',side_effect=TimeoutError('driver_timeout:15')):
            receipt=verify_step(adapter,{}, {'kind':'frame_change'})
        self.assertEqual(receipt['final_verdict'],'CANNOT_SEE')
        self.assertEqual(receipt['error'],'eevee_probe_timeout:15')
        adapter.close()


if __name__=='__main__': unittest.main()

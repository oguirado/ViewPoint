#!/usr/bin/env python3
import importlib.machinery
import importlib.util
import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import ROOT,vis,execute,note,compatibility_lock,ORDINARY_TEXTS,PLACEHOLDER_TEXTS,synthetic_keys
loader=importlib.machinery.SourceFileLoader('vis_core',str(ROOT/'vis'))
spec=importlib.util.spec_from_loader(loader.name,loader); core=importlib.util.module_from_spec(spec); loader.exec_module(core)

class Unit(unittest.TestCase):
    def test_fix5_run_identity_persists_and_legacy_load_upgrades_once(self):
        with tempfile.TemporaryDirectory(dir='/dev/shm',prefix='vis-fix5-id-') as tmp:
            root=Path(tmp);job=dict(job='identity')
            with patch.object(core,'jobpath',return_value=root):
                core.save_unlocked(job);first=job['run_id']
                core.save_unlocked(job);self.assertEqual(core.readjson(root/'job.json')['run_id'],first)
                (root/'job.json').write_text(core.canonical(dict(job='identity',pidns=core.pid_namespace())))
                upgraded=core.load('identity',enforce_budget=False)['run_id']
                self.assertEqual(core.load('identity',enforce_budget=False)['run_id'],upgraded)
                self.assertNotEqual(first,upgraded)

    def test_fix5_state_receipt_predicate_and_bounded_evidence(self):
        with tempfile.TemporaryDirectory(dir='/dev/shm',prefix='vis-fix5-state-') as tmp:
            job=dict(job='state',run_id='test-run',target_kind='web',display='web:state',ownership='started')
            root=Path(tmp)
            for absent,name in ((False,'Done'),(True,'Done'),(False,'sk-synthetic'),(False,'x'*1000)):
                args=core.parser().parse_args(['check','--job','state','--state-absent' if absent else '--state','button='+name])
                nodes=[dict(role='RootWebArea')]+([] if absent else [dict(role='button',name=name,box=[0,0,10,10],states={},untrusted='never archive '+('x'*10000))])
                with patch.object(core,'ARGS',args), patch.object(core,'jobpath',return_value=root), \
                     patch.object(core,'target'), patch.object(core.time,'sleep'), \
                     patch.object(core,'rpc',return_value={'roles':['button']}), \
                     patch.object(core,'state_observation',return_value={'nodes':nodes}):
                    r=core.do_check(job,args)['receipt']
                self.assertEqual(r['predicate']['kind'],'state-absent' if absent else 'state')
                self.assertEqual(r['predicate']['operand']['role'],'button')
                self.assertEqual(r['predicate']['operand']['name_sha256'],core.hashlib.sha256(name.encode()).hexdigest())
                self.assertEqual(r['predicate']['require_stable'],args.require_stable)
                self.assertTrue(r['observation']['stable'])
                self.assertEqual(r['observation']['match_counts'],[0 if absent else 1]*args.require_stable)
                self.assertLess(len(core.canonical(r['observation'])),1024)
                self.assertNotIn('untrusted',core.canonical(r))
                if name!='Done':
                    self.assertNotIn(name,core.canonical(r['predicate']))
                    self.assertNotIn(name,core.canonical(r['observation']))

    def test_fix5_archives_preserve_reused_job_and_refuse_collision(self):
        with tempfile.TemporaryDirectory(dir='/dev/shm',prefix='vis-fix5-archive-') as tmp:
            home=Path(tmp);root=home/'job';paths=[];contents=[]
            with patch.object(core.Path,'home',return_value=home), patch.object(core,'jobpath',return_value=root):
                for run_id in ('first-run','second-run'):
                    root.mkdir(); (root/'receipts.jsonl').write_text(run_id+'\n')
                    job=dict(job='reused',run_id=run_id,ownership='adopted',target_kind='desktop',display=':private')
                    args=core.parser().parse_args(['stop','--job','reused'])
                    with patch.object(core,'ARGS',args): paths.append(Path(core.stop(job)['receipts']))
                    contents.append(paths[-1].read_bytes())
                self.assertNotEqual(*paths)
                self.assertEqual(paths[0].read_bytes(),contents[0])
                self.assertEqual(json.loads(contents[1].splitlines()[-1])['run_id'],'second-run')
                root.mkdir();(root/'receipts.jsonl').write_text('collision\n')
                with self.assertRaises(core.Failure) as error: core.persist_receipts(job)
                self.assertEqual(error.exception.rc,5)
                self.assertEqual(paths[-1].read_bytes(),contents[-1])

    def test_fix6_latest_receipts_atomically_follow_reused_job(self):
        with tempfile.TemporaryDirectory(dir='/dev/shm',prefix='vis-fix6-archive-') as tmp:
            home=Path(tmp);root=home/'job';dest=home/'.local/state/vis/receipts'
            dest.mkdir(parents=True);latest=dest/'reused.jsonl'
            latest.write_text('legacy copy\n');archives=[]
            replace=core.os.replace
            def publish(source,target):
                self.assertEqual(target,latest)
                self.assertTrue(source.is_symlink())
                self.assertFalse(Path(core.os.readlink(source)).is_absolute())
                self.assertEqual(json.loads(source.read_bytes().splitlines()[-1])['action'],'stop')
                replace(source,target)
            with patch.object(core.Path,'home',return_value=home), patch.object(core,'jobpath',return_value=root):
                for run_id in ('first-run','second-run'):
                    root.mkdir();(root/'receipts.jsonl').write_text(run_id+'\n')
                    job=dict(job='reused',run_id=run_id,ownership='adopted',target_kind='desktop',display=':private')
                    args=core.parser().parse_args(['stop','--job','reused'])
                    with patch.object(core,'ARGS',args), patch.object(core.os,'replace',side_effect=publish) as swapped:
                        result=core.stop(job)
                    swapped.assert_called_once()
                    archive=Path(result['receipts']);archives.append((archive,archive.read_bytes()))
                    self.assertEqual(result['receipts_latest'],str(latest))
                    self.assertTrue(latest.is_symlink())
                    self.assertEqual(latest.resolve(),archive)
                    self.assertFalse(root.exists())
                self.assertNotEqual(archives[0][0],archives[1][0])
                root.mkdir();(root/'receipts.jsonl').write_text('collision\n')
                with self.assertRaises(core.Failure): core.stop(job)
                self.assertTrue(root.exists())
                self.assertEqual(latest.resolve(),archives[-1][0])
                for archive,payload in archives: self.assertEqual(archive.read_bytes(),payload)
                self.assertFalse(list(dest.glob('.*')))

    def test_fix6_failed_start_receipts_update_latest_without_overwrite(self):
        with tempfile.TemporaryDirectory(dir='/dev/shm',prefix='vis-fix6-failed-') as tmp:
            home=Path(tmp);root=home/'job';root.mkdir();archives=[]
            with patch.object(core.Path,'home',return_value=home), patch.object(core,'jobpath',return_value=root):
                for run_id in ('first-run','second-run'):
                    payload=core.canonical(dict(run_id=run_id,action='start',rc=8))+'\n'
                    (root/'receipts.jsonl').write_text(payload)
                    job=dict(job='failed',run_id=run_id)
                    archive=core.persist_receipts(job);archives.append((archive,payload))
                    latest=archive.parent/'failed.jsonl'
                    self.assertTrue(latest.is_symlink())
                    self.assertEqual(latest.resolve(),archive)
                with self.assertRaises(core.Failure): core.persist_receipts(job)
                self.assertEqual(latest.resolve(),archives[-1][0])
                for archive,payload in archives: self.assertEqual(archive.read_text(),payload)

    def test_fix5_guard_scope_is_per_payload(self):
        job=dict(ownership='started',target_kind='desktop')
        with patch.dict(os.environ,{'MV_HOSTED':'0'}):
            for argv in (['--type','sk'],['--type=-test'],['--key','minus'],['--type','sk-test']):
                core.rule(job,core.parser().parse_args(['act','--job','guard','--step','test','--expect-change','any',*argv]))
            args=core.parser().parse_args(['act','--job','guard','--step','test','--expect-change','any','--type',synthetic_keys()[0]])
            with self.assertRaises(core.Failure) as error: core.rule(job,args)
            self.assertEqual(error.exception.rc,5)

    def test_fix5_attempts_are_per_step_id(self):
        job=dict(job='scripted',run_id='retry-run',ownership='started',target_kind='web',display='web:scripted',
                 steps={'change':{'attempts':3,'passed':False}},engines={},pids=[])
        rc,r,_,_=self.scripted_act([b'\0'*6],'any',[],job=job)
        self.assertEqual((rc,r['attempts']),(4,3))
        rc,r,_,_=self.scripted_act([b'\0'*6],'any',[],job=job,step='renamed')
        self.assertEqual((rc,r['attempts']),(3,1))
        self.assertEqual(r['attempts_scope'],'per_step_id')
        self.assertEqual(r['step'],'renamed')

    def test_scope_collection_race(self):
        job=dict(job='race',scope='vis-race-123.scope',cgroup='/sys/fs/cgroup/vis-race-123.scope')
        for missing in (False,True):
            with self.subTest(missing=missing):
                root=MagicMock(); root.name=job['scope']; root.resolve.return_value=job['cgroup']; root.__str__.return_value=job['cgroup']
                root.exists.return_value=True
                if missing: (root/'cgroup.procs').read_text.side_effect=FileNotFoundError
                else: (root/'cgroup.procs').read_text.return_value=''
                with patch.object(core,'Path',return_value=root), patch.object(core,'run',return_value=core.sp.CompletedProcess([],0,b'\n',b'')):
                    self.assertEqual(core.scope_members(job),[])

    def test_scope_mismatch_and_unverified_live_scope_refuse(self):
        job=dict(job='race',scope='vis-race-123.scope',cgroup='/sys/fs/cgroup/vis-race-123.scope')
        for group,members in ((b'/other.scope\n',''),(b'\n','123\n'),(b'/other.scope\n','123\n')):
            with self.subTest(group=group,members=members):
                root=MagicMock(); root.name=job['scope']; root.resolve.return_value=job['cgroup']; root.__str__.return_value=job['cgroup']; root.exists.return_value=True
                (root/'cgroup.procs').read_text.return_value=members
                with patch.object(core,'Path',return_value=root), patch.object(core,'run',return_value=core.sp.CompletedProcess([],0,group,b'')):
                    with self.assertRaises(core.Failure) as error: core.scope_members(job)
                    self.assertEqual(error.exception.rc,5)

    def scripted_act(self,trail,expected,regions,*,masks=(),verified=True,
                     ownership='started',target_kind='web',responsiveness=None,job=None,step='change',typed=None,on_root=None,
                     root=None,tag='',describe_fn=None,initial=b'\0'*6,click=None):
        """Exercise do_act with real decoded frames and receipts, no GUI or wall-clock wait."""
        import struct,zlib
        def png(rgb):
            def chunk(k,b): return struct.pack('>I',len(b))+k+b+struct.pack('>I',zlib.crc32(k+b)&0xffffffff)
            return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',2,1,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(b'\0'+rgb))+chunk(b'IEND',b'')
        shared=root is not None
        with (contextlib.nullcontext(str(root)) if shared else tempfile.TemporaryDirectory(dir='/dev/shm',prefix='vis-fix2-act-')) as tmp:
            root=Path(tmp); frames=[]
            if on_root: on_root(root)
            for i,rgb in enumerate([initial,*trail]):
                frame=root/('frame_'+tag+str(i)+'.png'); frame.write_bytes(png(rgb)); frames.append(frame)
            seen=len((root/'receipts.jsonl').read_text().splitlines()) if (root/'receipts.jsonl').exists() else 0
            argv=['act','--job','scripted',*(['--step',step] if step is not None else []),*(['--type='+typed] if typed is not None else ['--click',click] if click is not None else ['--key','space']),*(['--expect-change',expected] if expected is not None else []),*(['--settle-ms','800'] if expected!='exit' else [])]
            for mask in masks: argv+=['--mask',mask]
            args=core.parser().parse_args(argv)
            if job is None: job=dict(job='scripted',ownership=ownership,target_kind=target_kind,display='web:scripted',
                     steps={},engines={},pids=[],allow_act=ownership=='attached')
            clock=[100.]; captures=[0]
            def capture(j):
                frame=frames[min(captures[0],len(frames)-1)]; captures[0]+=1; return frame
            def sleep(seconds): clock[0]+=seconds
            with contextlib.ExitStack() as stack:
                for name,value in [('ARGS',args),('START',100.),('RECEIPTED',False)]:
                    stack.enter_context(patch.object(core,name,value))
                for name,value in [('load',job),('jobpath',root),('save',None),('target',None),
                                   ('rpc',{'verified':verified})]:
                    stack.enter_context(patch.object(core,name,return_value=value))
                stack.enter_context(patch.object(core,'describe',side_effect=describe_fn or (lambda j,frame,*a,**kw: {'changed_regions':regions,'frame_sha256':core.frame_sha(frame)})))
                stack.enter_context(patch.object(core,'responsive',side_effect=responsiveness))
                for name in ('lifecycle','actor'):
                    stack.enter_context(patch.object(core,name,side_effect=lambda *a,**kw: contextlib.nullcontext()))
                stack.enter_context(patch.dict(os.environ,{'MV_HOSTED':'0'}))
                stack.enter_context(patch.object(core,'capture',side_effect=capture))
                stack.enter_context(patch.object(core.time,'monotonic',side_effect=lambda: clock[0]))
                stack.enter_context(patch.object(core.time,'sleep',side_effect=sleep))
                rc=0
                try: core.do_act(args)
                except core.Failure as error:
                    rc=error.rc
                    if not core.RECEIPTED: core.receipt(job,rc)
            self.assertTrue((root/'receipts.jsonl').exists(),'act stopped before receipt with rc '+str(rc))
            receipts=[json.loads(line) for line in (root/'receipts.jsonl').read_text().splitlines()]
            self.assertEqual(len(receipts),seen+1); r=receipts[-1]
            self.assertEqual((r['schema'],r['rc'],r['code'],r['verdict']),
                             ('vis.receipt/v1',rc,core.CODES[rc],'pass' if rc==0 else 'refused' if rc in (2,4,5) else 'fail'))
            if captures[0] and rc!=2:  # a usage refusal after the pre-input capture carries no frames
                self.assertEqual(r['frame_sha_before'],core.frame_sha(frames[0]))
                if expected=='exit': self.assertIsNone(r['frame_sha_after'])
                else: self.assertEqual(r['frame_sha_after'],core.frame_sha(frames[min(captures[0]-1,len(frames)-1)]))
            return rc,r,captures[0]-1,clock[0]-100.

    def probe_runner(self,properties,probe_result=0):
        def run(cmd,**kwargs):
            if cmd[:2]==['xdotool','search']: out=b'123\n'
            elif cmd[0]=='xprop': out=properties.encode()
            elif cmd[0]==sys.executable:
                if isinstance(probe_result,Exception): raise probe_result
                return core.sp.CompletedProcess(cmd,probe_result,b'',b'')
            else: out=b''
            return core.sp.CompletedProcess(cmd,0,out,b'')
        return run

    def test_desktop_act_missing_witness_dispatches_and_checks_pixels(self):
        for ownership in ('started','adopted','attached'):
            for properties,probe_rc in [('WM_CLASS = "fixture", "Fixture"',0),
                                        ('WM_CLASS = "tk", "Tk"',7),
                                        ('WM_PROTOCOLS = _NET_WM_PING',7)]:
                for changed,expected_rc in ((True,0),(False,3)):
                    with self.subTest(ownership=ownership,properties=properties,changed=changed), \
                         patch.object(core,'desktop_env',return_value={}), \
                         patch.object(core,'run',side_effect=self.probe_runner(properties,probe_rc)) as run:
                        rc,r,_,_=self.scripted_act([b'\xff'*6 if changed else b'\0'*6],'any',
                                                   [dict(x=0,y=0,w=2,h=1)],ownership=ownership,
                                                   target_kind='desktop',responsiveness=core.responsive)
                        self.assertEqual(rc,expected_rc)
                        self.assertEqual(r['loop_witness'],{'before':False,'after':False,'obtained':False})
                        self.assertIn(['xdotool','key','--clearmodifiers','--','space'],
                                      [call.args[0] for call in run.call_args_list])

    def test_tk_witness_uses_target_for_every_ownership(self):
        for ownership in ('started','adopted','attached'):
            with self.subTest(ownership=ownership), patch.object(core,'desktop_env',return_value={}), \
                 patch.object(core,'run',side_effect=self.probe_runner('WM_CLASS = "tk", "Tk"')) as run:
                core.responsive(dict(ownership=ownership,target_kind='desktop',pids=[],allow_act=True))
                probes=[c for c in run.call_args_list if c.args[0][0]==sys.executable]
                self.assertEqual(len(probes),1)
                self.assertLessEqual(probes[0].kwargs['timeout'],2)

    def test_tk_witness_timeout_never_falls_back(self):
        for ownership in ('started','adopted','attached'):
            with self.subTest(ownership=ownership), patch.object(core,'desktop_env',return_value={}), \
                 patch.object(core,'run',side_effect=self.probe_runner('WM_CLASS = "tk", "Tk"',
                                                                          core.Failure(6,'probe timed out'))):
                with self.assertRaises(core.Failure) as error:
                    core.responsive(dict(ownership=ownership,target_kind='desktop',pids=[],allow_act=True))
                self.assertEqual(error.exception.rc,8)

    def test_recorded_pid_identity_checked_for_every_ownership(self):
        for ownership in ('started','adopted','attached'):
            with self.subTest(ownership=ownership), tempfile.TemporaryDirectory(dir='/dev/shm') as tmp, \
                 patch.object(core,'jobpath',return_value=Path(tmp)), \
                 patch.object(core,'desktop_env',return_value={}), \
                 patch.object(core,'run',return_value=core.sp.CompletedProcess([],0,b'',b'')) as run:
                with self.assertRaises(core.Failure) as error:
                    core.target(dict(job='scripted',ownership=ownership,target_kind='desktop',
                                     pids=[dict(pid=os.getpid(),start='stale')]))
                self.assertEqual(error.exception.rc,8); run.assert_not_called()

    def test_f2_expect_change_defaults_to_any(self):
        rc,r,_,_=self.scripted_act([b'\xff'*6],None,[dict(x=0,y=0,w=2,h=1)])
        self.assertEqual((rc,r['expected']),(0,'any'))
        rc,r,_,_=self.scripted_act([b'\0'*6],None,[])
        self.assertEqual((rc,r['expected']),(3,'any'))
        for expected in ('none','1,0,1,1'):
            self.assertEqual(self.scripted_act([b'\0'*6],expected,[])[1]['expected'],expected)
    def exit_act(self,witness,*,oom_events=False,target_kind='desktop',ownership='started'):
        """Scripted exit expectation: the input write lands the supervisor's death witness."""
        with tempfile.TemporaryDirectory(dir='/dev/shm',prefix='vis-f3-cg-') as cg:
            if oom_events: Path(cg,'memory.events').write_text('low 0\nhigh 0\nmax 3\noom 1\noom_kill 1\n')
            job=dict(job='scripted',ownership=ownership,target_kind=target_kind,display=':private',steps={},engines={},
                     pids=[],clients=[dict(pid=4242,start='1')],cgroup=cg)
            holder={}
            def run(cmd,**kw):
                if cmd[:2]==['xdotool','key'] and witness is not None:
                    (holder['root']/'target.exit.json').write_text(core.canonical(witness))
                return core.sp.CompletedProcess(cmd,0,b'',b'')
            with patch.object(core,'desktop_env',return_value={}), patch.object(core,'run',side_effect=run):
                return self.scripted_act([b'\0'*6],'exit',[],job=job,on_root=lambda root: holder.__setitem__('root',root))
    def test_f3_expect_exit(self):
        clean=dict(rc=8,reason='target exited',oom=False,exits=[dict(pid=4242,process='dbus-run-session',returncode=0)])
        rc,r,captures,_=self.exit_act(clean)
        self.assertEqual((rc,r['expected'],r['exit'],r['frame_sha_after'],captures),(0,'exit',{'returncode':0},None,0))
        failed=dict(clean,exits=[dict(pid=4242,process='dbus-run-session',returncode=3)])
        self.assertEqual(self.exit_act(failed)[0],8)
        display_died=dict(clean,exits=[dict(pid=99,process='Xvfb',returncode=1)])
        self.assertEqual(self.exit_act(display_died)[0],8)
        # A clean client exit recorded together with any other exit (here Xvfb) is a display failure.
        for xvfb_rc in (1,0,-15):
            both=dict(clean,exits=[dict(pid=4242,process='dbus-run-session',returncode=0),dict(pid=99,process='Xvfb',returncode=xvfb_rc)])
            rc,r,_,_=self.exit_act(both)
            self.assertEqual((rc,r['verdict'],'exit' in r,r['frame_sha_after']),(8,'fail',False,None),xvfb_rc)
            self.assertIn('Xvfb',r['reason'])
        self.assertEqual(self.exit_act(dict(both,rc=6,oom=True))[0],6)
        self.assertEqual(self.exit_act(dict(clean,rc=6,oom=True))[0],6)
        self.assertEqual(self.exit_act(clean,oom_events=True)[0],6)
        rc,r,_,elapsed=self.exit_act(None)
        self.assertEqual((rc,r['expected']),(3,'exit')); self.assertAlmostEqual(elapsed,5.0,delta=.2)
        self.assertEqual(self.exit_act(clean,target_kind='web')[0],2)
        self.assertEqual(self.exit_act(clean,ownership='adopted')[0],2)
    def test_f4_step_is_optional(self):
        job=dict(job='scripted',ownership='started',target_kind='web',display='web:scripted',steps={},engines={},pids=[])
        ids=[]
        for _ in range(2):
            rc,r,_,_=self.scripted_act([b'\xff'*6],'any',[dict(x=0,y=0,w=2,h=1)],job=job,step=None)
            self.assertEqual((rc,r['attempts']),(0,1)); ids.append(r['step'])
        self.assertEqual(ids,['auto-1','auto-2'])
        job['steps']['auto-3']={'attempts':1,'passed':False}
        self.assertEqual(self.scripted_act([b'\xff'*6],'any',[dict(x=0,y=0,w=2,h=1)],job=job,step=None)[1]['step'],'auto-4')
        rc,r,_,_=self.scripted_act([b'\xff'*6],'any',[dict(x=0,y=0,w=2,h=1)],job=job,step='auto-1')
        self.assertEqual((rc,r['step']),(2,'auto-1'))
        job['steps']['manual']={'attempts':3,'passed':False}
        self.assertEqual(self.scripted_act([b'\xff'*6],'any',[],job=job,step='manual')[0],4)
        # A refused act without --step reserves its auto id: the next act gets a fresh one.
        job=dict(job='scripted',ownership='started',target_kind='web',display='web:scripted',steps={},engines={},pids=[])
        with tempfile.TemporaryDirectory(dir='/dev/shm',prefix='vis-f4-') as tmp:
            refused=self.scripted_act([b'\xff'*6],'any',[],job=job,step=None,typed=synthetic_keys()[0],root=Path(tmp),tag='r')
            usage=self.scripted_act([b'\xff'*6],'0,0,0,0',[],job=job,step=None,root=Path(tmp),tag='u')
            passed=self.scripted_act([b'\xff'*6],'any',[dict(x=0,y=0,w=2,h=1)],job=job,step=None,root=Path(tmp),tag='p')
            self.assertEqual([(x[0],x[1]['step']) for x in (refused,usage,passed)],[(5,'auto-1'),(2,'auto-2'),(0,'auto-3')])
            steps=[json.loads(l)['step'] for l in (Path(tmp)/'receipts.jsonl').read_text().splitlines()]
            self.assertEqual(len(steps),len(set(steps)))
    def test_f5_marks_returned_by_act_stay_valid(self):
        with tempfile.TemporaryDirectory(dir='/dev/shm',prefix='vis-f5-') as tmp:
            root=Path(tmp)
            job=dict(job='scripted',ownership='started',target_kind='web',display='web:scripted',steps={},engines={},pids=[],size='2x1')
            def describe(j,frame,prev=None,masks=()):
                return dict(changed_regions=[dict(x=0,y=0,w=2,h=1)],marks=[dict(id=1,x=0,y=0,w=1,h=1)],
                            frame=str(frame),marks_png=str(frame),frame_sha256=core.frame_sha(frame))
            act=lambda tag,trail,initial,**kw: self.scripted_act(trail,'any',[],job=job,root=root,tag=tag,describe_fn=describe,initial=initial,step=None,**kw)
            rc,r,_,_=act('a',[b'\xff'*6],b'\0'*6)
            self.assertEqual(rc,0); self.assertTrue(job['marks_valid']); self.assertEqual(job['marks_sha'],r['frame_sha_after'])
            # Click a mark returned by the previous act: its pre-input frame is that act's after-frame.
            rc,r,_,_=act('b',[b'\0'*6],b'\xff'*6,click='1')
            self.assertEqual(rc,0); self.assertTrue(job['marks_valid'])
            # The surface changed under the mark since that act: the region-hash check still refuses.
            rc,r,_,_=act('c',[b'\xff'*6],b'\xff'*6,click='1')
            self.assertEqual(rc,2); self.assertEqual(job['steps'][r['step']]['attempts'],1)
        with tempfile.TemporaryDirectory(dir='/dev/shm',prefix='vis-f5-') as tmp:
            root=Path(tmp)
            job=dict(job='scripted',ownership='started',target_kind='web',display='web:scripted',steps={},engines={},pids=[],size='2x1')
            nochange=lambda j,frame,prev=None,masks=(): dict(changed_regions=[],marks=[dict(id=1,x=0,y=0,w=1,h=1)],frame=str(frame),marks_png=str(frame),frame_sha256=core.frame_sha(frame))
            rc,_,_,_=self.scripted_act([b'\0'*6],'any',[],job=job,root=root,tag='d',describe_fn=nochange,step=None)
            self.assertEqual(rc,3); self.assertFalse(job.get('marks_valid'))
            rc,_,_,_=self.scripted_act([b'\0'*6],'any',[],job=job,root=root,tag='e',describe_fn=nochange,step=None,click='1')
            self.assertEqual(rc,2)
    def test_f6_act_waits_for_lifecycle_lock_then_budget(self):
        import fcntl,threading
        job=dict(job='lockwait',ownership='started',target_kind='web',display='web:lockwait',steps={},engines={},pids=[])
        args=core.parser().parse_args(['act','--job','lockwait','--key','space'])
        def reached(j,a): raise core.Failure(5,'lock acquired')
        with tempfile.TemporaryDirectory(dir='/dev/shm',prefix='vis-f6-') as tmp, \
             patch.object(core,'jobpath',return_value=Path(tmp)), patch.object(core,'load',return_value=job), \
             patch.object(core,'actor',side_effect=lambda *a,**kw: contextlib.nullcontext()), \
             patch.object(core,'budget'), patch.object(core,'rule',side_effect=reached), patch.dict(os.environ,{'MV_HOSTED':'0'}):
            holder=open(Path(tmp)/'lock','a'); fcntl.flock(holder,fcntl.LOCK_EX)
            release=threading.Timer(.4,holder.close); release.start(); t0=time.monotonic()
            with self.assertRaises(core.Failure) as error: core.do_act(args)
            release.join()
            self.assertEqual((error.exception.rc,error.exception.reason),(5,'lock acquired'))
            self.assertGreaterEqual(time.monotonic()-t0,.35)
            self.assertEqual(core.ACT_LOCK_WAIT,10)
            with open(Path(tmp)/'lock','a') as held, patch.object(core,'ACT_LOCK_WAIT',.3):
                fcntl.flock(held,fcntl.LOCK_EX); t0=time.monotonic()
                with self.assertRaises(core.Failure) as error: core.do_act(args)
                self.assertEqual(error.exception.rc,6); self.assertIn('lifecycle',error.exception.reason)
                self.assertLess(time.monotonic()-t0,2)
    def test_f7_failure_receipts_carry_reason_and_action(self):
        import io
        key=synthetic_keys()[0]
        cases=[(['act','--job','f7','--step','s','--xy','5,6'],core.Failure(8,'target exited: '+'x'*500+' '+key),8,'xy:5,6'),
               (['act','--job','f7','--type','hello there'],core.Failure(3,'expected visible change not verified'),3,'hello there'),
               (['act','--job','f7','--type='+key],core.Failure(5,'refused '+key),5,{'sha256':core.hashlib.sha256(key.encode()).hexdigest()}),
               (['look','--job','f7'],RuntimeError('boom'),9,'look')]
        for argv,error,rc,action in cases:
            with self.subTest(verb=argv[0],rc=rc), tempfile.TemporaryDirectory(dir='/dev/shm',prefix='vis-f7-') as tmp:
                root=Path(tmp); job=dict(job='f7',run_id='r',ownership='started',target_kind='desktop',display=':private',steps={},engines={},pids=[])
                (root/'job.json').write_text(core.canonical(job))
                def dispatch(a):
                    core.CURRENT=job; raise error
                out=io.StringIO()
                with patch.object(core,'jobpath',return_value=root), patch.object(core,'dispatch',side_effect=dispatch), \
                     patch.object(core,'CURRENT',None), patch.object(core,'RECEIPTED',False), patch.object(core,'ARGS',None), \
                     patch.object(core.sys,'argv',['vis',*argv]), contextlib.redirect_stdout(out):
                    self.assertEqual(core.main(),rc)
                r=json.loads((root/'receipts.jsonl').read_text().splitlines()[-1])
                self.assertEqual((r['rc'],r['action']),(rc,action))
                self.assertTrue(r['reason'] and len(r['reason'])<=200)
                self.assertNotIn(key,r['reason']); self.assertNotIn(key,core.canonical(r))
        self.assertEqual(core.redact('a '+key+' b'),'a [redacted] b')
    def test_f8a_death_reason_app_log_first_without_pci_noise(self):
        class Child:
            def __init__(self,pid,name,rc): self.pid,self.args,self.returncode=pid,[name],rc
        with tempfile.TemporaryDirectory(dir='/dev/shm',prefix='vis-f8a-') as tmp:
            root=Path(tmp)
            (root/'app.log').write_text('APP_SAID_GOODBYE\n')
            (root/'xvfb.log').write_text(''.join('(EE) pci id for fd %d: 1234:5678, driver (null)\n' % i for i in range(9))+'XVFB_REAL_ERROR\n')
            w=core.death_witness(root,[Child(10,'Xvfb',None),Child(11,'dbus-run-session',3)],False)
            self.assertEqual((w['rc'],w['oom'],w['exits']),(8,False,[{'pid':11,'process':'dbus-run-session','returncode':3}]))
            self.assertIn('app.log: APP_SAID_GOODBYE',w['reason']); self.assertNotIn('xvfb.log',w['reason'])
            w=core.death_witness(root,[Child(10,'Xvfb',1),Child(11,'dbus-run-session',None)],False)
            self.assertLess(w['reason'].index('app.log:'),w['reason'].index('xvfb.log:'))
            self.assertIn('XVFB_REAL_ERROR',w['reason']); self.assertNotIn('pci id for fd',w['reason'])
            w=core.death_witness(root,[Child(11,'dbus-run-session',-9)],True)
            self.assertEqual((w['rc'],w['oom'],w['reason']),(6,True,'scope memory budget exceeded'))
    def test_f8c_one_actor_lock_per_display_for_every_ownership(self):
        with tempfile.TemporaryDirectory(dir='/dev/shm',prefix='vis-f8c-') as tmp:
            base=Path(tmp)/'vis'; base.mkdir(); (base/'s1').mkdir()
            with patch.object(core,'BASE',base):
                started=dict(job='s1',ownership='started',display=':77')
                attached=dict(job='t1',ownership='attached',display=':77',allow_act=True)
                adopted=dict(job='a1',ownership='adopted',display=':77')
                for holder,other in ((started,attached),(attached,started),(started,adopted),(adopted,attached),(started,started)):
                    with self.subTest(holder=holder['job'],other=other['job']), core.actor(holder):
                        with self.assertRaises(core.Failure) as error:
                            with core.actor(other): self.fail('second actor acquired the same display')
                        self.assertEqual(error.exception.rc,5)
                        with core.actor(dict(job='s2',ownership='started',display=':78')): pass
                # One display, one lock file, outside every job directory: removing a job never changes its inode.
                self.assertEqual(len(list(base.glob('.actor-*'))),2)
                self.assertFalse((base/'s1/actor.lock').exists())
    def test_f8bd_sweep_collects_dead_targets_and_old_kept_frames(self):
        me={'pid':os.getpid(),'start':core.identity(os.getpid())}
        with tempfile.TemporaryDirectory(dir='/dev/shm',prefix='vis-f8b-') as tmp:
            base=Path(tmp)/'vis'; base.mkdir(); stopped=[]
            def job(name,**extra):
                (base/name).mkdir()
                j=dict(job=name,run_id='r-'+name,ownership='started',target_kind='desktop',display=':'+str(90+len(stopped)),
                       pids=[me],owner={'pid':1,'start':'not-this'},steps={},engines={},pidns=core.pid_namespace(),**extra)
                (base/name/'job.json').write_text(core.canonical(j)); return base/name
            (job('deadtarget')/'target.exit.json').write_text(core.canonical(dict(rc=8,reason='target exited',oom=False,exits=[])))
            job('live')
            for name,age in (('kept-old',25*3600),('kept-new',60)):
                (base/name).mkdir(); (base/name/'frame_1.png').write_bytes(b'x')
                (base/name/'kept.json').write_text(core.canonical(dict(kept_at=time.time()-age,pidns=core.pid_namespace())))
            def fake_stop(j,**kw):
                stopped.append(j['job']); core.shutil.rmtree(base/j['job']); return {}
            args=core.parser().parse_args(['sweep'])
            # The 'supervisor' is this test process: stop is replaced and any signal or command fails the test.
            with patch.object(core,'BASE',base), patch.object(core,'ARGS',args), patch.object(core,'stop',side_effect=fake_stop), \
                 patch.object(core.os,'kill',side_effect=AssertionError('signal sent')), \
                 patch.object(core,'run',side_effect=AssertionError('command run')):
                result=core._dispatch(args)
            self.assertEqual(stopped,['deadtarget'])
            self.assertEqual(sorted(result['removed']),['deadtarget','kept-old'])
            self.assertEqual(sorted(result['skipped']),['kept-new','live'])
            # Display actor lock files stay (never unlinked); only job directories are collected.
            self.assertEqual(sorted(p.name for p in base.iterdir() if not p.name.startswith('.actor-')),['kept-new','live'])
    def test_jobs_are_bound_to_the_pid_namespace_that_made_them(self):
        """PIDs recorded in another PID namespace name other processes here: such jobs are
        refused (rc 5) and sweep skips them without a signal, a command or a removal."""
        here=core.pid_namespace(); legacy_ok=here==core.INIT_PIDNS
        with tempfile.TemporaryDirectory(dir='/dev/shm',prefix='vis-pidns-') as tmp:
            base=Path(tmp)/'vis'; base.mkdir()
            def job(name,**extra):
                (base/name).mkdir()
                j=dict(job=name,run_id='r-'+name,ownership='started',target_kind='web',display='web:'+name,
                       pids=[],owner={'pid':1,'start':'not-this'},steps={},engines={},**extra)
                (base/name/'job.json').write_text(core.canonical(j))
                (base/name/'target.exit.json').write_text(core.canonical(dict(rc=8,reason='target exited',oom=False,exits=[])))
            job('foreign',pidns='pid:[1]'); job('legacy')
            (base/'kept-foreign').mkdir(); (base/'kept-foreign/kept.json').write_text(core.canonical(dict(kept_at=0,pidns='pid:[1]')))
            (base/'kept-legacy').mkdir(); (base/'kept-legacy/kept.json').write_text(core.canonical(dict(kept_at=0)))
            args=core.parser().parse_args(['sweep'])
            with patch.object(core,'BASE',base), patch.object(core,'ARGS',args), \
                 patch.object(core,'stop',side_effect=AssertionError('stopped a foreign job')) if not legacy_ok else contextlib.nullcontext(), \
                 patch.object(core.os,'kill',side_effect=AssertionError('signal sent')), \
                 patch.object(core,'run',side_effect=AssertionError('command run')):
                with self.assertRaises(core.Failure) as error: core.load('foreign',enforce_budget=False)
                self.assertEqual(error.exception.rc,5); self.assertIn('PID namespace',error.exception.reason)
                if legacy_ok: self.assertEqual(core.load('legacy',enforce_budget=False)['job'],'legacy')
                else:
                    with self.assertRaises(core.Failure) as error: core.load('legacy',enforce_budget=False)
                    self.assertEqual(error.exception.rc,5)
                    result=core._dispatch(args)
                    self.assertEqual(sorted(result['skipped']),['foreign','kept-foreign','kept-legacy','legacy'])
                    self.assertEqual(result['removed'],[])
                    self.assertEqual(sorted(p.name for p in base.iterdir()),['foreign','kept-foreign','kept-legacy','legacy'])
    def test_test_mode_start_needs_a_private_pid_namespace_and_a_capped_cgroup(self):
        for ns,env,want in ((core.INIT_PIDNS,'1',False),('pid:[4026533000]','1',True),('pid:[4026533000]','0',False),(None,'1',False)):
            with self.subTest(ns=ns,env=env), patch.object(core,'pid_namespace',return_value=ns), patch.dict(os.environ,{'NEWTON_TEST_PIDNS':env}):
                self.assertEqual(core.test_pidns_mode(),want)
        with tempfile.TemporaryDirectory(dir='/dev/shm',prefix='vis-capped-') as tmp:
            (Path(tmp)/'cg').mkdir(); (Path(tmp)/'self').write_text('0::/cg\n')
            (Path(tmp)/'cg/memory.max').write_text('8589934592\n')
            self.assertEqual(core.inherited_memory_cap(tmp,Path(tmp)/'self'),8589934592)
            for value in ('max\n',None):
                if value: (Path(tmp)/'cg/memory.max').write_text(value)
                else: (Path(tmp)/'cg/memory.max').unlink()
                with self.assertRaises(core.Failure) as error: core.inherited_memory_cap(tmp,Path(tmp)/'self')
                self.assertEqual(error.exception.rc,5)
        # An uncapped caller is refused before any job directory exists.
        job='vis-uncapped-'+str(os.getpid())
        args=core.parser().parse_args(['start','--job',job,'--app','true'])
        with patch.object(core,'test_pidns_mode',return_value=True), patch.object(core,'inherited_memory_cap',side_effect=core.Failure(5,'uncapped')), \
             patch.object(core,'engines',side_effect=AssertionError('engines reached')):
            with self.assertRaises(core.Failure) as error: core.start(args)
        self.assertEqual(error.exception.rc,5); self.assertFalse((core.BASE/job).exists())
    def test_f8d_stop_keep_frames(self):
        for ownership in ('adopted','started'):
            with self.subTest(ownership=ownership), tempfile.TemporaryDirectory(dir='/dev/shm',prefix='vis-f8d-') as tmp:
                home=Path(tmp)/'home'; root=Path(tmp)/'job'; root.mkdir()
                for name in ('frame_1.png','marks_1.png','frame_2.png','receipts.jsonl','job.json','lock','helper.sock','fb.bin'):
                    (root/name).write_text('r\n' if name=='receipts.jsonl' else 'x')
                (root/'home/.cache').mkdir(parents=True); (root/'home/.cache/blob').write_text('x'); (root/'crops_1').mkdir()
                job=dict(job='keep',run_id='keep-'+ownership,ownership=ownership,target_kind='desktop',display=':private',pids=[])
                args=core.parser().parse_args(['stop','--job','keep','--keep-frames'])
                with patch.object(core.Path,'home',return_value=home), patch.object(core,'jobpath',return_value=root), \
                     patch.object(core,'ARGS',args), patch.object(core,'shutdown_started') as shutdown:
                    result=core.stop(job,keep_frames=True)
                self.assertEqual(shutdown.called,ownership=='started')
                self.assertEqual(sorted(p.name for p in root.iterdir()),['frame_1.png','frame_2.png','kept.json','marks_1.png'])
                self.assertEqual(sorted(Path(p).name for p in result['kept_frames']),['frame_1.png','frame_2.png','marks_1.png'])
                last=json.loads(Path(result['receipts']).read_text().splitlines()[-1])
                self.assertEqual((last['action'],last['kept_frames']),('stop',3))
                self.assertLess(time.time()-json.loads((root/'kept.json').read_text())['kept_at'],60)
    def test_f9_type_file_only_inside_job_ram_dir(self):
        import builtins
        job=dict(job='tf',ownership='started',target_kind='desktop')
        with tempfile.TemporaryDirectory(dir='/dev/shm',prefix='vis-f9-') as tmp:
            base=Path(tmp); root=base/'job'; root.mkdir(); outside=base/'outside'; outside.mkdir()
            (root/'ok.txt').write_text('Typed from a file: ñ 42\n'); (root/'sub').mkdir(); (root/'sub/deep.txt').write_text('deep')
            (outside/'secret.txt').write_text('outside'); (root/'escape').symlink_to(outside/'secret.txt')
            (root/'inner-link').symlink_to(root/'ok.txt'); (root/'escape-dir').symlink_to(outside,target_is_directory=True)
            os.mkfifo(root/'fifo'); (root/'big.txt').write_bytes(b'a'*(64*1024+1)); (root/'max.txt').write_bytes(b'b'*(64*1024))
            (root/'latin1.txt').write_bytes('caf\xe9'.encode('latin-1')); (root/'key.txt').write_text('token '+synthetic_keys()[3])
            def parse(path): return core.parser().parse_args(['act','--job','tf','--type-file',str(path)])
            with patch.object(core,'jobpath',return_value=root), patch.dict(os.environ,{'MV_HOSTED':'0'}):
                for path,content in ((root/'ok.txt','Typed from a file: ñ 42\n'),(root/'sub/deep.txt','deep'),(root/'max.txt','b'*(64*1024))):
                    a=parse(path); core.rule(job,a); self.assertEqual(a.type,content)
                refused_unopened=[Path.home()/'.ssh/id_x',outside/'secret.txt',root/'escape',root/'inner-link',root/'escape-dir/secret.txt',
                                  root/'fifo',root/'sub',root/'big.txt',root/'missing.txt',root/'..'/'outside/secret.txt']
                for path in refused_unopened:
                    with self.subTest(path=path.name), patch.object(core.os,'open',side_effect=AssertionError('opened')), \
                         patch.object(builtins,'open',side_effect=AssertionError('opened')):
                        with self.assertRaises(core.Failure) as error: core.rule(job,parse(path))
                        self.assertEqual(error.exception.rc,5)
                for path in (root/'latin1.txt',root/'key.txt'):
                    with self.assertRaises(core.Failure) as error: core.rule(job,parse(path))
                    self.assertEqual(error.exception.rc,5)
            sent=[]
            def run(cmd,**kw):
                sent.append(cmd); return core.sp.CompletedProcess(cmd,0,b'',b'')
            with patch.object(core,'desktop_env',return_value={}), patch.object(core,'run',side_effect=run), \
                 patch.object(core,'jobpath',return_value=root):
                args=core.parser().parse_args(['act','--job','scripted','--type-file',str(root/'ok.txt')])
                job2=dict(job='scripted',ownership='started',target_kind='desktop',display=':p',steps={},engines={},pids=[])
                with patch.object(core,'ARGS',args), patch.object(core,'load',return_value=job2), patch.object(core,'save'), \
                     patch.object(core,'lifecycle',side_effect=lambda *a,**kw: contextlib.nullcontext()), \
                     patch.object(core,'actor',side_effect=lambda *a,**kw: contextlib.nullcontext()), \
                     patch.object(core,'budget'), patch.object(core,'target'), patch.object(core,'responsive',return_value=False), \
                     patch.object(core,'capture',side_effect=core.Failure(7,'stop after input check')), patch.dict(os.environ,{'MV_HOSTED':'0'}):
                    with self.assertRaises(core.Failure): core.do_act(args)
            self.assertEqual(args.type,'Typed from a file: ñ 42\n')
            # Receipts name the file, never its content: on failure (main) and on a passing act.
            self.assertEqual(core.action_of(args),'type-file:ok.txt')
            job3=dict(job='scripted',ownership='started',target_kind='desktop',display=':p',steps={},engines={},pids=[])
            (root/'receipts.jsonl').unlink(missing_ok=True)
            with patch.object(core,'desktop_env',return_value={}), patch.object(core,'run',side_effect=run), patch.object(core,'jobpath',return_value=root):
                argv=['act','--job','scripted','--step','tf','--type-file',str(root/'ok.txt'),'--settle-ms','800']
                for changed,rc in ((True,0),(False,3)):
                    a=core.parser().parse_args(argv[:4]+['tf-'+str(rc)]+argv[5:])
                    import struct,zlib
                    def png(rgb):
                        def chunk(k,b): return struct.pack('>I',len(b))+k+b+struct.pack('>I',zlib.crc32(k+b)&0xffffffff)
                        return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',2,1,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(b'\0'+rgb))+chunk(b'IEND',b'')
                    (root/'frame_0.png').write_bytes(png(b'\0'*6)); (root/'frame_1.png').write_bytes(png(b'\xff'*6 if changed else b'\0'*6))
                    frames=iter([root/'frame_0.png']+[root/'frame_1.png']*40)
                    with patch.object(core,'ARGS',a), patch.object(core,'RECEIPTED',False), patch.object(core,'load',return_value=job3), patch.object(core,'save'), \
                         patch.object(core,'lifecycle',side_effect=lambda *x,**kw: contextlib.nullcontext()), \
                         patch.object(core,'actor',side_effect=lambda *x,**kw: contextlib.nullcontext()), \
                         patch.object(core,'budget'), patch.object(core,'target'), patch.object(core,'responsive',return_value=False), \
                         patch.object(core,'capture',side_effect=lambda j: next(frames)), \
                         patch.object(core,'describe',side_effect=lambda j,frame,*x,**kw: {'changed_regions':[dict(x=0,y=0,w=2,h=1)] if changed else [],'frame_sha256':core.frame_sha(frame)}), \
                         patch.dict(os.environ,{'MV_HOSTED':'0'}):
                        try: core.do_act(a); got=0
                        except core.Failure as e: got=e.rc
                    self.assertEqual(got,rc)
                receipts=[json.loads(l) for l in (root/'receipts.jsonl').read_text().splitlines()]
                self.assertEqual([(r['rc'],r['action']) for r in receipts],[(0,'type-file:ok.txt'),(3,'type-file:ok.txt')])
    def test_f10_dead_surfaces_are_gone(self):
        for argv in (['start','--job','x','--app','true','--untrusted'],['ask','--job','x','--seat','sol','--q','q'],
                     ['ask','--job','x','--seat','sol','--q','q','--allow-hosted-pixels'],['check','--job','x','--ask-file']):
            with self.subTest(argv=argv), self.assertRaises(core.Failure) as error: core.parser().parse_args(argv)
            self.assertEqual(error.exception.rc,2)
        job='vis-f10-'+str(os.getpid())
        vis('start','--job',job,'--app','true','--untrusted',rc=2)
        self.assertFalse((core.BASE/job).exists())
        vis('ask','--job',job,'--seat','sol','--q','x',rc=2)
        self.assertFalse((core.BASE/job).exists())
        self.assertNotIn('ask',core.parser()._subparsers._group_actions[0].choices)
    def test_run_gates_is_not_green_when_a_required_set_skips(self):
        import re,shutil,subprocess
        runner=(ROOT/'gates/run_gates.sh').read_text()
        sets=re.findall(r"'(gates/[\w/]+\.py)'",runner)
        self.assertIn('gates/must_fail/test_exit.py',sets)
        with tempfile.TemporaryDirectory(dir='/dev/shm',prefix='vis-rungates-') as tmp:
            tree=Path(tmp); (tree/'docs').mkdir(); (tree/'gates').mkdir()
            shutil.copy(ROOT/'gates/run_gates.sh',tree/'gates/run_gates.sh'); shutil.copy(ROOT/'gates/common.py',tree/'gates/common.py')
            for path in sets:
                (tree/path).parent.mkdir(parents=True,exist_ok=True)
                (tree/path).write_text('import json,sys\nsys.exit(json.load(open(sys.argv[0].rsplit("/gates/",1)[0]+"/rc.json")).get('+repr(path)+',0))\n')
            def run(mode,**rcs):
                (tree/'rc.json').write_text(json.dumps({p:rc for p,rc in ((sets[i],v) for i,v in rcs.get('by',{}).items())}))
                r=subprocess.run(['sh',str(tree/'gates/run_gates.sh'),mode],env=dict(os.environ,NEWTON_TEST_PIDNS='1'),capture_output=True,timeout=120)
                return r.returncode
            exit_set=sets.index('gates/must_fail/test_exit.py'); unit_set=sets.index('gates/unit/test_cli.py')
            self.assertEqual(run('--all'),0)
            self.assertEqual(run('--all',by={exit_set:77}),3)
            self.assertEqual(run('--all',by={exit_set:77,unit_set:1}),1)
            self.assertEqual(run('--unit',by={exit_set:77}),0)
            self.assertEqual(run('--must-fail',by={exit_set:77}),3)
    def test_mv_hosted_reason_requires_core_admission_without_bypass(self):
        job=dict(ownership='started',target_kind='desktop')
        with patch.dict(os.environ,{'MV_HOSTED':'1'}), self.assertRaises(core.Failure) as error:
            core.rule(job,core.parser().parse_args(['act','--job','h','--key','space']))
        self.assertEqual(error.exception.rc,5)
        self.assertIn('MV_HOSTED=1',error.exception.reason)
        self.assertNotIn('unset',error.exception.reason.lower())
        self.assertIn('core admission',error.exception.reason)
    def test_settle_waits_for_requested_rectangle(self):
        outside=b'\xff\0\0\0\0\0'; both=b'\xff\0\0\xff\0\0'
        regions=[dict(x=0,y=0,w=2,h=1)]
        rc,r,samples,_=self.scripted_act([outside]*3+[both]*2,'1,0,1,1',regions)
        self.assertEqual(rc,0); self.assertGreaterEqual(samples,5)
        self.assertEqual(r['changed_regions'],regions)

    def test_settle_outside_only_exhausts_budget(self):
        rc,_,_,elapsed=self.scripted_act([b'\xff\0\0\0\0\0'],'1,0,1,1',[dict(x=0,y=0,w=1,h=1)])
        self.assertEqual(rc,3); self.assertAlmostEqual(elapsed,.8)

    def test_settle_requires_current_rectangle_change(self):
        inside=b'\0\0\0\xff\0\0'; outside=b'\xff\0\0\0\0\0'
        rc,_,samples,_=self.scripted_act([inside,outside,outside,outside,inside,inside],
                                        '1,0,1,1',[dict(x=1,y=0,w=1,h=1)])
        self.assertEqual(rc,0); self.assertGreaterEqual(samples,6)

    def test_positive_expectations_still_require_engine_regions_and_verification(self):
        for expected in ('any','1,0,1,1'):
            with self.subTest(expected=expected):
                self.assertEqual(self.scripted_act([b'\xff'*6],expected,[])[0],3)
                self.assertEqual(self.scripted_act([b'\xff'*6],expected,[dict(x=0,y=0,w=2,h=1)],verified=False)[0],3)

    def test_none_identical_pixels_ignore_phantom_engine_region(self):
        rc,r,_,_=self.scripted_act([b'\0'*6],'none',[dict(x=0,y=0,w=1,h=1)])
        self.assertEqual(rc,0); self.assertEqual(r['changed_regions'],[])
        self.assertEqual(r['frame_sha_before'],r['frame_sha_after'])

    def test_none_masked_change_uses_independent_pixels(self):
        for trail in ([b'\xff\0\0\0\0\0'],[b'\xff\0\0\0\0\0',b'\0'*6]):
            with self.subTest(trail=trail):
                rc,r,_,_=self.scripted_act(trail,'none',[dict(x=0,y=0,w=2,h=1)],masks=['0,0,1,1'])
                self.assertEqual(rc,0); self.assertEqual(r['changed_regions'],[])

    def test_none_rejects_unmasked_transient_final_and_unverified(self):
        changed=b'\0\0\0\xff\0\0'
        for trail in ([changed,b'\0'*6],[changed]):
            with self.subTest(trail=trail):
                rc,_,_,_=self.scripted_act(trail,'none',[],masks=['0,0,1,1'])
                self.assertEqual(rc,3)
        self.assertEqual(self.scripted_act([b'\0'*6],'none',[],verified=False)[0],3)

    def test_remove_job_private_fuse_mounts_only(self):
        with tempfile.TemporaryDirectory(dir='/dev/shm',prefix='vis-fix2-mount-') as tmp:
            base=Path(tmp); root=base/'job'; root.mkdir()
            outside=base/'other'; outside.mkdir()
            (root/'escape').symlink_to(outside,target_is_directory=True)
            mounts=[(outside/'runtime/doc','fuse.portal'),
                    (base/'job-sibling/doc','fuse.portal'),
                    (root/'escape/doc','fuse.portal'),
                    (root/'runtime/doc','fuse.portal'),
                    (root/'data/gvfs space','fuse.gvfsd-fuse')]
            info='\n'.join('1 0 0:1 / '+str(p).replace(' ','\\040')+' rw - '+fs+' source rw' for p,fs in mounts)
            with patch.object(core.Path,'read_text',return_value=info), \
                 patch.object(core.shutil,'which',return_value='/usr/bin/fusermount3'), \
                 patch.object(core,'run',return_value=core.sp.CompletedProcess([],0)) as run:
                core.remove_job(root)
            self.assertEqual([c.args[0] for c in run.call_args_list],
                             [['/usr/bin/fusermount3','-u','-z',str(p)] for p,_ in reversed(mounts[-2:])])
            self.assertFalse(root.exists()); self.assertTrue(outside.is_dir())

    def test_remove_job_refuses_unknown_mount(self):
        with tempfile.TemporaryDirectory(dir='/dev/shm',prefix='vis-fix2-mount-') as tmp:
            root=Path(tmp)/'job'; root.mkdir()
            info='1 0 0:1 / '+str(root/'unexpected')+' rw - tmpfs source rw'
            with patch.object(core.Path,'read_text',return_value=info), patch.object(core,'run') as run:
                with self.assertRaises(core.Failure) as error: core.remove_job(root)
            self.assertEqual(error.exception.rc,5); run.assert_not_called()
            self.assertTrue(root.is_dir())

    def test_usage_protocol(self):
        for args in [(),('wrong',),('look',),('look','--job','../escape'),('act','--job','missing','--step','x','--type','x')]: vis(*args,rc=2,env={'DISPLAY':''})
    def test_rectangles(self):
        for value in ('-1,0,1,1','0,0,0,1','0,0,2','nonsense'):
            with self.assertRaises(core.Failure): core.rect(value)
        self.assertEqual(core.rect('1,2,3,4'),(1,2,3,4))
    def test_secret_patterns(self):
        keys=synthetic_keys()
        self.assertEqual([k for k in keys if not core.is_secret(k)],[])
        self.assertEqual(len(keys),14)
        self.assertEqual(len(ORDINARY_TEXTS),27)
        self.assertEqual([t for t in ORDINARY_TEXTS if core.is_secret(t)],[])
        # Lowercase-only key shapes are refused; filler placeholders behind a provider prefix are not.
        self.assertEqual([k for k in keys[10:] if not core.is_secret(k)],[])
        self.assertEqual([t for t in PLACEHOLDER_TEXTS if core.is_secret(t)],[])
        for key in keys[10:]: self.assertEqual(core.redact('a '+key+' b'),'a [redacted] b')
        for text in ('ordinary message','sk-test','ghp_test','-----BEGIN RSA '+'PRIVATE KEY','sk-'+'a'*40,'ghp_'+'A1'*5,''):
            self.assertFalse(core.is_secret(text),text)
        for key in keys[:8]:
            # Inside a sentence the key is still a separate token.
            self.assertTrue(core.is_secret('see '+key+' here'))
    def test_f1_ordinary_text_reaches_input_and_keys_are_refused(self):
        job=dict(ownership='started',target_kind='desktop')
        with patch.dict(os.environ,{'MV_HOSTED':'0'}):
            for text in ORDINARY_TEXTS:
                core.rule(job,core.parser().parse_args(['act','--job','guard','--step','s','--expect-change','any','--type='+text]))
            for key in synthetic_keys():
                with self.assertRaises(core.Failure) as error:
                    core.rule(job,core.parser().parse_args(['act','--job','guard','--step','s','--expect-change','any','--type='+key]))
                self.assertEqual(error.exception.rc,5)
        for text in ORDINARY_TEXTS:
            with self.subTest(text=text):
                sent=[]
                def run(cmd,**kw):
                    sent.append(cmd); return core.sp.CompletedProcess(cmd,0,b'',b'')
                with patch.object(core,'desktop_env',return_value={}), patch.object(core,'run',side_effect=run):
                    rc,r,_,_=self.scripted_act([b'\xff'*6],'any',[dict(x=0,y=0,w=2,h=1)],target_kind='desktop',typed=text)
                self.assertEqual(rc,0)
                self.assertIn(['xdotool','type','--clearmodifiers','--',text],sent)
        key=synthetic_keys()[0]
        with patch.object(core,'desktop_env',return_value={}), patch.object(core,'run') as run:
            rc,r,captures,_=self.scripted_act([b'\xff'*6],'any',[],target_kind='desktop',typed=key)
        self.assertEqual((rc,captures),(5,-1)); run.assert_not_called()
        self.assertNotIn(key,core.canonical(r))
    def test_pid_start_time(self):
        rec={'pid':os.getpid(),'start':core.identity(os.getpid())}
        self.assertTrue(core.alive(rec)); rec['start']='stale'; self.assertFalse(core.alive(rec))
        with patch.object(core.os,'kill') as kill:
            core.terminate(rec); kill.assert_not_called()
    def test_reader_lock_time_budget_and_exclusion(self):
        with tempfile.TemporaryDirectory(dir='/dev/shm',prefix='vis-fix-lock-') as tmp:
            with patch.object(core,'jobpath',return_value=Path(tmp)):
                with core.lifecycle({'job':'reader'}):
                    with self.assertRaises(core.Failure) as refused:
                        with core.lifecycle({'job':'actor'}):
                            self.fail('conflicting operation acquired the lock')
                    self.assertEqual(refused.exception.rc,5)
                    with patch.object(core,'DEADLINE',time.monotonic()+.04):
                        with self.assertRaises(core.Failure) as expired:
                            with core.lifecycle({'job':'reader'},wait=True):
                                self.fail('reader acquired a held lock')
                    self.assertEqual(expired.exception.rc,6)
                with core.lifecycle({'job':'reader'},wait=True):
                    pass
    def test_png_and_masks(self):
        import struct,zlib
        def png(rgb):
            def chunk(k,b): return struct.pack('>I',len(b))+k+b+struct.pack('>I',zlib.crc32(k+b)&0xffffffff)
            return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',2,1,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(b'\0'+rgb))+chunk(b'IEND',b'')
        with tempfile.TemporaryDirectory(dir='/dev/shm',prefix='visb-unit-') as tmp:
            a=Path(tmp)/'a.png';b=Path(tmp)/'b.png';a.write_bytes(png(b'\0'*6));b.write_bytes(png(b'\xff\0\0\0\0\0'))
            ev,changed=core.pixel_evidence(a,b,[]);self.assertTrue(ev['different']);self.assertFalse(changed((1,0,1,1)))
            ev,_=core.pixel_evidence(a,b,['0,0,1,1']);self.assertFalse(ev['different'])
    @unittest.skipIf(os.environ.get("VIS_GATE_NO_INSTALL")=="1", "installation/rendering excluded by writer surface")
    def test_installer_dry(self):
        execute([ROOT/'install.sh','--dry-run'])
        for host in ('claude','codex'):
            text=(ROOT/'dist'/host/'vis/SKILL.md').read_text();self.assertIn(str(ROOT/'vis'),text);self.assertNotIn('@SOURCE_SHA@',text)
        self.assertLessEqual(len((ROOT/'skill/SKILL.md.tmpl').read_text().splitlines()),150)
        vis('doctor','--skill')
    def test_commit_selection(self):
        with tempfile.TemporaryDirectory(dir='/dev/shm',prefix='visw-lock-') as tmp:
            root=Path(tmp);current=vis('doctor')
            self.assertEqual(current['engines']['wender_ref'],'96120967b435b72fc6d29635f3844dae140388a9')
            self.assertTrue(current['capabilities']['frame_raw'])
            fallback=vis('doctor','--target-repo',tmp)
            self.assertIn('absent',fallback['engines']['selection']['wender_ref'])
            compatibility_lock(root)
            old=vis('doctor','--target-repo',tmp)
            self.assertFalse(old['capabilities']['frame_raw'])
            env=vis('doctor',env={'VIS_TARGET_REPO':tmp})
            self.assertEqual(env['engines']['wender_ref'],old['engines']['wender_ref'])
            (root/'deps.lock').write_text(json.dumps({'dependencies':{'wender-agent':{'version':'96120967'}}}))
            vis('doctor','--target-repo',tmp,rc=2)
            (root/'deps.lock').write_text(json.dumps({'dependencies':{'unrelated':{'version':'1'}}}))
            missing=vis('doctor','--target-repo',tmp)
            self.assertEqual(missing['engines']['wender_ref'],current['engines']['wender_ref'])
    def test_command_budgets(self):
        for code in ('import time;time.sleep(2)','import os;os.write(1,b"x"*(9*1024*1024))'):
            with self.assertRaises(core.Failure) as error:
                core.run([sys.executable,'-I','-c',code],timeout=.1)
            self.assertEqual(error.exception.rc,6)
    def test_no_product_profiles(self): vis('doctor','--profile','metaviewer',rc=2)

if __name__=='__main__':
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Unit))
    note('Unit tests: '+str(result.testsRun)+' [MEASURED]; successful='+str(result.wasSuccessful()))
    sys.exit(not result.wasSuccessful())

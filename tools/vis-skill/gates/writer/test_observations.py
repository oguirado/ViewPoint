#!/usr/bin/env python3
"""Additional writer controls; independent frozen false-green files are untouched."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import time
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import BASE,ROOT,VIS,vis,state,note,require,cleanup,execute, skip_started_jobs,synthetic_keys
loader=importlib.machinery.SourceFileLoader('writer_core',str(VIS))
spec=importlib.util.spec_from_loader(loader.name,loader);core=importlib.util.module_from_spec(spec);loader.exec_module(core)

def main():
    skip_started_jobs()
    work=Path(tempfile.mkdtemp(prefix='visw-controls-',dir='/dev/shm/vqa-cu'));jobs=[]
    def web(label,html):
        page=work/(label+'.html');page.write_text(html);job='visw-'+str(os.getpid())+'-'+label;jobs.append(job)
        vis('start','--job',job,'--web',page.as_uri(),'--size','600x400');return job
    try:
        # Same pixels, independently observed accessible-name mutation.
        job=web('hidden',"""<style>body{margin:0;background:white}button{width:600px;height:400px;background:white;border:0;color:black;outline:none}</style><button aria-label="Before" onclick="this.setAttribute('aria-label','After')">0</button>""")
        a=vis('look','--job',job)
        vis('act','--job',job,'--step','hidden','--xy','300,200','--expect-change','any',rc=3)
        b=vis('look','--job',job)
        require(a['frame_sha256']==b['frame_sha256'],'hidden-state control painted pixels')
        vis('check','--job',job,'--state','button=After')
        require(a['state']!=b['state'],'STATE did not witness handler execution')
        vis('stop','--job',job)
        note('Accessible-name hidden-mutation control: handler witnessed by STATE; identical pixels correctly gave E_EXPECT [MEASURED]. Frozen B was not edited.')

        for result,want in [('Done',0),('Failed',3)]:
            job=web('semantic-'+result,"<button style='width:250px;height:100px' onclick=\"this.innerText='"+result+"';this.style.background='lightgreen'\">Start</button>")
            look=vis('look','--job',job);node=next(n for n in look['state']['nodes'] if n['role']=='button')
            vis('act','--job',job,'--step','result','--ax',node['id'],'--op','click','--expect-change','any')
            vis('check','--job',job,'--state','button=Done',rc=want)
            # F10: the ask verb is gone (rc 2, no request), with or without hosted pixels.
            vis('ask','--job',job,'--seat','sol','--q','Did it pass?',rc=2)
            vis('ask','--job',job,'--seat','sol','--q','Did it pass?','--allow-hosted-pixels',rc=2)
            vis('stop','--job',job)
        note('Paired semantic controls: both actions paint; only Done passes button=Done. The removed ask verb gives rc 2 and sends no request [MEASURED].')

        job=web('handles',"<button style='width:200px;height:100px;background:red' onclick=\"this.style.background=this.style.background==='red'?'blue':'red'\">Toggle</button>")
        d=vis('look','--job',job);mark=max(d['marks'],key=lambda m:m['w']*m['h'])
        vis('act','--job',job,'--step','first','--click',str(mark['id']),'--expect-change','any')
        # F5: a passing act renews the marks, so the same handle clicks again (blue back to red).
        vis('act','--job',job,'--step','renewed','--click',str(mark['id']),'--expect-change','any')
        vis('act','--job',job,'--step','again','--xy','100,50','--expect-change','any')
        # A failed act (no change on empty page) invalidates marks: the handle is now stale.
        vis('act','--job',job,'--step','nochange','--xy','590,390','--expect-change','any',rc=3)
        vis('act','--job',job,'--step','stale','--click',str(mark['id']),'--expect-change','any',rc=2)
        vis('look','--job',job)
        # Current changed region passes, then an exact restoration must fail.
        vis('check','--job',job,'--region-changed-since','first','10,10,180,80')
        vis('act','--job',job,'--step','restore','--xy','100,50','--expect-change','any')
        vis('check','--job',job,'--region-changed-since','first','10,10,180,80',rc=3)
        # Eviction cannot silently substitute surviving evidence. Use captures,
        # without OCR, as production polling does; these are real screenshots.
        j=core.load(job)
        for _ in range(42): core.capture(j)
        j=state(job);require(j['frames_dropped']>0,'no frame loss accounting')
        require(len(list((BASE/job).glob('frame_*.png')))<=40,'frame cap exceeded')
        vis('check','--job',job,'--region-changed-since','first','10,10,180,80',rc=3)
        vis('check','--job',job,'--region-changed-since','unknown','10,10,180,80',rc=2)
        vis('stop','--job',job)
        note('Mark expiration, fresh historical comparisons, restoration negative control and real frame eviction: PASS [MEASURED].')

        job=web('transient',"<button style='width:200px;height:100px;background:white;outline:none' onclick=\"this.style.background='black';setTimeout(()=>this.style.background='white',300)\">Pulse</button>")
        vis('act','--job',job,'--step','pulse','--xy','100,50','--expect-change','none','--settle-ms','800',rc=3)
        vis('stop','--job',job)
        note('Observed change-then-revert fails none despite an equal final frame [MEASURED].')

        job=web('hidden-target',"<button style='width:0;height:0;padding:0;border:0;overflow:hidden' aria-label='Zero'>Z</button><button style='display:none' aria-label='Hidden'>H</button>")
        vis('check','--job',job,'--state','button=Zero',rc=3)
        vis('check','--job',job,'--state','button=Hidden',rc=3)
        vis('check','--job',job,'--state-absent','StaticText=anything',rc=7)
        d=vis('look','--job',job);zero=next(n for n in d['state']['nodes'] if n['name']=='Zero')
        vis('act','--job',job,'--step','zero','--ax',zero['id'],'--op','click','--expect-change','any',rc=3)
        vis('stop','--job',job)
        note('Zero-size/hidden positives fail; unsupported role absence refuses [MEASURED].')

        job=web('constant-engine',"<button style='width:600px;height:400px;background:white;border:0;outline:none'>No operation</button><style>body{margin:0}</style>")
        args=core.parser().parse_args(['act','--job',job,'--step','mutant','--xy','300,200','--expect-change','any'])
        core.ARGS=args;core.RECEIPTED=False
        def constant_changed(j,frame,prev=None,masks=()):
            return {'changed_regions':[{'x':0,'y':0,'w':600,'h':400}],'marks':[],'frame_sha256':core.frame_sha(frame),'frame':str(frame)}
        with patch.object(core,'describe',side_effect=constant_changed):
            try: core.dispatch(args)
            except core.Failure as exc:require(exc.rc==3,'wrong mutant result')
            else:raise AssertionError('constant-changed engine caused a false pass')
        vis('stop','--job',job)
        note('Constant-changed engine substitution rejected by independent pixel evidence [MEASURED]; this supplements rather than rewrites frozen G.')

        # Healthy delayed handler must pass the same target-loop probe as F.
        app=work/'slow.py';app.write_text("import tkinter as t,time\nr=t.Tk();r.title('VIS-W healthy delay');r.geometry('600x400');v=t.Label(r,text='Before');v.pack();\ndef go():\n time.sleep(.3);v.config(text='After',bg='black',fg='white')\nt.Button(r,text='Run',command=go).pack(fill='both',expand=True);r.mainloop()\n")
        job='visw-'+str(os.getpid())+'-healthy';jobs.append(job)
        vis('start','--job',job,'--app',shlex.join([sys.executable,'-I',str(app)]),'--size','600x400')
        vis('act','--job',job,'--step','slow','--xy','300,200','--expect-change','any')
        vis('check','--job',job,'--state','button=Run',rc=7)
        vis('stop','--job',job)
        note('Healthy delayed Tk handler passes; desktop STATE remains E_ABSENT [MEASURED].')

        # Reuse the exact job name; both completed archives must remain readable.
        archives=[];run_ids=[]
        for _ in range(2):
            job=web('reuse',"<button style='width:300px;height:150px' onclick=\"this.textContent='Gone';this.style.background='black'\">Ready</button>")
            run_ids.append(state(job)['run_id'])
            present=vis('check','--job',job,'--state','button=Ready')['receipt']
            acted=vis('act','--job',job,'--step','remove','--xy','100,75','--expect-change','any')
            require(acted['loop_witness']==dict(before=True,after=True,obtained=True),'web loop witness missing')
            absent=vis('check','--job',job,'--state-absent','button=Ready')['receipt']
            require(present['expected']==absent['expected'],'polarity control operands differ')
            require(present['predicate']['kind']=='state' and absent['predicate']['kind']=='state-absent','polarity lost')
            path=Path(vis('stop','--job',job)['receipts']);payload=path.read_bytes()
            require(all(json.loads(line)['run_id']==run_ids[-1] for line in payload.splitlines()),'mixed run IDs')
            archives.append((path,payload))
        require(run_ids[0]!=run_ids[1] and archives[0][0]!=archives[1][0],'job reuse reused identity/archive')
        require(archives[0][0].read_bytes()==archives[0][1],'earlier archive overwritten')
        note('VIS-FIX5: live presence/absence receipts distinguish identical operands; repeated job names preserve distinct run IDs and complete archives [MEASURED].')

        # Synthetic fragments only: demonstrate the guard's documented limit.
        value=work/'synthetic-entry.txt';app=work/'entry.py'
        app.write_text("import pathlib,sys,tkinter as t\nr=t.Tk();r.title('VIS-FIX5 entry');r.geometry('600x400');v=t.StringVar();v.trace_add('write',lambda *a:pathlib.Path(sys.argv[1]).write_text(v.get()));e=t.Entry(r,textvariable=v,font=('Sans',24));e.pack(fill='both',expand=True);r.bind('<F8>',lambda event:v.set(''));r.after(200,e.focus_force);r.mainloop()\n")
        job='visw-'+str(os.getpid())+'-entry';jobs.append(job)
        vis('start','--job',job,'--app',shlex.join([sys.executable,'-I',str(app),str(value)]),'--size','600x400')
        key=synthetic_keys()[0]
        vis('act','--job',job,'--step','whole','--type='+key,'--expect-change','any',rc=5)
        for step,arg in [('fragment-a','--type='+key[:3]),('fragment-b','--type='+key[3:])]:
            vis('act','--job',job,'--step',step,arg,'--expect-change','any')
        require(value.read_text()==key,'split text did not reach synthetic entry')
        vis('act','--job',job,'--step','clear','--key','F8','--expect-change','any')
        vis('act','--job',job,'--step','replace','--type','sk','--expect-change','any')
        result=vis('act','--job',job,'--step','key','--key','minus','--expect-change','any')
        require(value.read_text()=='sk-','desktop key did not complete the synthetic credential prefix')
        require(result['loop_witness']==dict(before=True,after=True,obtained=True),'Tk witness not reported')
        vis('stop','--job',job)
        note('VIS-FIX5: synthetic whole payload refused; split text and desktop keys reach the entry; no credential prevention claimed [MEASURED].')
    finally:
        for job in reversed(jobs):cleanup(job)
        shutil.rmtree(work,ignore_errors=True)

if __name__=='__main__':main()

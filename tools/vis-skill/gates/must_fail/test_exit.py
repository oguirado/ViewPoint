#!/usr/bin/env python3
"""Started jobs: an intended clean exit is a pass, not E_TARGET; cleanup leaves nothing running."""
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import BASE,ROOT,note,require,vis,cleanup,skip_started_jobs,not_in_test_mode

FIXTURE='''import sys,tkinter as tk
root=tk.Tk(); root.title('VIS-EXIT fixture'); root.geometry('600x400')
tk.Label(root,text='q quits 0, e exits 3',font=('Sans',24)).pack(pady=80)
def quit0(e): root.destroy()
def exit3(e): sys.stdout.flush(); print('VIS_EXIT_APP_LOG exiting 3',flush=True); root.destroy(); sys.exit(3)
root.bind_all('<KeyPress-q>',quit0); root.bind_all('<KeyPress-e>',exit3)
root.after(100,root.focus_force); root.mainloop()
'''

def receipts(job): return [json.loads(l) for l in (BASE/job/'receipts.jsonl').read_text().splitlines()]

def main():
    skip_started_jobs()
    work=Path(tempfile.mkdtemp(prefix='vis-exit-',dir='/dev/shm/vqa-cu')); jobs=[]
    fixture=work/'fixture.py'; fixture.write_text(FIXTURE)
    app=shlex.join([sys.executable,'-I',str(fixture)])
    def start(label):
        job='vis-exit-'+str(os.getpid())+'-'+label; jobs.append(job)
        vis('start','--job',job,'--app',app,'--size','600x400','--wait-title','VIS-EXIT'); time.sleep(.3)
        return job
    try:
        job=start('clean')
        r=vis('act','--job',job,'--key','q','--expect-change','exit')
        require(r['exit']=={'returncode':0} and r['receipt']['frame_sha_after'] is None and r['receipt']['expected']=='exit',r)
        vis('stop','--job',job)
        note('F3: intended clean exit passed rc 0 with frame_sha_after null; stop rc 0 [MEASURED]')

        job=start('alive')
        vis('act','--job',job,'--key','x','--expect-change','exit','--settle-ms','1500',rc=3)
        vis('stop','--job',job)
        note('F3: client still alive at the settle bound gave rc 3 [MEASURED]')

        job=start('nonzero')
        failure=vis('act','--job',job,'--key','e','--expect-change','exit',rc=8)
        require(failure['reason'].startswith('target exited:'),failure)
        reason=json.loads((BASE/job/'target.exit.json').read_text())['reason']
        require('app.log: ' in reason and 'VIS_EXIT_APP_LOG' in reason and 'pci id for fd' not in reason,reason)
        require('xvfb.log' not in reason or reason.index('app.log')<reason.index('xvfb.log'),reason)
        vis('stop','--job',job)
        note('F3: non-zero client exit gave rc 8; F8: death reason leads with app.log, no pci-id noise [MEASURED]')

        job=start('keep')
        vis('act','--job',job,'--key','x','--settle-ms','300',rc=None)
        recorded=json.loads((BASE/job/'job.json').read_text())['pids']
        kept=vis('stop','--job',job,'--keep-frames')
        names=sorted(p.name for p in (BASE/job).iterdir())
        require(kept['kept_frames'] and all(Path(f).is_file() for f in kept['kept_frames']),kept)
        require(all(n=='kept.json' or n.startswith(('frame_','marks_')) for n in names),names)
        def alive(rec):
            try: return Path('/proc',str(rec['pid']),'stat').read_text().rsplit(')',1)[1].split()[19]==rec['start']
            except (OSError,IndexError): return False
        require(not any(alive(r) for r in recorded),'kept-frames stop left a recorded process alive')
        vis('stop','--job',job,rc=2)
        shutil.rmtree(BASE/job)
        note('F8: stop --keep-frames kept '+str(len(kept['kept_frames']))+' PNGs in the RAM job with no recorded process alive [MEASURED]')

        if not_in_test_mode('F3 OOM inside a 128M job scope gives rc 6'): return
        hog='vis-exit-'+str(os.getpid())+'-hog'; jobs.append(hog)
        trigger=work/'hog-trigger'; hogpid=work/'hog-pid.json'
        vis('start','--job',hog,'--app',shlex.join([sys.executable,str(ROOT/'gates/must_fail/memory_target.py'),str(trigger),str(hogpid)]),
            '--size','600x400','--wait-title','VIS-B capped target','--mem','128M')
        cg=Path(json.loads((BASE/hog/'job.json').read_text())['cgroup'])
        trigger.touch(); deadline=time.monotonic()+12; events={}
        while time.monotonic()<deadline:
            events=dict(line.split() for line in (cg/'memory.events').read_text().splitlines()) if (cg/'memory.events').exists() else {}
            if int(events.get('oom_kill','0')): break
            time.sleep(.1)
        require(int(events.get('oom_kill','0'))>0,'hog did not hit its own 128M cap')
        vis('act','--job',hog,'--key','q','--expect-change','exit',rc=6)
        vis('stop','--job',hog)
        note('F3: OOM inside the job scope gave rc 6 [MEASURED]')
    finally:
        for job in reversed(jobs): cleanup(job)
        shutil.rmtree(work,ignore_errors=True)

if __name__=='__main__': main()

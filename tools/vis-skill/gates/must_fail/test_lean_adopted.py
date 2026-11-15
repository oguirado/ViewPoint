#!/usr/bin/env python3
"""Lean fast path on a private Xvfb and Tk fixture this test starts itself, then adopts.
Runs inside safe_run: every process here is a Popen child of this test; nothing else is signalled."""
import fcntl
import json
import os
import threading
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import BASE,execute,note,require,vis,VIS,ORDINARY_TEXTS,PLACEHOLDER_TEXTS,synthetic_keys

FIXTURE='''import json,pathlib,sys,tkinter as tk
out=pathlib.Path(sys.argv[1]); repaint=pathlib.Path(sys.argv[2]); n=0; typed=''
root=tk.Tk(); root.title('VIS-LEAN fixture'); root.geometry('800x600+0+0'); root.configure(background='white')
label=tk.Label(root,text='No events',font=('Sans',28),background='white'); label.pack(pady=60)
side=tk.Label(root,text='Side panel',font=('Sans',28),background='white'); side.pack(pady=60)
def save(): out.write_text(json.dumps({'events':n,'typed':typed}))
def event(e):
    global n,typed
    n+=1
    if e.type=='2' or str(e.type)=='KeyPress': typed+=e.char
    label.config(text='Events '+str(n)); save()
def poll():
    if repaint.exists():
        repaint.unlink(); colour='black' if side.cget('background')=='white' else 'white'
        root.configure(background=colour)
        for w in (label,side): w.configure(background=colour,foreground='white' if colour=='black' else 'black')
    root.after(50,poll)
root.bind_all('<KeyPress>',event); root.bind_all('<ButtonPress>',event)
save(); root.after(100,root.focus_force); root.after(50,poll); root.mainloop()
'''

class Lean:
    def __init__(self):
        self.work=Path(tempfile.mkdtemp(prefix='vis-lean-',dir='/dev/shm'))
        self.job='vis-lean-'+str(os.getpid()); self.server=self.app=None
        self.counter=self.work/'counter.json'; self.repaint=self.work/'repaint'

    def __enter__(self):
        display=None
        for number in range(2100,65536):
            if not Path('/tmp/.X11-unix/X'+str(number)).exists() and not Path('/tmp/.X'+str(number)+'-lock').exists():
                display=':'+str(number); break
        require(display is not None,'no private display available')
        self.server=subprocess.Popen(['Xvfb',display,'-screen','0','800x600x24','-nolisten','tcp','-noreset'],
                                     stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        env=os.environ.copy()
        for key,sub in [('HOME','home'),('XDG_CONFIG_HOME','config'),('XDG_CACHE_HOME','cache'),
                        ('XDG_DATA_HOME','data'),('XDG_RUNTIME_DIR','runtime'),('TMPDIR','tmp')]:
            (self.work/sub).mkdir(mode=0o700); env[key]=str(self.work/sub)
        for key in ('XAUTHORITY','WAYLAND_DISPLAY','DBUS_SESSION_BUS_ADDRESS','SESSION_MANAGER'): env.pop(key,None)
        env['DISPLAY']=display
        deadline=time.monotonic()+5
        while not Path('/tmp/.X11-unix/X'+display[1:]).exists():
            require(self.server.poll() is None and time.monotonic()<deadline,'private Xvfb failed to start')
            time.sleep(.05)
        fixture=self.work/'fixture.py'; fixture.write_text(FIXTURE)
        self.app=subprocess.Popen([sys.executable,'-I',str(fixture),str(self.counter),str(self.repaint)],env=env,
                                  stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        deadline=time.monotonic()+5
        while not self.counter.exists():
            require(self.app.poll() is None and time.monotonic()<deadline,'Tk fixture failed to start')
            time.sleep(.05)
        time.sleep(.5)
        self.display=display
        vis('adopt','--job',self.job,'--display',display+'.0','--xvfb-pid',str(self.server.pid))
        return self

    def __exit__(self,*exc):
        if (BASE/self.job/'job.json').exists(): vis('stop','--job',self.job)
        for process in (self.app,self.server):
            if process is not None:
                if process.poll() is None: process.terminate()
                try: process.wait(timeout=3)
                except subprocess.TimeoutExpired: process.kill(); process.wait()
        shutil.rmtree(self.work)
        if (BASE/self.job).is_dir() and not (BASE/self.job/'job.json').exists():
            shutil.rmtree(BASE/self.job)  # this test's own kept-frames job

    def events(self): return json.loads(self.counter.read_text())
    def receipts(self): return [json.loads(l) for l in (BASE/self.job/'receipts.jsonl').read_text().splitlines()]
    def act(self,*args,rc=0,env=None): return vis('act','--job',self.job,*args,rc=rc,env=env)

def f1(t):
    for i,text in enumerate(ORDINARY_TEXTS+PLACEHOLDER_TEXTS):
        before=t.events()
        t.act('--step','f1-ordinary-'+str(i),'--type='+text,'--expect-change','any')
        after=t.events()
        require(after['typed'].endswith(text) and after['events']>before['events'],('ordinary text did not reach input',text,after))
    total=len(ORDINARY_TEXTS+PLACEHOLDER_TEXTS)
    note('F1 ordinary corpus and filler placeholders: '+str(total)+'/'+str(total)+' typed into the fixture [MEASURED]')
    keys=synthetic_keys()
    for i,key in enumerate(keys):
        before=t.events()
        r=t.act('--step','f1-key-'+str(i),'--type='+key,'--expect-change','any',rc=5)
        time.sleep(.1)
        require(t.events()==before,'key-shaped text reached input')
        require(key not in json.dumps(r) and key not in (BASE/t.job/'receipts.jsonl').read_text(),'key echoed')
    note('F1 synthetic keys: '+str(len(keys))+'/'+str(len(keys))+' refused rc 5 with no input event [MEASURED]')

def f2(t):
    before=t.events()['events']
    r=t.act('--step','f2-default','--xy','400,300')
    require(t.events()['events']==before+1 and r['receipt']['expected']=='any','default expectation is not any')
    require(t.receipts()[-1]['expected']=='any','archived receipt lacks expected any')
    t.act('--step','f2-none','--xy','799,599','--expect-change','none',rc=3)
    note('F2: act without --expect-change passed with receipt expected any; explicit none still refuses a change [MEASURED]')

def f3(t):
    before=t.events()
    t.act('--step','f3-adopted','--key','q','--expect-change','exit',rc=2)
    time.sleep(.1); require(t.events()==before,'exit expectation on an adopted display sent input')
    note('F3: exit expectation on an adopted display refused rc 2 before input [MEASURED]')

def f4(t):
    ids=[]
    for _ in range(2):
        r=t.act('--xy','400,300'); ids.append(r['receipt']['step'])
        require(r['receipt']['attempts']==1,'auto step attempts not counted per id')
    require(ids[0].startswith('auto-') and ids[1].startswith('auto-') and ids[0]!=ids[1],ids)
    before=t.events()
    t.act('--step','f2-default','--xy','400,300',rc=2)
    time.sleep(.1); require(t.events()==before,'reused passed step sent input')
    note('F4: acts without --step got '+repr(ids)+'; explicit reuse of a passed id refused rc 2 [MEASURED]')

def f5(t):
    r=t.act('--xy','400,580')
    marks=[m for m in r['marks'] if m.get('w',0)>0 and m.get('h',0)>0]
    require(marks,'act returned no clickable marks')
    before=t.events()['events']
    r2=t.act('--click',str(marks[0]['id']))
    require(t.events()['events']==before+1,'click on a mark returned by act did not land')
    marks=[m for m in r2['marks'] if m.get('w',0)>0 and m.get('h',0)>0]
    require(marks,'second act returned no clickable marks')
    t.repaint.touch(); deadline=time.monotonic()+3
    while t.repaint.exists() and time.monotonic()<deadline: time.sleep(.05)
    time.sleep(.3); before=t.events()
    stale=t.act('--click',str(marks[0]['id']),rc=2)
    require('changed since look' in stale['reason'],stale)
    time.sleep(.1); require(t.events()==before,'stale mark click sent input')
    note('F5: a mark returned by act was clicked without a look; after a repaint the region-hash check refused rc 2 [MEASURED]')

def f6(t):
    lock=open(BASE/t.job/'lock','a'); fcntl.flock(lock,fcntl.LOCK_EX)
    release=threading.Timer(1.5,lock.close); release.start(); t0=time.monotonic(); before=t.events()['events']
    t.act('--xy','400,300'); waited=time.monotonic()-t0; release.join()
    require(waited>=1.4 and t.events()['events']==before+1,('act did not wait for the lifecycle lock',waited))
    with open(BASE/t.job/'lock','a') as held:
        fcntl.flock(held,fcntl.LOCK_EX); before=t.events(); t0=time.monotonic()
        r=t.act('--xy','400,300',rc=6); waited=time.monotonic()-t0
    time.sleep(.1)
    require(t.events()==before and 9.5<=waited<=20 and 'lifecycle' in r['reason'],('held lock',waited,r))
    note('F6: act waited for a 1.5 s lifecycle holder then passed; a holder past 10 s gave rc 6 after %.1f s with no input [MEASURED]' % waited)

def f7(t):
    t.act('--click','9999',rc=2)
    r=t.receipts()[-1]
    require(r['rc']==2 and r['action']=='mark:9999' and r['reason']=='mark absent from latest look',r)
    t.act('--xy','799,599','--expect-change','none',rc=3)
    r=t.receipts()[-1]
    require(r['rc']==3 and r['action']=='click:(799, 599)' and r['reason']=='expected visible change not verified',r)
    note('F7: failure receipts carry reason and action [MEASURED]')

def f8(t):
    kept=vis('stop','--job',t.job,'--keep-frames')
    names=sorted(p.name for p in (BASE/t.job).iterdir())
    require(kept['kept_frames'] and all(Path(f).is_file() for f in kept['kept_frames']),kept)
    require('kept.json' in names and all(n=='kept.json' or n.startswith(('frame_','marks_')) for n in names),names)
    require(t.server.poll() is None and t.app.poll() is None,'stop --keep-frames of an adopted job stopped external processes')
    vis('look','--job',t.job,rc=2)
    note('F8: stop --keep-frames on an adopted job kept '+str(len(kept['kept_frames']))+' PNGs; Xvfb and fixture alive; later verbs see no job [MEASURED]')

def f9(t):
    data=BASE/t.job/'typed-data.txt'; data.write_text('Typed from a job file 42')
    before=t.events()['events']
    t.act('--type-file',str(data))
    require(t.events()['typed'].endswith('Typed from a job file 42'),'job-dir file content was not typed')
    outside=t.work/'outside.txt'; outside.write_text('outside')
    for path in (Path.home()/'.ssh/id_x',outside):
        before=t.events()
        t.act('--type-file',str(path),rc=5)
        time.sleep(.1); require(t.events()==before,'refused type-file sent input')
    data.unlink()
    note('F9: --type-file typed a job-dir file; ~/.ssh/id_x and a file outside the job refused rc 5 [MEASURED]')

def f10(t):
    gone=t.job+'-untrusted'
    vis('start','--job',gone,'--app','true','--untrusted',rc=2); require(not (BASE/gone).exists(),'removed flag created a job dir')
    vis('ask','--job',t.job,'--seat','sol','--q','x','--allow-hosted-pixels',rc=2)
    vis('check','--job',t.job,'--ask-file',rc=2)
    before=t.events()
    r=t.act('--xy','400,300',rc=5,env={'MV_HOSTED':'1'})
    time.sleep(.1); require(t.events()==before and 'MV_HOSTED=1' in r['reason'] and 'unset' not in r['reason'].lower() and 'core admission' in r['reason'],r)
    note('F10: --untrusted, ask and --ask-file give rc 2 with no job dir; MV_HOSTED refusal requires core admission without bypass advice [MEASURED]')

def main():
    with Lean() as t:
        for item in (f1,f2,f3,f4,f5,f6,f7,f9,f10,f8):
            item(t)
    note('Lean adopted gate: PASS [MEASURED]')

if __name__=='__main__': main()

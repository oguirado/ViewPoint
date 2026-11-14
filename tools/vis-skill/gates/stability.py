#!/usr/bin/env python3
"""Measured private-display cycles. Run inside a MemoryMax-capped user scope.

Example: systemd-run --user --scope -q -p MemoryMax=4G -p MemorySwapMax=0
  --collect python3 -I gates/stability.py --cycles 20 --load-workers 2
Output is text/JSON only; all targets and frames stay in job RAM directories.
"""
import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
VIS=ROOT/'vis'
BASE=Path('/dev/shm/vqa-cu/vis')

def identity(pid):
    try:
        f=Path('/proc',str(pid),'stat').read_text().rsplit(')',1)[1].split()
        return f[19]  # Includes zombies: an unreaped PID is a leak.
    except (FileNotFoundError,ProcessLookupError): return None

def invoke(job,verb,*args):
    start=time.monotonic()
    r=subprocess.run([sys.executable,'-I',str(VIS),verb,'--job',job,*args],capture_output=True,timeout=150)
    try: data=json.loads(r.stdout)
    except ValueError: raise RuntimeError((verb,r.returncode,r.stdout[-1000:],r.stderr[-1000:]))
    if data.get('rc')!=r.returncode or data.get('ok')!=(r.returncode==0) or len(r.stdout.splitlines())!=1:
        raise RuntimeError(('bad CLI envelope',verb,r.returncode,data))
    return dict(verb=verb,rc=r.returncode,seconds=round(time.monotonic()-start,3),reason=data.get('reason')),data

def cycle(kind,index,work,tag):
    job='vis-stab-'+str(os.getpid())+'-'+tag+'-'+str(index)
    root=BASE/job; records={}; displays=set(); display_files={}; groups=set(); launchers=set(); done=threading.Event()
    result=dict(job=job,kind=kind,commands=[],failures=[],leaks=[])
    def observe():
        try:
            j=json.loads((root/'job.json').read_text())
            for rec in j.get('pids',[]):
                if rec.get('start'): records[rec['pid']]=rec['start']
            if j.get('display','').startswith(':'):
                number=j['display'][1:];displays.add(number)
                for path in (Path('/tmp/.X11-unix/X'+number),Path('/tmp/.X'+number+'-lock')):
                    try:
                        st=path.lstat();display_files.setdefault(path,(st.st_dev,st.st_ino))
                    except FileNotFoundError: pass
            if j.get('launcher'): launchers.add(j['launcher'])
            if j.get('cgroup'):
                group=Path(j['cgroup']);groups.add(group)
                try:
                    for pid in (group/'cgroup.procs').read_text().split():
                        start=identity(pid)
                        if start: records[int(pid)]=start
                except FileNotFoundError: pass
        except (OSError,ValueError): pass
    def monitor():
        while not done.wait(.03): observe()
    thread=threading.Thread(target=monitor,daemon=True);thread.start()
    def command(verb,*args):
        item,data=invoke(job,verb,*args);result['commands'].append(item);observe()
        if item['rc']: raise RuntimeError(json.dumps(item))
        return data
    try:
        if kind=='tk':
            counter=work/(job+'.json')
            app=shlex.join([sys.executable,str(ROOT/'gates/must_fail/event_target.py'),str(counter)])
            title='VIS-B event fixture'
        else:
            saved=work/(job+'.txt');saved.touch()
            app=shlex.join(['gedit','--standalone',str(saved)]);title=job
        started=command('start','--app',app,'--size','800x600','--wait-title',title,'--timeout','30')
        result['display']=started['display']
        command('look')
        command('act','--step','write','--type','stability '+job,'--expect-change','any')
        if kind=='tk': command('check','--window-title',title)
        else:
            # Judge the painted start of the document line. A full editing
            # area includes gedit's blinking caret and cannot be stable.
            command('check','--region-changed-since','write','32,54,100,24')
        if kind=='tk' and json.loads(counter.read_text())['events']==0:
            raise RuntimeError('Tk target did not receive input')
        command('stop')
    except Exception as exc: result['failures'].append(str(exc))
    finally:
        if root.exists():
            try:
                item,_=invoke(job,'stop');result['commands'].append(item)
                if item['rc']: result['failures'].append('cleanup: '+json.dumps(item))
            except Exception as exc: result['failures'].append('cleanup: '+str(exc))
        done.set();thread.join();observe()
        for pid,start in records.items():
            if identity(pid)==start: result['leaks'].append('pid:'+str(pid))
        for path,owned_inode in display_files.items():
            try:
                st=path.lstat()
                # Another private job may already have reused this number.
                if (st.st_dev,st.st_ino)==owned_inode: result['leaks'].append(str(path))
            except FileNotFoundError: pass
        for group in groups:
            try:
                if (group/'cgroup.procs').read_text().strip(): result['leaks'].append(str(group))
            except FileNotFoundError: pass
        # Manager may collect the now-empty launch service just after exit.
        for unit in launchers:
            r=subprocess.run(['systemctl','--user','show',unit,'--property=MainPID','--value'],capture_output=True,timeout=5)
            if r.stdout.strip() not in (b'',b'0'): result['leaks'].append('launcher:'+unit)
        if root.exists(): result['leaks'].append(str(root))
    result['ok']=not result['failures'] and not result['leaks']
    return result

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cycles',type=int,default=20)
    parser.add_argument('--load-workers',type=int,default=2)
    parser.add_argument('--out',type=Path,default=ROOT/'docs/STABILITY_RESULTS.json')
    args=parser.parse_args()
    if args.cycles<20 or not 0<=args.load_workers<=4: parser.error('cycles >=20; load-workers in 0..4')
    # Fail closed if the requested harness itself is uncapped.
    cg=Path('/sys/fs/cgroup'+Path('/proc/self/cgroup').read_text().strip().split('::',1)[1])
    cap=(cg/'memory.max').read_text().strip()
    if cap=='max': raise SystemExit('stability harness requires a capped scope')
    if not args.out.resolve().is_relative_to(ROOT): parser.error('text report must stay in this worktree')
    source_sha=hashlib.sha256(VIS.read_bytes()).hexdigest()
    report=dict(schema='vis.stability/v1',vis_sha256=source_sha,cycles=args.cycles,load_workers=args.load_workers,
                harness_memory_max=int(cap),scenarios={},results=[],measurement='MEASURED')
    load=[];start=time.monotonic()
    def persist():
        report['elapsed_seconds']=round(time.monotonic()-start,3)
        args.out.write_text(json.dumps(report,sort_keys=True,indent=2)+'\n')
    try:
        for _ in range(args.load_workers):
            load.append(subprocess.Popen([sys.executable,'-I','-c','import time\nend=time.monotonic()+1800\nx=1\nwhile time.monotonic()<end: x=(x*1664525+1013904223)%4294967296'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL))
        with tempfile.TemporaryDirectory(dir='/dev/shm',prefix='vis-stability-') as tmp:
            work=Path(tmp)
            for scenario in ('tk','gedit','parallel'):
                batches=[];rows=[]
                for i in range(args.cycles):
                    if scenario=='parallel':
                        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                            batch=list(pool.map(lambda k:cycle('tk' if k==1 else 'gedit',i*3+k,work,'parallel'),range(3)))
                    else: batch=[cycle(scenario,i,work,scenario)]
                    if scenario=='parallel':
                        allocated=[r['display'] for r in batch if 'display' in r]
                        if len(allocated)!=len(set(allocated)):
                            for row in batch: row['failures'].append('parallel displays are not distinct');row['ok']=False
                    rows+=batch;report['results']+=batch;batches.append(all(r['ok'] for r in batch))
                    print(json.dumps(dict(scenario=scenario,cycle=i+1,ok=batches[-1],failed_jobs=sum(not r['ok'] for r in batch))),flush=True)
                    failed=sum(not r['ok'] for r in rows)
                    report['scenarios'][scenario]=dict(cycles=len(batches),jobs=len(rows),failed_cycles=batches.count(False),
                        failed_jobs=failed,failures_per_100=100*batches.count(False)/len(batches),
                        job_failures_per_100=100*failed/len(rows),leaks=sum(len(r['leaks']) for r in rows))
                    persist()
    finally:
        for proc in load:
            if proc.poll() is None: proc.terminate()
        for proc in load:
            try: proc.wait(timeout=5)
            except subprocess.TimeoutExpired: proc.kill();proc.wait()
        report['load_reaped']=all(proc.returncode is not None for proc in load);persist()
    report['source_unchanged']=hashlib.sha256(VIS.read_bytes()).hexdigest()==source_sha
    report['ok']=all(s['failures_per_100']<=5 and not s['leaks'] for s in report['scenarios'].values()) and len(report['scenarios'])==3 and report['source_unchanged']
    persist();print(json.dumps(report['scenarios'],sort_keys=True),flush=True)
    return 0 if report['ok'] else 1

if __name__=='__main__': sys.exit(main())

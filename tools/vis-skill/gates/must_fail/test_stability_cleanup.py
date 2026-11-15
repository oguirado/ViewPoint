#!/usr/bin/env python3
"""Force supervisor teardown with stopped owned processes; inspect zombies too.
An uncooperative client ignores TERM and stops its own Popen child.
The harness only reads process metadata; no ancestor or file-derived PID is signalled."""
import ctypes
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import BASE, ROOT, vis, require, note, skip_started_jobs

def stat(pid):
    try:
        f=Path('/proc',str(pid),'stat').read_text().rsplit(')',1)[1].split()
        return f[0],f[19],int(f[1])
    except (OSError,IndexError,ValueError): return None

def tree(roots):
    """Live identities of the recorded processes and their descendants, read from /proc."""
    rows={}
    for p in Path('/proc').glob('[0-9]*'):
        s=stat(p.name)
        if s: rows[int(p.name)]=s
    own={r['pid'] for r in roots if r['pid'] in rows and rows[r['pid']][1]==r['start']}; changed=True
    while changed:
        changed=False
        for pid,(_,_,parent) in rows.items():
            if parent in own and pid not in own: own.add(pid); changed=True
    return {pid:rows[pid][1] for pid in own}

def main():
    skip_started_jobs()
    # Only adopt/reap descendants of this test, including pre-fix orphans.
    require(ctypes.CDLL(None).prctl(36,1,0,0,0)==0,'subreaper unavailable')
    job='vis-fix4-reap-'+str(os.getpid())
    with tempfile.TemporaryDirectory(dir='/dev/shm',prefix='vis-fix4-reap-') as tmp:
        try:
            counter=Path(tmp)/'counter'
            vis('start','--job',job,'--app',shlex.join([sys.executable,str(ROOT/'gates/must_fail/frozen_client.py'),str(counter)]), '--size','640x480')
            state=json.loads((BASE/job/'job.json').read_text())
            socket_path=Path('/tmp/.X11-unix/X'+state['display'][1:])
            socket_stat=socket_path.lstat();socket_inode=(socket_stat.st_dev,socket_stat.st_ino)
            records=tree(state['pids'])
            app=json.loads(counter.read_text())['pid']
            Path(str(counter)+'.freeze-session').touch()
            deadline=time.monotonic()+5
            while time.monotonic()<deadline and (stat(app) or ('',))[0] not in ('T','t'): time.sleep(.05)
            require((stat(app) or ('',))[0] in ('T','t'),('owned Popen child did not stop',stat(app)))
            # The wrapper ignores TERM; its Popen child is stopped.
            # Their supervisor must escalate, wait, then exit in that order.
            result=vis('stop','--job',job,rc=None)
            survivors={pid:stat(pid) for pid,start in records.items() if stat(pid) and stat(pid)[1]==start}
            require(result['rc']==0 and not survivors,(result,survivors))
            require(not (BASE/job).exists(),'job dir survived')
            if socket_path.exists():
                current=socket_path.lstat()
                require((current.st_dev,current.st_ino)!=socket_inode,'owned display survived')
            note('Forced stopped-child cleanup: PASS; '+str(len(records))+' owned PIDs (TERM-ignoring client and stopped Popen child included) left no live or zombie PID, display, or job directory [MEASURED].')
        finally:
            # Reap only zombies reparented to this subreaper; nothing is signalled.
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                try:
                    pid,_=os.waitpid(-1,os.WNOHANG)
                    if not pid: time.sleep(.02)
                except ChildProcessError: break
            if (BASE/job).exists(): vis('stop','--job',job)

if __name__=='__main__': main()

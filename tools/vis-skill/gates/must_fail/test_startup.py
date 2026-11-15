#!/usr/bin/env python3
"""Separate a slow private desktop from an actual target exit."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import ROOT, BASE, vis, require, note, cleanup, skip_started_jobs

def main():
    skip_started_jobs()
    slow='vis-fix4-slow-'+str(os.getpid()); dead='vis-fix4-dead-'+str(os.getpid()); old='vis-fix4-old-wm-'+str(os.getpid())
    openbox=shutil.which('openbox'); require(openbox,'openbox absent')
    # RAM is noexec on this host; the tiny executable shim stays in this worktree.
    with tempfile.TemporaryDirectory(dir=ROOT/'gates',prefix='.startup-') as shim:
        wrapper=Path(shim)/'openbox'
        wrapper.write_text('#!/bin/sh\nsleep 6\nexec '+shlex.quote(openbox)+' "$@"\n');wrapper.chmod(0o700)
        try:
            # Restore only the former WM cutoff in an otherwise current CLI.
            # Keep HERE at the repository root and launch this variant's own
            # supervisor; the manager still reaps it on the negative path.
            source=(ROOT/'vis').read_text()
            source=source.replace("wm_command='openbox & while :;", "wm_command='openbox & for _vis_ready in {1..100};")
            source=source.replace("+';; esac; sleep 0.05; done'", "+';; esac; sleep 0.05; done; exit 8'")
            source=source.replace("str(HERE/'vis'),'_serve'", "str(Path(__file__).resolve()),'_serve'")
            with tempfile.NamedTemporaryFile(mode='w',dir=ROOT,prefix='.vis-old-wm-',suffix='.py') as baseline:
                baseline.write(source);baseline.flush()
                env=os.environ.copy();env['PATH']=shim+':'+env['PATH']
                before=subprocess.run([sys.executable,'-I',baseline.name,'start','--job',old,'--app','gedit --standalone','--size','800x600','--timeout','20'],env=env,capture_output=True,text=True,timeout=40)
                require(before.returncode==8,(before.returncode,before.stdout,before.stderr))
                require(not (BASE/old).exists(),'old WM cutoff did not clean up')
            vis('start','--job',slow,'--app','gedit --standalone','--size','800x600','--timeout','20',env={'PATH':shim+':'+os.environ['PATH']})
            vis('check','--job',slow,'--window-title','gedit')
            vis('stop','--job',slow)
            failure=vis('start','--job',dead,'--app','echo VIS_FIX4_REAL_EXIT >&2; exit 23','--timeout','15',rc=8)
            require('VIS_FIX4_REAL_EXIT' in failure['reason'] and '23' in failure['reason'],failure)
            require(not (BASE/dead).exists(),'dead target job survived')
            note('Startup contrasts: former WM cutoff rejects delayed openbox; current private gedit starts; real exit remains E_TARGET with exit status and app log [MEASURED].')
        finally:
            cleanup(slow);cleanup(dead);cleanup(old)

if __name__=='__main__': main()

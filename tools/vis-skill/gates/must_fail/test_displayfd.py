#!/usr/bin/env python3
"""Force the X server's displayfd line to arrive in separate writes."""
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import ROOT, vis, require, note, cleanup, skip_started_jobs

def main():
    skip_started_jobs()
    job='vis-fix4-displayfd-'+str(os.getpid());real=shutil.which('Xvfb');require(real,'Xvfb absent')
    with tempfile.TemporaryDirectory(dir=ROOT/'gates',prefix='.displayfd-') as tmp:
        wrapper=Path(tmp)/'Xvfb'
        wrapper.write_text('''#!/usr/bin/python3
import os,subprocess,sys,time
args=sys.argv[1:];pos=args.index('-displayfd')+1;dest=int(args[pos]);rd,wr=os.pipe();args[pos]=str(wr)
child=subprocess.Popen([REAL,*args],pass_fds=(wr,));os.close(wr)
try:
    line=b''
    while not line.endswith(b'\\n'):
        block=os.read(rd,32)
        if not block: raise RuntimeError('real Xvfb displayfd closed')
        line+=block
    os.close(rd)
    os.write(dest,line[:-1]);time.sleep(.5);os.write(dest,b'\\n');os.close(dest)
    sys.exit(child.wait())
finally:
    if child.poll() is None: child.terminate();child.wait()
'''.replace('REAL',repr(real)))
        wrapper.chmod(0o700)
        try:
            vis('start','--job',job,'--app','gedit --standalone','--size','800x600',env={'PATH':tmp+':'+os.environ['PATH']})
            time.sleep(.7)
            vis('check','--job',job,'--window-title','gedit')
            vis('stop','--job',job)
            note('Split displayfd digits/newline: PASS; private Xvfb and gedit survive readiness and clean stop [MEASURED].')
        finally: cleanup(job)

if __name__=='__main__': main()

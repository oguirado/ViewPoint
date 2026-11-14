#!/usr/bin/env python3
"""A real adopted Tk loop must answer; freezing after input must never pass."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import BASE,ROOT,execute,note,require,vis

job='vis-fix3-adopt-'+str(os.getpid())
work=Path(tempfile.mkdtemp(prefix='vis-fix3-loop-',dir='/dev/shm'))
server=app=None
try:
    display=None
    for number in range(2000,65536):
        if not Path('/tmp/.X11-unix/X'+str(number)).exists() and not Path('/tmp/.X'+str(number)+'-lock').exists():
            display=':'+str(number); break
    require(display is not None,'no private display available')
    server=subprocess.Popen(['Xvfb',display,'-screen','0','800x600x24','-nolisten','tcp','-noreset'],
                            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    env=os.environ.copy()
    for key,sub in [('HOME','home'),('XDG_CONFIG_HOME','config'),('XDG_CACHE_HOME','cache'),
                    ('XDG_DATA_HOME','data'),('XDG_RUNTIME_DIR','runtime'),('TMPDIR','tmp')]:
        (work/sub).mkdir(mode=0o700); env[key]=str(work/sub)
    for key in ('XAUTHORITY','WAYLAND_DISPLAY','DBUS_SESSION_BUS_ADDRESS','SESSION_MANAGER'):
        env.pop(key,None)
    env['DISPLAY']=display
    deadline=time.monotonic()+5
    while not Path('/tmp/.X11-unix/X'+display[1:]).exists():
        require(server.poll() is None and time.monotonic()<deadline,'private Xvfb failed to start')
        time.sleep(.05)
    fixture=work/'target.py'; counter=work/'counter'; freeze=work/'freeze'
    fixture.write_text('''import pathlib,sys,time,tkinter as tk
counter=pathlib.Path(sys.argv[1]); freeze=pathlib.Path(sys.argv[2]); count=0
root=tk.Tk(); root.title('VIS-FIX3 adopted'); root.geometry('800x600+0+0')
root.configure(background='white')
def click(event):
 global count
 count+=1; counter.write_text(str(count)); root.configure(background='black' if count%2 else 'white')
 root.update_idletasks()
 if freeze.exists():
  while True: time.sleep(.1)
root.bind('<ButtonPress>',click); counter.write_text('0'); root.mainloop()
''')
    app=subprocess.Popen([sys.executable,'-I',str(fixture),str(counter),str(freeze)],env=env,
                         stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    deadline=time.monotonic()+5
    while not counter.exists():
        require(app.poll() is None and time.monotonic()<deadline,'Tk fixture failed to start')
        time.sleep(.05)
    vis('adopt','--job',job,'--display',display,'--xvfb-pid',str(server.pid))
    vis('act','--job',job,'--step','responsive','--xy','400,300','--expect-change','any')
    require(counter.read_text()=='1','adopted action did not execute')
    freeze.touch()
    result=execute([ROOT/'vis','act','--job',job,'--step','frozen','--xy','400,300','--expect-change','any'],expected=None)
    require(result['rc']==8,'a demonstrated adopted Tk timeout must be E_TARGET')
    require(counter.read_text()=='2','freeze was not triggered')
    vis('stop','--job',job)
    require(server.poll() is None and app.poll() is None,'stopping adopted job killed external processes')
    note('VIS-FIX3 adopted Tk: live action rc 0; frozen loop rc 8; adopted processes survive stop [MEASURED].')
finally:
    if (BASE/job).exists(): vis('stop','--job',job)
    for process in (app,server):
        if process is not None:
            if process.poll() is None: process.terminate()
            try: process.wait(timeout=3)
            except subprocess.TimeoutExpired: process.kill(); process.wait()
    shutil.rmtree(work)

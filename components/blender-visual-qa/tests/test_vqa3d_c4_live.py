"""Explicit owned GUI death exercise; unittest discovery starts no processes."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import threading
import time

from vqa3d.adapters.blender_live import BlenderLiveAdapter


def run():
    logs=Path('/home/admin/Documentos/publish/vqa3d-campaign/live')
    component=Path(__file__).resolve().parents[1]
    processes=[]
    with tempfile.TemporaryDirectory(prefix='vqa3d-c4-gui-') as tmp:
        root=Path(tmp)
        display=next(n for n in range(120,65536) if not Path('/tmp/.X11-unix/X'+str(n)).exists() and not Path('/tmp/.X'+str(n)+'-lock').exists())
        env={'PATH':'/usr/bin:/bin','HOME':tmp,'LANG':'C.UTF-8','DISPLAY':':'+str(display),
             'VQA3D_LIVE_DIR':tmp,'LIBGL_ALWAYS_SOFTWARE':'1','OMP_NUM_THREADS':'1'}
        streams=[]
        try:
            for label,argv in [('xvfb',['/usr/bin/Xvfb',env['DISPLAY'],'-screen','0','1024x768x24','-nolisten','tcp']),
                ('gui',['taskset','-c','0-15','nice','-n','10','/usr/bin/blender','--factory-startup','-noaudio',
                        str(component/'examples/cube3.blend'),'--python',str(component/'vqa3d/adapters/blender_live_bridge.py')])]:
                stream=(logs/('C4_0230_'+label+'.log')).open('w');streams.append(stream)
                proc=subprocess.Popen(argv,env=env,stdout=stream,stderr=subprocess.STDOUT);processes.append(proc)
                if label=='xvfb':
                    deadline=time.monotonic()+10
                    while not Path('/tmp/.X11-unix/X'+str(display)).exists():
                        assert proc.poll() is None and time.monotonic()<deadline
                        time.sleep(.05)
            gui=processes[-1];deadline=time.monotonic()+60
            while not (root/'bridge.json').exists():
                assert gui.poll() is None and time.monotonic()<deadline,'GUI startup failed'
                time.sleep(.05)
            adapter=BlenderLiveAdapter(root,timeout=5,frame_timeout=5)
            probe=adapter.probe();print(json.dumps({'probe':probe,'owned_gui_pid':gui.pid}),flush=True)
            assert probe['available'],probe
            script=root/'blocking.py';script.write_text('import time\ntime.sleep(30)\n')
            killer=threading.Timer(.3,lambda:os.kill(gui.pid,signal.SIGKILL));killer.start()
            started=time.monotonic()
            try:
                adapter.act({'op':'run_script','path':str(script)})
                raise AssertionError('dead GUI action passed')
            except RuntimeError as exc:
                elapsed=round((time.monotonic()-started)*1000)
                print(json.dumps({'status':'ERROR','reason':str(exc),'wall_ms':elapsed}),flush=True)
                assert str(exc)=='live_target_dead' and elapsed<2000
            finally:
                killer.join();adapter.close()
        finally:
            for proc in reversed(processes):
                if proc.poll() is None:
                    proc.terminate()
                    try:proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
                print(json.dumps({'pid':proc.pid,'rc':proc.returncode,'reaped':True}),flush=True)
            for stream in streams:stream.close()


if __name__=='__main__':run()

"""An owned Tk target launches one controlled hog on a test trigger."""
import json
from pathlib import Path
import subprocess
import sys
import tkinter as tk
root=tk.Tk();root.title('VIS-B capped target');root.geometry('500x350')
trigger=Path(sys.argv[1]);record=Path(sys.argv[2]);launched=False
label=tk.Label(root,text='Capped target');label.pack()
def poll():
    global launched
    if trigger.exists() and not launched:
        launched=True
        p=subprocess.Popen([sys.executable,'-I','-c','import time; b=bytearray(256*1024*1024); time.sleep(20)'])
        record.write_text(json.dumps({'pid':p.pid}))
        def reap():
            if p.poll() is None:root.after(100,reap)
        root.after(100,reap)
    root.after(100,poll)
root.after(100,poll);root.mainloop()

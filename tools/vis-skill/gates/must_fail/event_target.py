"""Own test target: every input event has an observable text-only counter."""
import json
from pathlib import Path
import sys
import tkinter as tk
out=Path(sys.argv[1]); n=0
root=tk.Tk();root.title('VIS-B event fixture'); root.geometry('600x400')
label=tk.Label(root,text='No events',font=('Sans',28));label.pack(pady=80)
def event(e):
    global n
    n+=1;out.write_text(json.dumps({'events':n,'pid':__import__('os').getpid()}));label.config(text='Events '+str(n))
root.bind_all('<KeyPress>',event);root.bind_all('<ButtonPress>',event)
def freeze():
    # Block the Tk event loop without signalling any process.
    if Path(str(out)+'.freeze').exists():
        import time
        Path(str(out)+'.frozen').touch()
        while True: time.sleep(.1)
    root.after(100,freeze)
out.write_text(json.dumps({'events':0,'pid':__import__('os').getpid()}));root.after(100,root.focus_force);root.after(100,freeze);root.mainloop()

#!/usr/bin/env python3
"""Uncooperative client wrapper; it signals only its own Popen child."""
from pathlib import Path
import signal
import subprocess
import sys
import time
counter=Path(sys.argv[1])
# Exercise supervisor escalation without signalling or stopping an ancestor.
signal.signal(signal.SIGTERM,signal.SIG_IGN)
child=subprocess.Popen([sys.executable,str(Path(__file__).with_name('event_target.py')),str(counter)])
try:
    while child.poll() is None:
        if Path(str(counter)+'.freeze-session').exists():
            child.send_signal(signal.SIGSTOP)
            Path(str(counter)+'.frozen').touch()
            break
        time.sleep(.02)
    child.wait()
finally:
    if child.poll() is None:
        child.send_signal(signal.SIGCONT);child.terminate()
        try: child.wait(timeout=2)
        except subprocess.TimeoutExpired: child.kill();child.wait()

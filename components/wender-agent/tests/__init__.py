# Process-signal guard for every test module (incident 2026-10-25: a MagicMock pid coerced to 1 made
# os.killpg(1, SIGKILL) = kill(-1) kill the whole user session). Refuse any signal to pid/pgid <= 1 or to our own group.
import os
import sys


def _signal_guard(event, args):
    if event in ('os.kill', 'os.killpg') and args:
        target = args[0]
        if type(target) is not int or target <= 1 or (event == 'os.killpg' and target == os.getpgid(0)):
            raise PermissionError('test signal guard: refused %s%r' % (event, tuple(args)))


sys.addaudithook(_signal_guard)

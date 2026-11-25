#!/usr/bin/python3 -I
"""Run the real reaper and deliver its ownership evidence after the child exits."""

from contextlib import redirect_stdout
from io import StringIO
import sys

assert sys.argv[1:6] == ["-I", "-S", "-B", "-u", "-c"]
source = sys.argv[6]
sys.argv = ["-c", *sys.argv[7:]]
evidence = StringIO()
try:
    with redirect_stdout(evidence):
        exec(compile(source, "<supervisor reaper>", "exec"), {"__name__": "__main__"})
finally:
    sys.stdout.write(evidence.getvalue())
    sys.stdout.flush()

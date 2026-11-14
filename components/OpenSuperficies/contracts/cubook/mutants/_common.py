"""Shim harness: a mutant wraps the real binary (CUBOOK_REAL_BIN dir) and corrupts one behaviour.
Invoked as: python3 -B mNN_*.py <cli> [args...] with the original stdin/env."""
import os
import subprocess
import sys


def main(hook):
    cli, argv = sys.argv[1], sys.argv[2:]
    stdin = sys.stdin.buffer.read()
    env = dict(os.environ)

    def run(a=None, e=None, s=None):
        p = subprocess.run([os.path.join(os.environ["CUBOOK_REAL_BIN"], "srf-cu-" + cli)] + (argv if a is None else a),
                           input=stdin if s is None else s, env=env if e is None else e, capture_output=True, timeout=60)
        return p.returncode, p.stdout, p.stderr

    rc, out, err = hook(cli, argv, env, run)
    sys.stdout.buffer.write(out)
    sys.stderr.buffer.write(err)
    sys.exit(rc)

"""Shared helpers for the P1 gui_synth tests (lane T). No tests here."""
import hashlib
import os

SMALL = dict(w=320, h=180, fps=60, n=16)


def walk_floats(o, path="scene"):
    """Yield the path of every float found in a JSON-able structure."""
    if isinstance(o, float):
        yield path
    elif isinstance(o, dict):
        for k, v in o.items():
            yield from walk_floats(v, "%s.%s" % (path, k))
    elif isinstance(o, (list, tuple)):
        for i, v in enumerate(o):
            yield from walk_floats(v, "%s[%d]" % (path, i))


def sha_list(scene):
    from eval.gui_synth import gen
    return [hashlib.sha256(a.tobytes()).hexdigest() for _, a in gen.render(scene)]


def refusal_code(exc):
    return getattr(exc, "code", None) or str(exc)

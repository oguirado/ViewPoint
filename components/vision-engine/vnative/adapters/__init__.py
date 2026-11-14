# @module {"id":"va.adapters","layer":4,"owner":"ARCH","deps":[],"book":"HB-VC-a0-arch","stage":"ADAPT"}
"""Unsealed reference-driver port for adapter orchestration and native kernel dispatch."""
from vnative.adapters import kernels
from vnative.run import run


# @anchor:va.adapters.analyze
def analyze(src, params=None, hints=None, out_dir=None, workers=1, cpus=None):
    """Analyze through the Python reference backend regardless of environment."""
    return run(src, params=params, hints=hints, out_dir=out_dir,
               workers=workers, cpus=cpus, mode="python")

"""D18 INPUT entry points independent of the legacy gate toolchain identity."""
from tools.input.evidence import qualify_input
from tools.input.matte import inspect_matte, prepare_matte

__all__ = ["inspect_matte", "prepare_matte", "qualify_input"]

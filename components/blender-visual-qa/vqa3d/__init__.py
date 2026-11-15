"""Obligation-specific visual QA with coherent observations and retained evidence."""
from .contract import AdapterNotImplemented, EnvAdapter, Observation
from .check import verify_step

__all__ = ["AdapterNotImplemented", "EnvAdapter", "Observation", "verify_step"]

"""Bounded, owner-scoped vision sessions over persistent VServe workers."""

from .pool import PerceptionPool, PoolError

__all__ = ["PerceptionPool", "PoolError"]

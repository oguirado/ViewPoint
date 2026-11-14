"""Utility helpers for the toy fixture."""
LIMIT = 3


def u1(x):
    """Return the input doubled."""
    return x * 2


def u2(x):
    """Return the doubled input plus the limit."""
    return u1(x) + LIMIT

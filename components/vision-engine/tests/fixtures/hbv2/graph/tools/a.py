"""Entry points of the toy fixture: pipelines, a child class and a mutual recursion."""
import util as U
from util import u1 as one
from base import Base


def g1(x):
    """Double a number through the util helper."""
    return one(x)


def f1(x):
    """Pipeline entry point that doubles then shifts a number."""
    return g1(x) + 1


def f3(x):
    """Add the limit to a doubled number via the module alias."""
    return U.u2(x)


class Child(Base):
    """Child class that extends the base helpers."""

    def own(self):
        return 5

    def run(self):
        """Run using inherited and own helpers."""
        return self.helper() + self.own()


def dyn(obj, name):
    """Call an attribute chosen at run time."""
    return getattr(obj, name)()


def ping(n):
    """Count down by mutual recursion with pong."""
    return pong(n - 1) if n else 0


def pong(n):
    return ping(n)

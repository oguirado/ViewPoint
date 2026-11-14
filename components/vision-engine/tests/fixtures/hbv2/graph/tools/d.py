"""Third layer: names arrive through a star import."""
from c import *


def d1(x):
    """Double through an alias inherited from the star import."""
    return U.u1(x)


def d2(x):
    """Call the second layer through the star import."""
    return c1(x)

"""Second layer of the toy fixture."""
import util as U
from a import f1


def c1(x):
    """Combine the util shifting with the pipeline entry point."""
    return U.u2(x) + f1(x)

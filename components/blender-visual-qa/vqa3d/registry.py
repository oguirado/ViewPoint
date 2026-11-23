"""Explicit adapter registry; optional runtimes are imported only on use."""
from .adapters.autocad import AutoCADAdapter
from .adapters.blender import BlenderAdapter
from .adapters.blender_live import BlenderLiveAdapter
from .adapters.isaac_sim import IsaacSimAdapter
from .adapters.mujoco_sim import MuJoCoSimulationAdapter
from .adapters.omniverse import OmniverseAdapter

ADAPTERS = {"blender": BlenderAdapter, "mujoco": MuJoCoSimulationAdapter,
            "autocad": AutoCADAdapter, "omniverse": OmniverseAdapter, "isaac_sim": IsaacSimAdapter,
            "blender_live": BlenderLiveAdapter}


def create_adapter(identifier, **kwargs):
    if identifier not in ADAPTERS:
        raise ValueError("unknown_adapter:" + str(identifier))
    return ADAPTERS[identifier](**kwargs)

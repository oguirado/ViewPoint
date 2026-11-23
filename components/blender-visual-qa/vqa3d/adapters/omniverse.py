"""NVIDIA Omniverse integration is not implemented."""
from .stub import StubAdapter


class OmniverseAdapter(StubAdapter):
    id = "omniverse"

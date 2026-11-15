"""Typed unavailable integrations; no synthetic observations."""
from ..contract import AdapterNotImplemented, EnvAdapter


class StubAdapter(EnvAdapter):
    status = "not_implemented"

    def probe(self):
        return {"id": self.id, "version": self.version, "status": self.status,
                "available": False, "reason": "adapter_not_implemented",
                "capabilities": dict(self.capabilities)}

    def _refuse(self, *args, **kwargs):
        raise AdapterNotImplemented("adapter_not_implemented:" + self.id)

    capture = _refuse
    scene_state = _refuse
    act = _refuse
    close = _refuse

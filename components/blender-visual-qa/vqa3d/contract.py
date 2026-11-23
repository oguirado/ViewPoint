"""Environment boundary and validated, coherent observation identities."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
import hashlib

from blender_visual_qa.drive.png import decode
from .canon import integer, sha256_canonical, state_copy


class AdapterNotImplemented(RuntimeError):
    """A declared integration has no implementation or evidence."""


@dataclass(frozen=True)
class Observation:
    obs_id: str
    frame_png: bytes | None
    frame_sha256: str | None
    width: int
    height: int
    state: dict
    state_sha256: str
    capture_available: bool
    notes: tuple = ()
    camera_source: str = "none"
    camera_name: str = ""

    @classmethod
    def create(cls, adapter, tag, state, frame_png=None, notes=(), camera_source="none", camera_name=""):
        state = state_copy(state)
        if type(tag) is not str:
            raise ValueError("tag_must_be_string")
        width, height, frame_sha = 0, 0, None
        if frame_png is not None:
            if type(frame_png) is not bytes:
                raise ValueError("frame_must_be_bytes")
            width, height, rgb = decode(frame_png)
            frame_sha = hashlib.sha256(rgb).hexdigest()
        state_sha = sha256_canonical(state)
        identity = {"adapter": adapter, "tag": tag, "frame_sha256": frame_sha,
                    "state_sha256": state_sha}
        if camera_source not in ("scene", "fallback", "none", "unknown") or type(camera_name) is not str:
            raise ValueError("invalid_camera_provenance")
        if (camera_source == "none") != (camera_name == ""):
            raise ValueError("incoherent_camera_provenance")
        # Legacy no-camera observations keep their identities; new camera claims bind them.
        if camera_source != "none":
            identity.update(camera_source=camera_source, camera_name=camera_name)
        if not all(type(note) is str for note in notes):
            raise ValueError("notes_must_be_strings")
        return cls(sha256_canonical(identity), frame_png, frame_sha, width, height,
                   state, state_sha, frame_png is not None, tuple(notes), camera_source, camera_name)

    def validate(self, adapter, tag):
        integer(self.width)
        integer(self.height)
        if type(self.capture_available) is not bool:
            raise ValueError("capture_available_must_be_boolean")
        expected = Observation.create(adapter, tag, self.state, self.frame_png, self.notes,
                                      self.camera_source, self.camera_name)
        if self != expected:
            raise ValueError("observation_integrity_mismatch")
        return self


class EnvAdapter(ABC):
    id = ""
    version = "1"
    status = "not_implemented"
    capabilities = {"capture_available": False, "scene_state": False, "act": False}

    @abstractmethod
    def probe(self, render=False) -> dict:
        """Declare installation and capabilities; availability is never inferred."""

    @abstractmethod
    def capture(self, tag) -> Observation:
        """Capture pixels and state from one stable environment state."""

    @abstractmethod
    def scene_state(self) -> dict:
        """Return canonical state with integer numeric values."""

    @abstractmethod
    def act(self, action: dict) -> dict:
        """Execute one action; success alone does not satisfy an obligation."""

    @abstractmethod
    def close(self):
        """Release only resources owned by this adapter."""

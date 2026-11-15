# @module {"id":"va.base","layer":4,"stage":"ADAPT","owner":"ARCH","deps":[],"book":"HB-VC-a0-arch"}
"""@module va.base - Adapter protocol (SPEC_ARCH §5.1). Duck-typed; no engine code."""
from pathlib import Path
from typing import Iterator, Optional, Protocol


class Adapter(Protocol):  # @anchor:va.base.adapter
    id: str
    version: str

    def frames(self, run_dir: Path):
        """-> FrameSource (meta dict, iterable of frames with i,t_us,render_index,rgb,sha256, len)."""

    def hints(self, run_dir: Path) -> Optional[Iterator[dict]]:
        """vqa.region-hint/v1 rows or None."""

    def still_probe(self, run_dir: Path):
        """-> FrameSource of an unchanging scene (CORE calibration) or None."""

    def scope(self, run_dir: Path) -> dict:
        """{app, scene, res, build_seq}"""

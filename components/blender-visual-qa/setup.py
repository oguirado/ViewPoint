from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildPy(build_py):
    def run(self):
        super().run()
        if self.editable_mode:
            return

        source = Path(__file__).resolve().parent / "config"
        destination = Path(self.build_lib) / "blender_visual_qa/assets/_runtime_config"
        destination.mkdir(parents=True, exist_ok=True)
        for name in ("render_protocol_v1.json", "class_gate_matrix_v1.json", "vision.json"):
            (destination / name).write_bytes((source / name).read_bytes())


setup(cmdclass={"build_py": BuildPy})

# SPDX-License-Identifier: AGPL-3.0-only
"""Measure a real MuJoCo contact with a separately sampled frame track."""
import json
from pathlib import Path
import sys

import mujoco


def simulate(job, source, mesh_width_um):
    radius = round(mesh_width_um / 50_000) / 100
    centre = job["drop_um"] / 1_000_000 + radius
    model = mujoco.MjModel.from_xml_string(f"""
        <mujoco><option timestep="0.001" gravity="0 0 -9.81"/>
        <worldbody>
          <geom name="floor" type="plane" size="2 2 0.05"/>
          <body name="falling" pos="0 0 {centre:.6f}">
            <freejoint/><geom name="probe" type="sphere" size="{radius:.6f}" mass="1"/>
          </body>
        </worldbody></mujoco>""")
    data = mujoco.MjData(model)
    heights = [int(round(centre * 1_000_000))]
    contact = None
    for tick in range(1, 601):
        mujoco.mj_step(model, data)
        heights.append(round(data.qpos[2] * 1_000_000))
        if contact is None:
            for item in data.contact[:data.ncon]:
                if {int(item.geom1), int(item.geom2)} == {0, 1} and item.dist <= 0:
                    contact = tick * 1000
    if contact is None:
        raise RuntimeError("expected contact unobserved")
    return {"engine": f"mujoco-{mujoco.__version__}", "source_rig_sha256": source,
            "first_contact_us": contact, "source_mesh_width_um": mesh_width_um,
            "presentation": [{"frame": frame, "time_us": tick * 1000, "height_um": heights[tick]}
                             for frame, tick in zip(job["frames"], [0, 200, 500])]}


if __name__ == "__main__":
    job, result, source, width = sys.argv[1:]
    value = simulate(json.loads(Path(job).read_text()), source, int(width))
    Path(result).write_text(json.dumps(value, sort_keys=True) + "\n")

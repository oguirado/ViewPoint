"""MJCF synthesis from admitted, fixed-point records; never from visual triangles."""
from decimal import Decimal
from xml.etree import ElementTree as ET

from .contracts import validate_scene, validate_protocol, require


def numbers(values, exponent):
    return " ".join(format(Decimal(v).scaleb(exponent), "f") for v in values)


def write_mjcf(scene, protocol, *, timestep_us=None):
    validate_scene(scene)
    validate_protocol(protocol)
    dt = protocol["timestep_us"] if timestep_us is None else timestep_us
    require(type(dt) is int and dt in (protocol["timestep_us"], protocol["timestep_us"] // 2))
    root = ET.Element("mujoco", model=scene["asset_id"])
    ET.SubElement(root, "compiler", angle="radian", inertiafromgeom="false", autolimits="false")
    ET.SubElement(root, "option", gravity=numbers(protocol["gravity_um_s2"], -6),
                  timestep=numbers([dt], -6), integrator="Euler", solver="Newton",
                  iterations=str(protocol["solver_iterations"]),
                  tolerance=numbers([protocol["solver_tolerance_ppb"]], -9))
    asset, world = ET.SubElement(root, "asset"), ET.SubElement(root, "worldbody")
    for body in scene["bodies"]:
        node = ET.SubElement(world, "body", name=body["id"],
                             pos=numbers(body["position_um"], -6),
                             quat=numbers(body["quaternion_ppm"], -6))
        if body["dynamic"]:
            ET.SubElement(node, "freejoint", name="joint_"+body["id"])
            i = body["inertial"]
            ET.SubElement(node, "inertial", pos=numbers(i["com_um"], -6),
                          mass=numbers([i["mass_ug"]], -9),
                          fullinertia=numbers(i["tensor_ug_um2"], -21))
        for geom in body["colliders"]:
            shape, data, c = geom["shape"], geom["data"], protocol["contact"]
            attrs = dict(name=geom["id"], type=shape, pos=numbers(geom["position_um"], -6),
                         quat=numbers(geom["quaternion_ppm"], -6), density="0", contype="1",
                         conaffinity="1", condim="3", margin="0", gap="0",
                         friction=numbers(c["friction_ppm_um"], -6),
                         solref=numbers([c["timeconst_us"], c["dampratio_ppm"]], -6))
            if shape == "box":
                attrs["size"] = numbers(data["half_extents_um"], -6)
            elif shape == "sphere":
                attrs["size"] = numbers([data["radius_um"]], -6)
            elif shape == "cylinder":
                attrs["size"] = numbers([data["radius_um"], data["half_height_um"]], -6)
            elif shape == "plane":
                attrs["size"] = numbers(data["half_extents_um"] + [1000], -6)
            else:
                mesh = "mesh_" + geom["id"]
                ET.SubElement(asset, "mesh", name=mesh,
                              vertex=numbers([v for xyz in data["vertices_um"] for v in xyz], -6),
                              face=" ".join(str(i) for face in data["faces"] for i in face))
                attrs.update(type="mesh", mesh=mesh)
            ET.SubElement(node, "geom", **attrs)
    return ET.tostring(root, encoding="unicode") + "\n"

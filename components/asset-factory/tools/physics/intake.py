"""Measure raw visual intake without inventing its physical record."""
import json
from pathlib import Path
import struct

from asset_factory import core
from .contracts import Refusal, require


def inspect_visual(path):
    try:
        path = Path(path)
        require(path.is_file(), 3, "source_unavailable")
        size = path.stat().st_size
        require(20 <= size <= 256*2**20, 6, "visual_budget_exceeded")
        with path.open("rb") as stream:
            magic, version, length, json_length, chunk = struct.unpack("<4sIIII", stream.read(20))
            require(magic == b"glTF" and version == 2 and length == size and chunk == 0x4e4f534a)
            require(json_length <= 4*2**20 and 20+json_length <= size, 6, "visual_budget_exceeded")
            doc = json.loads(stream.read(json_length))
        meshes = doc.get("meshes", [])
        require(type(meshes) is list)
        primitives = sum(len(mesh["primitives"]) for mesh in meshes)
        return {"schema":"af.physics-intake/v1","rc":3,"status":"INCOMPLETE","verdict":"CANNOT_SEE",
                "reason_codes":["units_unavailable","mass_inertia_unavailable","collision_unavailable"],
                "source_sha256":core.sha256_file(path),"metrics":{"bytes":size,"meshes":len(meshes),
                "primitives":primitives,"physical_mass_ug":None,"inertia_ug_um2":None},
                "absence_reasons":{"physical_mass_ug":"visual_glb_is_not_a_mass_record",
                                   "inertia_ug_um2":"no_cad_or_measured_inertia_supplied"}}
    except Refusal as exc:
        rc, code = exc.rc, exc.code
    except (OSError, ValueError, TypeError, KeyError, struct.error):
        rc, code = 2, "visual_invalid"
    return {"schema":"af.physics-intake/v1","rc":rc,"status":core.STATUS[rc],"verdict":core.VERDICT[rc],
            "reason_codes":[code],"source_sha256":None,"metrics":{},"absence_reasons":{}}

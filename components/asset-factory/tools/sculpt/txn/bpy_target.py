"""Blender-only scratch-mesh target; imported by Blender, never by host stages.

An owned Mesh.copy checkpoint includes mesh custom data, not Blender's undo stack.
Restore swaps in a fresh copy and frees the abandoned owned datablock. Only local,
single-user object-mode meshes without shape keys or modifiers are admitted.
Callbacks may change this mesh's geometry only. Other objects, materials, scene
settings, animation and pixels are outside this target's rollback contract.
"""

from array import array
import struct
import sys

from asset_factory import core
from .api import Checkpoint, MeshState


def _bytes(values):
    if sys.byteorder != "little":
        values.byteswap()
    return values.tobytes()


class BlenderMeshTarget:
    def __init__(self, obj):
        import bpy
        self._bpy, self.obj = bpy, obj
        self.identity = "blender-mesh:" + str(obj.as_pointer())
        self._check()

    def _check(self):
        obj = self.obj
        if (obj.type != "MESH" or obj.mode != "OBJECT" or obj.library is not None
                or obj.data.library is not None or obj.data.users != 1
                or obj.data.shape_keys is not None or obj.modifiers
                or obj.data.is_editmode):
            raise core.ContractError("scratch_mesh_required")

    def read(self):
        self._check()
        mesh = self.obj.data
        coordinates = array("f", [0]) * (3 * len(mesh.vertices))
        mesh.vertices.foreach_get("co", coordinates)
        pieces = [struct.pack("<4Q", len(mesh.vertices), len(mesh.edges),
                              len(mesh.loops), len(mesh.polygons))]
        for collection, field, width in ((mesh.edges, "vertices", 2),
                                          (mesh.loops, "vertex_index", 1),
                                          (mesh.polygons, "loop_start", 1),
                                          (mesh.polygons, "loop_total", 1)):
            values = array("i", [0]) * (len(collection) * width)
            collection.foreach_get(field, values)
            pieces.append(_bytes(values))
        # A geometry candidate must not silently change object units/transforms.
        pieces.append(struct.pack("<16d", *(v for row in self.obj.matrix_world for v in row)))
        return MeshState(_bytes(coordinates), b"".join(pieces))

    def snapshot(self):
        state = self.read()
        backup = self.obj.data.copy()
        return Checkpoint(state, (backup, self.obj.matrix_world.copy()))

    def restore(self, checkpoint):
        backup, matrix = checkpoint.token
        replacement = backup.copy()
        abandoned = self.obj.data
        self.obj.data = replacement
        self.obj.matrix_world = matrix
        replacement.update()
        if abandoned.users == 0:
            self._bpy.data.meshes.remove(abandoned)

    def discard(self, checkpoint):
        backup, _matrix = checkpoint.token
        if backup.users != 0:
            raise core.ContractError("checkpoint_borrowed")
        self._bpy.data.meshes.remove(backup)

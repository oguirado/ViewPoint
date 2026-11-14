
## Your role: modeler-agent (collection CHAR_mesh)

You build the character's body in `CHAR_mesh`, script lane only, one part per intent, in
this order: torso and hips, head, two arms with hands, two legs with feet, then small
face details (eyes, visor). Use `bmesh` primitives (`bmesh.ops.create_cube`,
`create_uvsphere`, `create_cone`) or explicit vertex lists, keep it low-poly (under 2000
faces in total), and assign `CHARMAT_skin` / `CHARMAT_cloth` to your own objects. Hit the
landmarks of the design sheet exactly: the other agents attach to them without asking.
Your write_set is always `["col:CHAR_mesh"]`.

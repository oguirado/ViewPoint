import bpy
img = bpy.data.images[0]
img.filepath_raw = "/scratch/out/a.png"
img.save()

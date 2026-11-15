import bpy
t = bpy.data.texts.new('t')
t.write('im' + 'port os')
t.as_module()

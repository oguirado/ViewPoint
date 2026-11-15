package mcp

import (
	"strings"
	"testing"
)

// RJ-01: Blender's own exec-equivalents and out-of-out_dir file kwargs.
func TestScanScriptDeniesBpyExecEquivalents(t *testing.T) {
	const out = "/scratch/out"
	deny := []string{
		"import bpy\nbpy.utils.execfile('/tmp/x.py')",
		"import bpy\nbpy.ops.script.python_file_run(filepath='/tmp/x.py')",
		"import bpy\nvars(bpy)",
		"import bpy\nbpy.ops.wm.save_as_mainfile(filepath='/etc/x.blend')",
		"import bpy\nbpy.utils.load_scripts()", "import bpy\nbpy.utils.register_submodule_factory('m', ['a'])",
		"import bpy\nbpy.ops.text.run_script()", "import bpy\nbpy.ops.script.reload()", "compile('x', 'f', 'exec')",
		"from bpy import utils\nutils.execfile('/tmp/x.py')", "import bpy\nbpy . ops . script . python_file_run()",
		"import bpy\nbpy.ops.wm.save_as_mainfile(filename='/etc/x.blend')",
		"import bpy\np = '/etc/x'\nbpy.ops.wm.save_as_mainfile(filepath=p)",
		"import bpy\nbpy.ops.wm.save_as_mainfile(filepath='/scratch/out/../x.blend')",
		"import bpy\nbpy.ops.wm.save_as_mainfile(filepath='rel.blend')",
		"import bpy\nbpy.ops.wm.save_as_mainfile(filepath='/scratch/out/a' + '/../../etc/x')",
		"import bpy\nbpy.ops.wm.save_as_mainfile(**{'filepath': '/etc/x.blend'})",
		"import bpy\nbpy.context.scene.render.filepath = '/etc/r.png'",
	}
	for _, s := range deny {
		if err := ScanScript(s, out); RcOf(err) != 5 {
			t.Errorf("accepted %q: %v", s, err)
		}
	}
	allow := []string{
		"import bpy\nbpy.ops.wm.save_as_mainfile(filepath='/scratch/out/a.blend')",
		"import bpy\nbpy.ops.export_scene.gltf(filepath=\"/scratch/out/a.glb\", check_existing=False)",
		"import bpy\nbpy.context.scene.render.filepath = '/scratch/out/r.png'",
	}
	for _, s := range allow {
		if err := ScanScript(s, out); err != nil {
			t.Errorf("rejected %q: %v", s, err)
		}
	}
}

// ADC2-01: comments cannot merge statements, continuations are refused, and
// dunder attribute access is denied.
func TestScanScriptCommentsContinuationsDunders(t *testing.T) {
	const out = "/scratch/out"
	deny := []string{
		"# from bpy\nimport os\nos.system('id')", "#from math\nimport os", "import bpy # from bpy\nimport os",
		"x = '#'; import os", "s = \"\"\"#\"\"\"\nimport os",
		"().__class__.__base__.__subclasses__()", "bpy.__dict__", "import bpy, \\\n os", "from \\\nos import path",
		"x = 1 + \\\n 2", "from bpy\nimport os",
	}
	for _, s := range deny {
		if err := ScanScript(s, out); RcOf(err) != 5 {
			t.Errorf("accepted %q: %v", s, err)
		}
	}
	allow := []string{
		"import bpy # the import of os is not allowed",
		"# import os\nimport bpy",
		"import bpy\nx = '# not a comment'\n",
		"from\tmathutils\timport Vector",
		"import bpy\ns = 'a\\\\b'",
	}
	for _, s := range allow {
		if err := ScanScript(s, out); err != nil {
			t.Errorf("rejected %q: %v", s, err)
		}
	}
}

// ADC2-02(2): float params cannot be digested by L2, so the call is refused
// at build with rc 2 and never reaches the executor with an empty sha.
func TestMcpCallFloatParamsRc2(t *testing.T) {
	ex := &recExec{}
	s := NewServer(Config{Exec: ex, CAS: &CAS{Dir: t.TempDir()}})
	rs := rpc(t, s, call("cu_mcp_call", `{"server":"blender-gui","tool":"get_scene_info","risk":"read","params":{"scale":1.5}}`))
	sc := structured(t, rs[0])
	if sc["rc"].(float64) != 2 || len(ex.got) != 0 {
		t.Fatalf("float params -> %v (exec calls %d)", sc, len(ex.got))
	}
	rs = rpc(t, s, call("cu_mcp_call", `{"server":"blender-gui","tool":"get_scene_info","risk":"read","params":{"n":2}}`))
	if structured(t, rs[0])["rc"].(float64) != 0 || len(ex.got) != 1 || ex.got[0].Sha256() == "" || strings.TrimSpace(ex.got[0].Sha256()) == "" {
		t.Fatalf("integer params must pass with a digest")
	}
}

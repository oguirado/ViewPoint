package mcp

import (
	"context"
	"encoding/json"
	"os"
	"strings"
	"testing"
)

// The script tools are fakes-only (OS_CU_TEST=1) until the W1 AST check;
// the package tests drive fakes, TestScriptExecToolsUnwiredInProduction clears it.
func TestMain(m *testing.M) {
	os.Setenv("OS_CU_TEST", "1")
	os.Exit(m.Run())
}

// FJ-01 (judge's 6 inputs) + ADC3-01 (2 inputs).
func TestScanScriptDeniesNFKCAndTextExec(t *testing.T) {
	const out = "/scratch/out"
	deny := []string{
		"ｅｘｅｃ(chr(105)+'mport os')",
		"import bpy\nbpy.utils.ｅｘｅｃｆｉｌｅ('/tmp/x.py')",
		"import bpy\nbpy.data.texts.load('/tmp/x.py').as_module()",
		"import bpy\nt = bpy.data.texts.new('t')\nt.write('im' + 'port os')\nt.as_module()",
		"import bpy\nbpy.data.libraries.write('/etc/x.blend', set())",
		"import bpy\nbpy.ops.wm.open_mainfile(filepath='/scratch/out/a.blend', use_scripts=True)",
		"globals()['_'+'_builtins_'+'_']['ev'+'al']('1')",
		"import bpy\nbpy.ops.wm.save_as_mainfile(directory='/etc/', filename='/scratch/out/a.blend')",
	}
	for _, s := range deny {
		if err := ScanScript(s, out); RcOf(err) != 5 || !strings.Contains(err.Error(), "script_denied") {
			t.Errorf("accepted %q: %v", s, err)
		}
	}
	if err := ScanScript(deny[0], out); err == nil || !strings.Contains(err.Error(), "non-ascii") {
		t.Errorf("NFKC identifier: want script_denied: non-ascii, got %v", err)
	}
}

// Neighbours of the FJ-01 classes: each sealed name, '/'-literals anywhere,
// non-ASCII hidden in an f-string expression, dynamic builtins.
func TestScanScriptFX3Neighbours(t *testing.T) {
	const out = "/scratch/out"
	deny := []string{
		"import bpy\nbpy.data.libraries.load('/scratch/out/a.blend')", "import bpy\nbpy . data . texts . load('/scratch/out/a.py')",
		"import bpy\nbpy.ops.wm.read_homefile()", "import bpy\nbpy.ops.wm.append(filename='/scratch/out/a')",
		"import bpy\nbpy.ops.wm.link(filename='/scratch/out/a')", "import bpy\nx = 'use_scripts'",
		"import bpy\np = '/etc/passwd'", "import bpy\nbpy.data.images.load(\"/etc/x.png\")", "import bpy\nx = b'/etc/x'",
		"import bpy\np = '/scratch/out/\\x2e\\x2e/etc'", "import bpy\nx = f'{ｅｖａｌ(\"1\")}'", "import bpy\nlocals()",
		"breakpoint()", "input()", "help(bpy)", "import bpy\nbpy.ops.wm.save_as_mainfile(**{'directory': '/etc/'})",
		"import bpy\nx = 1 ＋ 2",
	}
	for _, s := range deny {
		if err := ScanScript(s, out); RcOf(err) != 5 {
			t.Errorf("accepted %q: %v", s, err)
		}
	}
	allow := []string{
		"import bpy\nbpy.ops.wm.save_as_mainfile(filepath='/scratch/out/a.blend')",
		"import bpy\nbpy.data.images.load('/scratch/out/a.png')",
		"import bpy\nbpy.ops.wm.save_as_mainfile(directory='/scratch/out/d', filename='/scratch/out/d/a.blend')",
		"import bpy\nname = 'Würfel'", "import bpy\nx = 'a/b'",
	}
	for _, s := range allow {
		if err := ScanScript(s, out); err != nil {
			t.Errorf("rejected %q: %v", s, err)
		}
	}
}

// Conductor ruling item 3: the real script tools stay unwired in production.
func TestScriptExecToolsUnwiredInProduction(t *testing.T) {
	cases := []struct{ server, tool, ref, params string }{
		{"blender-gui", "execute_blender_code", "plan:s1", ""},
		{"blender-production", "python_exec", "ref", ""},
		{"blender-gui", "workspace_switch", "", `{"workspace":"Layout"}`},
	}
	for _, env := range []string{"", "0", "1"} {
		t.Setenv("OS_CU_TEST", env)
		for _, c := range cases {
			d, fc, _ := newDisp(t, nil)
			d.Scripts = func(string) (string, bool) { return "import bpy\nbpy.ops.object.select_all(action='DESELECT')\n", true }
			a := mcpAction(c.server, c.tool)
			a.Risk, a.Args.ScriptRef = "write", c.ref
			if c.params != "" {
				a.Args.Params = json.RawMessage(c.params)
			}
			out, _ := d.Execute(context.Background(), a)
			if env != "1" {
				if out.Step.RC != 5 || out.Step.Stop == nil || *out.Step.Stop != "script_exec_unwired" || len(fc.Calls) != 0 {
					t.Errorf("env %q %s reached the server: %+v calls=%d", env, c.tool, out.Step, len(fc.Calls))
				}
			} else if len(fc.Calls) != 1 {
				t.Errorf("OS_CU_TEST=1 %s: fake not called: %+v", c.tool, out.Step)
			}
		}
	}
}

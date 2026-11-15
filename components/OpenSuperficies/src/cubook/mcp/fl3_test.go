package mcp

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	"opensuperficies.local/srf/src/cubook/action"
	"opensuperficies.local/srf/src/cubook/exec"
)

// FX1 FL3 behaviour tests (JW-01, JW-02, ADC-06, ADC-07, ADC-08). They use
// JSON-level access only, so they compile and fail by behaviour at 0c2147f.

func stepJSON(t *testing.T, o Outcome) []byte {
	t.Helper()
	b, err := json.Marshal(o.Step)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func l2Fakes(t *testing.T) (*exec.Executor, *exec.FakeActuator) {
	t.Helper()
	deny, err := exec.LoadDenyList("../../../testdata/cu/plans/act_deny.tsv")
	if err != nil {
		t.Fatal(err)
	}
	var fr []exec.Frame
	for i := 0; i < 40; i++ {
		fr = append(fr, exec.MkFrame(uint32(i+1), "00000000000000aa", strings.Repeat("p", i+1), 64, 64))
	}
	fa := &exec.FakeActuator{}
	return exec.New(exec.Config{Deny: deny}, fa, &exec.SeqObserver{Frames: fr}, exec.XYLocator{}, nil), fa
}

var sampleCalls = map[string]string{
	"cu_observe": `{}`, "cu_locate": `{"role":"button","name":"OK"}`, "cu_click": `{"x":1,"y":2,"button":1}`,
	"cu_type": `{"text_ref":"p:1","text_sha256":"` + strings.Repeat("e", 64) + `","len":3}`, "cu_key": `{"chord":"Return"}`,
	"cu_drag": `{"x":1,"y":2,"to_x":3,"to_y":4,"steps":4}`, "cu_scroll": `{"x":1,"y":2,"dx":0,"dy":-3}`, "cu_wait": `{"max_frames":10,"settle_k":3}`,
	"cu_assert":   `{"expect":[{"kind":"readback","readback":{"via":"v","query":"q","eq":"1"}}]}`,
	"cu_mcp_call": `{"server":"blender-gui","tool":"get_scene_info","risk":"read","params":{}}`,
}

func TestActionShaMatchesL2(t *testing.T) {
	s := NewServer(Config{Exec: &recExec{}})
	for n, args := range sampleCalls {
		a, err := s.build(n, json.RawMessage(args))
		if err != nil {
			t.Fatalf("%s: %v", n, err)
		}
		b, _ := json.Marshal(a)
		la, err := action.DecodeAction(b)
		if err != nil {
			t.Fatalf("%s: %v", n, err)
		}
		want, _ := la.SHA()
		if got := a.Sha256(); got != want {
			t.Fatalf("%s: mcp sha %s != L2 sha %s", n, got, want)
		}
	}
}

func TestEmittedStepsPassL2DecodeStep(t *testing.T) {
	ctx := context.Background()
	var outs []Outcome
	d, _, _ := newDisp(t, okVerifier{b: bp(true)})
	o, _ := d.Execute(ctx, mcpAction("blender-gui", "get_scene_info"))
	outs = append(outs, o)
	o, _ = d.Execute(ctx, mcpAction("blender-gui", "no_such_tool")) // refusal
	outs = append(outs, o)
	g := mcpAction("blender-gui", "x")
	g.Op, g.Risk, g.Args = "click", "write", Args{}
	g.Target = &Target{By: "xy", XY: &[2]int{1, 1}}
	g.Expect = []Expect{}
	o, _ = Unwired{}.Execute(ctx, g)
	outs = append(outs, o)
	o, _ = MockGUI{}.Execute(ctx, g)
	outs = append(outs, o)
	for i, o := range outs {
		b := stepJSON(t, o)
		st, err := action.DecodeStep(b)
		if err != nil {
			t.Fatalf("outcome %d rejected by L2 DecodeStep: %v\n%s", i, err, b)
		}
		if strings.HasPrefix(st.InputSHA, "e3b0c442") {
			t.Fatalf("outcome %d: input_sha256 is the empty-string hash", i)
		}
	}
}

func TestMockGUINeverEffectOK(t *testing.T) {
	g := mcpAction("blender-gui", "x")
	g.Op, g.Risk, g.Args = "click", "write", Args{}
	g.Target = &Target{By: "xy", XY: &[2]int{1, 1}}
	o, _ := MockGUI{}.Execute(context.Background(), g)
	if o.Step.Verdict == "EFFECT_OK" {
		t.Fatal("mock GUI without verifier must not report EFFECT_OK")
	}
}

func TestL2BridgeEndToEnd(t *testing.T) {
	ctx := context.Background()
	ex, fa := l2Fakes(t)
	d, fc, _ := newDisp(t, nil)
	d.GUI = L2Bridge{E: ex}
	a := mcpAction("blender-gui", "gizmo_drag")
	a.Fallback, a.Risk = "gui", "write"
	a.Args.Params = json.RawMessage(`{"object":"Cube","from":[10,20],"to":[50,60],"expect_eq":"1"}`)
	out, err := d.Execute(ctx, a)
	if err != nil {
		t.Fatal(err)
	}
	if len(fc.Calls) != 0 || out.Fallback != "gui" || len(out.FallbackSteps) == 0 {
		t.Fatalf("expected GUI fallback through L2: %+v", out)
	}
	if fa.Events != 1 || len(fa.Calls) < 1 || fa.Calls[0] != "drag:10,20>50,60,1,8" {
		t.Fatalf("L2 actuator did not receive the drag: %v", fa.Calls)
	}
	fb, _ := json.Marshal(out.FallbackSteps)
	var raw []json.RawMessage
	_ = json.Unmarshal(fb, &raw)
	for i, r := range raw {
		if _, err := action.DecodeStep(r); err != nil {
			t.Fatalf("fallback step %d rejected by L2: %v\n%s", i, err, r)
		}
	}
	// the bridge must hold actions to L2's strict decoder: button 9 is refused, zero events
	bad := mcpAction("blender-gui", "x")
	bad.Op, bad.Risk, bad.Args.Button = "click", "write", 9
	bad.Target = &Target{By: "xy", XY: &[2]int{1, 1}}
	ex2, fa2 := l2Fakes(t)
	if _, err := (L2Bridge{E: ex2}).Execute(ctx, bad); err == nil || fa2.Events != 0 {
		t.Fatalf("invalid action reached the actuator: err=%v events=%d", err, fa2.Events)
	}
	// and a valid click is actuated once, with the L2 action digest
	ok := bad
	ok.Args.Button = 1
	o2, err := (L2Bridge{E: ex2}).Execute(ctx, ok)
	if err != nil || fa2.Events != 1 {
		t.Fatalf("valid click: err=%v events=%d", err, fa2.Events)
	}
	var m map[string]any
	_ = json.Unmarshal(stepJSON(t, o2), &m)
	if m["action_sha256"] != ok.Sha256() || m["op"] != "click" {
		t.Fatalf("step does not carry the L2 digest: %v", m)
	}
}

func TestGuiFallbackRefusedWhenOpListed(t *testing.T) {
	d, fc, g := newDisp(t, nil)
	d.Reg.Servers["godot-beckett"].Tools["inspector_edit"] = ToolSpec{Risk: "write"}
	a := mcpAction("godot-beckett", "inspector_edit")
	a.Fallback, a.Risk = "gui", "write"
	a.Args.Params = json.RawMessage(`{"project_path":"/scratch/cap/p","property":"size","value_sha256":"` + strings.Repeat("c", 64) + `","value_len":3}`)
	out, _ := d.Execute(context.Background(), a)
	if len(fc.Calls) != 1 || len(g.got) != 0 || out.Fallback != "" || out.Step.RC != 5 {
		t.Fatalf("listed op must not fall back to GUI: %+v calls=%d gui=%d", out.Step, len(fc.Calls), len(g.got))
	}
}

func TestGodotMCPPathRefusesOZ(t *testing.T) {
	for _, pp := range []string{"/home/user/Documentos/OZ/secret", "/other/place", "/scratch/cap/../x"} {
		d, fc, _ := newDisp(t, okVerifier{b: bp(true)})
		a := mcpAction("godot-beckett", "get_scene_tree")
		a.Args.Params = json.RawMessage(`{"project_path":"` + pp + `"}`)
		out, _ := d.Execute(context.Background(), a)
		if out.Step.RC != 5 || len(fc.Calls) != 0 {
			t.Fatalf("%s: %+v calls=%d", pp, out.Step, len(fc.Calls))
		}
	}
	d, fc, _ := newDisp(t, okVerifier{b: bp(true)})
	a := mcpAction("godot-beckett", "get_scene_tree")
	a.Args.Params = json.RawMessage(`{"project_path":"/scratch/cap/p"}`)
	if out, _ := d.Execute(context.Background(), a); out.Step.RC != 0 || len(fc.Calls) != 1 {
		t.Fatalf("scratch project must pass: %+v", out.Step)
	}
	// write/irrev godot calls must carry a project_path
	d.ConfirmDestructive = true
	w := mcpAction("godot-beckett", "set_property")
	w.Risk = "write"
	fc.Calls = nil
	if out, _ := d.Execute(context.Background(), w); out.Step.RC != 5 || len(fc.Calls) != 0 {
		t.Fatalf("write without project_path: %+v", out.Step)
	}
}

func TestPythonExecScanned(t *testing.T) {
	for _, code := range []string{"import os\nos.system('x')", "import pathlib", "getattr(bpy,'x')"} {
		d, fc, _ := newDisp(t, nil)
		d.Scripts = func(string) (string, bool) { return code, true }
		a := mcpAction("blender-production", "python_exec")
		a.Risk, a.Args.ScriptRef = "write", "ref"
		out, _ := d.Execute(context.Background(), a)
		if out.Step.RC != 5 || len(fc.Calls) != 0 {
			t.Fatalf("%q reached the server: %+v calls=%d", code, out.Step, len(fc.Calls))
		}
	}
}

func TestScanScriptAllowlistAndOpenBypasses(t *testing.T) {
	const out = "/scratch/out"
	deny := []string{
		"import pathlib; pathlib.Path('/etc/x').write_text('a')", "import sys", "import io", "import builtins",
		"from os import path", "import bpy, os", "import math as m, shutil", "from . import x",
		"getattr(bpy, 'ops')", "setattr(bpy, 'x', 1)", "open('/etc/passwd')", "open('/scratch/out/a', 'r', mode='w')",
		"open('/scratch/out/a', mode='w')", "open('/etc/passwd', 'r')", "open('/scratch/out/../x', 'w')", "open(p, 'w')",
		"f = open\nf('/etc/x','w')", "open('/scratch/out/a', 'w', 1)", "open(('/etc/x'), 'w')",
	}
	for _, s := range deny {
		if err := ScanScript(s, out); RcOf(err) != 5 {
			t.Errorf("accepted %q: %v", s, err)
		}
	}
	allow := []string{"import bpy\nbpy.ops.mesh.primitive_cube_add()", "import math\nfrom mathutils import Vector\nx = math.pi",
		"import bpy, math", "with open('/scratch/out/a.txt', 'w') as f:\n    f.write('x')", "open(\"/scratch/out/a.txt\", \"r\")"}
	for _, s := range allow {
		if err := ScanScript(s, out); err != nil {
			t.Errorf("rejected %q: %v", s, err)
		}
	}
}

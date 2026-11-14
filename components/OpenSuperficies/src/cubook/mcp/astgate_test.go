package mcp

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

type fakeRunner struct {
	rc     int
	out    string
	err    error
	calls  int
	script string
	outDir string
}

func (f *fakeRunner) Run(_ context.Context, script, outDir string) (int, []byte, error) {
	f.calls++
	f.script, f.outDir = script, outDir
	return f.rc, []byte(f.out), f.err
}

const (
	astAllow = `{"code":"","reason":"","schema":"os.cu.ast/v1","verdict":"allow"}`
	astDeny  = `{"code":"ast_denied","reason":"attribute texts","schema":"os.cu.ast/v1","verdict":"deny"}`
)

func scriptAction() Action {
	a := mcpAction("blender-gui", "execute_blender_code")
	a.Risk, a.Args.ScriptRef = "write", "plan:s1"
	return a
}

func gatedDisp(t *testing.T, r *fakeRunner, inCapsule bool) (*Dispatcher, *FakeCaller) {
	t.Helper()
	d, fc, _ := newDisp(t, nil)
	d.Scripts = func(string) (string, bool) { return "import bpy\nbpy.ops.object.select_all(action='DESELECT')\n", true }
	d.OutDir = "/scratch/out"
	d.ASTGate = &ASTGate{Runner: r, InCapsule: func() bool { return inCapsule }}
	return d, fc
}

func TestAstGateRequiredBeforeScriptDispatch(t *testing.T) {
	t.Setenv("OS_CU_TEST", "")
	r := &fakeRunner{out: astDeny, rc: 5}
	d, fc := gatedDisp(t, r, true)
	out, _ := d.Execute(context.Background(), scriptAction())
	if r.calls != 1 || len(fc.Calls) != 0 || out.Step.RC != 5 {
		t.Fatalf("denied script reached the server: runner=%d calls=%d %+v", r.calls, len(fc.Calls), out.Step)
	}
	if !strings.Contains(r.script, "select_all") || r.outDir != "/scratch/out" {
		t.Errorf("runner got script %q outDir %q", r.script, r.outDir)
	}
	r2 := &fakeRunner{out: astAllow}
	d, fc = gatedDisp(t, r2, true)
	if out, _ = d.Execute(context.Background(), scriptAction()); r2.calls != 1 || len(fc.Calls) != 1 || out.Step.RC != 0 {
		t.Fatalf("allowed script not dispatched in capsule without OS_CU_TEST: runner=%d calls=%d %+v", r2.calls, len(fc.Calls), out.Step)
	}
	// a lint-denied script never reaches the AST gate (ScanScript runs first)
	r3 := &fakeRunner{out: astAllow}
	d, fc = gatedDisp(t, r3, true)
	d.Scripts = func(string) (string, bool) { return "import os\n", true }
	if out, _ = d.Execute(context.Background(), scriptAction()); r3.calls != 0 || len(fc.Calls) != 0 || out.Step.RC != 5 {
		t.Fatalf("lint-denied script: runner=%d calls=%d", r3.calls, len(fc.Calls))
	}
	// fake runner may not bypass: rc 0 with a non-allow verdict, garbage, rc 1/2, runner error all refuse
	for _, rr := range []*fakeRunner{{out: astDeny}, {out: "garbage"}, {out: ""}, {rc: 1, out: astAllow}, {rc: 2, out: astAllow}, {err: os.ErrNotExist}} {
		d, fc = gatedDisp(t, rr, true)
		if out, _ = d.Execute(context.Background(), scriptAction()); len(fc.Calls) != 0 || out.Step.RC != 5 {
			t.Errorf("runner %+v reached the server: %+v", *rr, out.Step)
		}
	}
}

func TestAstGateDenyIsRc5ScriptDenied(t *testing.T) {
	t.Setenv("OS_CU_TEST", "1")
	d, fc := gatedDisp(t, &fakeRunner{out: astDeny, rc: 5}, true)
	out, _ := d.Execute(context.Background(), scriptAction())
	if out.Step.RC != 5 || out.Step.Stop == nil || *out.Step.Stop != "script_denied" || len(fc.Calls) != 0 {
		t.Fatalf("want rc 5 script_denied, got %+v", out.Step)
	}
	err := (&ASTGate{Runner: &fakeRunner{out: astDeny, rc: 5}, InCapsule: func() bool { return true }}).Check(context.Background(), "x", "/o")
	if RcOf(err) != 5 || !strings.Contains(err.Error(), "ast_denied: attribute texts") {
		t.Errorf("deny error: %v", err)
	}
}

func TestAstGateRunsInCapsuleOnly(t *testing.T) {
	for _, env := range []string{"", "1"} {
		t.Setenv("OS_CU_TEST", env)
		r := &fakeRunner{out: astAllow}
		d, fc := gatedDisp(t, r, false)
		out, _ := d.Execute(context.Background(), scriptAction())
		if r.calls != 0 || len(fc.Calls) != 0 || out.Step.RC != 5 || out.Step.Stop == nil || *out.Step.Stop != "script_exec_unwired" {
			t.Errorf("env %q outside capsule: runner=%d calls=%d %+v", env, r.calls, len(fc.Calls), out.Step)
		}
	}
	for _, g := range []*ASTGate{nil, {}, {Runner: &fakeRunner{out: astAllow}}} {
		if err := g.Check(context.Background(), "import bpy\n", ""); RcOf(err) != 5 || !strings.Contains(err.Error(), "script_exec_unwired") {
			t.Errorf("gate %+v: %v", g, err)
		}
	}
}

func TestScriptExecUnwiredOutsideCapsule(t *testing.T) {
	t.Setenv("OS_CU_TEST", "")
	d, fc, _ := newDisp(t, nil)
	d.Scripts = func(string) (string, bool) { return "import bpy\nbpy.ops.object.select_all(action='DESELECT')\n", true }
	out, _ := d.Execute(context.Background(), scriptAction())
	if out.Step.RC != 5 || out.Step.Stop == nil || *out.Step.Stop != "script_exec_unwired" || len(fc.Calls) != 0 {
		t.Fatalf("nil ASTGate without OS_CU_TEST must stay unwired: %+v", out.Step)
	}
	t.Setenv("OS_CU_TEST", "1")
	d, fc, _ = newDisp(t, nil)
	d.Scripts = func(string) (string, bool) { return "import bpy\nbpy.ops.object.select_all(action='DESELECT')\n", true }
	if out, _ = d.Execute(context.Background(), scriptAction()); len(fc.Calls) != 1 {
		t.Fatalf("OS_CU_TEST=1 fake path broken: %+v", out.Step)
	}
}

// The real checker through ExecRunner agrees with the corpus (skipped without python3).
func TestAstGateRealCheckerCorpus(t *testing.T) {
	if _, err := exec.LookPath("python3"); err != nil {
		t.Skip("no python3")
	}
	root := "../../../"
	g := &ASTGate{Runner: ExecRunner{Script: root + "capsule/astcheck/astcheck.py"}, InCapsule: func() bool { return true }}
	for dir, want := range map[string]bool{"deny": false, "allow": true} {
		files, _ := filepath.Glob(root + "testdata/cu/ast/" + dir + "/*.py")
		if len(files) == 0 {
			t.Fatalf("no %s corpus", dir)
		}
		for _, f := range files {
			b, _ := os.ReadFile(f)
			err := g.Check(context.Background(), string(b), "/scratch/out")
			if (err == nil) != want {
				t.Errorf("%s: err=%v want allow=%v", f, err, want)
			}
		}
	}
}

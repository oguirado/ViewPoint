package main

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"os"
	"strings"
	"testing"

	"opensuperficies.local/srf/src/cubook/action"
	"opensuperficies.local/srf/src/cubook/capture"
	"opensuperficies.local/srf/src/cubook/exec"
	"opensuperficies.local/srf/src/cubook/mcp"
	"opensuperficies.local/srf/src/cubook/prodgate"
)

const td = "../../testdata/cu/mcp/"

const deny = "../../testdata/cu/plans/act_deny.tsv"

func TestNoRc1(t *testing.T) {
	t.Setenv("OS_CU_TEST", "1")
	cas := t.TempDir()
	cases := [][]string{
		nil,
		{"--bogus"},
		{"--registry", "/nonexistent/r.json", "--cas-dir", cas},
		{"--registry", td + "registry.json"},
		{"--registry", td + "registry.json", "--cas-dir", cas, "--fake", td + "nope.json"},
		{"--registry", td + "registry.json", "--cas-dir", cas, "--fake", td + "fake_blender_gui.json", "--fake", td + "fake_beckett.json", "--mock-gui"},
	}
	for _, args := range cases {
		var o, e bytes.Buffer
		rc := run(args, strings.NewReader(`{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}`+"\n"), &o, &e)
		if rc == 1 || rc < 0 || rc > 7 {
			t.Fatalf("%v -> rc %d", args, rc)
		}
	}
	var o, e bytes.Buffer
	if rc := run([]string{"--registry", td + "registry.json", "--cas-dir", cas, "--fake", td + "fake_blender_gui.json", "--mock-gui"},
		strings.NewReader(`{"jsonrpc":"2.0","id":1,"method":"tools/list"}`+"\n"), &o, &e); rc != 0 || !strings.Contains(o.String(), "cu_mcp_call") {
		t.Fatalf("rc %d out %s err %s", rc, o.String(), e.String())
	}
	b, _ := os.ReadFile("main.go")
	for _, bad := range []string{"os.Exit(1)", "log.Fatal", "flag.ExitOnError"} {
		if bytes.Contains(b, []byte(bad)) {
			t.Fatalf("main.go contains %s", bad)
		}
	}
}

// callStep runs one tools/call through run() and returns the decoded envelope.
func callStep(t *testing.T, extra []string, tool, args string) map[string]any {
	t.Helper()
	argv := append([]string{"--registry", td + "registry.json", "--cas-dir", t.TempDir(),
		"--fake", td + "fake_blender_gui.json", "--fake", td + "fake_beckett.json"}, extra...)
	in := `{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"` + tool + `","arguments":` + args + `}}` + "\n"
	var o, e bytes.Buffer
	if rc := run(argv, strings.NewReader(in), &o, &e); rc != 0 {
		t.Fatalf("rc %d: %s", rc, e.String())
	}
	var resp struct {
		Result struct {
			Content []struct {
				Text string `json:"text"`
			} `json:"content"`
		} `json:"result"`
	}
	if err := json.Unmarshal(o.Bytes(), &resp); err != nil || len(resp.Result.Content) == 0 {
		t.Fatalf("bad response %s", o.String())
	}
	var env map[string]any
	if err := json.Unmarshal([]byte(resp.Result.Content[0].Text), &env); err != nil {
		t.Fatal(err)
	}
	return env
}

// --mock-gui has no verifier: a GUI click may be accepted but never EFFECT_OK.
func TestMockGUIWithoutVerifierIsUnverified(t *testing.T) {
	t.Setenv("OS_CU_TEST", "1")
	env := callStep(t, []string{"--mock-gui"}, "cu_click", `{"x":5,"y":6,"button":1}`)
	st := env["step"].(map[string]any)
	if st["verdict"] != "UNVERIFIED" {
		t.Fatalf("verdict %v, want UNVERIFIED", st["verdict"])
	}
	b, _ := json.Marshal(st)
	if _, err := action.DecodeStep(b); err != nil {
		t.Fatalf("step rejected by L2: %v", err)
	}
}

// --l2-fake wires L2Bridge: the click is lowered by the L2 executor and the
// step carries the L2 digest and a located target.
func TestL2FakeWiringEndToEnd(t *testing.T) {
	t.Setenv("OS_CU_TEST", "1")
	env := callStep(t, []string{"--l2-fake", "--deny-list", deny}, "cu_click", `{"x":5,"y":6,"button":1}`)
	st := env["step"].(map[string]any)
	b, _ := json.Marshal(st)
	dec, err := action.DecodeStep(b)
	if err != nil {
		t.Fatalf("step rejected by L2: %v\n%s", err, b)
	}
	if dec.Op != "click" || dec.Located.By != "xy" || dec.Located.BBox[0] != 5 || dec.Located.BBox[1] != 6 || dec.RIBefore == 0 {
		t.Fatalf("not executed by L2 exec: %+v", dec)
	}
}

// RJ-03: test-only flags are refused (rc 2) unless OS_CU_TEST=1.
func TestFakeFlagsRefusedWithoutTestEnv(t *testing.T) {
	t.Setenv("OS_CU_TEST", "")
	for _, f := range [][]string{{"--mock-gui"}, {"--l2-fake", "--deny-list", deny}} {
		argv := append([]string{"--registry", td + "registry.json", "--cas-dir", t.TempDir()}, f...)
		var o, e bytes.Buffer
		if rc := run(argv, strings.NewReader(`{"jsonrpc":"2.0","id":1,"method":"tools/list"}`+"\n"), &o, &e); rc != 2 || o.Len() != 0 {
			t.Fatalf("%v without OS_CU_TEST -> rc %d out %q", f, rc, o.String())
		}
	}
	t.Setenv("OS_CU_TEST", "1")
	var o, e bytes.Buffer
	if rc := run([]string{"--registry", td + "registry.json", "--cas-dir", t.TempDir(), "--l2-fake"},
		strings.NewReader(`{"jsonrpc":"2.0","id":1,"method":"tools/list"}`+"\n"), &o, &e); rc != 2 {
		t.Fatalf("--l2-fake without --deny-list -> rc %d", rc)
	}
}

// ADC2-02(1): the fake observer can never yield EFFECT_OK, however many
// frames it has served, and it runs with the deny list loaded.
func TestL2FakeNeverEffectOK(t *testing.T) {
	t.Setenv("OS_CU_TEST", "1")
	argv := []string{"--registry", td + "registry.json", "--cas-dir", t.TempDir(), "--l2-fake", "--deny-list", deny}
	var in strings.Builder
	for i := 1; i <= 6; i++ {
		in.WriteString(`{"jsonrpc":"2.0","id":` + string(rune('0'+i)) + `,"method":"tools/call","params":{"name":"cu_click","arguments":{"x":5,"y":6,"button":1}}}` + "\n")
	}
	var o, e bytes.Buffer
	if rc := run(argv, strings.NewReader(in.String()), &o, &e); rc != 0 {
		t.Fatalf("rc %d: %s", rc, e.String())
	}
	if n := strings.Count(o.String(), `\"verdict\":\"`); n != 6 {
		t.Fatalf("want 6 verdicts, got %d: %s", n, o.String())
	}
	if strings.Contains(o.String(), `EFFECT_OK`) {
		t.Fatalf("fake observer produced EFFECT_OK: %s", o.String())
	}
	if ex, err := fakeL2(deny); err != nil || ex.Cfg.Deny == nil {
		t.Fatalf("fake L2 without deny list: %v", err)
	}
}

type okIso struct{}

func (okIso) Read(string) (string, error) { return "0123456789abcdef0123456789abcdef", nil }

func prodDeps(inCapsule bool, opens *int) prodgate.Deps {
	return prodgate.Deps{
		Env: func(k string) string {
			return map[string]string{"DISPLAY": ":100", "VQA_ISO": "0123456789abcdef0123456789abcdef"}[k]
		},
		Probe: capture.CapsuleProbe{Exists: func(p string) bool { return inCapsule && p == "/.dockerenv" }},
		Iso:   okIso{},
		XTest: func(string) (exec.XTestAPI, func(), error) { *opens++; return nopX{}, func() {}, nil },
		NewObserver: func(string, string, string) (exec.Observer, func(), error) {
			return &exec.SeqObserver{Frames: []exec.Frame{exec.MkFrame(1, "00000000000000aa", "a", 64, 64)}}, func() {}, nil
		},
	}
}

type nopX struct{}

func (nopX) MovePointer(x, y int16) error { return nil }
func (nopX) ButtonDown(b byte) error      { return nil }
func (nopX) ButtonUp(b byte) error        { return nil }
func (nopX) KeyDown(k byte) error         { return nil }
func (nopX) KeyUp(k byte) error           { return nil }

func TestMcpGuiExecutorWiredInProd(t *testing.T) {
	t.Setenv("OS_CU_TEST", "")
	opens := 0
	g, closeAll, err := prodGUI(prodDeps(true, &opens), "/x/fb", "/dev/shm/r", deny, io.Discard)
	if err != nil {
		t.Fatal(err)
	}
	defer closeAll()
	if _, ok := g.(mcp.L2Bridge); !ok {
		t.Fatalf("Prod GUI executor is %T, want mcp.L2Bridge", g)
	}
	if opens != 1 {
		t.Fatalf("XTEST opens %d", opens)
	}
}

func TestMcpGuiUnwiredOnGateFail(t *testing.T) {
	t.Setenv("OS_CU_TEST", "")
	opens := 0
	g, _, err := prodGUI(prodDeps(false, &opens), "/x/fb", "/dev/shm/r", deny, io.Discard)
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := g.(mcp.Unwired); !ok || opens != 0 {
		t.Fatalf("outside the capsule: %T opens=%d", g, opens)
	}
	// End to end through run(): the host gate fails, GUI actions stop unwired.
	var o, e bytes.Buffer
	in := `{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"cu_click","arguments":{"x":5,"y":6,"button":1}}}` + "\n"
	if rc := run([]string{"--registry", td + "registry.json", "--cas-dir", t.TempDir()}, strings.NewReader(in), &o, &e); rc != 0 {
		t.Fatalf("rc %d %s", rc, e.String())
	}
	if !strings.Contains(o.String(), "gui_executor_unwired") {
		t.Fatalf("host run did not stay unwired: %s", o.String())
	}
}

func TestFakeFlagRefusedWithoutTestEnv(t *testing.T) {
	t.Setenv("OS_CU_TEST", "")
	var o, e bytes.Buffer
	argv := []string{"--registry", td + "registry.json", "--cas-dir", t.TempDir(), "--fake", td + "fake_blender_gui.json"}
	if rc := run(argv, strings.NewReader(`{"jsonrpc":"2.0","id":1,"method":"tools/list"}`+"\n"), &o, &e); rc != 2 || o.Len() != 0 {
		t.Fatalf("--fake without OS_CU_TEST -> rc %d", rc)
	}
}

// ADW1-03 / JW1-06: Prod injects the AST gate (capsule probe + ExecRunner on the in-capsule checker);
// a denying checker stops the script before any server call, and the host stays unwired.
// The run() wiring itself is TestMcpRunProdWiresASTGate.
func TestMcpProdInjectsASTGate(t *testing.T) {
	t.Setenv("OS_CU_TEST", "")
	chk := t.TempDir() + "/astcheck.py"
	body := "import sys\nsys.stdin.read()\nprint('{\"schema\":\"os.cu.ast/v1\",\"verdict\":\"deny\",\"code\":\"ast_denied\",\"reason\":\"fake\"}')\nsys.exit(5)\n"
	if err := os.WriteFile(chk, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	opens := 0
	g := astGateFor(prodDeps(true, &opens), chk)
	ag, ok := g.(*mcp.ASTGate)
	if !ok || ag == nil || !ag.InCapsule() {
		t.Fatalf("Prod gate is %T", g)
	}
	if r, ok := ag.Runner.(mcp.ExecRunner); !ok || r.Script != chk {
		t.Fatalf("runner %+v", ag.Runner)
	}
	err := g.Check(context.Background(), "import bpy\n", "/scratch/out")
	if err == nil || !strings.Contains(err.Error(), "script_denied") {
		t.Fatalf("denying checker did not refuse: %v", err)
	}
	if h := astGateFor(prodDeps(false, &opens), chk); h != nil {
		if herr := h.Check(context.Background(), "import bpy\n", ""); herr == nil || !strings.Contains(herr.Error(), "script_exec_unwired") {
			t.Fatalf("host gate not unwired: %v", herr)
		}
	}
	var o, e bytes.Buffer
	argv := []string{"--registry", td + "registry.json", "--cas-dir", t.TempDir(), "--ast-script", chk}
	if rc := run(argv, strings.NewReader(`{"jsonrpc":"2.0","id":1,"method":"tools/list"}`+"\n"), &o, &e); rc != 0 {
		t.Fatalf("--ast-script rc %d %s", rc, e.String())
	}
}

// denyChecker writes a stand-in astcheck.py that always answers deny (rc 5).
func denyChecker(t *testing.T) string {
	t.Helper()
	chk := t.TempDir() + "/astcheck.py"
	body := "import sys\nsys.stdin.read()\nprint('{\"schema\":\"os.cu.ast/v1\",\"verdict\":\"deny\",\"code\":\"ast_denied\",\"reason\":\"fake\"}')\nsys.exit(5)\n"
	if err := os.WriteFile(chk, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	return chk
}

// runScript drives one script tool call through run() with newDeps stubbed to d.
func runScript(t *testing.T, d prodgate.Deps, chk string) string {
	t.Helper()
	old := newDeps
	newDeps = func(func(string) string) prodgate.Deps { return d }
	defer func() { newDeps = old }()
	argv := []string{"--registry", td + "registry.json", "--cas-dir", t.TempDir(),
		"--fb", "/x/fb", "--ramws-dir", "/dev/shm/r", "--deny-list", deny, "--ast-script", chk}
	in := `{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"cu_mcp_call","arguments":{"server":"blender-gui","tool":"workspace_switch","risk":"write","params":{"workspace":"Layout"}}}}` + "\n"
	var o, e bytes.Buffer
	if rc := run(argv, strings.NewReader(in), &o, &e); rc != 0 {
		t.Fatalf("rc %d %s", rc, e.String())
	}
	return o.String()
}

// RJ1-02 / ADW2-03: run() itself injects the AST gate inside the capsule, whatever the X11 prodgate
// verdict: Prod and capsule-without-display both reach the denying checker; the host stays unwired.
func TestMcpRunProdWiresASTGate(t *testing.T) {
	t.Setenv("OS_CU_TEST", "")
	chk := denyChecker(t)
	opens := 0
	if out := runScript(t, prodDeps(true, &opens), chk); !strings.Contains(out, "script_denied") || strings.Contains(out, "script_exec_unwired") {
		t.Fatalf("Prod run() did not wire the AST gate: %s", out)
	}
	if opens != 1 {
		t.Fatalf("Prod XTEST opens %d", opens)
	}
	noX := prodDeps(true, &opens)
	noX.Env = func(string) string { return "" }
	if out := runScript(t, noX, chk); !strings.Contains(out, "script_denied") || strings.Contains(out, "script_exec_unwired") {
		t.Fatalf("capsule without X11 Prod did not wire the AST gate: %s", out)
	}
	if out := runScript(t, prodDeps(false, &opens), chk); !strings.Contains(out, "script_exec_unwired") || strings.Contains(out, "script_denied") {
		t.Fatalf("host run() wired the AST gate: %s", out)
	}
	if opens != 1 {
		t.Fatalf("XTEST opened off Prod: %d", opens)
	}
}

// RJ1-04 / ADW1-09: in Prod each of --fb, --ramws-dir, --deny-list is required (rc 2, no XTEST open).
func TestProdGUIRefusesMissingFB(t *testing.T) {
	t.Setenv("OS_CU_TEST", "")
	for _, c := range [][3]string{{"", "/dev/shm/r", deny}, {"/x/fb", "", deny}, {"/x/fb", "/dev/shm/r", ""}} {
		opens := 0
		g, _, err := prodGUI(prodDeps(true, &opens), c[0], c[1], c[2], io.Discard)
		if mcp.RcOf(err) != 2 || g != nil || opens != 0 {
			t.Fatalf("%q: rc %d exec %T opens %d", c, mcp.RcOf(err), g, opens)
		}
	}
}

package mcp

import (
	"bytes"
	"context"
	"encoding/json"
	"opensuperficies.local/srf/src/cubook/action"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const td = "../../../testdata/cu/mcp/"

type recExec struct{ got []Action }

func (r *recExec) Execute(_ context.Context, a Action) (Outcome, error) {
	r.got = append(r.got, a)
	return Outcome{Step: Step{Schema: SchemaStep, Step: a.Step, Op: a.Op, Verdict: "EFFECT_OK"}}, nil
}

type okVerifier struct{ r, v, b *bool }

func (o okVerifier) Verify(context.Context, Action, CallResult) Verification {
	return Verification{Receipt: o.r, Visual: o.v, Readback: o.b}
}

func bp(b bool) *bool { return &b }

func loadReg(t *testing.T) *Registry {
	t.Helper()
	r, err := LoadRegistry(td + "registry.json")
	if err != nil {
		t.Fatal(err)
	}
	return r
}

func newDisp(t *testing.T, v Verifier) (*Dispatcher, *FakeCaller, *recExec) {
	t.Helper()
	fc, err := NewFakeCaller(td+"fake_blender_gui.json", td+"fake_beckett.json")
	if err != nil {
		t.Fatal(err)
	}
	g := &recExec{}
	return &Dispatcher{Reg: loadReg(t), Caller: fc, Verifier: v, CAS: &CAS{Dir: t.TempDir()}, GUI: g,
		ScratchDir: "/scratch/cap"}, fc, g
}

func mcpAction(server, tool string) Action {
	return Action{Schema: SchemaAction, Step: 1, Op: "mcp_call", Risk: "read", Fallback: "none", OnFail: "stop", Policy: "script",
		Pre:    Pre{MatchMin: "L1"},
		Args:   Args{Server: server, Tool: tool},
		Expect: []Expect{{Kind: "readback", Readback: &Readback{Via: "blender -b", Query: "q", Eq: "1"}}}}
}

func rpc(t *testing.T, s *Server, lines ...string) []map[string]any {
	t.Helper()
	var out bytes.Buffer
	if err := s.Serve(strings.NewReader(strings.Join(lines, "\n")+"\n"), &out); err != nil {
		t.Fatal(err)
	}
	var rs []map[string]any
	for _, l := range strings.Split(strings.TrimSpace(out.String()), "\n") {
		if l == "" {
			continue
		}
		var m map[string]any
		if err := json.Unmarshal([]byte(l), &m); err != nil {
			t.Fatalf("bad line %q: %v", l, err)
		}
		rs = append(rs, m)
	}
	return rs
}

func call(name, args string) string {
	return `{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"` + name + `","arguments":` + args + `}}`
}

func structured(t *testing.T, m map[string]any) map[string]any {
	t.Helper()
	res := m["result"].(map[string]any)
	return res["structuredContent"].(map[string]any)
}

func TestMCPInitializeToolsList(t *testing.T) {
	s := NewServer(Config{Exec: &recExec{}, CAS: &CAS{Dir: t.TempDir()}})
	rs := rpc(t, s, `{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}`,
		`{"jsonrpc":"2.0","method":"notifications/initialized"}`,
		`{"jsonrpc":"2.0","id":2,"method":"tools/list"}`,
		`{"jsonrpc":"2.0","id":3,"method":"nope"}`)
	if len(rs) != 3 {
		t.Fatalf("want 3 responses (notification silent), got %d", len(rs))
	}
	if rs[0]["result"].(map[string]any)["serverInfo"].(map[string]any)["name"] != "srf-cu-mcp" {
		t.Fatal("serverInfo")
	}
	names := map[string]bool{}
	for _, x := range rs[1]["result"].(map[string]any)["tools"].([]any) {
		tl := x.(map[string]any)
		names[tl["name"].(string)] = true
		if tl["inputSchema"].(map[string]any)["type"] != "object" {
			t.Fatal("inputSchema")
		}
	}
	for _, n := range []string{"cu_observe", "cu_click", "cu_type", "cu_key", "cu_drag", "cu_scroll", "cu_wait", "cu_assert", "cu_locate", "cu_mcp_call"} {
		if !names[n] {
			t.Fatalf("missing tool %s", n)
		}
	}
	if rs[2]["error"].(map[string]any)["code"].(float64) != -32601 {
		t.Fatal("unknown method code")
	}
}

func TestMCPCallBecomesSingleActionV1(t *testing.T) {
	ex := &recExec{}
	s := NewServer(Config{Exec: ex, CAS: &CAS{Dir: t.TempDir()}})
	rs := rpc(t, s, call("cu_click", `{"x":10,"y":20,"button":1}`))
	if len(ex.got) != 1 {
		t.Fatalf("want exactly 1 action, got %d", len(ex.got))
	}
	a := ex.got[0]
	if a.Schema != SchemaAction || a.Op != "click" || a.Target == nil || a.Target.By != "xy" || a.Args.Button != 1 || a.Risk != "write" {
		t.Fatalf("action %+v", a)
	}
	st := structured(t, rs[0])["step"].(map[string]any)
	if st["schema"] != SchemaStep {
		t.Fatalf("step %v", st)
	}
	// typed literal text is refused, floats are refused, executor untouched
	for _, bad := range []string{call("cu_type", `{"text":"hunter2"}`), call("cu_click", `{"x":1.5,"y":2}`), call("cu_click", `{"x":1,"y":2,"zz":3}`)} {
		before := len(ex.got)
		r := rpc(t, s, bad)
		sc := structured(t, r[0])
		if sc["rc"].(float64) != 2 || len(ex.got) != before {
			t.Fatalf("schema reject %s -> %v", bad, sc)
		}
	}
	rs = rpc(t, s, call("cu_type", `{"text_ref":"plan:1","text_sha256":"`+strings.Repeat("a", 64)+`","len":7}`))
	if structured(t, rs[0])["rc"].(float64) != 0 || ex.got[len(ex.got)-1].Args.Len != 7 {
		t.Fatal("typed by hash")
	}
}

func TestMCPResultUntrustedCAS(t *testing.T) {
	d, _, _ := newDisp(t, okVerifier{b: bp(true)})
	out, err := d.Execute(context.Background(), mcpAction("blender-gui", "get_scene_info"))
	if err != nil {
		t.Fatal(err)
	}
	m := out.MCP
	if m == nil || !m.McpUntrusted || m.Schema != SchemaMCPResult || !strings.HasPrefix(m.McpContentRef, "cas:sha256:") || len(m.McpContentRef) != len("cas:sha256:")+64 {
		t.Fatalf("mcp result %+v", m)
	}
	b, err := os.ReadFile(filepath.Join(d.CAS.Dir, strings.TrimPrefix(m.McpContentRef, "cas:sha256:")))
	if err != nil || uint32(len(b)) != m.McpContentBytes || !strings.Contains(string(b), "Cube") {
		t.Fatalf("cas %v %q", err, b)
	}
	if out.Step.Verdict != "EFFECT_OK" || out.Step.Evidence.MCP == nil || !out.Step.Evidence.MCP.Untrusted {
		t.Fatalf("step %+v", out.Step)
	}
}

func TestMCPContentOver256KiBRc6(t *testing.T) {
	d, fc, _ := newDisp(t, okVerifier{b: bp(true)})
	fc.Inject("blender-gui", "get_scene_info", CallResult{Content: bytes.Repeat([]byte("x"), MaxContent+1)}, nil)
	out, _ := d.Execute(context.Background(), mcpAction("blender-gui", "get_scene_info"))
	if out.Step.RC != 6 || out.Step.Verdict == "EFFECT_OK" {
		t.Fatalf("step %+v", out.Step)
	}
	if ents, _ := os.ReadDir(d.CAS.Dir); len(ents) != 0 {
		t.Fatal("oversize content reached CAS")
	}
	fc.Inject("blender-gui", "get_scene_info", CallResult{Content: bytes.Repeat([]byte("x"), MaxContent)}, nil)
	out, _ = d.Execute(context.Background(), mcpAction("blender-gui", "get_scene_info"))
	if out.Step.RC != 0 {
		t.Fatalf("exact cap must pass: %+v", out.Step)
	}
}

func TestHostEndpointRefusedRc5(t *testing.T) {
	for _, ep := range []string{"127.0.0.1:9876", "127.0.0.1:9877", "http://127.0.0.1:9876/mcp", "localhost:19876", "0.0.0.0:19876", "127.0.0.1:19884"} {
		d, fc, _ := newDisp(t, okVerifier{b: bp(true)})
		s := d.Reg.Servers["blender-gui"]
		s.Endpoint = ep
		d.Reg.Servers["blender-gui"] = s
		out, _ := d.Execute(context.Background(), mcpAction("blender-gui", "get_scene_info"))
		if out.Step.RC != 5 || out.Step.Stop == nil || len(fc.Calls) != 0 {
			t.Fatalf("%s: %+v calls=%d", ep, out.Step, len(fc.Calls))
		}
	}
	if err := CheckEndpoint("127.0.0.1:19883"); err != nil {
		t.Fatal(err)
	}
}

func TestBeckett8770Refused(t *testing.T) {
	err := CheckEndpoint("127.0.0.1:8770")
	if RcOf(err) != 5 || !strings.Contains(err.Error(), "operator_session") {
		t.Fatalf("%v", err)
	}
	d, fc, _ := newDisp(t, nil)
	s := d.Reg.Servers["godot-beckett"]
	s.Endpoint = "127.0.0.1:8770"
	d.Reg.Servers["godot-beckett"] = s
	out, _ := d.Execute(context.Background(), mcpAction("godot-beckett", "get_scene_tree"))
	if out.Step.RC != 5 || out.Step.Stop == nil || *out.Step.Stop != "operator_session" || len(fc.Calls) != 0 {
		t.Fatalf("%+v", out.Step)
	}
}

func TestOZPathRefused(t *testing.T) {
	oz := []string{"/home/user/Documentos/OZ"}
	for _, p := range []string{"/home/user/Documentos/OZ/proj", "/home/user/Documentos/OZ", "/other/place", "/scratch/cap/../x", "relative/p", "/scratch/capx/p"} {
		if err := GuardGodotProject(p, "/scratch/cap", oz); RcOf(err) != 5 {
			t.Fatalf("%s accepted: %v", p, err)
		}
	}
	if err := GuardGodotProject("/scratch/cap/proj", "/scratch/cap", oz); err != nil {
		t.Fatal(err)
	}
	d, _, g := newDisp(t, nil)
	a := mcpAction("godot-beckett", "inspector_edit")
	a.Fallback = "gui"
	a.Risk = "write"
	a.Args.Params = json.RawMessage(`{"project_path":"/home/user/Documentos/OZ/p","property":"x","value_sha256":"` + strings.Repeat("b", 64) + `","value_len":2}`)
	out, _ := d.Execute(context.Background(), a)
	if out.Step.RC != 5 || len(g.got) != 0 {
		t.Fatalf("%+v", out.Step)
	}
}

func TestBeckettWriteWithoutConfirmRefused(t *testing.T) {
	d, fc, _ := newDisp(t, okVerifier{b: bp(true)})
	a := mcpAction("godot-beckett", "set_property")
	a.Risk = "write"
	out, _ := d.Execute(context.Background(), a)
	if out.Step.RC != 5 || len(fc.Calls) != 0 {
		t.Fatalf("no confirm: %+v", out.Step)
	}
	d.ConfirmDestructive = true
	b := a
	b.Risk = "read" // confirm alone is not enough: an explicit write/irrev risk is required
	out, _ = d.Execute(context.Background(), b)
	if out.Step.RC != 5 || len(fc.Calls) != 0 {
		t.Fatalf("read risk: %+v", out.Step)
	}
	fc.Inject("godot-beckett", "set_property", CallResult{Content: []byte("ok")}, nil)
	a.Args.Params = json.RawMessage(`{"project_path":"/scratch/cap/p"}`)
	out, _ = d.Execute(context.Background(), a)
	if len(fc.Calls) != 1 || out.MCP.Witness {
		t.Fatalf("confirmed call must dial once and never be a witness: %+v", out.Step)
	}
	ok := mcpAction("godot-beckett", "get_scene_tree")
	out, _ = d.Execute(context.Background(), ok)
	if !out.MCP.Witness {
		t.Fatal("registry read tool is a witness")
	}
}

func TestFallbackGuiOnlyWhenMissingOrUnavailable(t *testing.T) {
	ctx := context.Background()
	prm := `{"project_path":"/scratch/cap/p","property":"size","value_sha256":"` + strings.Repeat("c", 64) + `","value_len":3}`
	// op absent from registry + fallback gui + allowed op => GUI plan
	d, fc, g := newDisp(t, okVerifier{b: bp(true), v: bp(true)})
	a := mcpAction("godot-beckett", "inspector_edit")
	a.Fallback, a.Risk, a.Args.Params = "gui", "write", json.RawMessage(prm)
	out, _ := d.Execute(ctx, a)
	if len(g.got) == 0 || len(fc.Calls) != 0 || out.Fallback != "gui" || out.Step.RC != 0 {
		t.Fatalf("missing->gui: %+v calls=%d gui=%d", out.Step, len(fc.Calls), len(g.got))
	}
	// registry lists the op: server unavailable does NOT license GUI (ADC-07)
	d, fc, g = newDisp(t, okVerifier{b: bp(true), v: bp(true)})
	d.Reg.Servers["godot-beckett"].Tools["inspector_edit"] = ToolSpec{Risk: "write"}
	a2 := a
	out, _ = d.Execute(ctx, a2)
	if len(fc.Calls) != 1 || len(g.got) != 0 || out.Fallback != "" || out.Step.RC != 5 {
		t.Fatalf("listed op must not fall back: %+v", out.Step)
	}
	// op present, call fine => no GUI even with fallback gui
	d, fc, g = newDisp(t, okVerifier{b: bp(true)})
	b := mcpAction("blender-gui", "get_scene_info")
	b.Fallback = "gui"
	out, _ = d.Execute(ctx, b)
	if len(g.got) != 0 || out.Fallback != "" {
		t.Fatal("gui used though mcp worked")
	}
	// server error other than unavailable => no fallback
	fc.Inject("blender-gui", "get_scene_info", CallResult{ServerError: "E_TOOL_FAILED"}, nil)
	out, _ = d.Execute(ctx, b)
	if len(g.got) != 0 || out.Step.Verdict != "MCP_ERROR" || out.Step.RC != 5 {
		t.Fatalf("tool failure must not fall back: %+v", out.Step)
	}
	// fallback none + missing op => refused, no GUI
	c := mcpAction("godot-beckett", "inspector_edit")
	c.Risk, c.Args.Params = "write", json.RawMessage(prm)
	out, _ = d.Execute(ctx, c)
	if out.Step.RC != 5 || len(g.got) != 0 {
		t.Fatalf("none+missing: %+v", out.Step)
	}
	// op not in server gui_fallback_ops (area_split is MCP/bpy, not GUI)
	e := mcpAction("blender-gui", "unknown_tool")
	e.Fallback = "gui"
	out, _ = d.Execute(ctx, e)
	if out.Step.RC != 5 || len(g.got) != 0 {
		t.Fatalf("gui not allowed for op: %+v", out.Step)
	}
}

func TestEditorFallbackPlansForThreeOps(t *testing.T) {
	h := strings.Repeat("d", 64)
	gui := []struct {
		server, op string
		p          EditorParams
		wantOp     string
	}{
		{"blender-gui", "gizmo_drag", EditorParams{Object: "Cube", From: &[2]int{100, 100}, To: &[2]int{160, 100}}, "drag"},
		{"godot-beckett", "inspector_edit", EditorParams{ProjectPath: "/scratch/cap/p", Property: "size", ValueSha256: h, ValueLen: 3}, "type"},
		{"godot-beckett", "add_child_node", EditorParams{ProjectPath: "/scratch/cap/p", NodeType: "Node2D", ValueSha256: h, ValueLen: 6}, "key"},
		{"godot-beckett", "run_scene", EditorParams{ProjectPath: "/scratch/cap/p"}, "key"},
		{"godot-beckett", "stop_scene", EditorParams{ProjectPath: "/scratch/cap/p"}, "key"},
	}
	for _, c := range gui {
		pl, err := PlanEditorOp(c.server, c.op, c.p)
		if err != nil || pl.Channel != "gui" || len(pl.Actions) == 0 {
			t.Fatalf("%s: %v %+v", c.op, err, pl)
		}
		has := false
		for i, a := range pl.Actions {
			if a.Schema != SchemaAction || a.Op == "mcp_call" {
				t.Fatalf("%s action %d", c.op, i)
			}
			if a.Op == c.wantOp {
				has = true
			}
			if a.Op == "type" && (a.Args.TextSha256 == "" || a.Args.Len == 0) {
				t.Fatal("type must carry hash+len only")
			}
			if a.Risk != "read" && a.Pre.MatchMin != "L0" && a.Pre.MatchMin != "L1" {
				t.Fatalf("%s write step needs L0|L1", c.op)
			}
		}
		last := pl.Actions[len(pl.Actions)-1]
		if len(last.Expect) == 0 {
			t.Fatalf("%s last step has no expectation", c.op)
		}
		if !has {
			t.Fatalf("%s lacks %s step", c.op, c.wantOp)
		}
	}
	// gizmo drag is verified by readback AND visual region check
	pl, _ := PlanEditorOp("blender-gui", "gizmo_drag", gui[0].p)
	kinds := map[string]bool{}
	for _, e := range pl.Actions[len(pl.Actions)-1].Expect {
		kinds[e.Kind] = true
	}
	if !kinds["readback"] || !kinds["visual"] {
		t.Fatalf("gizmo verify %v", kinds)
	}
	// workspace switch and area split are MCP/bpy scripts, never GUI
	for _, op := range []string{"workspace_switch", "area_split"} {
		pl, err := PlanEditorOp("blender-gui", op, EditorParams{Workspace: "Layout", Area: "VIEW_3D", Factor: 500})
		if err != nil || pl.Channel != "mcp" || pl.Script == "" {
			t.Fatalf("%s: %v %+v", op, err, pl)
		}
		if err := ScanScript(pl.Script, "/scratch/out"); err != nil {
			t.Fatalf("%s script denied: %v", op, err)
		}
		if !strings.Contains(pl.Script, "temp_override") {
			t.Fatalf("%s needs a context override", op)
		}
	}
	if _, err := PlanEditorOp("godot-beckett", "inspector_edit", EditorParams{ProjectPath: "/home/user/Documentos/OZ/x", Property: "a", ValueSha256: h, ValueLen: 1}); err == nil {
		t.Fatal("OZ project path planned")
	}
	if _, err := PlanEditorOp("blender-gui", "no_such_op", EditorParams{}); err == nil {
		t.Fatal("unknown op planned")
	}
}

func TestMcpOkButNoReceiptIsUnverified(t *testing.T) {
	ctx := context.Background()
	cases := []struct {
		name string
		v    Verifier
		a    func(*Action)
		want string
	}{
		{"no verifier", nil, nil, "UNVERIFIED"},
		{"verifier silent", okVerifier{}, nil, "UNVERIFIED"},
		{"no expectation", okVerifier{b: bp(true)}, func(a *Action) { a.Expect = nil }, "UNVERIFIED"},
		{"readback false", okVerifier{b: bp(false)}, nil, "NO_EFFECT"},
		{"visual false", okVerifier{b: bp(true), v: bp(false)}, func(a *Action) { a.Expect = append(a.Expect, Expect{Kind: "visual", Visual: &Visual{Via: "s4"}}) }, "SIDE_EFFECT"},
		{"missing visual", okVerifier{b: bp(true)}, func(a *Action) { a.Expect = append(a.Expect, Expect{Kind: "visual", Visual: &Visual{Via: "s4"}}) }, "UNVERIFIED"},
		{"all present", okVerifier{b: bp(true)}, nil, "EFFECT_OK"},
	}
	for _, c := range cases {
		d, _, _ := newDisp(t, c.v)
		a := mcpAction("blender-gui", "execute_blender_code")
		a.Risk = "write"
		a.Args.ScriptRef = "plan:s1"
		d.Scripts = func(string) (string, bool) { return "import bpy\nbpy.ops.object.select_all(action='DESELECT')\n", true }
		if c.a != nil {
			c.a(&a)
		}
		out, _ := d.Execute(ctx, a)
		if out.Step.Verdict != c.want {
			t.Fatalf("%s: got %s want %s (%+v)", c.name, out.Step.Verdict, c.want, out.Step)
		}
		if c.want == "UNVERIFIED" && out.Step.RC != 0 {
			t.Fatalf("%s rc", c.name)
		}
	}
}

func TestScriptDenied(t *testing.T) {
	deny := []string{
		"import os", "import bpy, subprocess", "from socket import socket", "import urllib.request", "import requests", "import ctypes", "import shutil",
		"x = eval('1')", "exec(code)", "compile(s,'a','exec')", "bpy.app.handlers.save_post.append(f)", "addon_utils.enable('x')",
		"__import__('os')", "import importlib", "open('/etc/x','w')", "open(p, 'a')", "open('/scratch/out/../x','w')", "open(name,mode)",
	}
	for _, s := range deny {
		err := ScanScript("import bpy\n"+s+"\n", "/scratch/out")
		if RcOf(err) != 5 || !strings.Contains(err.Error(), "script_denied") {
			t.Fatalf("%q not denied: %v", s, err)
		}
	}
	allow := []string{"import bpy", "bpy.ops.transform.translate(value=(2,0,0))", "open('/scratch/out/a.txt','w')", "open('/scratch/out/a.txt','r')", "x = 'os.path'  # not an import"}
	for _, s := range allow {
		if err := ScanScript("import bpy\n"+s+"\n", "/scratch/out"); err != nil {
			t.Fatalf("%q denied: %v", s, err)
		}
	}
	// dispatcher: denied script never dials
	d, fc, _ := newDisp(t, okVerifier{b: bp(true)})
	d.Scripts = func(string) (string, bool) { return "import os\n", true }
	a := mcpAction("blender-gui", "execute_blender_code")
	a.Risk, a.Args.ScriptRef = "write", "plan:s1"
	out, _ := d.Execute(context.Background(), a)
	if out.Step.RC != 5 || out.Step.Stop == nil || *out.Step.Stop != "script_denied" || len(fc.Calls) != 0 {
		t.Fatalf("%+v", out.Step)
	}
	// execute_blender_code without a resolvable script is refused too
	d.Scripts = nil
	out, _ = d.Execute(context.Background(), a)
	if out.Step.RC != 5 || len(fc.Calls) != 0 {
		t.Fatalf("no script: %+v", out.Step)
	}
}

func TestEnvelopeLeak(t *testing.T) {
	srv := []byte("SERVER-SECRET-PAYLOAD-0123456789")
	if err := CheckEnvelope([]byte(`{"x":"before SERVER-SECRET-PAYLOAD-0123456789 after"}`), srv); RcOf(err) != 5 || !strings.Contains(err.Error(), "envelope_leak") {
		t.Fatalf("leak undetected: %v", err)
	}
	if err := CheckEnvelope([]byte(`{"x":"mcp_content_ref","r":"cas:sha256:`+strings.Repeat("a", 64)+`"}`), srv); err != nil {
		t.Fatal(err)
	}
	// a server error string never reaches the step
	d, fc, _ := newDisp(t, okVerifier{b: bp(true)})
	fc.Inject("blender-gui", "get_scene_info", CallResult{ServerError: "attacker-controlled-error-message-1234"}, nil)
	out, _ := d.Execute(context.Background(), mcpAction("blender-gui", "get_scene_info"))
	b, _ := json.Marshal(out)
	if bytes.Contains(b, []byte("attacker-controlled")) {
		t.Fatalf("server text echoed: %s", b)
	}
}

func TestRegistryStrict(t *testing.T) {
	r := loadReg(t)
	if len(r.Servers) != 3 || !r.Servers["godot-beckett"].Tools["get_scene_tree"].Witness {
		t.Fatalf("%+v", r)
	}
	for n, s := range r.Servers {
		if err := CheckEndpoint(s.Endpoint); err != nil {
			t.Fatalf("%s: %v", n, err)
		}
	}
	p := filepath.Join(t.TempDir(), "r.json")
	os.WriteFile(p, []byte(`{"schema":"os.cu.mcp-registry/v1","servers":{},"extra":1}`), 0o600)
	if _, err := LoadRegistry(p); RcOf(err) != 2 {
		t.Fatalf("unknown field: %v", err)
	}
	os.WriteFile(p, []byte(`{"schema":"os.cu.mcp-registry/v1","servers":{"a":{"endpoint":"127.0.0.1:19876","unlisted":"maybe","tools":{}}}}`), 0o600)
	if _, err := LoadRegistry(p); RcOf(err) != 2 {
		t.Fatalf("closed enum: %v", err)
	}
}

func TestNoListenSocketInPackage(t *testing.T) {
	ents, _ := os.ReadDir(".")
	for _, e := range ents {
		if strings.HasSuffix(e.Name(), ".go") && !strings.HasSuffix(e.Name(), "_test.go") {
			b, _ := os.ReadFile(e.Name())
			if bytes.Contains(b, []byte("net.Listen")) || bytes.Contains(b, []byte("http.ListenAndServe")) || bytes.Contains(b, []byte("os.Exit(1)")) {
				t.Fatalf("%s listens or exits 1", e.Name())
			}
		}
	}
}

// Every action L3 emits (tools and editor plans) must pass L2's strict decoder.
func TestEmittedActionsPassL2Decoder(t *testing.T) {
	h := strings.Repeat("e", 64)
	s := NewServer(Config{Exec: &recExec{}})
	calls := map[string]string{
		"cu_observe": `{}`, "cu_locate": `{"role":"button","name":"OK"}`, "cu_click": `{"x":1,"y":2,"button":1}`,
		"cu_type": `{"text_ref":"p:1","text_sha256":"` + h + `","len":3}`, "cu_key": `{"chord":"Return"}`,
		"cu_drag": `{"x":1,"y":2,"to_x":3,"to_y":4,"steps":4}`, "cu_scroll": `{"x":1,"y":2,"dx":0,"dy":-3}`, "cu_wait": `{"max_frames":10,"settle_k":3}`,
		"cu_assert":   `{"expect":[{"kind":"readback","readback":{"via":"v","query":"q","eq":"1"}}]}`,
		"cu_mcp_call": `{"server":"blender-gui","tool":"get_scene_info","risk":"read","params":{}}`,
	}
	for n, args := range calls {
		a, err := s.build(n, json.RawMessage(args))
		if err != nil {
			t.Fatalf("%s: %v", n, err)
		}
		b, _ := json.Marshal(a)
		if _, err := action.DecodeAction(b); err != nil {
			t.Fatalf("%s rejected by L2: %v\n%s", n, err, b)
		}
	}
	for _, c := range []struct{ srv, op string }{{"blender-gui", "gizmo_drag"}, {"godot-beckett", "inspector_edit"}, {"godot-beckett", "add_child_node"}, {"godot-beckett", "run_scene"}, {"godot-beckett", "stop_scene"}, {"godot-beckett", "read_output"}} {
		pl, err := PlanEditorOp(c.srv, c.op, EditorParams{Object: "Cube", From: &[2]int{1, 1}, To: &[2]int{5, 5}, ProjectPath: "/scratch/cap/p", Property: "size", NodeType: "Node2D", ValueSha256: h, ValueLen: 2})
		if err != nil {
			t.Fatal(err)
		}
		for _, a := range pl.Actions {
			b, _ := json.Marshal(a)
			if _, err := action.DecodeAction(b); err != nil {
				t.Fatalf("%s/%s step %d rejected by L2: %v\n%s", c.srv, c.op, a.Step, err, b)
			}
		}
	}
}

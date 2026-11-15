package mcp

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"os"
)

// Dispatcher is the Executor for op mcp_call; every other op is delegated to
// GUI (L2 exec). Verification never uses the MCP rc: only Verifier evidence.
type Dispatcher struct {
	Reg                *Registry
	Caller             Caller
	Verifier           Verifier
	CAS                *CAS
	GUI                Executor
	ConfirmDestructive bool
	Scripts            func(ref string) (string, bool)
	ScratchDir         string
	OutDir             string
	// ASTGate admits live script tools (capsule only). Nil keeps them
	// unwired unless OS_CU_TEST=1 (fakes).
	ASTGate ASTChecker
}

type Verification struct{ Receipt, Visual, Readback *bool }

type Verifier interface {
	Verify(ctx context.Context, a Action, res CallResult) Verification
}

func (d *Dispatcher) refuse(a Action, err error, stage string) Outcome {
	s := newStep(a, "NO_ACT")
	if a.Op == "mcp_call" && StopOf(err) != "io" && RcOf(err) == 5 && (stage == "call") {
		s.Verdict = "MCP_ERROR"
	}
	s.RC, s.Stop = RcOf(err), strp(StopOf(err))
	return Outcome{Step: s}
}

func (d *Dispatcher) Execute(ctx context.Context, a Action) (Outcome, error) {
	if a.Op != "mcp_call" {
		if d.GUI == nil {
			return Unwired{}.Execute(ctx, a)
		}
		return d.GUI.Execute(ctx, a)
	}
	srv, known := d.Reg.Servers[a.Args.Server]
	tool, listed := srv.Tools[a.Args.Tool]
	if !known || !listed {
		switch {
		case a.Fallback == "gui" && known && srv.guiAllowed(a.Args.Tool):
			return d.runGUI(ctx, a, srv)
		case a.Fallback == "gui":
			return d.refuse(a, gate("gui_fallback_not_allowed"), "admit"), nil
		case known && srv.Unlisted == "confirm":
			if (a.Risk != "write" && a.Risk != "irrev") || !d.ConfirmDestructive {
				return d.refuse(a, gate("confirm_required"), "admit"), nil
			}
			tool = ToolSpec{Risk: a.Risk}
		default:
			return d.refuse(a, gate("not_in_registry"), "admit"), nil
		}
	}
	if a.Args.Server == "godot-beckett" {
		if err := d.guardGodotCall(a); err != nil {
			return d.refuse(a, err, "admit"), nil
		}
	}
	if err := CheckEndpoint(srv.Endpoint); err != nil {
		return d.refuse(a, err, "admit"), nil
	}
	if rank(a.Risk) < rank(tool.Risk) {
		return d.refuse(a, gate("risk_understated"), "admit"), nil
	}
	callTool, args, scriptSha, scriptOut := a.Args.Tool, a.Args.Params, "", ""
	if tool.Via != "" || a.Args.Tool == "execute_blender_code" || a.Args.Tool == "python_exec" {
		script, outDir, err := d.script(a, tool)
		if err != nil {
			return d.refuse(a, err, "admit"), nil
		}
		scriptOut = outDir
		scriptSha = hexSum([]byte(script))
		args, _ = json.Marshal(map[string]string{"code": script})
		if tool.Via != "" {
			callTool = tool.Via
		}
	}
	// ScanScript (in d.script) is a lint, not a sandbox: a live script also
	// needs the capsule AST gate. Without one only fakes (OS_CU_TEST=1) run.
	if callTool == "execute_blender_code" || callTool == "python_exec" {
		switch {
		case d.ASTGate != nil:
			if err := d.ASTGate.Check(ctx, scriptOf(args), scriptOut); err != nil {
				return d.refuse(a, err, "admit"), nil
			}
		case os.Getenv("OS_CU_TEST") != "1":
			return d.refuse(a, gate("script_exec_unwired"), "admit"), nil
		}
	}
	res, err := d.Caller.Call(ctx, srv.Endpoint, a.Args.Server, callTool, args)
	if errors.Is(err, ErrUnavailable) || (err == nil && res.ServerError == ErrUnavailable.Error()) {
		if a.Fallback == "gui" {
			// a listed op has an MCP path: GUI is only for ops the registry lacks
			if !listed && srv.guiAllowed(a.Args.Tool) {
				return d.runGUI(ctx, a, srv)
			}
			return d.refuse(a, gate("gui_fallback_not_allowed"), "call"), nil
		}
		return d.refuse(a, gate("mcp_unavailable"), "call"), nil
	}
	if err != nil || res.ServerError != "" {
		return d.refuse(a, gate("mcp_error"), "call"), nil
	}
	ref, sha, err := d.CAS.Put(res.Content)
	if err != nil {
		if RcOf(err) == 6 {
			o := d.refuse(a, err, "call")
			o.Step.Verdict = "MCP_ERROR"
			return o, nil
		}
		return Outcome{}, err
	}
	verdict, stage := d.judge(ctx, a, res)
	st := newStep(a, verdict)
	st.Evidence.MCP = &MCPEvidence{Server: a.Args.Server, Tool: a.Args.Tool, ResultSHA: sha, Untrusted: true}
	m := &MCPResult{Schema: SchemaMCPResult, Step: a.Step, Server: a.Args.Server, Tool: a.Args.Tool, Risk: a.Risk, ArgsSha: hexSum(args),
		ScriptSha: scriptSha, Stage: stage, Verdict: verdict, McpUntrusted: true, McpContentRef: ref, McpContentBytes: uint32(len(res.Content)), Witness: tool.Witness && listed}
	out := Outcome{Step: st, MCP: m}
	env, _ := json.Marshal(out)
	if err := CheckEnvelope(env, res.Content); err != nil {
		return d.refuse(a, err, "call"), nil
	}
	return out, nil
}

func rank(r string) int {
	switch r {
	case "write":
		return 1
	case "irrev":
		return 2
	}
	return 0
}

// scriptOf recovers the script text from the {"code": ...} call arguments.
func scriptOf(args json.RawMessage) string {
	var m map[string]string
	_ = json.Unmarshal(args, &m)
	return m["code"]
}

func (d *Dispatcher) script(a Action, tool ToolSpec) (string, string, error) {
	var p EditorParams
	if len(a.Args.Params) > 0 {
		dec := json.NewDecoder(bytes.NewReader(a.Args.Params))
		dec.DisallowUnknownFields()
		if err := dec.Decode(&p); err != nil {
			return "", "", schema("params: " + err.Error())
		}
	}
	var src string
	if tool.Via != "" {
		pl, err := PlanEditorOp(a.Args.Server, a.Args.Tool, p)
		if err != nil || pl.Channel != "mcp" {
			return "", "", gate("script_denied: no mcp plan")
		}
		src = pl.Script
	} else {
		if d.Scripts == nil {
			return "", "", gate("script_denied: no script source")
		}
		var ok bool
		if src, ok = d.Scripts(a.Args.ScriptRef); !ok {
			return "", "", gate("script_denied: script_ref unresolved")
		}
	}
	out := p.OutDir
	if out == "" {
		out = d.OutDir
	}
	if err := ScanScript(src, out); err != nil {
		return "", "", err
	}
	return src, out, nil
}

// judge decides the verdict from the disjoint evidence only.
func (d *Dispatcher) judge(ctx context.Context, a Action, res CallResult) (string, string) {
	var need []string
	for _, e := range a.Expect {
		switch e.Kind {
		case "receipt", "visual", "readback":
			need = append(need, e.Kind)
		}
	}
	if len(need) == 0 || d.Verifier == nil {
		return "UNVERIFIED", "receipt"
	}
	v := d.Verifier.Verify(ctx, a, res)
	get := map[string]*bool{"receipt": v.Receipt, "visual": v.Visual, "readback": v.Readback}
	missing := ""
	for _, k := range need {
		switch r := get[k]; {
		case r != nil && !*r:
			if k == "visual" {
				return "SIDE_EFFECT", k
			}
			return "NO_EFFECT", k
		case r == nil && missing == "":
			missing = k
		}
	}
	if missing != "" {
		return "UNVERIFIED", missing
	}
	return "EFFECT_OK", need[len(need)-1]
}

func (d *Dispatcher) runGUI(ctx context.Context, a Action, srv ServerSpec) (Outcome, error) {
	var p EditorParams
	if len(a.Args.Params) > 0 {
		dec := json.NewDecoder(bytes.NewReader(a.Args.Params))
		dec.DisallowUnknownFields()
		if err := dec.Decode(&p); err != nil {
			return d.refuse(a, schema("params: "+err.Error()), "admit"), nil
		}
	}
	if a.Args.Server == "godot-beckett" {
		if err := GuardGodotProject(p.ProjectPath, d.ScratchDir, d.Reg.OZRoots); err != nil {
			return d.refuse(a, err, "admit"), nil
		}
	}
	p.OZRoots = d.Reg.OZRoots
	pl, err := PlanEditorOp(a.Args.Server, a.Args.Tool, p)
	if err != nil || pl.Channel != "gui" {
		return d.refuse(a, gate("no_gui_plan"), "admit"), nil
	}
	if d.GUI == nil {
		return Unwired{}.Execute(ctx, a)
	}
	out := Outcome{Fallback: "gui"}
	for _, pa := range pl.Actions {
		o, err := d.GUI.Execute(ctx, pa)
		if err != nil {
			return Outcome{}, err
		}
		out.FallbackSteps = append(out.FallbackSteps, o.Step)
		out.Step = o.Step
		if o.Step.RC != 0 || (pa.Risk != "read" && o.Step.Verdict != "EFFECT_OK") {
			break
		}
	}
	out.Step.Step = a.Step
	return out, nil
}

// guardGodotCall applies GuardGodotProject at admission to every godot-beckett
// call that names a project_path; a write/irrev call must name one.
func (d *Dispatcher) guardGodotCall(a Action) error {
	var p map[string]json.RawMessage
	if len(a.Args.Params) > 0 {
		if err := json.Unmarshal(a.Args.Params, &p); err != nil {
			return schema("params must be an object")
		}
	}
	raw, have := p["project_path"]
	if !have && a.Risk == "read" {
		return nil
	}
	var pp string
	if have {
		if err := json.Unmarshal(raw, &pp); err != nil {
			return schema("project_path must be a string")
		}
	}
	return GuardGodotProject(pp, d.ScratchDir, d.Reg.OZRoots)
}

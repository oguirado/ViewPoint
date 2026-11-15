package mcp

import (
	"fmt"
	"regexp"
)

// EditorParams are the closed parameters of the editor operations.
type EditorParams struct {
	Object      string   `json:"object,omitempty"`
	From        *[2]int  `json:"from,omitempty"`
	To          *[2]int  `json:"to,omitempty"`
	ExpectEq    string   `json:"expect_eq,omitempty"`
	ProjectPath string   `json:"project_path,omitempty"`
	Property    string   `json:"property,omitempty"`
	NodeType    string   `json:"node_type,omitempty"`
	ValueSha256 string   `json:"value_sha256,omitempty"`
	ValueLen    uint32   `json:"value_len,omitempty"`
	Workspace   string   `json:"workspace,omitempty"`
	Area        string   `json:"area,omitempty"`
	Factor      int      `json:"factor,omitempty"`
	OutDir      string   `json:"out_dir,omitempty"`
	OZRoots     []string `json:"-"`
}

// Plan is the lowering of an editor op: a GUI action list or a bpy script.
type Plan struct {
	Channel string // "gui" | "mcp"
	Actions []Action
	Script  string
}

var (
	reHex64 = regexp.MustCompile(`^[0-9a-f]{64}$`)
	reWS    = regexp.MustCompile(`^[A-Za-z0-9 _.-]{1,64}$`)
	reArea  = regexp.MustCompile(`^[A-Z_0-9]{1,32}$`)
)

func act(step uint32, op, risk string, t *Target, ar Args, ex ...Expect) Action {
	m := "L1"
	if risk == "read" {
		m = "L4"
	}
	return Action{Schema: SchemaAction, Step: step, Op: op, Target: t, Args: ar, Pre: Pre{MatchMin: m}, Expect: append([]Expect{}, ex...),
		Risk: risk, Fallback: "none", OnFail: "stop", Policy: "script", ReasonSha: "00000000"}
}

func a11y(role, name string) *Target {
	return &Target{By: "a11y", A11y: &A11yRef{Role: role, Name: name}}
}

func typed(p EditorParams) (Args, error) {
	if !reHex64.MatchString(p.ValueSha256) || p.ValueLen == 0 {
		return Args{}, fmt.Errorf("typed value needs value_sha256 and value_len (no literal text)")
	}
	return Args{TextRef: "plan:editor_value", TextSha256: p.ValueSha256, Len: p.ValueLen}, nil
}

// PlanEditorOp lowers one editor operation. Blender gizmo drag and the Godot
// editor ops are GUI plans; workspace switch / area split are bpy scripts.
func PlanEditorOp(server, op string, p EditorParams) (Plan, error) {
	godot := server == "godot-beckett"
	if godot {
		for _, r := range append(append([]string{}, defaultOZ...), p.OZRoots...) {
			if r != "" && p.ProjectPath != "" && under(p.ProjectPath, r) {
				return Plan{}, gate("operator_session: OZ tree")
			}
		}
		if p.ProjectPath == "" {
			return Plan{}, gate("operator_session: project_path required")
		}
	}
	obs := act(1, "observe", "read", nil, Args{})
	switch {
	case server == "blender-gui" && op == "gizmo_drag":
		if p.From == nil || p.To == nil || p.Object == "" {
			return Plan{}, schema("gizmo_drag needs object, from, to")
		}
		drag := act(2, "drag", "write", &Target{By: "xy", XY: p.From}, Args{To: &Target{By: "xy", XY: p.To}, Steps: 8})
		asrt := act(3, "assert", "read", nil, Args{},
			Expect{Kind: "readback", Readback: &Readback{Via: "mcp:blender-production.get_transform", Query: p.Object, Eq: p.ExpectEq}},
			Expect{Kind: "visual", Visual: &Visual{Via: "s4", Regions: []string{"viewport"}, ForbidChangeOutside: true}})
		return Plan{Channel: "gui", Actions: []Action{obs, drag, asrt}}, nil
	case godot && op == "inspector_edit":
		ar, err := typed(p)
		if err != nil || p.Property == "" {
			return Plan{}, schema("inspector_edit needs property and hashed value")
		}
		return Plan{Channel: "gui", Actions: []Action{obs,
			act(2, "click", "write", a11y("text", p.Property), Args{Button: 1, Count: 1}),
			act(3, "key", "write", nil, Args{Chord: "ctrl+a"}),
			act(4, "type", "write", nil, ar),
			act(5, "key", "write", nil, Args{Chord: "Return"}),
			act(6, "assert", "read", nil, Args{},
				Expect{Kind: "readback", Readback: &Readback{Via: "headless_tscn_reopen", Query: p.ProjectPath + "#" + p.Property, Eq: p.ValueSha256}},
				Expect{Kind: "visual", Visual: &Visual{Via: "s4", Regions: []string{"inspector"}, ForbidChangeOutside: true}})}}, nil
	case godot && op == "add_child_node":
		ar, err := typed(p)
		if err != nil || p.NodeType == "" {
			return Plan{}, schema("add_child_node needs node_type and hashed name")
		}
		return Plan{Channel: "gui", Actions: []Action{obs,
			act(2, "click", "write", a11y("tree", "Scene"), Args{Button: 1, Count: 1}),
			act(3, "key", "write", nil, Args{Chord: "ctrl+a"}),
			act(4, "type", "write", nil, ar),
			act(5, "key", "write", nil, Args{Chord: "Return"}),
			act(6, "assert", "read", nil, Args{},
				Expect{Kind: "readback", Readback: &Readback{Via: "headless_tscn_reopen", Query: p.ProjectPath + "#node:" + p.NodeType, Eq: p.ValueSha256}},
				Expect{Kind: "appear", Visual: &Visual{Via: "s4", Regions: []string{"scene_tree"}}})}}, nil
	case godot && (op == "run_scene" || op == "stop_scene"):
		key, eq := "F5", "+1"
		if op == "stop_scene" {
			key, eq = "F8", "baseline"
		}
		return Plan{Channel: "gui", Actions: []Action{obs,
			act(2, "key", "write", nil, Args{Chord: key}),
			act(3, "wait", "read", nil, Args{MaxFrames: 30, SettleK: 3}),
			act(4, "assert", "read", nil, Args{},
				Expect{Kind: "readback", Readback: &Readback{Via: "proc_count", Query: "godot", Eq: eq}},
				Expect{Kind: "visual", Visual: &Visual{Via: "s4", Regions: []string{"output_panel"}}})}}, nil
	case godot && op == "read_output":
		return Plan{Channel: "gui", Actions: []Action{obs,
			act(2, "assert", "read", nil, Args{}, Expect{Kind: "visual", Visual: &Visual{Via: "s4", Regions: []string{"output_panel"}}})}}, nil
	case server == "blender-gui" && op == "workspace_switch":
		if !reWS.MatchString(p.Workspace) {
			return Plan{}, schema("workspace name")
		}
		return Plan{Channel: "mcp", Script: "import bpy\nws = bpy.data.workspaces.get(\"" + p.Workspace + "\")\nif ws is None:\n    raise ValueError(\"no such workspace\")\nwin = bpy.context.window_manager.windows[0]\nwith bpy.context.temp_override(window=win):\n    win.workspace = ws\n"}, nil
	case server == "blender-gui" && op == "area_split":
		if !reArea.MatchString(p.Area) || p.Factor < 1 || p.Factor > 999 {
			return Plan{}, schema("area/factor")
		}
		return Plan{Channel: "mcp", Script: fmt.Sprintf("import bpy\nwin = bpy.context.window_manager.windows[0]\narea = next(a for a in win.screen.areas if a.type == \"%s\")\nwith bpy.context.temp_override(window=win, area=area):\n    bpy.ops.screen.area_split(direction=\"VERTICAL\", factor=%d / 1000)\n", p.Area, p.Factor)}, nil
	}
	return Plan{}, gate("no_plan: " + server + "/" + op)
}

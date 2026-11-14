// Package action holds the vqa.action/v1, vqa.act-plan/v1 and vqa.act-step/v1
// types (DESIGN_CU §4) and their strict, integer-only, closed-enum decoder.
package action

const (
	SchemaAction = "vqa.action/v1"
	SchemaPlan   = "vqa.act-plan/v1"
	SchemaStep   = "vqa.act-step/v1"
)

type A11yRef struct {
	Role string `json:"role"`
	Name string `json:"name"`
	Occ  int    `json:"occ"`
	Path string `json:"path,omitempty"`
}

type MCPRef struct {
	Server string `json:"server"`
	Object string `json:"object"`
}

type Target struct {
	By     string   `json:"by"`
	A11y   *A11yRef `json:"a11y,omitempty"`
	Region string   `json:"region,omitempty"`
	Mark   *uint32  `json:"mark,omitempty"`
	MCP    *MCPRef  `json:"mcp,omitempty"`
	XY     *[2]int  `json:"xy,omitempty"`
}

type A11yMatch struct {
	RID    string   `json:"rid,omitempty"`
	Role   string   `json:"role,omitempty"`
	Name   string   `json:"name,omitempty"`
	States []string `json:"states,omitempty"`
}

type ReceiptSpec struct {
	Path       string `json:"path"`
	MinBytes   uint32 `json:"min_bytes"`
	TestImport string `json:"test_import,omitempty"`
}

type VisualSpec struct {
	Via                 string   `json:"via"`
	Regions             []string `json:"regions"`
	ForbidChangeOutside bool     `json:"forbid_change_outside"`
}

type ReadbackSpec struct {
	Via   string `json:"via"`
	Query string `json:"query"`
	Eq    string `json:"eq"`
}

// Expect is one disjoint-channel expectation. ROI is "target" or [x,y,w,h].
type Expect struct {
	Kind         string        `json:"kind"`
	ROI          *ROI          `json:"roi,omitempty"`
	WithinFrames *uint32       `json:"within_frames,omitempty"`
	SpillPPM     *uint32       `json:"spill_ppm,omitempty"`
	ScreenID     string        `json:"screen_id,omitempty"`
	A11y         *A11yMatch    `json:"a11y,omitempty"`
	Receipt      *ReceiptSpec  `json:"receipt,omitempty"`
	Visual       *VisualSpec   `json:"visual,omitempty"`
	Readback     *ReadbackSpec `json:"readback,omitempty"`
}

type Args struct {
	Button     *int           `json:"button,omitempty"`
	Count      *int           `json:"count,omitempty"`
	Mods       []string       `json:"mods,omitempty"`
	TextRef    string         `json:"text_ref,omitempty"`
	TextSHA256 string         `json:"text_sha256,omitempty"`
	Len        *uint32        `json:"len,omitempty"`
	Chord      string         `json:"chord,omitempty"`
	To         *Target        `json:"to,omitempty"`
	Steps      *int           `json:"steps,omitempty"`
	DX         *int           `json:"dx,omitempty"`
	DY         *int           `json:"dy,omitempty"`
	Until      *Expect        `json:"until,omitempty"`
	MaxFrames  *uint32        `json:"max_frames,omitempty"`
	SettleK    *int           `json:"settle_k,omitempty"`
	Server     string         `json:"server,omitempty"`
	Tool       string         `json:"tool,omitempty"`
	ScriptRef  string         `json:"script_ref,omitempty"`
	Params     map[string]any `json:"params,omitempty"`
	TimeoutMS  *uint32        `json:"timeout_ms,omitempty"`
}

type Pre struct {
	ScreenIs string `json:"screen_is,omitempty"`
	MatchMin string `json:"match_min"`
}

type Action struct {
	Schema    string   `json:"schema"`
	Step      uint32   `json:"step"`
	Op        string   `json:"op"`
	Target    *Target  `json:"target,omitempty"`
	Args      Args     `json:"args"`
	Pre       Pre      `json:"pre"`
	Expect    []Expect `json:"expect"`
	Risk      string   `json:"risk"`
	Allow     *string  `json:"allow"`
	Fallback  string   `json:"fallback"`
	OnFail    string   `json:"on_fail"`
	Policy    string   `json:"policy"`
	ReasonSHA string   `json:"reason_sha8"`
}

type Plan struct {
	Schema           string   `json:"schema"`
	App              string   `json:"app"`
	Scene            string   `json:"scene"`
	Grid             string   `json:"grid"`
	SourceDistillate *string  `json:"source_distillate_sha256"`
	OperatorPin      *string  `json:"operator_pin_sha256"`
	Actions          []Action `json:"actions"`
}

type Located struct {
	By         string `json:"by"`
	RID        string `json:"rid,omitempty"`
	Region     string `json:"region,omitempty"`
	BBox       [4]int `json:"bbox_px"`
	MatchLevel string `json:"match_level"`
}

type A11yDelta struct {
	Added   []string `json:"added"`
	Removed []string `json:"removed"`
	Moved   []string `json:"moved"`
}

type MCPEvidence struct {
	Server    string `json:"server"`
	Tool      string `json:"tool"`
	ResultSHA string `json:"result_sha256"`
	Untrusted bool   `json:"untrusted"`
}

type Evidence struct {
	Regions   []string     `json:"regions"`
	Frames    []string     `json:"frames"`
	A11yDelta A11yDelta    `json:"a11y_delta"`
	MCP       *MCPEvidence `json:"mcp,omitempty"`
	Receipt   string       `json:"receipt_sha256,omitempty"`
}

type Budget struct {
	Steps  uint32 `json:"steps"`
	Tok    uint32 `json:"tok"`
	WallMS uint32 `json:"wall_ms"`
}

// Step is one line of act_steps.jsonl (vqa.act-step/v1).
type Step struct {
	Schema       string   `json:"schema"`
	Step         uint32   `json:"step"`
	ActionSHA    string   `json:"action_sha256"`
	Op           string   `json:"op"`
	Located      Located  `json:"located"`
	ActuatorSeq  uint32   `json:"actuator_seq"`
	RIBefore     uint32   `json:"ri_before"`
	RIAfter      uint32   `json:"ri_after"`
	Settle       uint32   `json:"settle_frames"`
	Unsettled    bool     `json:"unsettled"`
	ScreenBefore string   `json:"screen_before"`
	ScreenAfter  string   `json:"screen_after"`
	Verdict      string   `json:"verdict"`
	Evidence     Evidence `json:"evidence"`
	InputSHA     string   `json:"input_sha256"`
	Budget       Budget   `json:"budget"`
	Stop         *string  `json:"stop"`
	RC           int      `json:"rc"`
}

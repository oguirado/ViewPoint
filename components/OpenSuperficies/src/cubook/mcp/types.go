// Package mcp is the MCP action surface of the CU layer: a stdio JSON-RPC
// server that turns each tool call into one vqa.action/v1, plus the downstream
// MCP dispatcher (registry, guards, CAS, GUI fallback plans).
//
// The Action/Step types below mirror DESIGN_CU §4 for the fields L3 needs; the
// executor boundary is the Executor interface so L2 exec plugs in unchanged.
package mcp

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"

	"opensuperficies.local/srf/src/cubook/action"
)

const (
	SchemaAction    = "vqa.action/v1"
	SchemaStep      = "vqa.act-step/v1"
	SchemaMCPResult = "vqa.mcp-result/v1"
	MaxContent      = 256 << 10
)

type A11yRef struct {
	Role string `json:"role"`
	Name string `json:"name"`
	Occ  int    `json:"occ"`
}

type MCPRef struct {
	Server string `json:"server"`
	Object string `json:"object"`
}

type Target struct {
	By     string   `json:"by"`
	A11y   *A11yRef `json:"a11y,omitempty"`
	Region string   `json:"region,omitempty"`
	MCP    *MCPRef  `json:"mcp,omitempty"`
	XY     *[2]int  `json:"xy,omitempty"`
}

type Receipt struct {
	Path     string `json:"path"`
	MinBytes uint32 `json:"min_bytes"`
}

type Visual struct {
	Via                 string   `json:"via"`
	Regions             []string `json:"regions,omitempty"`
	ForbidChangeOutside bool     `json:"forbid_change_outside"`
}

type Readback struct {
	Via   string `json:"via"`
	Query string `json:"query"`
	Eq    string `json:"eq"`
}

type Expect struct {
	Kind         string    `json:"kind"`
	WithinFrames uint32    `json:"within_frames,omitempty"`
	Receipt      *Receipt  `json:"receipt,omitempty"`
	Visual       *Visual   `json:"visual,omitempty"`
	Readback     *Readback `json:"readback,omitempty"`
}

type Args struct {
	Button     int             `json:"button,omitempty"`
	Count      int             `json:"count,omitempty"`
	TextRef    string          `json:"text_ref,omitempty"`
	TextSha256 string          `json:"text_sha256,omitempty"`
	Len        uint32          `json:"len,omitempty"`
	Chord      string          `json:"chord,omitempty"`
	To         *Target         `json:"to,omitempty"`
	Steps      int             `json:"steps,omitempty"`
	DX         int             `json:"dx,omitempty"`
	DY         int             `json:"dy,omitempty"`
	MaxFrames  uint32          `json:"max_frames,omitempty"`
	SettleK    int             `json:"settle_k,omitempty"`
	Server     string          `json:"server,omitempty"`
	Tool       string          `json:"tool,omitempty"`
	ScriptRef  string          `json:"script_ref,omitempty"`
	Params     json.RawMessage `json:"params,omitempty"`
}

type Pre struct {
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
	ReasonSha string   `json:"reason_sha8"`
}

// Sha256 is the action digest. It is L2's: sha256 of the canonical JSON of the
// action as decoded by action.DecodeAction, so AJSON keys agree across L2/L3.
// An action L2 cannot decode has no digest ("").
func (a Action) Sha256() string {
	b, err := json.Marshal(a)
	if err != nil {
		return ""
	}
	la, err := action.DecodeAction(b)
	if err != nil {
		return ""
	}
	s, err := la.SHA()
	if err != nil {
		return ""
	}
	return s
}

// Step, Evidence and MCPEvidence are L2's vqa.act-step/v1 types: L3 emits the
// frozen schema, never a mirror of it.
type (
	Step        = action.Step
	Evidence    = action.Evidence
	MCPEvidence = action.MCPEvidence
)

type MCPResult struct {
	Schema          string `json:"schema"`
	Step            uint32 `json:"step"`
	Server          string `json:"server"`
	Tool            string `json:"tool"`
	Risk            string `json:"risk"`
	ArgsSha         string `json:"args_sha256"`
	ScriptSha       string `json:"script_sha256"`
	Rc              int    `json:"rc"`
	Stage           string `json:"stage"`
	Verdict         string `json:"verdict"`
	McpUntrusted    bool   `json:"mcp_untrusted"`
	McpContentRef   string `json:"mcp_content_ref"`
	McpContentBytes uint32 `json:"mcp_content_bytes"`
	Witness         bool   `json:"witness"`
}

// Outcome is what one Execute produces.
type Outcome struct {
	Step          Step       `json:"step"`
	MCP           *MCPResult `json:"mcp_result,omitempty"`
	Fallback      string     `json:"fallback,omitempty"`
	FallbackSteps []Step     `json:"fallback_steps,omitempty"`
}

// Executor runs one action. Domain refusals are reported in Outcome.Step.Rc
// (nil error); error is reserved for I/O or programming faults.
type Executor interface {
	Execute(ctx context.Context, a Action) (Outcome, error)
}

func hexSum(b []byte) string {
	h := sha256.Sum256(b)
	return hex.EncodeToString(h[:])
}

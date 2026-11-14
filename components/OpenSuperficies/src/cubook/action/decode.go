package action

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"regexp"
	"strings"

	"opensuperficies.local/srf/src/cubook/rc"
)

// ROI is "target" or [x,y,w,h].
type ROI struct {
	Target bool
	Rect   [4]int
}

func (r *ROI) UnmarshalJSON(b []byte) error {
	var s string
	if json.Unmarshal(b, &s) == nil {
		if s != "target" {
			return fmt.Errorf("roi string must be \"target\"")
		}
		r.Target = true
		return nil
	}
	var a []int
	if err := json.Unmarshal(b, &a); err != nil || len(a) != 4 {
		return fmt.Errorf("roi must be \"target\" or [x,y,w,h]")
	}
	copy(r.Rect[:], a)
	return nil
}

func (r ROI) MarshalJSON() ([]byte, error) {
	if r.Target {
		return []byte(`"target"`), nil
	}
	return json.Marshal(r.Rect[:])
}

var (
	hex8   = regexp.MustCompile(`^[0-9a-f]{8}$`)
	hex16  = regexp.MustCompile(`^[0-9a-f]{16}$`)
	hex64  = regexp.MustCompile(`^[0-9a-f]{64}$`)
	region = regexp.MustCompile(`^r[0-9]+:[0-9a-f]{4}$`)
)

var (
	ops       = set("observe", "locate", "click", "type", "key", "drag", "scroll", "wait", "assert", "mcp_call")
	byEnum    = set("a11y", "region", "mark", "mcp", "xy")
	matchMin  = set("L0", "L1", "L2", "L3", "L4")
	expKinds  = set("changed_roi", "unchanged_outside", "screen_is", "a11y_state", "appear", "vanish", "telemetry", "receipt", "visual", "readback")
	risks     = set("read", "write", "irrev")
	fallbacks = set("none", "gui")
	onFails   = set("stop", "retry_once", "ask")
	policies  = set("script", "explore", "judge")
	verdicts  = set("EFFECT_OK", "NO_EFFECT", "WRONG_EFFECT", "CANNOT_SEE", "NO_ACT", "STOPPED", "UNVERIFIED", "SIDE_EFFECT", "MCP_ERROR")
)

func set(v ...string) map[string]bool {
	m := map[string]bool{}
	for _, s := range v {
		m[s] = true
	}
	return m
}

func schemaErr(format string, a ...any) error {
	return rc.New("E_SCHEMA", fmt.Sprintf(format, a...))
}

// rejectFloats walks raw JSON and fails on any non-integer number.
func rejectFloats(data []byte) error {
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber()
	var v any
	if err := dec.Decode(&v); err != nil {
		return schemaErr("invalid json: %v", err)
	}
	if dec.More() {
		return schemaErr("trailing data")
	}
	var walk func(any) error
	walk = func(x any) error {
		switch t := x.(type) {
		case json.Number:
			if strings.ContainsAny(t.String(), ".eE") {
				return schemaErr("non-integer number %s", t.String())
			}
		case map[string]any:
			for _, e := range t {
				if err := walk(e); err != nil {
					return err
				}
			}
		case []any:
			for _, e := range t {
				if err := walk(e); err != nil {
					return err
				}
			}
		}
		return nil
	}
	return walk(v)
}

func strictUnmarshal(data []byte, out any) error {
	if err := rejectFloats(data); err != nil {
		return err
	}
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if err := dec.Decode(out); err != nil {
		return schemaErr("%v", err)
	}
	return nil
}

// DecodeAction strictly decodes and validates one vqa.action/v1.
func DecodeAction(data []byte) (*Action, error) {
	var a Action
	if err := strictUnmarshal(data, &a); err != nil {
		return nil, err
	}
	if err := a.Validate(); err != nil {
		return nil, err
	}
	return &a, nil
}

// DecodePlan strictly decodes and validates one vqa.act-plan/v1.
func DecodePlan(data []byte) (*Plan, error) {
	var p Plan
	if err := strictUnmarshal(data, &p); err != nil {
		return nil, err
	}
	if p.Schema != SchemaPlan {
		return nil, schemaErr("schema must be %s", SchemaPlan)
	}
	if len(p.Actions) == 0 {
		return nil, schemaErr("plan has no actions")
	}
	for i := range p.Actions {
		if err := p.Actions[i].Validate(); err != nil {
			return nil, schemaErr("actions[%d]: %v", i, err)
		}
	}
	return &p, nil
}

// DecodeStep strictly decodes one act_steps.jsonl line.
func DecodeStep(data []byte) (*Step, error) {
	var s Step
	if err := strictUnmarshal(data, &s); err != nil {
		return nil, err
	}
	if s.Schema != SchemaStep {
		return nil, schemaErr("schema must be %s", SchemaStep)
	}
	if !verdicts[s.Verdict] || !hex64.MatchString(s.ActionSHA) || !hex64.MatchString(s.InputSHA) ||
		!hex16.MatchString(s.ScreenBefore) || !hex16.MatchString(s.ScreenAfter) {
		return nil, schemaErr("bad step fields")
	}
	return &s, nil
}

func (t *Target) validate(path string) error {
	if !byEnum[t.By] {
		return schemaErr("%s.by %q not in enum", path, t.By)
	}
	switch t.By {
	case "a11y":
		if t.A11y == nil || t.A11y.Role == "" {
			return schemaErr("%s.a11y required", path)
		}
	case "region":
		if !region.MatchString(t.Region) {
			return schemaErr("%s.region must match r<F>:<m4>", path)
		}
	case "mark":
		if t.Mark == nil {
			return schemaErr("%s.mark required", path)
		}
	case "mcp":
		if t.MCP == nil {
			return schemaErr("%s.mcp required", path)
		}
	case "xy":
		if t.XY == nil {
			return schemaErr("%s.xy required", path)
		}
	}
	return nil
}

func inRange(p *int, lo, hi int) bool { return p == nil || (*p >= lo && *p <= hi) }

// Validate enforces closed enums, bounds and the no-literal-text rule.
func (a *Action) Validate() error {
	if a.Schema != SchemaAction {
		return schemaErr("schema must be %s", SchemaAction)
	}
	if !ops[a.Op] {
		return schemaErr("op %q not in enum", a.Op)
	}
	if !matchMin[a.Pre.MatchMin] {
		return schemaErr("pre.match_min %q not in enum", a.Pre.MatchMin)
	}
	if a.Pre.ScreenIs != "" && !hex16.MatchString(a.Pre.ScreenIs) {
		return schemaErr("pre.screen_is must be 16 hex")
	}
	if !risks[a.Risk] || !fallbacks[a.Fallback] || !onFails[a.OnFail] || !policies[a.Policy] {
		return schemaErr("risk/fallback/on_fail/policy not in enum")
	}
	if a.ReasonSHA != "" && !hex8.MatchString(a.ReasonSHA) {
		return schemaErr("reason_sha8 must be 8 hex")
	}
	if a.Target != nil {
		if err := a.Target.validate("target"); err != nil {
			return err
		}
	}
	g := a.Args
	if !inRange(g.Button, 1, 3) || !inRange(g.Count, 1, 2) || !inRange(g.Steps, 2, 32) ||
		!inRange(g.DX, -32768, 32767) || !inRange(g.DY, -32768, 32767) {
		return schemaErr("args out of bounds (button 1..3, count 1..2, steps 2..32, dx/dy i16)")
	}
	if g.To != nil {
		if err := g.To.validate("args.to"); err != nil {
			return err
		}
	}
	if g.TextSHA256 != "" && !hex64.MatchString(g.TextSHA256) {
		return schemaErr("args.text_sha256 must be 64 hex")
	}
	for i, e := range a.Expect {
		if !expKinds[e.Kind] {
			return schemaErr("expect[%d].kind %q not in enum", i, e.Kind)
		}
		if e.ScreenID != "" && !hex16.MatchString(e.ScreenID) {
			return schemaErr("expect[%d].screen_id must be 16 hex", i)
		}
	}
	if g.Until != nil && !expKinds[g.Until.Kind] {
		return schemaErr("args.until.kind not in enum")
	}
	switch a.Op {
	case "type":
		if g.TextSHA256 == "" || g.Len == nil {
			return schemaErr("type needs text_sha256 and len (no literal text)")
		}
	case "key":
		if g.Chord == "" {
			return schemaErr("key needs chord")
		}
	case "click", "scroll":
		if a.Target == nil {
			return schemaErr("%s needs target", a.Op)
		}
	case "drag":
		if a.Target == nil || g.To == nil || g.Steps == nil {
			return schemaErr("drag needs target, args.to and args.steps")
		}
	}
	return nil
}

// Canonical returns key-sorted compact JSON of any value (floats never occur
// because the types are integer-only).
func Canonical(v any) ([]byte, error) {
	raw, err := json.Marshal(v)
	if err != nil {
		return nil, err
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var g any
	if err := dec.Decode(&g); err != nil {
		return nil, err
	}
	return json.Marshal(g) // maps marshal with sorted keys
}

// SHA256Hex is the hex sha256 of b.
func SHA256Hex(b []byte) string {
	h := sha256.Sum256(b)
	return hex.EncodeToString(h[:])
}

// SHA returns action_sha256: sha256 over the canonical JSON of the action.
func (a *Action) SHA() (string, error) {
	c, err := Canonical(a)
	if err != nil {
		return "", err
	}
	return SHA256Hex(c), nil
}

// ValidVerdict reports whether v is in the closed verdict enum.
func ValidVerdict(v string) bool { return verdicts[v] }

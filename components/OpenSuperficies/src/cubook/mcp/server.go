package mcp

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"regexp"
	"sync/atomic"
)

type Config struct {
	Exec Executor
	CAS  *CAS
}

type Server struct {
	cfg  Config
	step atomic.Uint32
}

func NewServer(c Config) *Server { return &Server{cfg: c} }

type toolDef struct {
	name, desc string
	props      string
}

var tools = []toolDef{
	{"cu_observe", "Observe the screen (read).", `{}`},
	{"cu_locate", "Locate an a11y target (read).", `{"role":{"type":"string"},"name":{"type":"string"},"occ":{"type":"integer"}}`},
	{"cu_click", "Click at x,y.", `{"x":{"type":"integer"},"y":{"type":"integer"},"button":{"type":"integer"},"count":{"type":"integer"}}`},
	{"cu_type", "Type text by hash and length only.", `{"text_ref":{"type":"string"},"text_sha256":{"type":"string"},"len":{"type":"integer"}}`},
	{"cu_key", "Press a key chord.", `{"chord":{"type":"string"}}`},
	{"cu_drag", "Drag from x,y to to_x,to_y.", `{"x":{"type":"integer"},"y":{"type":"integer"},"to_x":{"type":"integer"},"to_y":{"type":"integer"},"steps":{"type":"integer"}}`},
	{"cu_scroll", "Scroll by dx,dy at x,y.", `{"x":{"type":"integer"},"y":{"type":"integer"},"dx":{"type":"integer"},"dy":{"type":"integer"}}`},
	{"cu_wait", "Wait until settled.", `{"max_frames":{"type":"integer"},"settle_k":{"type":"integer"}}`},
	{"cu_assert", "Assert expectations.", `{"expect":{"type":"array"}}`},
	{"cu_mcp_call", "Call a downstream MCP tool via the registry.", `{"server":{"type":"string"},"tool":{"type":"string"},"params":{"type":"object"},"script_ref":{"type":"string"},"risk":{"type":"string"},"fallback":{"type":"string"},"expect":{"type":"array"}}`},
}

type req struct {
	ID     json.RawMessage `json:"id"`
	Method string          `json:"method"`
	Params json.RawMessage `json:"params"`
}

func (s *Server) Serve(r io.Reader, w io.Writer) error {
	sc := bufio.NewScanner(r)
	sc.Buffer(make([]byte, 64<<10), 1<<20)
	enc := func(v any) error {
		b, _ := json.Marshal(v)
		_, err := w.Write(append(b, '\n'))
		return err
	}
	rerr := func(id json.RawMessage, code int, msg string) error {
		return enc(map[string]any{"jsonrpc": "2.0", "id": id, "error": map[string]any{"code": code, "message": msg}})
	}
	for sc.Scan() {
		line := bytes.TrimSpace(sc.Bytes())
		if len(line) == 0 {
			continue
		}
		var q req
		if err := json.Unmarshal(line, &q); err != nil {
			if err := rerr(json.RawMessage("null"), -32700, "parse error"); err != nil {
				return err
			}
			continue
		}
		if q.ID == nil {
			continue // notification
		}
		var err error
		switch q.Method {
		case "initialize":
			err = enc(map[string]any{"jsonrpc": "2.0", "id": q.ID, "result": map[string]any{
				"protocolVersion": "2025-03-26", "capabilities": map[string]any{"tools": map[string]any{}},
				"serverInfo": map[string]any{"name": "srf-cu-mcp", "version": "0.1.0"}}})
		case "ping":
			err = enc(map[string]any{"jsonrpc": "2.0", "id": q.ID, "result": map[string]any{}})
		case "tools/list":
			var l []map[string]any
			for _, t := range tools {
				var props map[string]any
				_ = json.Unmarshal([]byte(t.props), &props)
				l = append(l, map[string]any{"name": t.name, "description": t.desc,
					"inputSchema": map[string]any{"type": "object", "properties": props, "additionalProperties": false}})
			}
			err = enc(map[string]any{"jsonrpc": "2.0", "id": q.ID, "result": map[string]any{"tools": l}})
		case "tools/call":
			err = enc(map[string]any{"jsonrpc": "2.0", "id": q.ID, "result": s.callTool(q.Params)})
		default:
			err = rerr(q.ID, -32601, "method not found")
		}
		if err != nil {
			return err
		}
	}
	if errors.Is(sc.Err(), bufio.ErrTooLong) {
		b, _ := json.Marshal(map[string]any{"jsonrpc": "2.0", "id": nil, "error": map[string]any{"code": -32600, "message": "request too large"}})
		_, _ = w.Write(append(b, '\n'))
		return nil
	}
	return sc.Err()
}

func failResult(rcv int, stop string) map[string]any {
	return map[string]any{"isError": true, "content": []any{map[string]any{"type": "text", "text": stop}},
		"structuredContent": map[string]any{"rc": rcv, "stop": stop}}
}

func (s *Server) callTool(params json.RawMessage) map[string]any {
	var p struct {
		Name string          `json:"name"`
		Args json.RawMessage `json:"arguments"`
	}
	if json.Unmarshal(params, &p) != nil {
		return failResult(2, "E_SCHEMA")
	}
	if len(p.Args) == 0 {
		p.Args = json.RawMessage(`{}`)
	}
	a, err := s.build(p.Name, p.Args)
	if err != nil {
		return failResult(RcOf(err), "E_SCHEMA")
	}
	out, err := s.cfg.Exec.Execute(context.Background(), a)
	if err != nil {
		return failResult(RcOf(err), "E_IO")
	}
	env, _ := json.Marshal(out)
	var sc map[string]any
	_ = json.Unmarshal(env, &sc)
	sc["rc"] = out.Step.RC
	return map[string]any{"isError": out.Step.RC != 0, "content": []any{map[string]any{"type": "text", "text": string(env)}},
		"structuredContent": map[string]any{"step": sc["step"], "mcp_result": sc["mcp_result"], "rc": out.Step.RC, "fallback": sc["fallback"]}}
}

func strict(raw json.RawMessage, v any) error {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		return schema(err.Error())
	}
	return nil
}

var reSha = regexp.MustCompile(`^[0-9a-f]{64}$`)

func inRange(v, lo, hi int) bool { return v >= lo && v <= hi }

// build converts one tool call into exactly one vqa.action/v1.
func (s *Server) build(name string, raw json.RawMessage) (Action, error) {
	var c struct {
		Step      *uint32         `json:"step"`
		X         *int            `json:"x"`
		Y         *int            `json:"y"`
		ToX       *int            `json:"to_x"`
		ToY       *int            `json:"to_y"`
		Button    int             `json:"button"`
		Count     int             `json:"count"`
		TextRef   string          `json:"text_ref"`
		TextSha   string          `json:"text_sha256"`
		Len       uint32          `json:"len"`
		Chord     string          `json:"chord"`
		Steps     int             `json:"steps"`
		DX        int             `json:"dx"`
		DY        int             `json:"dy"`
		MaxFrames uint32          `json:"max_frames"`
		SettleK   int             `json:"settle_k"`
		Role      string          `json:"role"`
		Nm        string          `json:"name"`
		Occ       int             `json:"occ"`
		Expect    []Expect        `json:"expect"`
		Server    string          `json:"server"`
		Tool      string          `json:"tool"`
		Params    json.RawMessage `json:"params"`
		ScriptRef string          `json:"script_ref"`
		Risk      string          `json:"risk"`
		Fallback  string          `json:"fallback"`
	}
	if err := strictFields(name, raw, &c); err != nil {
		return Action{}, err
	}
	step := s.step.Add(1)
	if c.Step != nil {
		step = *c.Step
	}
	a := Action{Schema: SchemaAction, Step: step, Pre: Pre{MatchMin: "L1"}, Expect: []Expect{}, Fallback: "none", OnFail: "stop", Policy: "script",
		ReasonSha: hexSum(append([]byte(name), raw...))[:8]}
	xy := func() (*Target, error) {
		if c.X == nil || c.Y == nil {
			return nil, schema("x,y required")
		}
		return &Target{By: "xy", XY: &[2]int{*c.X, *c.Y}}, nil
	}
	var err error
	switch name {
	case "cu_observe":
		a.Op, a.Risk = "observe", "read"
	case "cu_locate":
		a.Op, a.Risk = "locate", "read"
		a.Target = &Target{By: "a11y", A11y: &A11yRef{Role: c.Role, Name: c.Nm, Occ: c.Occ}}
		if c.Role == "" {
			return a, schema("role required")
		}
	case "cu_click":
		a.Op, a.Risk = "click", "write"
		if a.Target, err = xy(); err != nil {
			return a, err
		}
		if (c.Button != 0 && !inRange(c.Button, 1, 3)) || (c.Count != 0 && !inRange(c.Count, 1, 2)) {
			return a, schema("button/count bounds")
		}
		a.Args.Button, a.Args.Count = c.Button, c.Count
	case "cu_type":
		a.Op, a.Risk = "type", "write"
		if c.TextRef == "" || !reSha.MatchString(c.TextSha) || c.Len == 0 {
			return a, schema("type needs text_ref, text_sha256, len")
		}
		a.Args.TextRef, a.Args.TextSha256, a.Args.Len = c.TextRef, c.TextSha, c.Len
	case "cu_key":
		a.Op, a.Risk = "key", "write"
		if c.Chord == "" {
			return a, schema("chord required")
		}
		a.Args.Chord = c.Chord
	case "cu_drag":
		a.Op, a.Risk = "drag", "write"
		if a.Target, err = xy(); err != nil {
			return a, err
		}
		if c.ToX == nil || c.ToY == nil || !inRange(c.Steps, 2, 32) {
			return a, schema("drag needs to_x,to_y and steps 2..32")
		}
		a.Args.To, a.Args.Steps = &Target{By: "xy", XY: &[2]int{*c.ToX, *c.ToY}}, c.Steps
	case "cu_scroll":
		a.Op, a.Risk = "scroll", "write"
		if a.Target, err = xy(); err != nil {
			return a, err
		}
		if !inRange(c.DX, -32768, 32767) || !inRange(c.DY, -32768, 32767) {
			return a, schema("dx/dy i16")
		}
		a.Args.DX, a.Args.DY = c.DX, c.DY
	case "cu_wait":
		a.Op, a.Risk = "wait", "read"
		a.Args.MaxFrames, a.Args.SettleK = c.MaxFrames, c.SettleK
	case "cu_assert":
		a.Op, a.Risk = "assert", "read"
		if len(c.Expect) == 0 {
			return a, schema("expect required")
		}
		a.Expect = c.Expect
	case "cu_mcp_call":
		a.Op = "mcp_call"
		if c.Server == "" || c.Tool == "" || (c.Risk != "read" && c.Risk != "write" && c.Risk != "irrev") || (c.Fallback != "" && c.Fallback != "none" && c.Fallback != "gui") {
			return a, schema("mcp_call needs server, tool, risk; fallback none|gui")
		}
		a.Risk, a.Args.Server, a.Args.Tool, a.Args.Params, a.Args.ScriptRef = c.Risk, c.Server, c.Tool, c.Params, c.ScriptRef
		if c.Fallback != "" {
			a.Fallback = c.Fallback
		}
		if c.Expect != nil {
			a.Expect = c.Expect
		}
	default:
		return a, schema("unknown tool")
	}
	// An action L2 cannot digest (e.g. float params) is refused here: no step
	// is ever emitted with an empty action_sha256.
	if a.Sha256() == "" {
		return a, schema("action not decodable by L2 (float params?)")
	}
	return a, nil
}

// strictFields rejects keys outside the tool's own property list, then decodes.
func strictFields(name string, raw json.RawMessage, c any) error {
	var keys map[string]json.RawMessage
	if err := json.Unmarshal(raw, &keys); err != nil {
		return schema("arguments must be an object")
	}
	for _, t := range tools {
		if t.name != name {
			continue
		}
		var props map[string]any
		_ = json.Unmarshal([]byte(t.props), &props)
		for k := range keys {
			if _, ok := props[k]; !ok && k != "step" {
				return schema("unknown field " + k)
			}
		}
	}
	return strict(raw, c)
}

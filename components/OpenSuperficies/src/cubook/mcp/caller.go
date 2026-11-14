package mcp

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"time"

	"opensuperficies.local/srf/src/cubook/action"
)

// ErrUnavailable is the only error that licenses the GUI fallback.
var ErrUnavailable = errors.New("E_MCP_UNAVAILABLE")

type CallResult struct {
	Content     []byte
	ServerError string
}

type Caller interface {
	Call(ctx context.Context, endpoint, server, tool string, args json.RawMessage) (CallResult, error)
}

type CallRecord struct{ Endpoint, Server, Tool string }

type fakeTool struct {
	Content string `json:"content"`
	Error   string `json:"error"`
}

type fakeFile struct {
	Schema string              `json:"schema"`
	Server string              `json:"server"`
	Tools  map[string]fakeTool `json:"tools"`
}

type injected struct {
	r   CallResult
	err error
}

// FakeCaller serves fixtures from testdata/cu/mcp/fake_*.json (no sockets).
type FakeCaller struct {
	Calls []CallRecord
	fx    map[string]fakeFile
	inj   map[string]injected
}

func NewFakeCaller(files ...string) (*FakeCaller, error) {
	f := &FakeCaller{fx: map[string]fakeFile{}, inj: map[string]injected{}}
	for _, p := range files {
		b, err := os.ReadFile(p)
		if err != nil {
			return nil, schema("fake fixture unreadable")
		}
		dec := json.NewDecoder(bytes.NewReader(b))
		dec.DisallowUnknownFields()
		var ff fakeFile
		if err := dec.Decode(&ff); err != nil || ff.Schema != "os.cu.mcp-fake/v1" {
			return nil, schema("fake fixture schema")
		}
		f.fx[ff.Server] = ff
	}
	return f, nil
}

func (f *FakeCaller) Inject(server, tool string, r CallResult, err error) {
	f.inj[server+"/"+tool] = injected{r, err}
}

func (f *FakeCaller) Call(_ context.Context, ep, server, tool string, _ json.RawMessage) (CallResult, error) {
	f.Calls = append(f.Calls, CallRecord{ep, server, tool})
	if i, ok := f.inj[server+"/"+tool]; ok {
		return i.r, i.err
	}
	t, ok := f.fx[server].Tools[tool]
	if !ok || t.Error == ErrUnavailable.Error() {
		return CallResult{}, ErrUnavailable
	}
	if t.Error != "" {
		return CallResult{ServerError: t.Error}, nil
	}
	return CallResult{Content: []byte(t.Content)}, nil
}

// HTTPCaller dials a capsule forwarder with JSON-RPC tools/call. Any transport
// failure is E_MCP_UNAVAILABLE. Never exercised by tests (no sockets).
type HTTPCaller struct{ Client *http.Client }

func (h HTTPCaller) Call(ctx context.Context, ep, _, tool string, args json.RawMessage) (CallResult, error) {
	if args == nil {
		args = json.RawMessage(`{}`)
	}
	body, _ := json.Marshal(map[string]any{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": map[string]any{"name": tool, "arguments": args}})
	cl := h.Client
	if cl == nil {
		cl = &http.Client{Timeout: 30 * time.Second}
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, "http://"+ep+"/", bytes.NewReader(body))
	if err != nil {
		return CallResult{}, ErrUnavailable
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := cl.Do(req)
	if err != nil {
		return CallResult{}, ErrUnavailable
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, MaxContent+1024))
	if err != nil {
		return CallResult{}, ErrUnavailable
	}
	var r struct {
		Result struct {
			Content []struct {
				Text string `json:"text"`
			} `json:"content"`
			IsError bool `json:"isError"`
		} `json:"result"`
		Error *struct{} `json:"error"`
	}
	if json.Unmarshal(raw, &r) != nil || r.Error != nil || r.Result.IsError {
		return CallResult{ServerError: "E_TOOL_FAILED"}, nil
	}
	var out []byte
	for _, c := range r.Result.Content {
		out = append(out, c.Text...)
	}
	return CallResult{Content: out}, nil
}

// MockGUI is a test-only Executor for --mock-gui: it records nothing and has no
// verifier, so it reports UNVERIFIED and must never be wired in production.
type MockGUI struct{}

func (MockGUI) Execute(_ context.Context, a Action) (Outcome, error) {
	return Outcome{Step: newStep(a, "UNVERIFIED")}, nil
}

// Unwired refuses every GUI action until the L2 exec is bound.
type Unwired struct{}

func (Unwired) Execute(_ context.Context, a Action) (Outcome, error) {
	s := newStep(a, "NO_ACT")
	s.RC, s.Stop = 5, strp("gui_executor_unwired")
	return Outcome{Step: s}, nil
}

const zeroScreen = "0000000000000000"

func strp(s string) *string { return &s }

// newStep builds a complete vqa.act-step/v1 for an action L3 did not actuate
// itself: frame fields are zero-valued and input_sha256 follows L2's formula
// (sha256(action_sha256 \n digest \n canonical(located))) over the hash of the
// call params, so it is never the empty-string hash.
func newStep(a Action, verdict string) Step {
	sha := a.Sha256()
	loc := action.Located{By: "xy", MatchLevel: "NONE"}
	c, _ := action.Canonical(loc)
	return Step{
		Schema: SchemaStep, Step: a.Step, ActionSHA: sha, Op: a.Op, Located: loc,
		ScreenBefore: zeroScreen, ScreenAfter: zeroScreen, Verdict: verdict,
		Evidence: Evidence{Regions: []string{}, Frames: []string{},
			A11yDelta: action.A11yDelta{Added: []string{}, Removed: []string{}, Moved: []string{}}},
		InputSHA: hexSum([]byte(sha + "\n" + hexSum(a.Args.Params) + "\n" + string(c))),
	}
}

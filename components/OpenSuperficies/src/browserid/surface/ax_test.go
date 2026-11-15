package surface

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"strings"
	"testing"
)

type axFakeRunner struct {
	*fakeRunner
	reply   []byte
	err     error
	request []byte
	argv    []string
}

func (r *axFakeRunner) Run(ctx context.Context, a []string, b []byte) ([]byte, error) {
	if slices.Contains(a, "-I") {
		r.request = slices.Clone(b)
		r.argv = slices.Clone(a)
		return r.reply, r.err
	}
	return r.fakeRunner.Run(ctx, a, b)
}
func axFixture() map[string]any {
	return map[string]any{
		"window": map[string]any{"id": "w1", "x": 0, "y": 0, "w": 1280, "h": 900, "title": "Example https://example.org/a", "url": "https://example.org/a"}, "frame_seq": 1, "scroll": map[string]any{"x": 0, "y": 0}, "truncated": false, "skipped_frames": 0,
		"elements": []any{map[string]any{"id": "ab12cd34", "role": "textbox", "name": "Search", "box": []any{10, 80, 120, 30}, "region": "main", "actionable": true, "focused": false, "states": map[string]any{"enabled": true, "focused": false, "expanded": nil, "checked": nil, "selected": nil}, "container": map[string]any{"id": "aa12", "ordinal": 0}, "value_len": 3}},
	}
}
func TestAXReplyValidation(t *testing.T) {
	cases := []struct {
		name   string
		mutate func(map[string]any)
		code   int
		text   string
	}{
		{"good", func(m map[string]any) {}, 0, ""},
		{"leak", func(m map[string]any) { m["value"] = "secret" }, 6, ""},
		{"outside", func(m map[string]any) { m["elements"].([]any)[0].(map[string]any)["box"] = []int{1270, 80, 120, 30} }, 6, ""},
		{"long-name", func(m map[string]any) { m["elements"].([]any)[0].(map[string]any)["name"] = strings.Repeat("x", 121) }, 5, ""},
		{"unicode-name", func(m map[string]any) { m["elements"].([]any)[0].(map[string]any)["name"] = strings.Repeat("é", 120) }, 0, ""},
		{"nested-leak", func(m map[string]any) {
			m["elements"].([]any)[0].(map[string]any)["states"].(map[string]any)["value"] = "secret"
		}, 6, ""},
		{"mixed-state-refused", func(m map[string]any) {
			m["elements"].([]any)[0].(map[string]any)["states"].(map[string]any)["checked"] = "mixed"
		}, 5, ""},
		{"bad-state", func(m map[string]any) {
			m["elements"].([]any)[0].(map[string]any)["states"].(map[string]any)["enabled"] = "true"
		}, 5, ""},
		{"wrong-window", func(m map[string]any) { m["window"].(map[string]any)["id"] = "w2" }, 5, ""},
		{"wrong-geometry", func(m map[string]any) { m["window"].(map[string]any)["x"] = 1 }, 5, ""},
		{"wrong-role-value-len", func(m map[string]any) { m["elements"].([]any)[0].(map[string]any)["role"] = "button" }, 6, ""},
		{"negative-box", func(m map[string]any) { m["elements"].([]any)[0].(map[string]any)["box"] = []int{-1, 80, 120, 30} }, 6, ""},
		{"negative-skipped-frames", func(m map[string]any) { m["skipped_frames"] = -1 }, 5, ""},
		{"missing-skipped-frames", func(m map[string]any) { delete(m, "skipped_frames") }, 5, ""},
		{"iframes-skipped", func(m map[string]any) { m["skipped_frames"] = 2 }, 0, ""},
		{"missing", func(m map[string]any) { delete(m, "scroll") }, 5, ""},
		{"null", func(m map[string]any) { m["truncated"] = nil }, 5, ""},
		{"seq", func(m map[string]any) { m["frame_seq"] = 2 }, 5, ""},
		{"too-many", func(m map[string]any) {
			a := m["elements"].([]any)
			for len(a) < 301 {
				a = append(a, a[0])
			}
			m["elements"] = a
		}, 5, ""},
		{"text", func(m map[string]any) { m["text"] = map[string]any{"container_id": "aa12", "bytes": 2, "text": "é"} }, 0, "aa12"},
		{"text-overflow", func(m map[string]any) {
			m["text"] = map[string]any{"container_id": "aa12", "bytes": 65537, "text": strings.Repeat("x", 65537)}
		}, 5, "aa12"},
		{"text-byte-mismatch", func(m map[string]any) { m["text"] = map[string]any{"container_id": "aa12", "bytes": 1, "text": "é"} }, 5, "aa12"},
		{"text-wrong-container", func(m map[string]any) { m["text"] = map[string]any{"container_id": "bb12", "bytes": 1, "text": "x"} }, 5, "aa12"},
		{"unsolicited-text", func(m map[string]any) { m["text"] = map[string]any{"container_id": "aa12", "bytes": 1, "text": "x"} }, 5, ""},
		{"refused", func(m map[string]any) { clear(m); m["error"] = "refused"; m["code"] = 6 }, 6, ""},
		{"absent", func(m map[string]any) { clear(m); m["error"] = "not_ready"; m["code"] = 4 }, 4, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			tool, r, _, _ := setup(t)
			c := start(t, tool)
			s, e := tool.load(c.Container)
			if e != nil {
				t.Fatal(e)
			}
			s.Seq["w1"] = 1
			if e = tool.save(s); e != nil {
				t.Fatal(e)
			}
			m := axFixture()
			tc.mutate(m)
			b, _ := json.Marshal(m)
			ar := &axFakeRunner{fakeRunner: r, reply: b}
			tool.R = ar
			v, e := tool.AX(context.Background(), c.Container, "w1", tc.text)
			if Code(e) != tc.code {
				t.Fatalf("code %d expected %d", Code(e), tc.code)
			}
			if tc.code == 0 && !json.Valid(v) {
				t.Fatal("invalid output")
			}
			if !slices.Equal(ar.argv[:9], []string{"docker", "exec", "-i", "-u", "10001:10001", c.Container, "python3", "-I", "-c"}) {
				t.Fatal("relay argv")
			}
			var q map[string]any
			if json.Unmarshal(ar.request, &q) != nil || q["op"] != "ax" || q["frame_seq"] != float64(1) || q["window_title_or_index"] != "w1" {
				t.Fatal("request")
			}
		})
	}
}
func TestAXTransportAndFramePrecondition(t *testing.T) {
	tool, r, _, _ := setup(t)
	c := start(t, tool)
	ar := &axFakeRunner{fakeRunner: r}
	tool.R = ar
	if _, e := tool.AX(context.Background(), c.Container, "w1", ""); Code(e) != 4 || ar.argv != nil {
		t.Fatal("missing frame accepted")
	}
	s, _ := tool.load(c.Container)
	s.Seq["w1"] = 1
	_ = tool.save(s)
	for _, tc := range []struct {
		reply []byte
		err   error
		code  int
	}{{nil, errors.New("absent"), 4}, {[]byte(strings.Repeat("x", (16<<20)+1)), nil, 5}, {[]byte(`{"value":"secret"}`), nil, 6}, {[]byte(`{"frame_seq":1,"frame_seq":2}`), nil, 5}} {
		ar.reply = tc.reply
		ar.err = tc.err
		if _, e := tool.AX(context.Background(), c.Container, "w1", ""); Code(e) != tc.code {
			t.Fatal("transport code", Code(e), tc.code)
		}
	}
}

func TestAXWindowLocalAndLimits(t *testing.T) {
	m := axFixture()
	win := m["window"].(map[string]any)
	win["id"] = "w2"
	win["x"] = 640
	win["w"] = 640
	w := Window{ID: "w2", X: 640, W: 640, H: 900}
	elements := make([]any, 300)
	for i := range elements {
		e := map[string]any{}
		for k, v := range m["elements"].([]any)[0].(map[string]any) {
			e[k] = v
		}
		e["id"] = fmt.Sprintf("%08x", i)
		elements[i] = e
	}
	m["elements"] = elements
	m["text"] = map[string]any{"container_id": "aa12", "bytes": 65536, "text": strings.Repeat("é", 32768)}
	raw, _ := json.Marshal(m)
	if _, e := validateAX(raw, w, 1, "aa12"); e != nil {
		t.Fatal("valid boundary reply refused", Code(e))
	}
	elements[0].(map[string]any)["box"] = []int{650, 80, 120, 30}
	raw, _ = json.Marshal(m)
	if _, e := validateAX(raw, w, 1, "aa12"); Code(e) != 6 {
		t.Fatal("display-space box accepted")
	}
}

func TestAXURLRedaction(t *testing.T) {
	m := axFixture()
	win := m["window"].(map[string]any)
	win["url"] = "https://user:pass@[::1]:8443/x?session=abc#access_token=xyz"
	win["title"] = "Page " + win["url"].(string)
	raw, _ := json.Marshal(m)
	out, err := validateAX(raw, Window{ID: "w1", W: 1280, H: 900}, 1, "")
	if err != nil {
		t.Fatal(err)
	}
	var result map[string]any
	_ = json.Unmarshal(out, &result)
	window := result["window"].(map[string]any)
	if window["url"] != "https://[::1]:8443/x" || window["title"] != "Page https://[::1]:8443/x" {
		t.Fatal("URL metadata not redacted")
	}
	if strings.Contains(string(out), "session=") || strings.Contains(string(out), "access_token=") {
		t.Fatal("URL secrets escaped projection")
	}
	if got := titleURL("Page https://user:pass@example.org/a?session=abc#access_token=xyz"); got != "https://example.org/a" {
		t.Fatal("title URL not redacted", got)
	}
}

func TestAXFocusedMismatch(t *testing.T) {
	m := axFixture()
	m["elements"].([]any)[0].(map[string]any)["states"].(map[string]any)["focused"] = true
	raw, _ := json.Marshal(m)
	if _, err := validateAX(raw, Window{ID: "w1", W: 1280, H: 900}, 1, ""); Code(err) != 5 {
		t.Fatal("inconsistent focused states accepted")
	}
}

type urlTitleRunner struct{ *fakeRunner }

func (r urlTitleRunner) Run(ctx context.Context, a []string, b []byte) ([]byte, error) {
	if slices.Contains(a, "getwindowname") {
		return []byte("Page https://user:pass@example.org/a?session=abc#access_token=xyz\n"), nil
	}
	return r.fakeRunner.Run(ctx, a, b)
}
func TestFrameURLRedaction(t *testing.T) {
	tool, r, _, _ := setup(t)
	c := start(t, tool)
	r.pair = pairFixture(1280, 900)
	tool.R = urlTitleRunner{r}
	frame, err := tool.Frame(context.Background(), c.Container, "w1")
	if err != nil {
		t.Fatal(err)
	}
	if frame.Window.URL != "https://example.org/a" || frame.Window.Title != "Page https://example.org/a" {
		t.Fatal("frame URL metadata not redacted")
	}
}

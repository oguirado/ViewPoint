package surface

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"math"
	"net/url"
	"regexp"
	"strings"
	"time"
	"unicode/utf8"
)

const axMaxReply = 16 << 20
const axPython = `import socket, sys, time
try:
 s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
 s.settimeout(5)
 s.connect('/run/newton-tenant/ax.sock')
 s.sendall(sys.stdin.buffer.read(32769))
 s.shutdown(socket.SHUT_WR)
 data = bytearray()
 end = time.monotonic() + 5
 while len(data) <= 16777216:
  remaining = end - time.monotonic()
  if remaining <= 0: raise TimeoutError()
  s.settimeout(remaining)
  chunk = s.recv(min(65536,16777217-len(data)))
  if not chunk: break
  data.extend(chunk)
 s.close()
 if len(data) > 16777216: sys.stdout.write('{"error":"oversize","code":5}')
 else: sys.stdout.buffer.write(data)
except Exception:
 sys.stdout.write('{"error":"not_ready","code":4}')
`

// AX reads the current frame's accessibility metadata through the capsule-owned
// socket. It never accepts a CDP method or allocates a new frame sequence.
func (t Tool) AX(ctx context.Context, c, id, textContainer string) (json.RawMessage, error) {
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	if !capsuleRE.MatchString(c) || !windowRE.MatchString(id) || (textContainer != "" && !axID.MatchString(textContainer)) {
		return nil, fail(2, "invalid AX selector")
	}
	if e := t.ports(); e != nil {
		return nil, e
	}
	unlock, e := t.lock(ctx)
	if e != nil {
		return nil, e
	}
	defer unlock()
	s, e := t.load(c)
	if e != nil {
		return nil, e
	}
	if s.Kind == "program" {
		return nil, fail(4, "AUSENTE — program ax arrives in P2")
	}
	if !s.Ready {
		return nil, fail(4, "capsule not ready")
	}
	w, x, e := selectWindow(s, id)
	if e != nil {
		return nil, e
	}
	seq := s.Seq[id]
	if seq < 1 || seq > 999999 {
		return nil, fail(4, "capture a frame before AX")
	}
	before, e := t.geometry(ctx, c, x)
	if e != nil {
		return nil, e // a failed probe is not a geometry change (e.g. docker missing from PATH)
	}
	if !sameRect(before, w) {
		return nil, fail(6, "window geometry changed before AX")
	}
	title, e := t.exec(ctx, c, "xdotool", "getwindowname", x)
	if e != nil {
		return nil, e
	}
	if len(title) > 4096 || !utf8.Valid(title) {
		return nil, fail(6, "invalid window title metadata")
	}
	w.Title = strings.TrimSpace(string(title))
	w.URL = titleURL(w.Title)
	// An empty title cannot identify a page unambiguously.
	if w.Title == "" {
		return nil, fail(4, "window title unavailable")
	}
	// Select by window id: several windows can share a title (e.g. three Perplexity pages); the
	// shim then picks the page whose browser window has this window's rectangle.
	q := map[string]any{"op": "ax", "window_title_or_index": w.ID, "frame_seq": seq}
	if textContainer != "" {
		q["text_container"] = textContainer
	}
	request, _ := json.Marshal(q)
	request = append(request, '\n')
	raw, e := t.R.Run(ctx, []string{"docker", "exec", "-i", "-u", "10001:10001", c, "python3", "-I", "-c", axPython}, request)
	if e != nil {
		return nil, fail(4, "AX shim unavailable")
	}
	result, e := validateAX(raw, w, seq, textContainer)
	if e != nil {
		return nil, e
	}
	after, e := t.geometry(ctx, c, x)
	if e != nil || !sameRect(after, w) {
		return nil, fail(6, "window geometry changed during AX")
	}
	return result, nil
}

// safeURL strips credentials and transient URL metadata at the client boundary.
func safeURL(raw string) string {
	u, err := url.Parse(raw)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Hostname() == "" || u.Opaque != "" {
		return ""
	}
	u.User = nil
	u.RawQuery, u.Fragment, u.RawFragment = "", "", ""
	u.ForceQuery = false
	if u.Path == "" {
		u.Path = "/"
	}
	return u.String()
}

var titleURLs = regexp.MustCompile(`(?i)https?://[^\s]+`)

func safeTitle(title string) string { return titleURLs.ReplaceAllStringFunc(title, safeURL) }

var axID = regexp.MustCompile(`^[a-f0-9]{4,64}$`)

// Parse tokens explicitly: encoding/json otherwise silently accepts duplicate
// keys. All payload failures use fixed messages, never returned page text.
func axParse(d *json.Decoder, depth int) (any, error) {
	if depth > 32 {
		return nil, fail(5, "AX nesting bound")
	}
	tok, e := d.Token()
	if e != nil {
		return nil, fail(5, "malformed AX JSON")
	}
	delim, ok := tok.(json.Delim)
	if !ok {
		return tok, nil
	}
	switch delim {
	case '{':
		m := map[string]any{}
		for d.More() {
			k, e := d.Token()
			if e != nil {
				return nil, fail(5, "malformed AX key")
			}
			key, ok := k.(string)
			if !ok {
				return nil, fail(5, "malformed AX key")
			}
			if key == "value" {
				return nil, fail(6, "AX value disclosure refused")
			}
			if _, ok = m[key]; ok {
				return nil, fail(5, "duplicate AX key")
			}
			v, e := axParse(d, depth+1)
			if e != nil {
				return nil, e
			}
			m[key] = v
		}
		if _, e = d.Token(); e != nil {
			return nil, fail(5, "malformed AX object")
		}
		return m, nil
	case '[':
		a := []any{}
		for d.More() {
			v, e := axParse(d, depth+1)
			if e != nil {
				return nil, e
			}
			a = append(a, v)
		}
		if _, e = d.Token(); e != nil {
			return nil, fail(5, "malformed AX array")
		}
		return a, nil
	}
	return nil, fail(5, "invalid AX delimiter")
}
func axObj(v any, keys ...string) (map[string]any, bool) {
	m, ok := v.(map[string]any)
	if !ok || len(m) != len(keys) {
		return nil, false
	}
	for _, k := range keys {
		if _, ok := m[k]; !ok {
			return nil, false
		}
	}
	return m, true
}
func axString(v any, max int) bool {
	s, ok := v.(string)
	return ok && utf8.RuneCountInString(s) <= max
}
func axBool(v any) bool { _, ok := v.(bool); return ok }
func axInt(v any, min, max int) bool {
	n, ok := v.(json.Number)
	if !ok {
		return false
	}
	i, e := n.Int64()
	return e == nil && i >= int64(min) && i <= int64(max)
}
func axNumber(v any) bool {
	n, ok := v.(json.Number)
	if !ok {
		return false
	}
	f, e := n.Float64()
	return e == nil && !math.IsInf(f, 0) && !math.IsNaN(f)
}
func axInteger(v any) int     { n, _ := v.(json.Number).Int64(); return int(n) }
func axIdentifier(v any) bool { s, ok := v.(string); return ok && axID.MatchString(s) }
func validateAX(raw []byte, w Window, seq int, textContainer string) (json.RawMessage, error) {
	bad := func() (json.RawMessage, error) { return nil, fail(5, "malformed AX reply") }
	if len(raw) == 0 || len(raw) > axMaxReply || !utf8.Valid(raw) {
		return bad()
	}
	d := json.NewDecoder(bytes.NewReader(raw))
	d.UseNumber()
	v, e := axParse(d, 0)
	if e != nil {
		return nil, e
	}
	if _, e = d.Token(); e != io.EOF {
		return bad()
	}
	if m, ok := axObj(v, "error", "code"); ok {
		if !axString(m["error"], 120) || !axInt(m["code"], 4, 6) {
			return bad()
		}
		return nil, fail(axInteger(m["code"]), "AX shim refused or unavailable")
	}
	keys := []string{"window", "frame_seq", "scroll", "elements", "truncated", "skipped_frames"}
	if textContainer != "" {
		keys = append(keys, "text")
	}
	m, ok := axObj(v, keys...)
	if !ok || !axInt(m["frame_seq"], seq, seq) || !axBool(m["truncated"]) || !axInt(m["skipped_frames"], 0, 100000) {
		return bad()
	}
	win, ok := axObj(m["window"], "id", "x", "y", "w", "h", "title", "url")
	if !ok || win["id"] != w.ID || !axInt(win["x"], w.X, w.X) || !axInt(win["y"], w.Y, w.Y) || !axInt(win["w"], w.W, w.W) || !axInt(win["h"], w.H, w.H) || !axString(win["title"], 4096) || !axString(win["url"], 4096) {
		return bad()
	}
	win["url"] = safeURL(win["url"].(string))
	win["title"] = safeTitle(win["title"].(string))
	scroll, ok := axObj(m["scroll"], "x", "y")
	if !ok || !axNumber(scroll["x"]) || !axNumber(scroll["y"]) {
		return bad()
	}
	elements, ok := m["elements"].([]any)
	if !ok || len(elements) > 300 {
		return bad()
	}
	ids := map[string]bool{}
	for _, item := range elements {
		el, ok := axObj(item, "id", "role", "name", "box", "region", "actionable", "focused", "states", "container", "value_len")
		if !ok || !axIdentifier(el["id"]) || !axString(el["role"], 120) || el["role"] == "" || !axString(el["name"], 120) || !axBool(el["actionable"]) || !axBool(el["focused"]) {
			return bad()
		}
		id := el["id"].(string)
		if ids[id] {
			return bad()
		}
		ids[id] = true
		box, ok := el["box"].([]any)
		if !ok || len(box) != 4 {
			return bad()
		}
		for _, n := range box {
			if !axInt(n, 0, Width) {
				return nil, fail(6, "AX box outside window")
			}
		}
		x, y, bw, bh := axInteger(box[0]), axInteger(box[1]), axInteger(box[2]), axInteger(box[3])
		if x > w.W-bw || y > w.H-bh {
			return nil, fail(6, "AX box outside window")
		}
		if el["region"] != nil {
			r, ok := el["region"].(string)
			if !ok || !strings.Contains("|main|navigation|banner|contentinfo|complementary|form|search|dialog|", "|"+r+"|") {
				return bad()
			}
		}
		states, ok := axObj(el["states"], "enabled", "focused", "expanded", "checked", "selected")
		if !ok {
			return bad()
		}
		for _, s := range states {
			if s != nil && !axBool(s) {
				return bad()
			}
		}
		if states["focused"] != nil && states["focused"] != el["focused"] {
			return bad()
		}
		if el["container"] != nil {
			c, ok := axObj(el["container"], "id", "ordinal")
			if !ok || !axIdentifier(c["id"]) || !axInt(c["ordinal"], 0, 1<<31-1) {
				return bad()
			}
		}
		if el["value_len"] != nil {
			if el["role"] != "textbox" && el["role"] != "combobox" {
				return nil, fail(6, "AX value metadata refused")
			}
			if !axInt(el["value_len"], 0, 1<<31-1) {
				return bad()
			}
		}
	}
	if textContainer != "" {
		text, ok := axObj(m["text"], "container_id", "bytes", "text")
		if !ok || text["container_id"] != textContainer || !axString(text["text"], 65536) || !axInt(text["bytes"], 0, 65536) {
			return bad()
		}
		if len(text["text"].(string)) != axInteger(text["bytes"]) {
			return bad()
		}
	}
	// Canonical re-encoding guarantees exactly one JSON value to the CLI encoder.
	out, e := json.Marshal(v)
	if e != nil {
		return bad()
	}
	return out, nil
}

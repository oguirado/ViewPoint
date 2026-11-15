package surface

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"image"
	"image/png"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"golang.org/x/sys/unix"
	"opensuperficies.local/srf/src/browserid/inject"
	"opensuperficies.local/srf/src/ramcap"
)

func xid(s string) (string, error) {
	base := 10
	if strings.HasPrefix(s, "0x") {
		base = 16
		s = s[2:]
	}
	n, e := strconv.ParseUint(s, base, 32)
	if e != nil || n == 0 {
		return "", fail(6, "invalid X window id")
	}
	return strconv.FormatUint(n, 10), nil
}
func matchWindows(raw string, windows []Window) (map[string]string, error) {
	result := map[string]string{}
	used := map[string]bool{}
	for _, line := range strings.Split(raw, "\n") {
		f := strings.Fields(line)
		if len(f) < 7 {
			continue
		}
		id, e := xid(f[0])
		if e != nil {
			return nil, e
		}
		v := [4]int{}
		for i := range v {
			v[i], e = strconv.Atoi(f[i+2])
			if e != nil {
				return nil, fail(6, "invalid window geometry")
			}
		}
		for _, w := range windows {
			if v == [4]int{w.X, w.Y, w.W, w.H} {
				if result[w.ID] != "" || used[id] {
					return nil, fail(6, "ambiguous window geometry")
				}
				result[w.ID] = id
				used[id] = true
			}
		}
	}
	if len(result) != len(windows) {
		return nil, fail(6, "planned windows not verified")
	}
	return result, nil
}
func selectWindow(s session, id string) (Window, string, error) {
	if !s.Ready || !windowRE.MatchString(id) {
		return Window{}, "", fail(2, "unready capsule or invalid window")
	}
	for _, w := range s.Capsule.Windows {
		if w.ID == id {
			if _, e := CropRGB(nil, Width, Height, w); e != nil && Code(e) == 2 {
				return Window{}, "", e
			}
			x, e := xid(s.XIDs[id])
			return w, x, e
		}
	}
	return Window{}, "", fail(2, "unknown window")
}
func (t Tool) geometry(ctx context.Context, c, id string) (Window, error) {
	b, e := t.exec(ctx, c, "xdotool", "getwindowgeometry", "--shell", id)
	if e != nil {
		return Window{}, e
	}
	m := map[string]int{}
	for _, l := range strings.Split(string(b), "\n") {
		k, v, ok := strings.Cut(l, "=")
		if !ok {
			continue
		}
		n, e := strconv.Atoi(v)
		if e != nil {
			return Window{}, fail(6, "invalid live geometry")
		}
		if _, exists := m[k]; exists {
			return Window{}, fail(6, "duplicate live geometry field")
		}
		m[k] = n
	}
	for _, key := range []string{"WINDOW", "X", "Y", "WIDTH", "HEIGHT", "SCREEN"} {
		if _, ok := m[key]; !ok {
			return Window{}, fail(6, "missing live geometry")
		}
	}
	if strconv.Itoa(m["WINDOW"]) != id || m["SCREEN"] != 0 {
		return Window{}, fail(6, "window geometry target mismatch")
	}
	return Window{X: m["X"], Y: m["Y"], W: m["WIDTH"], H: m["HEIGHT"]}, nil
}
func sameRect(a, b Window) bool { return a.X == b.X && a.Y == b.Y && a.W == b.W && a.H == b.H }

type placement struct {
	t   Tool
	ctx context.Context
	c   string
	w   Window
	xid string
}

func (p placement) Resolve(id string) (inject.Placement, error) {
	if id != p.w.ID {
		return inject.Placement{}, fail(2, "unknown surface")
	}
	g, e := p.t.geometry(p.ctx, p.c, p.xid)
	if e != nil || !sameRect(g, p.w) {
		return inject.Placement{}, fail(6, "window moved or geometry unavailable")
	}
	return inject.Placement{DisplayID: p.c + Display, WindowID: p.xid, X: g.X, Y: g.Y, Width: g.W, Height: g.H}, nil
}

type executor struct {
	t     Tool
	c     string
	paste bool
}

// pastePython owns the capsule CLIPBOARD with the text from stdin, sends ctrl+v to the focused
// window and clears the clipboard again; it never reads the clipboard. Rich editors (e.g. Google
// Stitch) turn typed text into link chips, and xdotool type drops non-ASCII characters.
const pastePython = `import subprocess, sys
import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gdk, GLib, Gtk
text = sys.stdin.buffer.read(16384).decode('utf-8')
board = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
board.set_text(text, -1)
result = []
def done():
    board.set_text('', 0)
    board.clear()
    Gtk.main_quit()
    return False
def paste():
    result.append(subprocess.run(['xdotool', 'key', '--clearmodifiers', 'ctrl+v'], timeout=5).returncode)
    GLib.timeout_add(1500, done)
    return False
GLib.timeout_add(200, paste)
GLib.timeout_add(9000, done)
Gtk.main()
sys.exit(0 if result == [0] else 6)
`

func (e executor) Activate(ctx context.Context, _ string, w string) error {
	_, err := e.t.exec(ctx, e.c, "xdotool", "windowactivate", "--sync", w)
	return err
}
func (e executor) Focused(ctx context.Context, _ string) (string, error) {
	b, err := e.t.exec(ctx, e.c, "xdotool", "getwindowfocus")
	if err != nil {
		return "", err
	}
	return xid(strings.TrimSpace(string(b)))
}
func (e executor) Key(ctx context.Context, _ string, k string) error {
	_, err := e.t.exec(ctx, e.c, "xdotool", "key", "--clearmodifiers", k)
	return err
}
func (e executor) Type(ctx context.Context, _ string, text string) error {
	if e.paste {
		a := []string{"docker", "exec", "-i", "-u", "10001:10001", "-e", "DISPLAY=" + Display, "-e", "NO_AT_BRIDGE=1", e.c, "timeout", "--signal=TERM", "--kill-after=1s", "12s", "python3", "-I", "-c", pastePython}
		if _, err := e.t.R.Run(ctx, a, []byte(text)); err != nil {
			return fail(6, "paste failed")
		}
		return nil
	}
	// The text goes through stdin (xdotool type --file -), never argv: it stays out of ps and
	// /proc/<pid>/cmdline on the host and in the capsule.
	a := []string{"docker", "exec", "-i", "-u", "10001:10001", "-e", "DISPLAY=" + Display, e.c, "timeout", "--signal=TERM", "--kill-after=1s", "30s", "xdotool", "type", "--clearmodifiers", "--delay", "0", "--file", "-"}
	if _, err := e.t.R.Run(ctx, a, []byte(text)); err != nil {
		return fail(6, "capsule command failed")
	}
	return nil
}
func (e executor) Click(ctx context.Context, _ string, x, y, button int) error {
	// mousemove --sync waits for pointer motion and never returns when the pointer already
	// sits on the target (a second click on the same point), so step next to it first.
	nx := x - 1
	if x == 0 {
		nx = 1
	}
	_, err := e.t.exec(ctx, e.c, "xdotool", "mousemove", strconv.Itoa(nx), strconv.Itoa(y), "mousemove", "--sync", strconv.Itoa(x), strconv.Itoa(y), "click", "--clearmodifiers", strconv.Itoa(button))
	return err
}

// Scroll sends count wheel steps at display x,y, holding modifier ("ctrl" zooms many canvases).
func (e executor) Scroll(ctx context.Context, _ string, x, y, button, count int, modifier string) error {
	nx := x - 1
	if x == 0 {
		nx = 1
	}
	a := []string{"xdotool"}
	if modifier != "" {
		a = append(a, "keydown", modifier)
	}
	a = append(a, "mousemove", strconv.Itoa(nx), strconv.Itoa(y), "mousemove", "--sync", strconv.Itoa(x), strconv.Itoa(y),
		"click", "--repeat", strconv.Itoa(count), "--delay", "40", strconv.Itoa(button))
	if modifier != "" {
		a = append(a, "keyup", modifier)
	}
	_, err := e.t.exec(ctx, e.c, a...)
	return err
}

// Drag presses at x1,y1, moves to x2,y2 in 8 steps (canvas pans need intermediate motion) and
// releases.
func (e executor) Drag(ctx context.Context, _ string, x1, y1, x2, y2, button int) error {
	nx := x1 - 1
	if x1 == 0 {
		nx = 1
	}
	a := []string{"xdotool", "mousemove", strconv.Itoa(nx), strconv.Itoa(y1), "mousemove", "--sync", strconv.Itoa(x1), strconv.Itoa(y1), "mousedown", strconv.Itoa(button)}
	for i := 1; i <= 8; i++ {
		a = append(a, "sleep", "0.03", "mousemove", strconv.Itoa(x1+(x2-x1)*i/8), strconv.Itoa(y1+(y2-y1)*i/8))
	}
	a = append(a, "sleep", "0.05", "mouseup", strconv.Itoa(button))
	_, err := e.t.exec(ctx, e.c, a...)
	return err
}

func (t Tool) Input(ctx context.Context, c, id string, req inject.Request) (inject.Action, error) {
	return t.input(ctx, c, id, req, false)
}

// Paste enters text like Input with KindText, but through the capsule clipboard and ctrl+v.
func (t Tool) Paste(ctx context.Context, c, id string, req inject.Request) (inject.Action, error) {
	if req.Kind != inject.KindText {
		return inject.Action{}, fail(2, "paste needs text")
	}
	return t.input(ctx, c, id, req, true)
}

func (t Tool) input(ctx context.Context, c, id string, req inject.Request, paste bool) (inject.Action, error) {
	var a inject.Action
	if e := t.ports(); e != nil {
		return a, e
	}
	unlock, e := t.lock(ctx)
	if e != nil {
		return a, e
	}
	defer unlock()
	s, e := t.load(c)
	if e != nil {
		return a, e
	}
	w, x, e := selectWindow(s, id)
	if e != nil {
		return a, e
	}
	req.SurfaceID = id
	inj, e := inject.New(executor{t, c, paste}, placement{t, ctx, c, w, x}, inject.Clock(t.Clock), 1)
	if e != nil {
		return a, fail(3, "injector unavailable")
	}
	a, e = inj.Submit(ctx, req)
	if e != nil {
		code := 6
		if e == inject.ErrInvalidRequest {
			code = 2
		}
		return a, fail(code, a.Result)
	}
	return a, nil
}

type Frame struct {
	Path          string `json:"path"`
	Seq           int    `json:"seq"`
	SHA256RGB24   string `json:"sha256_rgb24"`
	Width         int    `json:"width"`
	Height        int    `json:"height"`
	CaptureMonoNS uint64 `json:"capture_mono_ns"`
	Generation    string `json:"generation"`
	Window        Window `json:"window"`
}

// CropRGB copies a half-open window rectangle from canonical display RGB24.
func CropRGB(rgb []byte, width, height int, w Window) ([]byte, error) {
	if width < 1 || height < 1 || width > Width || height > Height || w.X < 0 || w.Y < 0 || w.W < 1 || w.H < 1 || w.X > width-w.W || w.Y > height-w.H {
		return nil, fail(2, "window crop outside display")
	}
	if len(rgb) != 3*width*height {
		return nil, fail(5, "RGB24 length mismatch")
	}
	out := make([]byte, 3*w.W*w.H)
	for y := 0; y < w.H; y++ {
		start := 3 * ((w.Y+y)*width + w.X)
		copy(out[3*y*w.W:3*(y+1)*w.W], rgb[start:start+3*w.W])
	}
	return out, nil
}
func splitPair(raw []byte) ([2][]byte, error) {
	var legs [2][]byte
	for i := range legs {
		if len(raw) < 8 {
			return legs, fail(5, "pair framing")
		}
		n := binary.BigEndian.Uint64(raw[:8])
		if n > uint64(len(raw)-8) {
			return legs, fail(5, "pair length")
		}
		legs[i] = raw[8 : 8+n]
		raw = raw[8+n:]
	}
	if len(raw) != 0 {
		return legs, fail(5, "pair trailing bytes")
	}
	return legs, nil
}
func (t Tool) Frame(ctx context.Context, c, id string) (Frame, error) {
	var f Frame
	if e := t.ports(); e != nil {
		return f, e
	}
	unlock, e := t.lock(ctx)
	if e != nil {
		return f, e
	}
	defer unlock()
	s, e := t.load(c)
	if e != nil {
		return f, e
	}
	w, x, e := selectWindow(s, id)
	if e != nil {
		return f, e
	}
	before, e := t.geometry(ctx, c, x)
	if e != nil {
		return f, e // a failed probe is not a geometry change (e.g. docker missing from PATH)
	}
	if !sameRect(before, w) {
		return f, fail(6, "window geometry changed before capture")
	}
	title, e := t.exec(ctx, c, "xdotool", "getwindowname", x)
	if e != nil {
		return f, e
	}
	if len(title) > 4096 {
		return f, fail(6, "window title exceeds metadata bound")
	}
	w.Title = safeTitle(strings.TrimSpace(string(title)))
	if s.Kind != "program" {
		w.URL = titleURL(w.Title)
	} else {
		w.URL = ""
	}
	stamp := t.Clock()
	raw, e := t.exec(ctx, c, "/usr/local/libexec/srf-fbread", "pair", Display, "0")
	if e != nil {
		return f, fail(5, "pair capture failed")
	}
	legs, e := splitPair(raw)
	if e != nil {
		return f, e
	}
	rgb, _, e := ramcap.PairLegs(legs[0], legs[1], Width, Height)
	if e != nil {
		return f, fail(5, "pair invalid or pixels differ")
	}
	after, e := t.geometry(ctx, c, x)
	if e != nil || !sameRect(after, w) {
		return f, fail(6, "window geometry changed during capture")
	}
	rgb, e = CropRGB(rgb, Width, Height, w)
	if e != nil {
		return f, e
	}
	sha := sha256.Sum256(rgb)
	seq := s.Seq[id] + 1
	if seq < 1 || seq > 999999 {
		return f, fail(4, "frame sequence exhausted")
	}
	s.Seq[id] = seq
	// Persist allocation first so an ambiguous write cannot reuse a sequence.
	if e = t.save(s); e != nil {
		return f, e
	}
	sink := t.Sink
	if sink == nil {
		sink = ramSink{}
	}
	path, e := sink.Write(s.Capsule.RunID, id, seq, rgb, w.W, w.H)
	if e != nil {
		return f, e
	}
	return Frame{Path: path, Seq: seq, SHA256RGB24: hex.EncodeToString(sha[:]), Width: w.W, Height: w.H, CaptureMonoNS: stamp, Generation: c, Window: w}, nil
}
func titleURL(title string) string {
	for _, word := range strings.Fields(title) {
		if u := safeURL(word); u != "" {
			return u
		}
	}
	return ""
}

type ramSink struct{}

func (ramSink) Write(runID, window string, seq int, rgb []byte, w, h int) (string, error) {
	if !capsuleRE.MatchString(runID) || !windowRE.MatchString(window) || seq < 1 || seq > 999999 || len(rgb) != 3*w*h {
		return "", fail(2, "invalid frame output")
	}
	dir := filepath.Join(RAMRoot, runID, "frames", window)
	if e := privateDir(dir); e != nil {
		return "", e
	}
	var fs unix.Statfs_t
	if unix.Statfs(dir, &fs) != nil || fs.Type != unix.TMPFS_MAGIC {
		return "", fail(4, "frame destination must be tmpfs")
	}
	path := filepath.Join(dir, fmt.Sprintf("f%06d.png", seq))
	if e := writeRAMPNG(path, rgb, w, h); e != nil {
		return "", e
	}
	return path, nil
}

// writeRAMPNG encodes RGB24 as PNG next to path and links it into place (0600; an existing
// path is never replaced).
func writeRAMPNG(path string, rgb []byte, w, h int) error {
	if len(rgb) != 3*w*h {
		return fail(2, "invalid frame output")
	}
	if _, e := os.Lstat(path); !os.IsNotExist(e) {
		return fail(4, "frame already exists or unavailable")
	}
	img := image.NewNRGBA(image.Rect(0, 0, w, h))
	for i := 0; i < w*h; i++ {
		copy(img.Pix[4*i:4*i+3], rgb[3*i:3*i+3])
		img.Pix[4*i+3] = 255
	}
	file, e := os.CreateTemp(filepath.Dir(path), ".frame-*")
	if e != nil {
		return fail(4, "RAM frame unavailable")
	}
	tmp := file.Name()
	defer os.Remove(tmp)
	if e = file.Chmod(0600); e == nil {
		e = png.Encode(file, img)
	}
	closeErr := file.Close()
	if e == nil {
		e = closeErr
	}
	if e == nil {
		e = os.Link(tmp, path)
	}
	if e != nil {
		return fail(4, "RAM frame write failed")
	}
	return nil
}

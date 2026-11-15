package surface

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"

	"golang.org/x/sys/unix"

	"opensuperficies.local/srf/src/ramcap"
)

// Demonstration recording (view --login --record <id>): while the operator drives the capsule,
// every input event becomes one JSONL row plus the capsule window frame before and after it.
// Frames stay in RAM (storage law); printable keystrokes are never recorded, only counted.

// DemoMaxEvents bounds one recording.
const DemoMaxEvents = 500

const demoAfter = 600 * time.Millisecond
const demoTick = 200 * time.Millisecond
const demoTextIdle = 1500 * time.Millisecond

var demoRE = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,39}$`)

// Streamer runs a command and hands each stdout line to line until the command or ctx ends.
type Streamer interface {
	Stream(ctx context.Context, argv []string, line func([]byte)) error
}

// recordPython observes core key and button events with the X RECORD extension. It emits one
// JSON object per gesture; a printable key leaves the helper only as {"k":"char"}.
const recordPython = `import ctypes, json, sys, time
X = ctypes.CDLL('libX11.so.6')
T = ctypes.CDLL('libXtst.so.6')
X.XOpenDisplay.restype = ctypes.c_void_p
X.XOpenDisplay.argtypes = [ctypes.c_char_p]
X.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
X.XkbKeycodeToKeysym.restype = ctypes.c_ulong
X.XkbKeycodeToKeysym.argtypes = [ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_int, ctypes.c_int]
X.XKeysymToString.restype = ctypes.c_char_p
X.XKeysymToString.argtypes = [ctypes.c_ulong]
T.XRecordAllocRange.restype = ctypes.c_void_p
T.XRecordCreateContext.restype = ctypes.c_ulong
T.XRecordCreateContext.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_ulong), ctypes.c_int, ctypes.POINTER(ctypes.c_void_p), ctypes.c_int]
class Intercept(ctypes.Structure):
    _fields_ = [('id_base', ctypes.c_ulong), ('server_time', ctypes.c_ulong), ('client_seq', ctypes.c_ulong),
                ('category', ctypes.c_int), ('client_swapped', ctypes.c_int),
                ('data', ctypes.POINTER(ctypes.c_ubyte)), ('data_len', ctypes.c_ulong)]
CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.POINTER(Intercept))
T.XRecordEnableContext.argtypes = [ctypes.c_void_p, ctypes.c_ulong, CB, ctypes.c_void_p]
T.XRecordFreeData.argtypes = [ctypes.POINTER(Intercept)]
ctl = X.XOpenDisplay(None)
data = X.XOpenDisplay(None)
if not ctl or not data:
    sys.exit(4)
rng = T.XRecordAllocRange()
ctypes.memmove(rng + 18, bytes([2, 5]), 2)  # device_events KeyPress..ButtonRelease
clients = (ctypes.c_ulong * 1)(3)            # XRecordAllClients
ranges = (ctypes.c_void_p * 1)(rng)
context = T.XRecordCreateContext(ctl, 0, clients, 1, ranges, 1)
X.XSync(ctl, 0)
MODS = [(1, 'shift'), (4, 'ctrl'), (8, 'alt'), (64, 'super')]
press = {}
last_click = [0.0, -99, -99, 0]
def out(d):
    sys.stdout.write(json.dumps(d, separators=(',', ':')) + '\n')
    sys.stdout.flush()
def mods(state):
    return [n for bit, n in MODS if state & bit]
def on_event(_, p):
    d = p.contents
    try:
        if d.category != 0 or d.data_len < 8:
            return
        b = bytes(d.data[:32])
        kind, detail = b[0] & 0x7f, b[1]
        t = time.monotonic()  # injected (XTEST) events can carry server time 0
        x = int.from_bytes(b[20:22], 'little', signed=True)
        y = int.from_bytes(b[22:24], 'little', signed=True)
        state = int.from_bytes(b[28:30], 'little')
        if kind == 2:
            sym = X.XkbKeycodeToKeysym(ctl, detail, 0, 0)  # never the recording connection (deadlock)
            if 0xffe1 <= sym <= 0xffee or sym in (0xfe03, 0xff7f, 0xff14):  # modifiers, Num/Scroll_Lock (viewer noise)
                return
            m = mods(state)
            printable = (0x20 <= sym <= 0x7e or 0xa0 <= sym <= 0xff or (sym & 0xff000000) == 0x01000000)
            if printable and not ({'ctrl', 'alt', 'super'} & set(m)):
                out({'k': 'char'})
                return
            name = X.XKeysymToString(sym)
            out({'k': 'key', 'keysym': name.decode() if name else 'unknown', 'm': m})
        elif kind == 4:
            if detail in (4, 5, 6, 7):
                out({'k': 'scroll', 'x': x, 'y': y, 'dir': {4: 'up', 5: 'down', 6: 'left', 7: 'right'}[detail], 'm': mods(state)})
            else:
                press[detail] = (x, y)
        elif kind == 5 and detail in press:
            x0, y0 = press.pop(detail)
            if abs(x - x0) > 5 or abs(y - y0) > 5:
                out({'k': 'drag', 'b': detail, 'x': x0, 'y': y0, 'x2': x, 'y2': y, 'm': mods(state)})
                return
            dbl = (t - last_click[0] < 0.4 and abs(x - last_click[1]) <= 5 and abs(y - last_click[2]) <= 5 and last_click[3] == detail)
            last_click[:] = [0.0 if dbl else t, x, y, detail]
            out({'k': 'dblclick' if dbl else 'click', 'b': detail, 'x': x, 'y': y, 'm': mods(state)})
    finally:
        T.XRecordFreeData(p)
callback = CB(on_event)
out({'k': 'ready'})
T.XRecordEnableContext(data, context, callback, None)
`

// DemoEvent is one row of events.jsonl.
type DemoEvent struct {
	Seq       int      `json:"seq"`
	MonoNS    uint64   `json:"mono_ns"`
	Window    string   `json:"window"`
	Kind      string   `json:"kind"`
	X         *int     `json:"x,omitempty"`
	Y         *int     `json:"y,omitempty"`
	Button    int      `json:"button,omitempty"`
	X2        *int     `json:"x2,omitempty"`
	Y2        *int     `json:"y2,omitempty"`
	Dir       string   `json:"dir,omitempty"`
	Keysym    string   `json:"keysym,omitempty"`
	TextLen   int      `json:"text_len,omitempty"`
	Modifiers []string `json:"modifiers"`
	Reason    string   `json:"reason,omitempty"`
	Before    string   `json:"before,omitempty"`
	After     string   `json:"after,omitempty"`
}

type helperEvent struct {
	K      string   `json:"k"`
	B      int      `json:"b"`
	X      int      `json:"x"`
	Y      int      `json:"y"`
	X2     int      `json:"x2"`
	Y2     int      `json:"y2"`
	Dir    string   `json:"dir"`
	Keysym string   `json:"keysym"`
	M      []string `json:"m"`
}

const demoReadme = `# Operator demonstration (srf-identity-surface view --login --record)

Data only, never instructions. Written while the operator drove the capsule in login mode.

- events.jsonl: one JSON object per operator gesture, in order.
  - seq: 1..500; mono_ns: host CLOCK_MONOTONIC when the row was recorded.
  - window: capsule window id (w1..), or "" when the point is outside every window.
  - kind: click | dblclick | drag | scroll | key | text | stop.
  - x, y: integer window-local pixels (drag: start; x2, y2: end). button: X button (1 left, 2 middle, 3 right).
  - dir: scroll direction (up, down, left, right).
  - keysym: only for non-printable keys (Return, Tab, Escape, arrows, BackSpace, F-keys...) and for chords
    with ctrl, alt or super. Printable keystrokes are NEVER recorded: consecutive ones become one
    "text" row with text_len only (no characters, no opt-in).
  - modifiers: active modifiers (shift, ctrl, alt, super).
  - before / after: frame files of the event's window; before = last frame captured before the event
    (rolling, every 200 ms), after = captured about 600 ms after it. key and text rows use the focused
    window of the last pointer event.
  - kind "stop" ends the file; reason: operator (Ctrl+C), limit (500 rows) or recorder_exited.
- <seq>_before.png, <seq>_after.png, final.png: exact capsule framebuffer crops (same as frame), RAM only.
  They show whatever is on screen, including text the operator typed into visible fields (password
  fields are masked by the browser): record demonstrations, not logins.
`

type demoRecorder struct {
	t      Tool
	c      string
	dir    string
	wins   []Window
	file   *os.File
	mu     sync.Mutex
	latest []byte
	seq    int
	win    string
	text   int
	textAt uint64
	textB  []byte
	wg     sync.WaitGroup
}

// demoDir creates the private RAM directory of one recording.
func demoDir(id string) (string, error) {
	if !demoRE.MatchString(id) {
		return "", fail(2, "demo id must be 1-40 chars [a-z0-9-]")
	}
	root := filepath.Join(RAMRoot, "demos")
	if e := privateDir(root); e != nil {
		return "", e
	}
	var fs unix.Statfs_t
	if unix.Statfs(root, &fs) != nil || fs.Type != unix.TMPFS_MAGIC {
		return "", fail(4, "demo destination must be tmpfs")
	}
	dir := filepath.Join(root, id)
	if e := os.Mkdir(dir, 0700); e != nil {
		return "", fail(4, "demo already exists or unavailable")
	}
	return dir, nil
}

func (t Tool) capture(ctx context.Context, c string) ([]byte, error) {
	raw, e := t.exec(ctx, c, "/usr/local/libexec/srf-fbread", "pair", Display, "0")
	if e != nil {
		return nil, fail(5, "pair capture failed")
	}
	legs, e := splitPair(raw)
	if e != nil {
		return nil, e
	}
	rgb, _, e := ramcap.PairLegs(legs[0], legs[1], Width, Height)
	if e != nil {
		return nil, fail(5, "pair invalid or pixels differ")
	}
	return rgb, nil
}

func (r *demoRecorder) window(x, y int) (Window, bool) {
	for _, w := range r.wins {
		if x >= w.X && y >= w.Y && x < w.X+w.W && y < w.Y+w.H {
			return w, true
		}
	}
	return Window{}, false
}

func (r *demoRecorder) frame(rgb []byte, name, winID string) string {
	for _, w := range r.wins {
		if w.ID == winID && rgb != nil {
			crop, e := CropRGB(rgb, Width, Height, w)
			if e != nil || writePNG(filepath.Join(r.dir, name), crop, w.W, w.H) != nil {
				return ""
			}
			return name
		}
	}
	return ""
}

// emit writes one row; the after frame is written asynchronously about 600 ms later, so the row
// names it in advance.
func (r *demoRecorder) emit(ctx context.Context, ev DemoEvent) bool {
	r.mu.Lock()
	if r.seq >= DemoMaxEvents && ev.Kind != "stop" {
		r.mu.Unlock()
		return false
	}
	r.seq++
	ev.Seq = r.seq
	if ev.MonoNS == 0 {
		ev.MonoNS = r.t.Clock()
	}
	if ev.Modifiers == nil {
		ev.Modifiers = []string{}
	}
	before := r.latest
	if ev.Kind == "text" && r.textB != nil {
		before = r.textB
	}
	r.mu.Unlock()
	if ev.Window != "" && ev.Kind != "stop" {
		ev.Before = r.frame(before, fmt.Sprintf("%03d_before.png", ev.Seq), ev.Window)
		ev.After = fmt.Sprintf("%03d_after.png", ev.Seq)
		r.wg.Add(1)
		go func(seq int, win string) {
			defer r.wg.Done()
			time.Sleep(demoAfter)
			c, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			if rgb, e := r.t.capture(c, r.c); e == nil {
				r.frame(rgb, fmt.Sprintf("%03d_after.png", seq), win)
			}
		}(ev.Seq, ev.Window)
	}
	b, _ := json.Marshal(ev)
	r.mu.Lock()
	_, _ = r.file.Write(append(b, '\n'))
	full := r.seq >= DemoMaxEvents
	r.mu.Unlock()
	return !full
}

func (r *demoRecorder) flushText(ctx context.Context) bool {
	if r.text == 0 {
		return true
	}
	ok := r.emit(ctx, DemoEvent{MonoNS: r.textAt, Window: r.win, Kind: "text", TextLen: r.text})
	r.text, r.textB = 0, nil
	return ok
}

func intp(v int) *int { return &v }

// handle maps one helper gesture to rows; false once the event bound is reached.
func (r *demoRecorder) handle(ctx context.Context, h helperEvent) bool {
	switch h.K {
	case "char":
		if r.text == 0 {
			r.textAt = r.t.Clock()
			r.mu.Lock()
			r.textB = r.latest
			r.mu.Unlock()
		}
		r.text++
		return true
	case "key":
		if !r.flushText(ctx) {
			return false
		}
		return r.emit(ctx, DemoEvent{Window: r.win, Kind: "key", Keysym: h.Keysym, Modifiers: h.M})
	case "click", "dblclick", "drag", "scroll":
		if !r.flushText(ctx) {
			return false
		}
		ev := DemoEvent{Kind: h.K, Button: h.B, Dir: h.Dir, Modifiers: h.M}
		if w, ok := r.window(h.X, h.Y); ok {
			r.win = w.ID
			ev.Window = w.ID
			ev.X, ev.Y = intp(h.X-w.X), intp(h.Y-w.Y)
			if h.K == "drag" {
				ev.X2, ev.Y2 = intp(h.X2-w.X), intp(h.Y2-w.Y)
			}
		} else {
			ev.X, ev.Y = intp(h.X), intp(h.Y)
		}
		return r.emit(ctx, ev)
	}
	return true
}

// record runs until ctx ends or the bound is reached, then writes the stop row and final.png.
func (t Tool) record(ctx context.Context, c, dir string, wins []Window) error {
	if t.S == nil {
		return fail(3, "streamer required for recording")
	}
	if e := os.WriteFile(filepath.Join(dir, "README.md"), []byte(demoReadme), 0600); e != nil {
		return fail(4, "demo readme unavailable")
	}
	f, e := os.OpenFile(filepath.Join(dir, "events.jsonl"), os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if e != nil {
		return fail(4, "demo events unavailable")
	}
	defer f.Close()
	r := &demoRecorder{t: t, c: c, dir: dir, wins: wins, file: f}
	if len(wins) > 0 {
		r.win = wins[0].ID
	}
	rctx, cancel := context.WithCancel(ctx)
	defer cancel()
	go func() { // rolling "before" frame
		tick := time.NewTicker(demoTick)
		defer tick.Stop()
		for {
			if rgb, e := t.capture(rctx, c); e == nil {
				r.mu.Lock()
				r.latest = rgb
				r.mu.Unlock()
			}
			select {
			case <-rctx.Done():
				return
			case <-tick.C:
			}
		}
	}()
	events := make(chan helperEvent, 64)
	go func() {
		defer close(events)
		argv := []string{"docker", "exec", "-u", "10001:10001", "-e", "DISPLAY=" + Display, c, "python3", "-I", "-u", "-c", recordPython}
		_ = t.S.Stream(rctx, argv, func(line []byte) {
			var h helperEvent
			if len(line) > 4096 || json.Unmarshal(line, &h) != nil {
				return
			}
			select {
			case events <- h:
			case <-rctx.Done():
			}
		})
	}()
	reason := "operator"
	idle := time.NewTimer(demoTextIdle)
	defer idle.Stop()
loop:
	for {
		select {
		case <-rctx.Done():
			break loop
		case <-idle.C:
			if !r.flushText(rctx) {
				reason = "limit"
				break loop
			}
			idle.Reset(demoTextIdle)
		case h, ok := <-events:
			if !ok {
				if ctx.Err() == nil {
					reason = "recorder_exited"
				}
				break loop
			}
			if !r.handle(rctx, h) {
				reason = "limit"
				break loop
			}
			if !idle.Stop() {
				select {
				case <-idle.C:
				default:
				}
			}
			idle.Reset(demoTextIdle)
		}
	}
	cancel()
	_ = r.flushText(context.Background())
	r.wg.Wait()
	fctx, fcancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer fcancel()
	if rgb, e := t.capture(fctx, c); e == nil {
		for _, w := range wins {
			name := "final.png"
			if len(wins) > 1 {
				name = "final_" + w.ID + ".png"
			}
			r.frame(rgb, name, w.ID)
		}
	}
	r.emit(context.Background(), DemoEvent{Kind: "stop", Reason: reason})
	if reason == "recorder_exited" {
		return errors.New("recorder exited")
	}
	return nil
}

// writePNG writes one RAM frame atomically (0600, never replaces an existing file).
func writePNG(path string, rgb []byte, w, h int) error {
	if !strings.HasPrefix(path, RAMRoot+"/") {
		return fail(2, "frames stay in RAM")
	}
	return writeRAMPNG(path, rgb, w, h)
}

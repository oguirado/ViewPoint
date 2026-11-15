package exec

// WL prereg v2 actuation (CF-01): the os.cu.prereg-wl/v2 item steps are
// expanded into actuator events, each fired at t0 + t_plan_us where t0 is the
// CLOCK_MONOTONIC capture start written by srf-cu-wlcap in <run>/START. Every
// event is logged as one os.cu.wl-act-step/v1 line whose t_us is the MEASURED
// monotonic offset from t0 right after the XTEST batch returned; weak labels
// are built from these lines (labels_v2.labels_from_act), never from the plan.

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"time"
	"unicode/utf8"

	"golang.org/x/sys/unix"

	"opensuperficies.local/srf/src/cubook/rc"
)

const (
	WLActSchema   = "os.cu.wl-act-step/v1"
	wlSchema      = "os.cu.prereg-wl/v2"
	wlSchemaV3    = "os.cu.prereg-wl/v3"
	wlMaxPlanMs   = 600000 // 10 min hard cap per item
	wlMaxEvents   = 4096
	wlStopPollNs  = 50_000_000
	wlScreenLimit = 32767 // XTEST coordinates are int16
)

// WLStep is one prereg v2 step; unknown keys are refused at decode.
type WLStep struct {
	TMs  int64   `json:"t_ms"`
	Op   string  `json:"op"`
	AtPx []int   `json:"at_px"`
	Text *string `json:"text"`
	Dy   *int    `json:"dy"`
	Path [][]int `json:"path"`
	DtMs *int64  `json:"dt_ms"`
	// v3 keys (os.cu.prereg-wl/v3, lane V): unknown to v2 plans, which refuse them.
	Dx     *int    `json:"dx"`
	Keysym *string `json:"keysym"`
	Fps    *int    `json:"fps"`
}

// parseV3Step / fireV3Step are the lane V hooks (AW4-02): wlplan_v3.go assigns them in init(). The defaults fail closed (rc 5)
// so a binary without the v3 ops refuses a v3 plan instead of silently skipping steps. parseV3Step lowers one hscroll|key|drag
// step to events (Point/TPlanUS/X/Y/Dx/Dy filled, Step left to the caller) given the screen bound w x h; fireV3Step fires one
// hscroll|key|drag_press|drag_move|drag_release event.
var parseV3Step = func(WLStep, int, int) ([]WLEvent, error) {
	return nil, rc.New("E_GATE", "prereg v3 step ops are not linked")
}
var fireV3Step = func(WLActuator, WLEvent) error {
	return rc.New("E_GATE", "prereg v3 step ops are not linked")
}

// WLEvent is one actuator event: Step is the 1-based plan step, Point the
// path index of a move (0 otherwise).
type WLEvent struct {
	Step, Point int
	Op          string
	TPlanUS     int64
	X, Y, Dy    int
	Dx          int
	Text        string
}

// WLItem is a validated prereg v2 item lowered to events.
type WLItem struct {
	ID       string
	CursorPx int
	Events   []WLEvent
	// Resolutions are the manifest's allowed screen sizes [w, h]; START's measured (w, h) must be one of them (wl.go).
	Resolutions [][2]int
	// Rects are the prereg rects [x,y,w,h] that must stay on screen (JW4-06): pane_rect, div_rect0 and div_rect0 translated by
	// each drag (last path point - at_px). CheckBounds re-checks them with InRect against START's w x h.
	Rects []WLRect
}

// WLRect is one named on-screen rect of an item.
type WLRect struct {
	Name string
	R    []int
}

// WLActRow is one AJSON act_steps line of a prereg run.
type WLActRow struct {
	Schema      string `json:"schema"`
	Item        string `json:"item"`
	ActuatorSeq uint32 `json:"actuator_seq"`
	Step        int    `json:"step"`
	Point       int    `json:"point"`
	Op          string `json:"op"`
	TPlanUS     int64  `json:"t_plan_us"`
	TUS         int64  `json:"t_us"`
	RC          int    `json:"rc"`
}

// WLActuator is an Actuator that can also move the pointer (WL-7).
type WLActuator interface {
	Actuator
	Move(x, y int) error
}

// MonoClock is CLOCK_MONOTONIC in ns plus a sleep; tests inject a fake.
type MonoClock interface {
	NowNs() int64
	SleepNs(ns int64)
}

// RealMono reads CLOCK_MONOTONIC, the clock wlcap writes t0 in.
type RealMono struct{}

func (RealMono) NowNs() int64 {
	var ts unix.Timespec
	if err := unix.ClockGettime(unix.CLOCK_MONOTONIC, &ts); err != nil {
		return 0
	}
	return ts.Nano()
}

func (RealMono) SleepNs(ns int64) {
	if ns > 0 {
		time.Sleep(time.Duration(ns))
	}
}

// Move warps the pointer (one XTEST motion event).
func (a *XTestActuator) Move(x, y int) error { return a.X.MovePointer(int16(x), int16(y)) }

func (f *FakeActuator) Move(x, y int) error { return f.rec("move", fmt.Sprintf("%d,%d", x, y)) }

func wlBad(msg string) error { return rc.New("E_SCHEMA", "prereg v2: "+msg) }

// coordOps are the events that carry a pointer position.
var coordOps = map[string]bool{"click": true, "scroll": true, "move": true, "hscroll": true,
	"drag_press": true, "drag_move": true, "drag_release": true}

// CheckBounds re-checks every positioned event against the measured screen w x h (START header): rc 5 on the first point
// with x >= w or y >= h (ADC3-W3-03). Called by srf-cu-exec once START is read.
func (it *WLItem) CheckBounds(w, h int) error {
	if !dimsOK(w, h) {
		return rc.New("E_GATE", fmt.Sprintf("screen %dx%d outside 1..%d", w, h, maxScreenDim))
	}
	for _, r := range it.Rects {
		if !InRect(r.R, w, h) {
			return rc.New("E_GATE", fmt.Sprintf("rect %s %v outside the %dx%d screen", r.Name, r.R, w, h))
		}
	}
	for i, ev := range it.Events {
		if coordOps[ev.Op] && !InScreen([]int{ev.X, ev.Y}, w, h) {
			return rc.New("E_GATE", fmt.Sprintf("event %d (%s) at (%d,%d) outside the %dx%d screen", i+1, ev.Op, ev.X, ev.Y, w, h))
		}
	}
	return nil
}

func strictInt(raw json.RawMessage, what string) (int, error) {
	if len(raw) == 0 {
		return 0, nil
	}
	var n int
	if err := json.Unmarshal(raw, &n); err != nil {
		return 0, wlBad(what + " must be an integer")
	}
	return n, nil
}

// LoadWLItem decodes a prereg v2 manifest and lowers item id to events. The
// rules mirror labels_v2.check_manifest (ADC2-W3-04: scroll dy != 0, move
// needs cursor_px > 0) plus actuator limits (non-decreasing time, <= 10 min,
// on-screen coordinates, single supported character per type step).
func LoadWLItem(b []byte, id string) (*WLItem, error) {
	var m struct {
		Schema      string            `json:"schema"`
		Resolutions [][]int           `json:"resolutions"`
		Items       []json.RawMessage `json:"items"`
	}
	if err := json.Unmarshal(b, &m); err != nil {
		return nil, wlBad(err.Error())
	}
	if m.Schema != wlSchema && m.Schema != wlSchemaV3 {
		return nil, wlBad("schema " + m.Schema)
	}
	// plan-time bound: the largest prereg resolution (START's measured w x h narrows it in CheckBounds)
	bw, bh := maxScreenDim, maxScreenDim
	var res [][2]int
	if len(m.Resolutions) > 0 {
		bw, bh = 0, 0
		for _, r := range m.Resolutions {
			if len(r) != 2 || !dimsOK(r[0], r[1]) {
				return nil, wlBad("resolutions must be [w,h] pairs within 1..32767")
			}
			res = append(res, [2]int{r[0], r[1]})
			bw, bh = max(bw, r[0]), max(bh, r[1])
		}
	}
	for _, raw := range m.Items {
		var it map[string]json.RawMessage
		if err := json.Unmarshal(raw, &it); err != nil {
			return nil, wlBad(err.Error())
		}
		var got string
		if json.Unmarshal(it["id"], &got) != nil || got != id {
			continue
		}
		cp, err := strictInt(it["cursor_px"], "cursor_px")
		if err != nil {
			return nil, err
		}
		var steps []WLStep
		d := json.NewDecoder(bytes.NewReader(it["steps"]))
		d.DisallowUnknownFields()
		if err := d.Decode(&steps); err != nil {
			return nil, wlBad("steps: " + err.Error())
		}
		wit, err := lowerWL(id, cp, steps, m.Schema == wlSchemaV3, bw, bh)
		if wit != nil {
			wit.Resolutions = res
			if err == nil {
				err = v3Rects(wit, it, bw, bh)
			}
		}
		return wit, err
	}
	return nil, wlBad("unknown item " + id)
}

func lowerWL(id string, cp int, steps []WLStep, v3 bool, bw, bh int) (*WLItem, error) {
	it := &WLItem{ID: id, CursorPx: cp}
	last := int64(-1)
	add := func(ev WLEvent) error {
		if ev.TPlanUS < last || ev.TPlanUS > wlMaxPlanMs*1000 {
			return wlBad(fmt.Sprintf("step %d: t_ms must be non-decreasing and <= %d", ev.Step, wlMaxPlanMs))
		}
		if len(it.Events) >= wlMaxEvents {
			return wlBad("too many events")
		}
		last = ev.TPlanUS
		it.Events = append(it.Events, ev)
		return nil
	}
	for i, s := range steps {
		ev := WLEvent{Step: i + 1, Op: s.Op, TPlanUS: s.TMs * 1000}
		if s.TMs < 0 {
			return nil, wlBad("negative t_ms")
		}
		if !v3 && (s.Dx != nil || s.Keysym != nil || s.Fps != nil) {
			return nil, wlBad("dx/keysym/fps need schema " + wlSchemaV3)
		}
		switch s.Op {
		case "click", "scroll":
			if !InScreen(s.AtPx, bw, bh) {
				return nil, wlBad(s.Op + " needs on-screen at_px [x,y]")
			}
			ev.X, ev.Y = s.AtPx[0], s.AtPx[1]
			if s.Op == "scroll" {
				if s.Dy == nil || *s.Dy == 0 {
					return nil, wlBad("scroll needs dy != 0")
				}
				ev.Dy = *s.Dy
			}
		case "type":
			if s.Text == nil || utf8.RuneCountInString(*s.Text) != 1 || !typeable(*s.Text) {
				return nil, wlBad("type needs exactly one supported character")
			}
			ev.Text = *s.Text
		case "wait":
		case "move":
			if cp <= 0 {
				return nil, wlBad("move needs item cursor_px > 0")
			}
			if len(s.Path) == 0 || s.DtMs == nil || *s.DtMs <= 0 {
				return nil, wlBad("move needs a non-empty path and dt_ms > 0")
			}
			for j, p := range s.Path {
				if !InScreen(p, bw, bh) {
					return nil, wlBad("move path point off-screen")
				}
				pe := ev
				pe.Point, pe.X, pe.Y, pe.TPlanUS = j, p[0], p[1], ev.TPlanUS+int64(j)**s.DtMs*1000
				if err := add(pe); err != nil {
					return nil, err
				}
			}
			continue
		case "hscroll", "key", "drag":
			if !v3 {
				return nil, wlBad("op " + s.Op + " needs schema " + wlSchemaV3)
			}
			evs, err := parseV3Step(s, bw, bh)
			if err != nil {
				return nil, err
			}
			for _, e := range evs {
				e.Step = i + 1
				if coordOps[e.Op] && !InScreen([]int{e.X, e.Y}, bw, bh) {
					return nil, wlBad(s.Op + " point off-screen")
				}
				if err := add(e); err != nil {
					return nil, err
				}
			}
			continue
		default:
			return nil, wlBad("unknown op " + s.Op)
		}
		if err := add(ev); err != nil {
			return nil, err
		}
	}
	return it, nil
}

func typeable(s string) bool {
	r, _ := utf8.DecodeRuneInString(s)
	if r == ' ' {
		return true
	}
	if r >= 'A' && r <= 'Z' {
		r += 'a' - 'A'
	}
	_, ok := keycodes[string(r)]
	return ok
}

// StartSchema is the wlcap START file (P2 contract P22, frozen).
const StartSchema = "os.cu.wlcap-start/v1"

// ParseStart reads the t0_ns (CLOCK_MONOTONIC, > 0) of a wlcap START file.
func ParseStart(b []byte) (int64, error) {
	var s map[string]json.RawMessage
	if err := json.Unmarshal(b, &s); err != nil {
		return 0, rc.New("E_SCHEMA", "START: "+err.Error())
	}
	var schema, clock string
	if json.Unmarshal(s["schema"], &schema) != nil || schema != StartSchema ||
		json.Unmarshal(s["clock"], &clock) != nil || clock != "CLOCK_MONOTONIC" {
		return 0, rc.New("E_SCHEMA", "START: schema "+StartSchema+" with clock CLOCK_MONOTONIC required")
	}
	var t0 int64
	if err := json.Unmarshal(s["t0_ns"], &t0); err != nil || t0 <= 0 {
		return 0, rc.New("E_SCHEMA", "START: t0_ns must be a positive integer")
	}
	return t0, nil
}

// StartInfo is the validated wlcap START header: the capture clock origin, the measured screen and the run fence (W4).
type StartInfo struct {
	T0Ns     int64
	W, H     int
	RunNonce string // 128-bit lowercase hex, "" for a pre-W4 START
}

// ParseStartInfo reads t0_ns, the measured w x h (1..32767) and the optional run_nonce of a wlcap START file.
func ParseStartInfo(b []byte) (StartInfo, error) {
	var si StartInfo
	t0, err := ParseStart(b)
	if err != nil {
		return si, err
	}
	var s map[string]json.RawMessage
	json.Unmarshal(b, &s)
	var w, h int
	if json.Unmarshal(s["w"], &w) != nil || json.Unmarshal(s["h"], &h) != nil || !dimsOK(w, h) {
		return si, rc.New("E_SCHEMA", "START: w and h must be integers in 1..32767")
	}
	if raw, ok := s["run_nonce"]; ok {
		var n string
		if json.Unmarshal(raw, &n) != nil || !validNonce(n) {
			return si, rc.New("E_SCHEMA", "START: run_nonce must be 32 lowercase hex characters")
		}
		si.RunNonce = n
	}
	si.T0Ns, si.W, si.H = t0, w, h
	return si, nil
}

func validNonce(n string) bool {
	if len(n) != 32 {
		return false
	}
	for _, c := range n {
		if !(c >= '0' && c <= '9' || c >= 'a' && c <= 'f') {
			return false
		}
	}
	return true
}

func (it *WLItem) fire(act WLActuator, ev WLEvent) error {
	switch ev.Op {
	case "click":
		return act.Click(ev.X, ev.Y, 1, 1)
	case "scroll": // one wheel notch per step: the v2 scene advances its fixed dy sequence once per wheel event
		n := 1
		if ev.Dy < 0 {
			n = -1
		}
		return act.Scroll(ev.X, ev.Y, 0, n)
	case "type":
		return act.Type(ev.Text)
	case "move":
		return act.Move(ev.X, ev.Y)
	case "hscroll", "key", "drag_press", "drag_move", "drag_release":
		return fireV3Step(act, ev)
	}
	return nil // wait: no event, logged so the host sees the plan end
}

// RunWL fires every event at t0Ns + t_plan_us and writes one act row per
// event. A failed event is logged with rc 5 and ends the run (rc 5); stop()
// true before an event ends it with rc 3. ReleaseAll always runs once.
func RunWL(it *WLItem, act WLActuator, clk MonoClock, t0Ns int64, out io.Writer, stop func() bool) (r int, err error) {
	if it == nil || t0Ns <= 0 {
		return rc.Usage, rc.New("E_USAGE", "RunWL needs an item and t0_ns > 0")
	}
	defer func() {
		if rerr := act.ReleaseAll(); rerr != nil && err == nil {
			r, err = rc.Gate, rc.New("E_GATE", "release_all: "+rerr.Error())
		}
	}()
	for i, ev := range it.Events {
		target := t0Ns + ev.TPlanUS*1000
		for {
			if stop != nil && stop() {
				return rc.Anchor, rc.New("STOP", "stop file present")
			}
			d := target - clk.NowNs()
			if d <= 0 {
				break
			}
			clk.SleepNs(min(d, wlStopPollNs))
		}
		ferr := it.fire(act, ev)
		row := WLActRow{Schema: WLActSchema, Item: it.ID, ActuatorSeq: uint32(i + 1), Step: ev.Step, Point: ev.Point,
			Op: ev.Op, TPlanUS: ev.TPlanUS, TUS: (clk.NowNs() - t0Ns) / 1000}
		if ferr != nil {
			row.RC = rc.Gate
		}
		line, _ := json.Marshal(row)
		if _, werr := out.Write(append(line, '\n')); werr != nil {
			return rc.Usage, rc.New("E_IO", werr.Error())
		}
		if ferr != nil {
			return rc.Gate, rc.New("E_GATE", fmt.Sprintf("actuator %s at seq %d: %v", ev.Op, i+1, ferr))
		}
	}
	return rc.OK, nil
}

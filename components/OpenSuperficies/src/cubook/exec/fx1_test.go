package exec

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"testing"

	"opensuperficies.local/srf/src/cubook/action"
	"opensuperficies.local/srf/src/cubook/rc"
)

// detX records every XTEST call with its arguments.
type detX struct{ ev []string }

func (r *detX) MovePointer(x, y int16) error {
	r.ev = append(r.ev, fmt.Sprintf("mv%d,%d", x, y))
	return nil
}
func (r *detX) ButtonDown(b byte) error { r.ev = append(r.ev, fmt.Sprintf("bd%d", b)); return nil }
func (r *detX) ButtonUp(b byte) error   { r.ev = append(r.ev, fmt.Sprintf("bu%d", b)); return nil }
func (r *detX) KeyDown(k byte) error    { r.ev = append(r.ev, fmt.Sprintf("kd%d", k)); return nil }
func (r *detX) KeyUp(k byte) error      { r.ev = append(r.ev, fmt.Sprintf("ku%d", k)); return nil }

func evs(r *detX) string { return strings.Join(r.ev, " ") }

// Kills M3 (Click disabled).
func TestXTestClickEvents(t *testing.T) {
	x := &detX{}
	a := &XTestActuator{X: x}
	if err := a.Click(5, 6, 2, 2); err != nil {
		t.Fatal(err)
	}
	if got := evs(x); got != "mv5,6 bd2 bu2 bd2 bu2" {
		t.Fatalf("click events %q", got)
	}
	if len(a.btn) != 0 {
		t.Fatal("button left held")
	}
}

// Kills M2 (Drag disabled).
func TestXTestDragEvents(t *testing.T) {
	x := &detX{}
	a := &XTestActuator{X: x}
	if err := a.Drag(0, 0, 40, 20, 1, 4); err != nil {
		t.Fatal(err)
	}
	if got := evs(x); got != "mv0,0 bd1 mv10,5 mv20,10 mv30,15 mv40,20 bu1" {
		t.Fatalf("drag events %q", got)
	}
	if len(a.btn) != 0 {
		t.Fatal("button left held")
	}
}

// Kills M4 (Scroll disabled).
func TestXTestScrollEvents(t *testing.T) {
	x := &detX{}
	a := &XTestActuator{X: x}
	if err := a.Scroll(7, 8, -1, 2); err != nil {
		t.Fatal(err)
	}
	if got := evs(x); got != "mv7,8 bd5 bu5 bd5 bu5 bd6 bu6" {
		t.Fatalf("scroll events %q", got)
	}
	x.ev = nil
	a.Scroll(1, 1, 1, -200) // up clamped to 64 notches, then right
	if n := strings.Count(evs(x), "bd4"); n != 64 || !strings.HasSuffix(evs(x), "bd7 bu7") {
		t.Fatalf("scroll clamp: %d up notches, %q", n, evs(x)[len(evs(x))-12:])
	}
}

// Kills M19b (Key releases pressed as kdown) and M20b (Type drops the key release).
func TestXTestKeyAndTypeEvents(t *testing.T) {
	x := &detX{}
	a := &XTestActuator{X: x}
	if err := a.Key("ctrl+s"); err != nil {
		t.Fatal(err)
	}
	if got := evs(x); got != "kd37 kd39 ku39 ku37" {
		t.Fatalf("key events %q", got)
	}
	if len(a.key) != 0 {
		t.Fatalf("key left held after Key: %v", a.key)
	}
	x.ev = nil
	if err := a.Type("Hi"); err != nil {
		t.Fatal(err)
	}
	if got := evs(x); got != "kd50 kd43 ku43 ku50 kd31 ku31" {
		t.Fatalf("type events %q", got)
	}
	if len(a.key) != 0 {
		t.Fatalf("key left held after Type: %v", a.key)
	}
}

// toLoc resolves the drag source (xy) and a "region" destination.
type toLoc struct{ fail bool }

func (l toLoc) Locate(a *action.Action, f Frame) (Located, error) {
	switch a.Target.By {
	case "xy":
		return Located{Located: action.Located{By: "xy", BBox: [4]int{10, 10, 20, 20}, MatchLevel: "L0"}, RI: f.RI}, nil
	case "region":
		if l.fail {
			return Located{}, fmt.Errorf("region not found")
		}
		return Located{Located: action.Located{By: "region", BBox: [4]int{300, 300, 40, 40}, MatchLevel: "L0"}, RI: f.RI}, nil
	}
	return Located{}, fmt.Errorf("unknown")
}

const dragArgs = `"steps":4,"to":{"by":"region","region":"r1:abcd"}`

func TestDragTargetResolvedViaLocator(t *testing.T) {
	w := newWorld(true)
	e := New(Config{Deny: mustDeny()}, w, w, toLoc{}, nil)
	st, _ := e.Step(act("drag", "read", dragArgs))
	if st.RC != 0 || len(w.Calls) < 1 || w.Calls[0] != "drag:20,20>320,320,1,4" {
		t.Fatalf("rc %d calls %v (destination must be the located centre)", st.RC, w.Calls)
	}
	// xy destination still honoured verbatim
	w = newWorld(true)
	e = New(Config{Deny: mustDeny()}, w, w, toLoc{}, nil)
	e.Step(act("drag", "read", `"steps":3,"to":{"by":"xy","xy":[90,91]}`))
	if w.Calls[0] != "drag:20,20>90,91,1,3" {
		t.Fatalf("xy drag %v", w.Calls)
	}
}

func TestDragUnresolvedDestinationRc3NoEvents(t *testing.T) {
	w := newWorld(true)
	e := New(Config{Deny: mustDeny()}, w, w, toLoc{fail: true}, nil)
	st, _ := e.Step(act("drag", "read", dragArgs))
	if st.RC != 3 || st.Stop == nil || *st.Stop != "not_found" || st.Verdict != "CANNOT_SEE" || w.Events != 0 {
		t.Fatalf("rc %d stop %v verdict %s events %d", st.RC, st.Stop, st.Verdict, w.Events)
	}
}

func mustDeny() *DenyList {
	d, err := LoadDenyList(denyPath)
	if err != nil {
		panic(err)
	}
	return d
}

// Kills M17 (step cap off).
func TestStepCapBoundary(t *testing.T) {
	w := newWorld(true)
	e := newExec(w, loc("L0"), Config{})
	e.steps = 199
	if st, _ := e.Step(act("click", "read", `"button":1`)); st.RC != 0 {
		t.Fatalf("step 200 under script policy must run, rc %d", st.RC)
	}
	e.steps = 200
	before := w.Events
	st, _ := e.Step(act("click", "read", `"button":1`))
	if st.RC != 5 || st.Stop == nil || *st.Stop != "step_cap" || st.Verdict != "NO_ACT" || w.Events != before {
		t.Fatalf("step 201: rc %d stop %v verdict %s", st.RC, st.Stop, st.Verdict)
	}
	a := act("click", "read", `"button":1`)
	a.Policy = "explore"
	w = newWorld(true)
	e = newExec(w, loc("L0"), Config{})
	e.steps = 60
	if st, _ := e.Step(a); st.RC != 5 || w.Events != 0 {
		t.Fatalf("explore cap is 60: rc %d", st.RC)
	}
}

type churnObs struct {
	*world
	n int
}

func (c *churnObs) Observe() (Frame, error) {
	c.n++
	c.world.pix += fmt.Sprint(c.n) // never converges
	return c.world.Observe()
}

// Kills M18 (unsettled ignored).
func TestUnsettledIsCannotSee(t *testing.T) {
	w := newWorld(false)
	c := &churnObs{world: w}
	e := New(Config{Deny: mustDeny(), SettleK: 2, SettleMax: 5}, w, c, fixedLoc{loc("L0")}, nil)
	st, _ := e.Step(act("click", "read", `"button":1`))
	if !st.Unsettled || st.Verdict != "CANNOT_SEE" || st.Settle != 5 {
		t.Fatalf("unsettled %v verdict %s settle %d", st.Unsettled, st.Verdict, st.Settle)
	}
}

type countLog struct{ steps []action.Step }

func (c *countLog) Append(s action.Step) error { c.steps = append(c.steps, s); return nil }

// Kills M19 (retry_once ignored).
func TestRetryOnceRunsTwiceAndLogsOneLine(t *testing.T) {
	w := newWorld(false)
	n := 0
	w.OnEvent = func(string) {
		n++
		if n >= 2 {
			w.pix += "x" // only the retry has an effect
		}
	}
	lg := &countLog{}
	e := newExec(w, loc("L0"), Config{})
	e.Log = lg
	a := act("click", "read", `"button":1`)
	a.OnFail = "retry_once"
	r, out, err := e.Run(&action.Plan{Schema: action.SchemaPlan, Actions: []action.Action{*a}})
	if r != 0 || err != nil || w.Events != 2 || len(out) != 2 || out[1].Verdict != "EFFECT_OK" {
		t.Fatalf("rc %d events %d out %d err %v", r, w.Events, len(out), err)
	}
	if len(lg.steps) != 1 || lg.steps[0].Verdict != "EFFECT_OK" {
		t.Fatalf("log must hold one line per plan action, got %d", len(lg.steps))
	}
	// without retry_once the first NO_EFFECT halts rc 5 after one event
	w = newWorld(false)
	lg = &countLog{}
	e = newExec(w, loc("L0"), Config{})
	e.Log = lg
	a.OnFail = "stop"
	r, _, _ = e.Run(&action.Plan{Schema: action.SchemaPlan, Actions: []action.Action{*a}})
	if r != 5 || w.Events != 1 || len(lg.steps) != 1 {
		t.Fatalf("stop: rc %d events %d", r, w.Events)
	}
	// retry also failing: both attempts end NO_EFFECT, one logged line, rc 5
	w = newWorld(false)
	lg = &countLog{}
	e = newExec(w, loc("L0"), Config{})
	e.Log = lg
	a.OnFail = "retry_once"
	r, _, _ = e.Run(&action.Plan{Schema: action.SchemaPlan, Actions: []action.Action{*a}})
	if r != 5 || w.Events != 2 || len(lg.steps) != 1 {
		t.Fatalf("retry fail: rc %d events %d lines %d", r, w.Events, len(lg.steps))
	}
}

// Kills M20 (on_fail ask -> rc 7).
func TestOnFailAskIsRc7(t *testing.T) {
	for _, c := range []struct {
		onFail string
		want   int
	}{{"ask", 7}, {"stop", 5}} {
		w := newWorld(false)
		e := newExec(w, loc("L0"), Config{})
		a := act("click", "read", `"button":1`)
		a.OnFail = c.onFail
		r, _, _ := e.Run(&action.Plan{Schema: action.SchemaPlan, Actions: []action.Action{*a}})
		if r != c.want {
			t.Fatalf("on_fail %s: rc %d want %d", c.onFail, r, c.want)
		}
	}
}

func TestReplayRequiresLenAndStepMatch(t *testing.T) {
	p, stepsB, framesB := buildRun(t)
	lines := bytes.SplitAfter(bytes.TrimRight(stepsB, "\n"), []byte("\n"))
	if len(lines) != 2 {
		t.Fatalf("fixture has %d lines", len(lines))
	}
	l0 := append(append([]byte{}, lines[0]...), '\n')
	l0 = bytes.TrimRight(l0, "\n")
	l0 = append(l0, '\n')
	l1 := append(bytes.TrimRight(lines[1], "\n"), '\n')
	cases := map[string][]byte{
		"truncated":  l0,
		"duplicated": append(append([]byte{}, l0...), l0...),
		"reordered":  append(append([]byte{}, l1...), l0...),
	}
	for name, b := range cases {
		if _, err := replayOnce(t, p, b, framesB); rc.Of(err) != 5 {
			t.Errorf("%s log must diverge rc 5, got %v", name, err)
		}
	}
	if _, err := replayOnce(t, p, stepsB, framesB); err != nil {
		t.Fatalf("intact log must replay: %v", err)
	}
}

// fx1Mut loads the v3 prereg, applies mut to the item with the given id and re-marshals it.
func fx1Mut(t *testing.T, id string, mut func(it map[string]any)) []byte {
	t.Helper()
	b, err := os.ReadFile(prereg3)
	if err != nil {
		t.Fatal(err)
	}
	var m map[string]any
	if err := json.Unmarshal(b, &m); err != nil {
		t.Fatal(err)
	}
	for _, x := range m["items"].([]any) {
		if it := x.(map[string]any); it["id"] == id {
			mut(it)
		}
	}
	out, _ := json.Marshal(m)
	return out
}

func wantRc5(t *testing.T, err error, what string) {
	t.Helper()
	if err == nil || rc.Of(err) != 5 || !strings.Contains(err.Error(), "rect") {
		t.Fatalf("%s: err=%v", what, err)
	}
}

// JW4-06: pane_rect off the prereg resolution is refused at load time (InRect in production).
func TestV3RectOffScreenRefused(t *testing.T) {
	b := fx1Mut(t, "WL-3", func(it map[string]any) { it["pane_rect"] = []int{1800, 420, 320, 240} })
	_, err := LoadWLItem(b, "WL-3")
	wantRc5(t, err, "pane_rect x+w>W")
	b = fx1Mut(t, "WL-4", func(it map[string]any) { it["div_rect0"] = []int{100, 700, 640, 480} })
	_, err = LoadWLItem(b, "WL-4")
	wantRc5(t, err, "div_rect0 y+h>H")
	// the unmodified plan loads
	for _, id := range []string{"WL-3", "WL-4"} {
		raw, _ := os.ReadFile(prereg3)
		if _, err := LoadWLItem(raw, id); err != nil {
			t.Fatalf("%s: %v", id, err)
		}
	}
}

// JW4-06: the dragged div rect (div_rect0 translated by each drag's last point - at_px) must stay on screen.
func TestV3DragRectOffScreenRefused(t *testing.T) {
	b := fx1Mut(t, "WL-4", func(it map[string]any) { it["div_rect0"] = []int{1200, 200, 640, 480} }) // fits at rest, not after the drag
	_, err := LoadWLItem(b, "WL-4")
	wantRc5(t, err, "dragged div_rect")
}

// JW4-06: CheckBounds re-checks the rects against START's measured w x h.
func TestV3RectCheckBoundsStart(t *testing.T) {
	raw, _ := os.ReadFile(prereg3)
	it, err := LoadWLItem(raw, "WL-3")
	if err != nil {
		t.Fatal(err)
	}
	if err := it.CheckBounds(1920, 1080); err != nil {
		t.Fatalf("full screen: %v", err)
	}
	wantRc5(t, it.CheckBounds(1000, 1080), "START narrower than pane_rect")
}

package exec

import (
	"bytes"
	"encoding/json"
	"os"
	"strconv"
	"strings"
	"testing"

	"opensuperficies.local/srf/src/cubook/rc"
)

const prereg2 = "../../../bench/wl/prereg_wl_v2.json"

// fakeMono is a CLOCK_MONOTONIC stand-in: SleepNs advances it exactly.
type fakeMono struct {
	now    int64
	sleeps int
}

func (c *fakeMono) NowNs() int64 { return c.now }
func (c *fakeMono) SleepNs(ns int64) {
	c.sleeps++
	if ns > 0 {
		c.now += ns
	}
}

func loadItem(t *testing.T, id string) *WLItem {
	t.Helper()
	b, err := os.ReadFile(prereg2)
	if err != nil {
		t.Fatal(err)
	}
	it, err := LoadWLItem(b, id)
	if err != nil {
		t.Fatalf("load %s: %v", id, err)
	}
	return it
}

func actRows(t *testing.T, b []byte) []WLActRow {
	t.Helper()
	var out []WLActRow
	for _, ln := range strings.Split(strings.TrimRight(string(b), "\n"), "\n") {
		if ln == "" {
			continue
		}
		var r WLActRow
		d := json.NewDecoder(strings.NewReader(ln))
		d.DisallowUnknownFields()
		if err := d.Decode(&r); err != nil {
			t.Fatalf("row %q: %v", ln, err)
		}
		out = append(out, r)
	}
	return out
}

// mutPrereg loads the committed prereg, applies mut to item id and re-encodes it.
func mutPrereg(t *testing.T, id string, mut func(it map[string]any)) []byte {
	t.Helper()
	b, _ := os.ReadFile(prereg2)
	var m map[string]any
	if err := json.Unmarshal(b, &m); err != nil {
		t.Fatal(err)
	}
	for _, x := range m["items"].([]any) {
		if it := x.(map[string]any); it["id"] == id {
			mut(it)
		}
	}
	o, _ := json.Marshal(m)
	return o
}

func TestWLLoadExpandsCommittedPrereg(t *testing.T) {
	want := map[string]int{"WL-2b": 1, "WL-2": 20, "WL-1": 3, "WL-7": 8}
	for id, n := range want {
		it := loadItem(t, id)
		if len(it.Events) != n {
			t.Errorf("%s events %d want %d", id, len(it.Events), n)
		}
	}
	w7 := loadItem(t, "WL-7")
	if w7.CursorPx != 16 || len(w7.Events) != 8 {
		t.Fatalf("WL-7 %+v", w7)
	}
	for j, ev := range w7.Events {
		if ev.Op != "move" || ev.Step != 1 || ev.Point != j || ev.TPlanUS != int64(1000000+j*500000) {
			t.Errorf("WL-7 point %d: %+v", j, ev)
		}
	}
	if e := w7.Events[2]; e.X != 600 || e.Y != 300 {
		t.Errorf("WL-7 point 2 xy %+v", e)
	}
	w1 := loadItem(t, "WL-1")
	if e := w1.Events[1]; e.Op != "scroll" || e.Dy != 120 || e.X != 960 || e.Y != 540 || e.TPlanUS != 3000000 || e.Step != 2 {
		t.Errorf("WL-1 step 2 %+v", e)
	}
	w2 := loadItem(t, "WL-2")
	var s strings.Builder
	for _, e := range w2.Events {
		s.WriteString(e.Text)
	}
	if s.String() != "the quick brown fox." {
		t.Errorf("WL-2 text %q", s.String())
	}
}

// ADC2-W3-04 on the actuator side: the same plans labels_v2 refuses never reach XTEST.
func TestWLLoadRefusesBadPlans(t *testing.T) {
	cases := map[string][]byte{
		"scroll_dy0": mutPrereg(t, "WL-1", func(it map[string]any) {
			it["steps"].([]any)[0].(map[string]any)["dy"] = 0
		}),
		"scroll_no_dy": mutPrereg(t, "WL-1", func(it map[string]any) {
			delete(it["steps"].([]any)[0].(map[string]any), "dy")
		}),
		"move_no_cursor_px": mutPrereg(t, "WL-7", func(it map[string]any) { delete(it, "cursor_px") }),
		"move_dt0": mutPrereg(t, "WL-7", func(it map[string]any) {
			it["steps"].([]any)[0].(map[string]any)["dt_ms"] = 0
		}),
		"type_two_chars": mutPrereg(t, "WL-2", func(it map[string]any) {
			it["steps"].([]any)[0].(map[string]any)["text"] = "th"
		}),
		"t_ms_decreasing": mutPrereg(t, "WL-1", func(it map[string]any) {
			it["steps"].([]any)[2].(map[string]any)["t_ms"] = 10
		}),
		"unknown_op": mutPrereg(t, "WL-1", func(it map[string]any) {
			it["steps"].([]any)[0].(map[string]any)["op"] = "drag"
		}),
		"unknown_key": mutPrereg(t, "WL-1", func(it map[string]any) {
			it["steps"].([]any)[0].(map[string]any)["button"] = 1
		}),
		"float_t_ms": mutPrereg(t, "WL-1", func(it map[string]any) {
			it["steps"].([]any)[0].(map[string]any)["t_ms"] = 1000.5
		}),
	}
	for name, b := range cases {
		id := map[string]string{"move_no_cursor_px": "WL-7", "move_dt0": "WL-7", "type_two_chars": "WL-2"}[name]
		if id == "" {
			id = "WL-1"
		}
		if _, err := LoadWLItem(b, id); rc.Of(err) != rc.Usage {
			t.Errorf("%s: err %v rc %d want 2", name, err, rc.Of(err))
		}
	}
	b, _ := os.ReadFile(prereg2)
	if _, err := LoadWLItem(b, "WL-9"); rc.Of(err) != rc.Usage {
		t.Errorf("unknown item err %v", err)
	}
	if _, err := LoadWLItem([]byte(strings.Replace(string(b), "os.cu.prereg-wl/v2", "os.cu.prereg-wl/v1", 1)), "WL-1"); rc.Of(err) != rc.Usage {
		t.Errorf("v1 schema accepted: %v", err)
	}
}

func TestParseStartT0(t *testing.T) {
	const hdr = `{"schema":"os.cu.wlcap-start/v1","clock":"CLOCK_MONOTONIC",`
	ok := hdr + `"t0_ns":123456789,"delta_ns":16666667,"frames":360,"w":1920,"h":1080,"chunk_frames":60,"mode":"wl"}`
	if t0, err := ParseStart([]byte(ok)); err != nil || t0 != 123456789 {
		t.Fatalf("t0 %d err %v", t0, err)
	}
	for _, bad := range []string{``, `{}`, `nope`, hdr + `"t0_ns":0}`, hdr + `"t0_ns":-5}`, hdr + `"t0_ns":"12"}`, hdr + `"t0_ns":1.5}`,
		`{"schema":"os.cu.wl-start/v1","clock":"CLOCK_MONOTONIC","t0_ns":5}`,
		`{"schema":"os.cu.wlcap-start/v1","clock":"CLOCK_REALTIME","t0_ns":5}`, `{"t0_ns":5}`} {
		if _, err := ParseStart([]byte(bad)); err == nil {
			t.Errorf("START %q accepted", bad)
		}
	}
}

// CF-01: events fire at t0 + t_ms and t_us is the MEASURED monotonic offset from t0, not the plan.
func TestWLRunSchedulesRelativeToT0AndLogsMeasuredTUS(t *testing.T) {
	it := loadItem(t, "WL-1")
	const t0 = int64(5_000_000_000)
	clk := &fakeMono{now: t0 + 250_000}                             // actuator starts 250 us after capture t0
	fa := &FakeActuator{OnEvent: func(string) { clk.now += 7_000 }} // 7 us per XTEST batch
	var out bytes.Buffer
	r, err := RunWL(it, fa, clk, t0, &out, nil)
	if r != 0 || err != nil {
		t.Fatalf("rc %d err %v", r, err)
	}
	want := []string{"scroll:960,540,0,1", "scroll:960,540,0,1", "scroll:960,540,0,1", "release_all"}
	if strings.Join(fa.Calls, " ") != strings.Join(want, " ") {
		t.Fatalf("calls %v", fa.Calls)
	}
	rows := actRows(t, out.Bytes())
	if len(rows) != 3 {
		t.Fatalf("rows %d", len(rows))
	}
	for i, r := range rows {
		plan := int64(1_000_000 + 2_000_000*i)
		if r.Schema != WLActSchema || r.Item != "WL-1" || r.ActuatorSeq != uint32(i+1) || r.Step != i+1 || r.Op != "scroll" || r.RC != 0 {
			t.Errorf("row %d %+v", i, r)
		}
		if r.TPlanUS != plan || r.TUS != plan+7 {
			t.Errorf("row %d t_plan_us %d t_us %d want %d/%d", i, r.TPlanUS, r.TUS, plan, plan+7)
		}
	}
	// a late actuator (clock already 2 s past t0) logs the real time, not the plan time
	clk2 := &fakeMono{now: t0 + 2_000_000_000}
	var out2 bytes.Buffer
	if r, err := RunWL(it, &FakeActuator{}, clk2, t0, &out2, nil); r != 0 || err != nil {
		t.Fatalf("late rc %d %v", r, err)
	}
	if rows := actRows(t, out2.Bytes()); rows[0].TUS != 2_000_000 || rows[0].TPlanUS != 1_000_000 || rows[1].TUS != 3_000_000 {
		t.Errorf("late rows %+v", rows)
	}
	if _, err := RunWL(it, &FakeActuator{}, &fakeMono{now: 1}, 0, &bytes.Buffer{}, nil); rc.Of(err) != rc.Usage {
		t.Errorf("t0=0 accepted: %v", err)
	}
}

func TestWLRunPointerPathTypingAndWait(t *testing.T) {
	const t0 = int64(1_000_000_000)
	for _, tc := range []struct {
		id    string
		calls int
		first string
	}{{"WL-7", 8, "move:200,200"}, {"WL-2", 20, "type:1"}, {"WL-2b", 0, "release_all"}} {
		clk := &fakeMono{now: t0}
		fa := &FakeActuator{}
		var out bytes.Buffer
		if r, err := RunWL(loadItem(t, tc.id), fa, clk, t0, &out, nil); r != 0 || err != nil {
			t.Fatalf("%s rc %d %v", tc.id, r, err)
		}
		if fa.Events != tc.calls || fa.Releases != 1 || fa.Calls[0] != tc.first {
			t.Errorf("%s events %d releases %d calls %v", tc.id, fa.Events, fa.Releases, fa.Calls)
		}
		rows := actRows(t, out.Bytes())
		n := tc.calls
		if tc.id == "WL-2b" {
			n = 1 // the wait is logged (no event) so the host knows the plan end time
		}
		if len(rows) != n {
			t.Fatalf("%s rows %d", tc.id, len(rows))
		}
		last := rows[len(rows)-1]
		switch tc.id {
		case "WL-7":
			if last.Point != 7 || last.TUS != 4_500_000 || fa.Calls[7] != "move:1600,700" {
				t.Errorf("WL-7 last %+v %v", last, fa.Calls)
			}
		case "WL-2b":
			if last.Op != "wait" || last.TUS != 6_000_000 {
				t.Errorf("WL-2b %+v", last)
			}
		case "WL-2":
			if last.TUS != 5_750_000 || last.ActuatorSeq != 20 {
				t.Errorf("WL-2 %+v", last)
			}
		}
	}
}

func TestWLRunActuatorFailureLogsRc5AndReleases(t *testing.T) {
	const t0 = int64(1_000_000_000)
	fa := &FakeActuator{FailOp: "type"}
	var out bytes.Buffer
	r, err := RunWL(loadItem(t, "WL-2"), fa, &fakeMono{now: t0}, t0, &out, nil)
	if r != rc.Gate || err == nil {
		t.Fatalf("rc %d err %v", r, err)
	}
	rows := actRows(t, out.Bytes())
	if fa.Events != 1 || fa.Releases != 1 || len(rows) != 1 || rows[0].RC != rc.Gate {
		t.Errorf("events %d releases %d rows %+v", fa.Events, fa.Releases, rows)
	}
}

func TestWLRunStopFileHaltsBeforeNextEvent(t *testing.T) {
	const t0 = int64(1_000_000_000)
	fa := &FakeActuator{}
	var out bytes.Buffer
	stop := func() bool { return fa.Events >= 2 }
	r, err := RunWL(loadItem(t, "WL-1"), fa, &fakeMono{now: t0}, t0, &out, stop)
	if r != rc.Anchor || err == nil {
		t.Fatalf("rc %d err %v", r, err)
	}
	if fa.Events != 2 || fa.Releases != 1 || len(actRows(t, out.Bytes())) != 2 {
		t.Errorf("events %d releases %d", fa.Events, fa.Releases)
	}
}

func TestXTestActuatorMoveWarpsPointer(t *testing.T) {
	x := &wireX{}
	a := &XTestActuator{X: x}
	if err := a.Move(321, 123); err != nil || len(x.log) != 1 || x.log[0] != "m321,123" {
		t.Fatalf("move %v %v", err, x.log)
	}
}

type wireX struct{ log []string }

func (w *wireX) MovePointer(x, y int16) error {
	w.log = append(w.log, "m"+itoa(int(x))+","+itoa(int(y)))
	return nil
}
func (w *wireX) ButtonDown(b byte) error { w.log = append(w.log, "bd"+itoa(int(b))); return nil }
func (w *wireX) ButtonUp(b byte) error   { w.log = append(w.log, "bu"+itoa(int(b))); return nil }
func (w *wireX) KeyDown(k byte) error    { w.log = append(w.log, "kd"+itoa(int(k))); return nil }
func (w *wireX) KeyUp(k byte) error      { w.log = append(w.log, "ku"+itoa(int(k))); return nil }

func itoa(n int) string { return strconv.Itoa(n) }

func TestRealMonoAdvances(t *testing.T) {
	a := RealMono{}.NowNs()
	RealMono{}.SleepNs(1_000_000)
	if b := (RealMono{}).NowNs(); a <= 0 || b-a < 1_000_000 {
		t.Fatalf("mono %d -> %d", a, b)
	}
}

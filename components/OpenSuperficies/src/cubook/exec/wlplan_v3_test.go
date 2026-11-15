package exec

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"testing"
)

const prereg3 = "../../../bench/wl/prereg_wl_v3.json"

func v3Steps(t *testing.T, id string) []WLStep {
	t.Helper()
	b, err := os.ReadFile(prereg3)
	if err != nil {
		t.Fatal(err)
	}
	var m struct {
		Items []struct {
			ID    string          `json:"id"`
			Steps json.RawMessage `json:"steps"`
		} `json:"items"`
	}
	if err := json.Unmarshal(b, &m); err != nil {
		t.Fatal(err)
	}
	for _, it := range m.Items {
		if it.ID != id {
			continue
		}
		var steps []WLStep
		d := json.NewDecoder(bytes.NewReader(it.Steps))
		d.DisallowUnknownFields()
		if err := d.Decode(&steps); err != nil {
			t.Fatalf("decode %s: %v", id, err)
		}
		return steps
	}
	t.Fatalf("item %s absent", id)
	return nil
}

func i64(v int) *int      { return &v }
func sp(s string) *string { return &s }

func TestV3HscrollKeyDragParse(t *testing.T) {
	hs := v3Steps(t, "WL-1h")
	ev, err := parseV3Step(hs[1], 1920, 1080)
	if err != nil || len(ev) != 1 || ev[0].Op != "hscroll" || ev[0].Dx != 120 || ev[0].X != 960 || ev[0].Y != 540 || ev[0].TPlanUS != 1_800_000 {
		t.Fatalf("hscroll: %+v %v", ev, err)
	}
	ks := v3Steps(t, "WL-1L")
	ev, err = parseV3Step(ks[3], 1920, 1080)
	if err != nil || len(ev) != 1 || ev[0].Op != "key" || ev[0].Text != "PageDown" || ev[0].TPlanUS != 3_400_000 {
		t.Fatalf("key: %+v %v", ev, err)
	}
	ds := v3Steps(t, "WL-4")
	ev, err = parseV3Step(ds[0], 1920, 1080)
	if err != nil || len(ev) != 62 {
		t.Fatalf("drag1: n=%d %v", len(ev), err)
	}
	if ev[0].Op != "drag_press" || ev[0].X != 400 || ev[0].Y != 300 || ev[0].TPlanUS != 1_000_000 {
		t.Fatalf("press: %+v", ev[0])
	}
	if ev[1].Op != "drag_move" || ev[1].Point != 1 || ev[1].X != 403 || ev[1].TPlanUS != 1_016_666 {
		t.Fatalf("move1: %+v", ev[1])
	}
	if ev[60].Point != 60 || ev[60].X != 580 || ev[60].TPlanUS != 2_000_000 {
		t.Fatalf("move60: %+v", ev[60])
	}
	if ev[61].Op != "drag_release" || ev[61].Point != 61 || ev[61].TPlanUS != 2_016_666 {
		t.Fatalf("release: %+v", ev[61])
	}
	ev, err = parseV3Step(ds[1], 1920, 1080)
	if err != nil || len(ev) != 32 || ev[30].X != 1180 || ev[0].TPlanUS != 2_600_000 {
		t.Fatalf("drag2: n=%d %v", len(ev), err)
	}
	bad := []WLStep{
		{TMs: 0, Op: "hscroll", AtPx: []int{1, 1}},
		{TMs: 0, Op: "hscroll", AtPx: []int{1, 1}, Dx: i64(0)},
		{TMs: 0, Op: "key", Keysym: sp("Return")},
		{TMs: 0, Op: "key"},
		{TMs: 0, Op: "drag", AtPx: []int{1, 1}, Path: [][]int{{2, 2}}, Fps: i64(30)},
		{TMs: 0, Op: "drag", AtPx: []int{1, 1}, Fps: i64(60)},
		{TMs: -1, Op: "key", Keysym: sp("PageDown")},
		{TMs: 0, Op: "zoom"},
	}
	for i, s := range bad {
		if _, err := parseV3Step(s, 1920, 1080); err == nil {
			t.Fatalf("bad step %d accepted", i)
		}
	}
}

func TestV3DragPathOffScreenRefused(t *testing.T) {
	mk := func(at []int, path [][]int) WLStep {
		return WLStep{TMs: 0, Op: "drag", AtPx: at, Path: path, Fps: i64(60)}
	}
	if ev, err := parseV3Step(mk([]int{0, 0}, [][]int{{1919, 1079}}), 1920, 1080); err != nil || len(ev) != 3 {
		t.Fatalf("in-bounds drag refused: %v", err)
	}
	for _, c := range []struct {
		at   []int
		path [][]int
	}{
		{[]int{100, 100}, [][]int{{1920, 100}}},
		{[]int{100, 100}, [][]int{{100, 1080}}},
		{[]int{100, 100}, [][]int{{200, 100}, {-1, 100}}},
		{[]int{1920, 100}, [][]int{{200, 100}}},
		{[]int{100, 100}, [][]int{{1, 2, 3}}},
	} {
		if _, err := parseV3Step(mk(c.at, c.path), 1920, 1080); err == nil {
			t.Fatalf("off-screen drag accepted: %v %v", c.at, c.path)
		}
	}
	if _, err := parseV3Step(WLStep{Op: "hscroll", AtPx: []int{1920, 5}, Dx: i64(40)}, 1920, 1080); err == nil {
		t.Fatal("off-screen hscroll accepted")
	}
	if _, err := parseV3Step(WLStep{Op: "hscroll", AtPx: []int{1919, 5}, Dx: i64(40)}, 1920, 1080); err != nil {
		t.Fatalf("edge hscroll refused: %v", err)
	}
}

func TestV3FireOrderPressMoveRelease(t *testing.T) {
	f := &FakeActuator{}
	st := WLStep{TMs: 0, Op: "drag", AtPx: []int{10, 20}, Path: [][]int{{13, 20}, {16, 20}}, Fps: i64(60)}
	ev, err := parseV3Step(st, 1920, 1080)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range ev {
		if err := fireV3Step(f, e); err != nil {
			t.Fatal(err)
		}
	}
	want := []string{"drag_press:10,20,1", "move:13,20", "move:16,20", "drag_release:1"}
	if fmt.Sprint(f.Calls) != fmt.Sprint(want) {
		t.Fatalf("calls %v want %v", f.Calls, want)
	}
	f2 := &FakeActuator{}
	for _, e := range []WLEvent{{Op: "hscroll", X: 5, Y: 6, Dx: -120}, {Op: "hscroll", X: 5, Y: 6, Dx: 40}, {Op: "key", Text: "PageDown"}} {
		if err := fireV3Step(f2, e); err != nil {
			t.Fatal(err)
		}
	}
	want2 := []string{"scroll:5,6,-1,0", "scroll:5,6,1,0", "key:pagedown"}
	if fmt.Sprint(f2.Calls) != fmt.Sprint(want2) {
		t.Fatalf("calls %v want %v", f2.Calls, want2)
	}
	if err := fireV3Step(f2, WLEvent{Op: "bogus"}); err == nil {
		t.Fatal("unknown event fired")
	}
}

func TestV3LoadItemRunWL(t *testing.T) {
	b, err := os.ReadFile(prereg3)
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]int{"WL-1s": 12, "WL-1c": 3, "WL-1L": 6, "WL-1h": 6, "WL-4": 94, "WL-3": 1}
	for id, n := range want {
		it, err := LoadWLItem(b, id)
		if err != nil {
			t.Fatalf("%s: %v", id, err)
		}
		if len(it.Events) != n {
			t.Fatalf("%s: %d events want %d", id, len(it.Events), n)
		}
	}
	it, _ := LoadWLItem(b, "WL-4")
	f := &FakeActuator{}
	var out bytes.Buffer
	if r, err := RunWL(it, f, &fakeMono{now: 1}, 1, &out, nil); r != 0 || err != nil {
		t.Fatalf("RunWL rc=%d %v", r, err)
	}
	rows := actRows(t, out.Bytes())
	if len(rows) != 94 || rows[0].Op != "drag_press" || rows[61].Op != "drag_release" || rows[62].Op != "drag_press" || rows[93].Op != "drag_release" {
		t.Fatalf("act rows: n=%d", len(rows))
	}
	if f.Calls[0] != "drag_press:400,300,1" || f.Calls[len(f.Calls)-2] != "drag_release:1" || f.Calls[len(f.Calls)-1] != "release_all" {
		t.Fatalf("calls head/tail: %v ... %v", f.Calls[0], f.Calls[len(f.Calls)-2:])
	}
	if _, err := LoadWLItem(bytes.ReplaceAll(b, []byte(`"x":1`), nil), "WL-9"); err == nil {
		t.Fatal("unknown item accepted")
	}
}

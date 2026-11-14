package exec

import (
	"math"
	"os"
	"testing"

	"opensuperficies.local/srf/src/cubook/rc"
)

// W4 lane M: coordinates are checked against the measured screen W x H (ADC3-W3-03, AW4-07), in int64.

func TestAtPxOutsideScene(t *testing.T) {
	for _, c := range []struct {
		p    []int
		w, h int
		want bool
	}{
		{[]int{0, 0}, 1920, 1080, true}, {[]int{1919, 1079}, 1920, 1080, true},
		{[]int{1920, 0}, 1920, 1080, false}, {[]int{0, 1080}, 1920, 1080, false}, {[]int{-1, 5}, 1920, 1080, false},
		{[]int{5}, 1920, 1080, false}, {[]int{1, 2, 3}, 1920, 1080, false},
		{[]int{0, 0}, 0, 1080, false}, {[]int{0, 0}, 1920, -1, false}, {[]int{0, 0}, 32768, 1080, false},
		{[]int{32766, 32766}, 32767, 32767, true}, {[]int{32767, 0}, 32767, 32767, false},
	} {
		if got := InScreen(c.p, c.w, c.h); got != c.want {
			t.Errorf("InScreen(%v,%d,%d)=%v want %v", c.p, c.w, c.h, got, c.want)
		}
	}
	// a plan whose at_px is legal for XTEST (<= 32767) but outside the 1920x1080 scene is refused against START's w x h
	b, err := os.ReadFile(prereg2)
	if err != nil {
		t.Fatal(err)
	}
	it, err := LoadWLItem(b, "WL-1")
	if err != nil {
		t.Fatal(err)
	}
	if err := it.CheckBounds(1920, 1080); err != nil {
		t.Fatalf("WL-1 inside 1920x1080: %v", err)
	}
	it.Events[0].X = 1920
	err = it.CheckBounds(1920, 1080)
	if err == nil || rc.Of(err) != rc.Gate {
		t.Fatalf("at_px x == W accepted or wrong rc: %v", err)
	}
}

func TestDragBoundsInt64(t *testing.T) {
	// no int overflow can turn a hostile rect or point into an accepted one
	if InScreen([]int{math.MaxInt, 0}, 1920, 1080) || InScreen([]int{0, math.MinInt}, 1920, 1080) {
		t.Error("extreme point accepted")
	}
	for _, c := range []struct {
		r    []int
		want bool
	}{
		{[]int{0, 0, 1920, 1080}, true}, {[]int{100, 200, 640, 480}, true},
		{[]int{1281, 0, 640, 480}, false}, {[]int{0, 601, 640, 480}, false},
		{[]int{0, 0, 0, 5}, false}, {[]int{0, 0, 5, -1}, false}, {[]int{-1, 0, 5, 5}, false},
		{[]int{math.MaxInt, 0, math.MaxInt, 5}, false}, {[]int{5, 5, math.MaxInt, 5}, false}, {[]int{1, 2, 3}, false},
	} {
		if got := InRect(c.r, 1920, 1080); got != c.want {
			t.Errorf("InRect(%v)=%v want %v", c.r, got, c.want)
		}
	}
	it := &WLItem{ID: "WL-4", Events: []WLEvent{{Op: "drag_press", X: 100, Y: 200}, {Op: "drag_move", X: 880, Y: 200}, {Op: "drag_release", X: 880, Y: 200}}}
	if err := it.CheckBounds(1920, 1080); err != nil {
		t.Fatalf("in-scene drag: %v", err)
	}
	it.Events[1].X = 1920
	if err := it.CheckBounds(1920, 1080); err == nil || rc.Of(err) != rc.Gate {
		t.Fatalf("drag point x == W accepted: %v", err)
	}
	it.Events[1].X = 880
	it.Events[2].Y = math.MaxInt
	if err := it.CheckBounds(1920, 1080); err == nil {
		t.Fatal("MaxInt drag point accepted")
	}
}

func TestParseStartInfoWH(t *testing.T) {
	ok := `{"schema":"os.cu.wlcap-start/v1","clock":"CLOCK_MONOTONIC","t0_ns":5,"w":1920,"h":1080,"run_nonce":"0123456789abcdef0123456789abcdef"}`
	si, err := ParseStartInfo([]byte(ok))
	if err != nil || si.T0Ns != 5 || si.W != 1920 || si.H != 1080 || si.RunNonce != "0123456789abcdef0123456789abcdef" {
		t.Fatalf("%+v %v", si, err)
	}
	for _, bad := range []string{`{"schema":"os.cu.wlcap-start/v1","clock":"CLOCK_MONOTONIC","t0_ns":5}`,
		`{"schema":"os.cu.wlcap-start/v1","clock":"CLOCK_MONOTONIC","t0_ns":5,"w":0,"h":1080}`,
		`{"schema":"os.cu.wlcap-start/v1","clock":"CLOCK_MONOTONIC","t0_ns":5,"w":40000,"h":1080}`,
		`{"schema":"os.cu.wlcap-start/v1","clock":"CLOCK_MONOTONIC","t0_ns":5,"w":1920,"h":1080,"run_nonce":"xyz"}`} {
		if _, err := ParseStartInfo([]byte(bad)); err == nil {
			t.Errorf("accepted %s", bad)
		}
	}
}

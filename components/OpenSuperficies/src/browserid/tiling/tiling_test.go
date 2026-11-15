package tiling_test

import (
	"errors"
	"reflect"
	"testing"

	"opensuperficies.local/srf/src/browserid/tiling"
)

func window(id string, x, y, w, h int) tiling.Window {
	return tiling.Window{SurfaceID: "perplexity-1/" + id, Rect: tiling.Rect{X: x, Y: y, Width: w, Height: h}}
}

func kindIs(t *testing.T, err error, want tiling.ErrorKind) {
	t.Helper()
	var typed *tiling.Error
	if !errors.As(err, &typed) || typed.Kind != want {
		t.Fatalf("error = %v; want typed kind %q", err, want)
	}
}

func TestBidTilingExactGeometry(t *testing.T) {
	cases := []struct {
		name      string
		width     int
		height    int
		n         int
		cols      int
		rows      int
		want      []tiling.Window
		wantError tiling.ErrorKind
	}{
		{"2560x1800_N1", 2560, 1800, 1, 1, 1, []tiling.Window{
			window("w1", 0, 0, 2560, 1800),
		}, ""},
		{"2560x1800_N2", 2560, 1800, 2, 2, 1, []tiling.Window{
			window("w1", 0, 0, 1280, 1800), window("w2", 1280, 0, 1280, 1800),
		}, ""},
		{"2560x1800_N3", 2560, 1800, 3, 2, 2, []tiling.Window{
			window("w1", 0, 0, 1280, 900), window("w2", 1280, 0, 1280, 900),
			window("w3", 0, 900, 1280, 900),
		}, ""},
		{"2560x1800_N4", 2560, 1800, 4, 2, 2, []tiling.Window{
			window("w1", 0, 0, 1280, 900), window("w2", 1280, 0, 1280, 900),
			window("w3", 0, 900, 1280, 900), window("w4", 1280, 900, 1280, 900),
		}, ""},
		{"2560x1800_N6_REFUSE", 2560, 1800, 6, 0, 0, nil, tiling.InsufficientSpace},
		{"3840x2160_N1", 3840, 2160, 1, 1, 1, []tiling.Window{
			window("w1", 0, 0, 3840, 2160),
		}, ""},
		{"3840x2160_N2", 3840, 2160, 2, 2, 1, []tiling.Window{
			window("w1", 0, 0, 1920, 2160), window("w2", 1920, 0, 1920, 2160),
		}, ""},
		{"3840x2160_N3", 3840, 2160, 3, 3, 1, []tiling.Window{
			window("w1", 0, 0, 1280, 2160), window("w2", 1280, 0, 1280, 2160),
			window("w3", 2560, 0, 1280, 2160),
		}, ""},
		{"3840x2160_N4", 3840, 2160, 4, 3, 2, []tiling.Window{
			window("w1", 0, 0, 1280, 1080), window("w2", 1280, 0, 1280, 1080),
			window("w3", 2560, 0, 1280, 1080), window("w4", 0, 1080, 1280, 1080),
		}, ""},
		{"3840x2160_N6", 3840, 2160, 6, 3, 2, []tiling.Window{
			window("w1", 0, 0, 1280, 1080), window("w2", 1280, 0, 1280, 1080),
			window("w3", 2560, 0, 1280, 1080), window("w4", 0, 1080, 1280, 1080),
			window("w5", 1280, 1080, 1280, 1080), window("w6", 2560, 1080, 1280, 1080),
		}, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			layout, err := tiling.Plan(tiling.Spec{ProfileID: "perplexity-1", DisplayWidth: tc.width, DisplayHeight: tc.height, Windows: tc.n})
			if tc.wantError != "" {
				kindIs(t, err, tc.wantError)
				if len(layout.Windows) != 0 {
					t.Fatalf("refusal returned windows: %+v", layout.Windows)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if layout.Columns != tc.cols || layout.Rows != tc.rows ||
				layout.DisplayWidth != tc.width || layout.DisplayHeight != tc.height ||
				!reflect.DeepEqual(layout.Windows, tc.want) {
				t.Fatalf("layout %+v; want cols=%d rows=%d windows=%+v", layout, tc.cols, tc.rows, tc.want)
			}
		})
	}
}

func TestBidTilingRemaindersAndArgv(t *testing.T) {
	layout, err := tiling.Plan(tiling.Spec{ProfileID: "perplexity-1", DisplayWidth: 2561, DisplayHeight: 1801, Windows: 4})
	if err != nil {
		t.Fatal(err)
	}
	want := []tiling.Window{
		window("w1", 0, 0, 1280, 900), window("w2", 1280, 0, 1281, 900),
		window("w3", 0, 900, 1280, 901), window("w4", 1280, 900, 1281, 901),
	}
	if !reflect.DeepEqual(layout.Windows, want) {
		t.Fatalf("remainder layout = %+v; want %+v", layout.Windows, want)
	}
	for _, tc := range []struct {
		index int
		args  []string
	}{
		{0, []string{"--window-position=0,0", "--window-size=1280,900"}},
		{3, []string{"--window-position=1280,900", "--window-size=1281,901"}},
	} {
		if got := layout.Windows[tc.index].ChromiumArgs(); !reflect.DeepEqual(got, tc.args) {
			t.Fatalf("args %d = %v; want %v", tc.index, got, tc.args)
		}
	}
}

func TestBidTilingCoordinateRoundTrips(t *testing.T) {
	for _, n := range []int{1, 2, 3, 4, 6} {
		layout, err := tiling.Plan(tiling.Spec{ProfileID: "perplexity-1", DisplayWidth: 3841, DisplayHeight: 2161, Windows: n})
		if err != nil {
			t.Fatal(err)
		}
		for _, w := range layout.Windows {
			for _, local := range []tiling.Point{
				{X: 0, Y: 0},
				{X: w.Rect.Width - 1, Y: 0},
				{X: 0, Y: w.Rect.Height - 1},
				{X: w.Rect.Width - 1, Y: w.Rect.Height - 1},
				{X: w.Rect.Width / 2, Y: w.Rect.Height / 2},
			} {
				display, err := layout.WindowToDisplay(w.SurfaceID, local)
				if err != nil {
					t.Fatal(err)
				}
				id, back, err := layout.DisplayToWindow(display)
				if err != nil || id != w.SurfaceID || back != local {
					t.Fatalf("N=%d id=%q local=%v display=%v inverse: id=%q point=%v err=%v", n, w.SurfaceID, local, display, id, back, err)
				}
			}
		}
	}
}

func TestBidTilingPointRefusals(t *testing.T) {
	layout, err := tiling.Plan(tiling.Spec{ProfileID: "perplexity-1", DisplayWidth: 2560, DisplayHeight: 1800, Windows: 3})
	if err != nil {
		t.Fatal(err)
	}
	for _, p := range []tiling.Point{{X: -1}, {Y: -1}, {X: 2560}, {Y: 1800}, {X: 1280, Y: 900}, {X: 2559, Y: 1799}} {
		_, _, err := layout.DisplayToWindow(p)
		kindIs(t, err, tiling.OutsideWindow)
	}
	for _, p := range []tiling.Point{{X: -1}, {Y: -1}, {X: 1280}, {Y: 900}} {
		_, err := layout.WindowToDisplay("perplexity-1/w3", p)
		kindIs(t, err, tiling.OutsideWindow)
	}
	_, err = layout.WindowToDisplay("perplexity-2/w1", tiling.Point{})
	kindIs(t, err, tiling.UnknownSurface)
	_, err = layout.WindowToDisplay("perplexity-1/w4", tiling.Point{})
	kindIs(t, err, tiling.UnknownSurface)
}

func TestBidTilingMinimumRefusals(t *testing.T) {
	for _, tc := range []tiling.Spec{
		{ProfileID: "google-main", DisplayWidth: 1263, DisplayHeight: 900, Windows: 1},
		{ProfileID: "google-main", DisplayWidth: 1264, DisplayHeight: 899, Windows: 1},
		{ProfileID: "google-main", DisplayWidth: 2560, DisplayHeight: 1800, Windows: 6},
		{ProfileID: "google-main", DisplayWidth: 3840, DisplayHeight: 2160, Windows: 7},
		{ProfileID: "google-main", DisplayWidth: 10, DisplayHeight: 10, Windows: 4, MinWidth: 6, MinHeight: 3},
	} {
		layout, err := tiling.Plan(tc)
		kindIs(t, err, tiling.InsufficientSpace)
		if len(layout.Windows) != 0 {
			t.Fatalf("partial windows on failed layout: %+v", layout)
		}
	}
	layout, err := tiling.Plan(tiling.Spec{ProfileID: "chatgpt-1", DisplayWidth: 7680, DisplayHeight: 4320, Windows: 16})
	if err != nil || len(layout.Windows) != 16 {
		t.Fatalf("maximum supported layout: %+v, %v", layout, err)
	}
}

func TestBidTilingInvalidInput(t *testing.T) {
	base := tiling.Spec{ProfileID: "google-main", DisplayWidth: 2560, DisplayHeight: 1800, Windows: 2}
	bad := []tiling.Spec{
		{ProfileID: "", DisplayWidth: 2560, DisplayHeight: 1800, Windows: 2},
		{ProfileID: "google/main", DisplayWidth: 2560, DisplayHeight: 1800, Windows: 2},
		{ProfileID: " google", DisplayWidth: 2560, DisplayHeight: 1800, Windows: 2},
		{ProfileID: "google\nmain", DisplayWidth: 2560, DisplayHeight: 1800, Windows: 2},
		{ProfileID: "google-main", DisplayWidth: 0, DisplayHeight: 1800, Windows: 2},
		{ProfileID: "google-main", DisplayWidth: 7681, DisplayHeight: 1800, Windows: 2},
		{ProfileID: "google-main", DisplayWidth: 2560, DisplayHeight: 4321, Windows: 2},
		{ProfileID: "google-main", DisplayWidth: 2560, DisplayHeight: 1800, Windows: 0},
		{ProfileID: "google-main", DisplayWidth: 2560, DisplayHeight: 1800, Windows: 17},
	}
	negativeMinW, negativeMinH := base, base
	negativeMinW.MinWidth = -1
	negativeMinH.MinHeight = -1
	bad = append(bad, negativeMinW, negativeMinH)
	for _, tc := range bad {
		layout, err := tiling.Plan(tc)
		kindIs(t, err, tiling.InvalidInput)
		if len(layout.Windows) != 0 {
			t.Fatalf("partial windows for invalid input: %+v", layout)
		}
	}
}

// Pixel ownership is checked exhaustively on small virtual displays, including
// irregular cell sizes and grids with an unused tail. No display services run.
func TestBidTilingNoOverlapsBruteForce(t *testing.T) {
	for width := 1; width <= 9; width++ {
		for height := 1; height <= 8; height++ {
			for n := 1; n <= tiling.MaxWindows; n++ {
				layout, err := tiling.Plan(tiling.Spec{
					ProfileID: "google-main", DisplayWidth: width, DisplayHeight: height,
					Windows: n, MinWidth: 2, MinHeight: 2,
				})
				if err != nil {
					kindIs(t, err, tiling.InsufficientSpace)
					continue
				}
				if len(layout.Windows) != n {
					t.Fatalf("%dx%d, n=%d: got %d windows", width, height, n, len(layout.Windows))
				}
				for _, w := range layout.Windows {
					if w.Rect.Width < 2 || w.Rect.Height < 2 || w.Rect.X < 0 || w.Rect.Y < 0 ||
						w.Rect.X+w.Rect.Width > width || w.Rect.Y+w.Rect.Height > height {
						t.Fatalf("%dx%d, n=%d: invalid rect %+v", width, height, n, w)
					}
				}
				for y := 0; y < height; y++ {
					for x := 0; x < width; x++ {
						p := tiling.Point{X: x, Y: y}
						owners := 0
						for _, w := range layout.Windows {
							if w.Rect.Contains(p) {
								owners++
							}
						}
						if owners > 1 || (owners == 0 && n == layout.Columns*layout.Rows) {
							t.Fatalf("%dx%d n=%d point=%v owned %d times", width, height, n, p, owners)
						}
						_, _, err := layout.DisplayToWindow(p)
						if owners == 0 {
							kindIs(t, err, tiling.OutsideWindow)
						} else if err != nil {
							t.Fatal(err)
						}
					}
				}
			}
		}
	}
}

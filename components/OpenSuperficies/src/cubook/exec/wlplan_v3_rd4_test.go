package exec

import (
	"encoding/json"
	"testing"

	"opensuperficies.local/srf/src/cubook/rc"
)

func TestV3DivRect0OffScreenRefused(t *testing.T) {
	manifest := map[string]any{
		"schema":      "os.cu.prereg-wl/v3",
		"resolutions": [][]int{{1920, 1080}},
		"items": []map[string]any{
			{
				"id":        "WL-RD4-1",
				"div_rect0": []int{1500, 0, 640, 480},
				"steps": []map[string]any{
					{"t_ms": 0, "op": "hscroll", "at_px": []int{960, 540}, "dx": 120},
				},
			},
		},
	}
	b, err := json.Marshal(manifest)
	if err != nil {
		t.Fatal(err)
	}

	_, err = LoadWLItem(b, "WL-RD4-1")
	if err == nil {
		t.Fatal("expected off-screen div_rect0 to be refused, got nil error")
	}
	if code := rc.Of(err); code != rc.Gate {
		t.Fatalf("expected rc %d (E_GATE), got rc %d (%v)", rc.Gate, code, err)
	}
}

func TestV3DragRectVerticalOffScreenRefused(t *testing.T) {
	manifest := map[string]any{
		"schema":      "os.cu.prereg-wl/v3",
		"resolutions": [][]int{{1920, 1080}},
		"items": []map[string]any{
			{
				"id":        "WL-RD4-2",
				"div_rect0": []int{100, 800, 200, 200},
				"steps": []map[string]any{
					{
						"t_ms":  0,
						"op":    "drag",
						"at_px": []int{100, 100},
						"path":  [][]int{{100, 200}},
						"fps":   60,
					},
				},
			},
		},
	}
	b, err := json.Marshal(manifest)
	if err != nil {
		t.Fatal(err)
	}

	_, err = LoadWLItem(b, "WL-RD4-2")
	if err == nil {
		t.Fatal("expected vertically off-screen derived div_rect to be refused, got nil error")
	}
	if code := rc.Of(err); code != rc.Gate {
		t.Fatalf("expected rc %d (E_GATE), got rc %d (%v)", rc.Gate, code, err)
	}
}

func TestV3RectLen3Schema(t *testing.T) {
	manifest := map[string]any{
		"schema":      "os.cu.prereg-wl/v3",
		"resolutions": [][]int{{1920, 1080}},
		"items": []map[string]any{
			{
				"id":        "WL-RD4-3",
				"div_rect0": []int{100, 100, 200},
				"steps": []map[string]any{
					{"t_ms": 0, "op": "hscroll", "at_px": []int{960, 540}, "dx": 120},
				},
			},
		},
	}
	b, err := json.Marshal(manifest)
	if err != nil {
		t.Fatal(err)
	}

	_, err = LoadWLItem(b, "WL-RD4-3")
	if err == nil {
		t.Fatal("expected 3-integer rect to be refused, got nil error")
	}
	if code := rc.Of(err); code != rc.Usage {
		t.Fatalf("expected rc %d (E_SCHEMA), got rc %d (%v)", rc.Usage, code, err)
	}
}

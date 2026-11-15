package exec

// prereg v3 actuation (lane V): ops hscroll{dx}, key{keysym PageDown} and
// drag{at_px, path, fps 60}. wlplan.go (lane M) declares the parseV3Step /
// fireV3Step hooks; this file installs them. A drag lowers to one press event,
// one move event per path point and one release event; point j is scheduled at
// t_ms*1000 + j*1e6/60 (drift-free 60 Hz grid, VG-0053 F_60(j)).

import (
	"encoding/json"
	"fmt"
	"strings"

	"opensuperficies.local/srf/src/cubook/rc"
)

const (
	v3DragFps    = 60
	v3MaxDragPts = 1000
	v3Key        = "PageDown"
)

func init() {
	parseV3Step = parseV3
	fireV3Step = fireV3
}

func v3Bad(msg string) error { return rc.New("E_SCHEMA", "prereg v3: "+msg) }

// dragActuator is the press/release half of a pointer drag (moves use WLActuator.Move).
type dragActuator interface {
	DragPress(x, y, button int) error
	DragRelease(button int) error
}

// DragPress warps to (x,y) and presses the button.
func (a *XTestActuator) DragPress(x, y, button int) error {
	if err := a.X.MovePointer(int16(x), int16(y)); err != nil {
		return err
	}
	return a.down(byte(button))
}

// DragRelease releases the button.
func (a *XTestActuator) DragRelease(button int) error { return a.up(byte(button)) }

func (f *FakeActuator) DragPress(x, y, b int) error {
	return f.rec("drag_press", fmt.Sprintf("%d,%d,%d", x, y, b))
}
func (f *FakeActuator) DragRelease(b int) error { return f.rec("drag_release", fmt.Sprint(b)) }

// parseV3 lowers one v3 step to events against a w x h screen. Events carry
// Point and TPlanUS; the caller sets Step.
func parseV3(s WLStep, w, h int) ([]WLEvent, error) {
	if s.TMs < 0 {
		return nil, v3Bad("negative t_ms")
	}
	t0 := s.TMs * 1000
	switch s.Op {
	case "hscroll":
		if !InScreen(s.AtPx, w, h) {
			return nil, v3Bad("hscroll needs on-screen at_px [x,y]")
		}
		if s.Dx == nil || *s.Dx == 0 {
			return nil, v3Bad("hscroll needs dx != 0")
		}
		return []WLEvent{{Op: s.Op, TPlanUS: t0, X: s.AtPx[0], Y: s.AtPx[1], Dx: *s.Dx}}, nil
	case "key":
		if s.Keysym == nil || *s.Keysym != v3Key {
			return nil, v3Bad("key supports keysym " + v3Key + " only")
		}
		return []WLEvent{{Op: s.Op, TPlanUS: t0, Text: *s.Keysym}}, nil
	case "drag":
		if !InScreen(s.AtPx, w, h) {
			return nil, v3Bad("drag needs on-screen at_px [x,y]")
		}
		if s.Fps == nil || *s.Fps != v3DragFps {
			return nil, v3Bad("drag needs fps 60")
		}
		if len(s.Path) == 0 || len(s.Path) > v3MaxDragPts {
			return nil, v3Bad("drag needs a path of 1..1000 points")
		}
		n := len(s.Path)
		ev := make([]WLEvent, 0, n+2)
		ev = append(ev, WLEvent{Op: "drag_press", TPlanUS: t0, X: s.AtPx[0], Y: s.AtPx[1]})
		for j, p := range s.Path {
			if !InScreen(p, w, h) {
				return nil, v3Bad("drag path point off-screen")
			}
			ev = append(ev, WLEvent{Op: "drag_move", Point: j + 1, TPlanUS: t0 + int64(j+1)*1_000_000/v3DragFps, X: p[0], Y: p[1]})
		}
		ev = append(ev, WLEvent{Op: "drag_release", Point: n + 1, TPlanUS: t0 + int64(n+1)*1_000_000/v3DragFps})
		return ev, nil
	}
	return nil, v3Bad("unknown v3 op " + s.Op)
}

// fireV3 performs one v3 event. hscroll is one wheel notch (buttons 6/7) per
// step: the scene advances its fixed dx table once per wheel event.
func fireV3(act WLActuator, ev WLEvent) error {
	switch ev.Op {
	case "hscroll":
		n := 1
		if ev.Dx < 0 {
			n = -1
		}
		return act.Scroll(ev.X, ev.Y, n, 0)
	case "key":
		return act.Key(strings.ToLower(ev.Text))
	case "drag_press":
		d, ok := act.(dragActuator)
		if !ok {
			return fmt.Errorf("actuator cannot press/release")
		}
		return d.DragPress(ev.X, ev.Y, 1)
	case "drag_move":
		return act.Move(ev.X, ev.Y)
	case "drag_release":
		d, ok := act.(dragActuator)
		if !ok {
			return fmt.Errorf("actuator cannot press/release")
		}
		return d.DragRelease(1)
	}
	return fmt.Errorf("unknown v3 event %q", ev.Op)
}

// v3Rects reads the item's pane_rect / div_rect0 (JW4-06), derives the div rect after every drag (translated by the last move
// point minus the press point) and refuses, rc 5, any rect outside the plan-time bound bw x bh. CheckBounds repeats it for START's w x h.
func v3Rects(it *WLItem, raw map[string]json.RawMessage, bw, bh int) error {
	rect := func(name string) ([]int, error) {
		b, ok := raw[name]
		if !ok {
			return nil, nil
		}
		var r []int
		if err := json.Unmarshal(b, &r); err != nil || len(r) != 4 {
			return nil, v3Bad(name + " must be [x,y,w,h] integers")
		}
		return r, nil
	}
	if r, err := rect("pane_rect"); err != nil {
		return err
	} else if r != nil {
		it.Rects = append(it.Rects, WLRect{"pane_rect", r})
	}
	r, err := rect("div_rect0")
	if err != nil {
		return err
	}
	if r != nil {
		it.Rects = append(it.Rects, WLRect{"div_rect0", r})
		cur := append([]int(nil), r...)
		var px, py, lx, ly int
		for _, ev := range it.Events {
			switch ev.Op {
			case "drag_press":
				px, py, lx, ly = ev.X, ev.Y, ev.X, ev.Y
			case "drag_move":
				lx, ly = ev.X, ev.Y
			case "drag_release":
				cur = []int{cur[0] + lx - px, cur[1] + ly - py, cur[2], cur[3]}
				it.Rects = append(it.Rects, WLRect{fmt.Sprintf("div_rect@step%d", ev.Step), cur})
			}
		}
	}
	for _, x := range it.Rects {
		if !InRect(x.R, bw, bh) {
			return rc.New("E_GATE", fmt.Sprintf("rect %s %v outside the %dx%d screen", x.Name, x.R, bw, bh))
		}
	}
	return nil
}

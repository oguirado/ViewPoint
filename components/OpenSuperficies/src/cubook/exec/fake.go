package exec

import (
	"fmt"

	"opensuperficies.local/srf/src/cubook/action"
)

// FakeActuator records calls; it never touches X11.
type FakeActuator struct {
	Calls    []string
	Events   int // input events emitted (ReleaseAll excluded)
	Releases int
	FailOp   string // op name that returns an error ("click", "type", ...)
	OnEvent  func(op string)
}

func (f *FakeActuator) rec(op, d string) error {
	f.Calls = append(f.Calls, op+":"+d)
	f.Events++
	if f.OnEvent != nil {
		f.OnEvent(op)
	}
	if f.FailOp == op {
		return fmt.Errorf("fake actuator failure on %s", op)
	}
	return nil
}

func (f *FakeActuator) Click(x, y, b, c int) error {
	return f.rec("click", fmt.Sprintf("%d,%d,%d,%d", x, y, b, c))
}
func (f *FakeActuator) Type(text string) error { return f.rec("type", fmt.Sprint(len(text))) }
func (f *FakeActuator) Key(c string) error     { return f.rec("key", c) }
func (f *FakeActuator) Drag(x1, y1, x2, y2, b, s int) error {
	return f.rec("drag", fmt.Sprintf("%d,%d>%d,%d,%d,%d", x1, y1, x2, y2, b, s))
}
func (f *FakeActuator) Scroll(x, y, dx, dy int) error {
	return f.rec("scroll", fmt.Sprintf("%d,%d,%d,%d", x, y, dx, dy))
}
func (f *FakeActuator) ReleaseAll() error {
	f.Releases++
	f.Calls = append(f.Calls, "release_all")
	return nil
}

// SeqObserver serves a fixed frame sequence (RI = recorded ri); past the end
// it repeats the last frame with RI+1 so settling can converge.
type SeqObserver struct {
	Frames []Frame
	i      int
}

func (s *SeqObserver) Observe() (Frame, error) {
	if len(s.Frames) == 0 {
		return Frame{}, fmt.Errorf("no frames")
	}
	if s.i < len(s.Frames) {
		f := s.Frames[s.i]
		s.i++
		return f, nil
	}
	f := s.Frames[len(s.Frames)-1]
	f.RI += uint32(s.i - len(s.Frames) + 1)
	s.i++
	return f, nil
}

// XYLocator resolves by:"xy" targets only, at match level L0 on the current frame.
type XYLocator struct{}

func (XYLocator) Locate(a *action.Action, f Frame) (Located, error) {
	if a.Target != nil && a.Target.By == "xy" && a.Target.XY != nil {
		return Located{Located: action.Located{By: "xy", BBox: [4]int{a.Target.XY[0], a.Target.XY[1], 0, 0}, MatchLevel: "L0"}, RI: f.RI}, nil
	}
	if !Actuating(a.Op) || a.Op == "key" || a.Op == "type" {
		return Located{Located: action.Located{By: "xy", MatchLevel: "L0"}, RI: f.RI}, nil
	}
	return Located{}, fmt.Errorf("xy locator cannot resolve target")
}

// MapTexts is a TextSource over a literal map (operator plan file stand-in).
type MapTexts map[string]string

func (m MapTexts) Text(ref string) (string, error) {
	if t, ok := m[ref]; ok {
		return t, nil
	}
	return "", fmt.Errorf("unknown text_ref %q", ref)
}

package exec

import "testing"

type recX struct{ ev []string }

func (r *recX) MovePointer(x, y int16) error { r.ev = append(r.ev, "mv"); return nil }
func (r *recX) ButtonDown(b byte) error      { r.ev = append(r.ev, "bd"); return nil }
func (r *recX) ButtonUp(b byte) error        { r.ev = append(r.ev, "bu"); return nil }
func (r *recX) KeyDown(k byte) error         { r.ev = append(r.ev, "kd"); return nil }
func (r *recX) KeyUp(k byte) error           { r.ev = append(r.ev, "ku"); return nil }

func TestXTestActuatorReleaseAll(t *testing.T) {
	x := &recX{}
	a := &XTestActuator{X: x}
	a.kdown(37)
	a.down(1)
	if err := a.ReleaseAll(); err != nil {
		t.Fatal(err)
	}
	n := len(x.ev)
	if n != 4 || len(a.btn) != 0 || len(a.key) != 0 {
		t.Fatalf("events %v", x.ev)
	}
	a.ReleaseAll()
	if len(x.ev) != n {
		t.Fatal("ReleaseAll must be idempotent")
	}
	if err := a.Key("ctrl+nope"); err == nil {
		t.Fatal("unknown key must error")
	}
	if err := a.Type("Hi 1"); err != nil {
		t.Fatal(err)
	}
}

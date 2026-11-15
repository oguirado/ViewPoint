package exec

import (
	"testing"

	"opensuperficies.local/srf/src/cubook/action"
)

// Wiring contract: the executor lowers a click onto the XTEST actuator and the
// actuator's return value alone never makes a step EFFECT_OK.
func TestXTestWireClickEmitsEventsButNoVerdictFromActuator(t *testing.T) {
	x := &recX{}
	fr := []Frame{MkFrame(1, "00000000000000aa", "same", 64, 64), MkFrame(2, "00000000000000aa", "same", 64, 64),
		MkFrame(3, "00000000000000aa", "same", 64, 64), MkFrame(4, "00000000000000aa", "same", 64, 64),
		MkFrame(5, "00000000000000aa", "same", 64, 64), MkFrame(6, "00000000000000aa", "same", 64, 64)}
	e := New(Config{}, &XTestActuator{X: x}, &SeqObserver{Frames: fr}, XYLocator{}, nil)
	a, err := action.DecodeAction([]byte(`{"schema":"vqa.action/v1","step":1,"op":"click","target":{"by":"xy","xy":[100,200]},"args":{"button":1,"count":1},"pre":{"match_min":"L1"},"expect":[],"risk":"read","allow":null,"fallback":"none","on_fail":"stop","policy":"script","reason_sha8":"00000000"}`))
	if err != nil {
		t.Fatal(err)
	}
	st, err := e.Step(a)
	if err != nil {
		t.Fatal(err)
	}
	if len(x.ev) == 0 {
		t.Fatal("no XTEST events reached the actuator")
	}
	if st.Verdict == "EFFECT_OK" {
		t.Fatalf("unchanged frames yielded EFFECT_OK: %+v", st)
	}
}

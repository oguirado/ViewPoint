package ajson

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"opensuperficies.local/srf/src/cubook/action"
	"opensuperficies.local/srf/src/cubook/rc"
)

func mk(n uint32) action.Step {
	h := strings.Repeat("a", 64)
	return action.Step{Schema: action.SchemaStep, Step: n, ActionSHA: h, InputSHA: h, Op: "click",
		ScreenBefore: strings.Repeat("b", 16), ScreenAfter: strings.Repeat("b", 16), Verdict: "NO_EFFECT",
		Located:  action.Located{By: "xy", MatchLevel: "L0"},
		Evidence: action.Evidence{Regions: []string{}, Frames: []string{}, A11yDelta: action.A11yDelta{Added: []string{}, Removed: []string{}, Moved: []string{}}}}
}

func TestAJSONAppendFlockSingleWriter(t *testing.T) {
	p := filepath.Join(t.TempDir(), "act_steps.jsonl")
	w, err := Open(p)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := Open(p); err == nil || rc.Of(err) != 5 {
		t.Fatalf("second writer must be refused rc 5, got %v", err)
	}
	for i := uint32(0); i < 3; i++ {
		if err := w.Append(mk(i)); err != nil {
			t.Fatal(err)
		}
	}
	bad := mk(9)
	bad.InputSHA = ""
	if err := w.Append(bad); err == nil {
		t.Fatal("step without input_sha256 must be refused")
	}
	w.Close()
	w2, err := Open(p)
	if err != nil {
		t.Fatalf("lock must be released on close: %v", err)
	}
	w2.Close()
	got, err := ReadAll(p)
	if err != nil || len(got) != 3 {
		t.Fatalf("got %d err %v", len(got), err)
	}
}

func TestAJSONTornTailRecovered(t *testing.T) {
	p := filepath.Join(t.TempDir(), "act_steps.jsonl")
	w, _ := Open(p)
	w.Append(mk(0))
	w.Append(mk(1))
	w.Close()
	f, _ := os.OpenFile(p, os.O_APPEND|os.O_WRONLY, 0)
	f.WriteString(`{"schema":"vqa.act-step/v1","step":2,"act`) // torn
	f.Close()
	w, err := Open(p)
	if err != nil {
		t.Fatal(err)
	}
	if w.Recovered == 0 {
		t.Fatal("torn tail not detected")
	}
	if err := w.Append(mk(2)); err != nil {
		t.Fatal(err)
	}
	w.Close()
	got, err := ReadAll(p)
	if err != nil || len(got) != 3 || got[2].Step != 2 {
		t.Fatalf("after recovery got %v err %v", got, err)
	}
}

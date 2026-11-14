package rc

import (
	"errors"
	"fmt"
	"testing"
)

func TestRcTableNever1(t *testing.T) {
	allowed := map[int]bool{0: true, 2: true, 3: true, 4: true, 5: true, 6: true, 7: true}
	if len(Table()) < 12 {
		t.Fatalf("table too small: %d", len(Table()))
	}
	for name, v := range Table() {
		if v == 1 || !allowed[v] {
			t.Errorf("%s maps to forbidden rc %d", name, v)
		}
	}
	for _, e := range []error{nil, errors.New("plain"), New("E_GATE", "x"), New("NOPE", "y"), fmt.Errorf("w: %w", New("E_BUDGET", "z"))} {
		if got := Of(e); got == 1 || !allowed[got] {
			t.Errorf("Of(%v)=%d", e, got)
		}
	}
	if Of(New("E_STALE", "s")) != 4 || Of(fmt.Errorf("w: %w", New("RC_HUMANO", "h"))) != 7 || Of(nil) != 0 {
		t.Fatal("wrong mapping")
	}
}

func TestAnalyzeCodes(t *testing.T) {
	if v, ok := Table()["E_ANALYZE"]; !ok || v != Gate || Code("E_ANALYZE") != Gate || Code("E_RAM_NOT_TMPFS") != Gate || Code("E_GRID") != Usage || Of(New("E_ANALYZE", "x")) != 5 {
		t.Fatal("analyze rc mapping")
	}
	for _, v := range Table() {
		if v == 1 {
			t.Fatal("rc 1")
		}
	}
}

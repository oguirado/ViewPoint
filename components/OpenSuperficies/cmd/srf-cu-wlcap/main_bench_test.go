package main

import (
	"strings"
	"testing"
)

// W4 lane B: --mode bench through the CLI. rc 2 for the refused flag combinations (never 1), checked before any
// ramws guard so a non-ramws dir cannot mask them.
func TestWlcapBenchNoRc1(t *testing.T) {
	t.Setenv("OS_CU_TEST", "1")
	out := t.TempDir()
	base := []string{"--fake-fb", "--ramws-dir", t.TempDir(), "--out", out, "--frames", "10", "--mode", "bench"}
	for _, c := range []struct {
		extra []string
		rc    int
		tok   string
	}{
		{[]string{"--keep"}, 2, "keep_not_allowed_bench"},
		{[]string{"--analyze", "engine"}, 2, "analyze_not_allowed_bench"},
		{[]string{"--analyze", "host"}, 2, "analyze_not_allowed_bench"},
		{[]string{"--det", "on"}, 5, "E_GATE"}, // accepted by the mode, then the non-ramws dir is refused
		{[]string{"--det", "off", "--src", "readat"}, 5, "E_GATE"},
	} {
		rc, _, e := runCLI(append(append([]string{}, base...), c.extra...)...)
		if rc == 1 || rc != c.rc || !strings.Contains(e, c.tok) {
			t.Fatalf("%v rc=%d %s", c.extra, rc, e)
		}
	}
}

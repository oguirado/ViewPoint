package main

import (
	"bytes"
	"os"
	"regexp"
	"strings"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

func runCLI(args ...string) (int, string, string) {
	var o, e bytes.Buffer
	c := run(args, &o, &e)
	return c, o.String(), e.String()
}

func TestNoRc1(t *testing.T) {
	src, err := os.ReadFile("main.go")
	if err != nil {
		t.Fatal(err)
	}
	if regexp.MustCompile(`os\.Exit\(\s*1\s*\)|return\s+1\b`).Match(src) {
		t.Fatal("rc 1 in source")
	}
	out := t.TempDir()
	for _, a := range [][]string{
		{}, {"--bogus"}, {"--frames", "10"}, {"--fake-fb", "--ramws-dir", "/tmp/x", "--out", out, "--frames", "10"},
		{"--fake-fb", "--ramws-dir", "/tmp/x", "--out", out, "--frames", "10", "--det", "maybe"},
		{"--fb", "/x", "--fake-fb", "--ramws-dir", "/tmp/x", "--out", out, "--frames", "10"},
		{"--reap"},
	} {
		if c, _, _ := runCLI(a...); c == 1 || c == 0 {
			t.Fatalf("%v rc=%d", a, c)
		}
	}
	t.Setenv("OS_CU_TEST", "1")
	// not under the ramws root: rc 5 gate
	if c, _, e := runCLI("--fake-fb", "--ramws-dir", t.TempDir(), "--out", out, "--frames", "10"); c != 5 {
		t.Fatalf("non-ramws rc=%d %s", c, e)
	}
}

func TestWlcapSrcFlagNoRc1(t *testing.T) {
	t.Setenv("OS_CU_TEST", "1")
	out := t.TempDir()
	base := []string{"--fake-fb", "--ramws-dir", t.TempDir(), "--out", out, "--frames", "10"}
	for _, c := range []struct {
		extra []string
		rc    int
		tok   string
	}{
		{[]string{"--src", "readat"}, 5, "E_GATE"},
		{[]string{"--src", "bogus"}, 2, "src"},
		{[]string{"--src", "xshm"}, 5, "xshm_unavailable"},
		{[]string{"--src", "mmap"}, 2, "mmap_needs_fb"},
		{[]string{"--analyze", "maybe"}, 2, "analyze"},
		{[]string{"--require-continuous", "--src", "readat"}, 5, "E_GATE"},
	} {
		rc, _, e := runCLI(append(append([]string{}, base...), c.extra...)...)
		if rc == 1 || rc != c.rc || !strings.Contains(e, c.tok) {
			t.Fatalf("%v rc=%d %s", c.extra, rc, e)
		}
	}
}

func TestWlcapAnalyzeHostFlags(t *testing.T) {
	t.Setenv("OS_CU_TEST", "1")
	out := t.TempDir()
	base := []string{"--fake-fb", "--ramws-dir", t.TempDir(), "--out", out, "--frames", "10"}
	for _, c := range []struct {
		extra []string
		rc    int
		tok   string
	}{
		{[]string{"--analyze", "host"}, 5, "E_GATE"},
		{[]string{"--analyze", "host", "--analyze-wait-ms", "0"}, 2, "analyze_wait"},
		{[]string{"--analyze", "host", "--analyze-wait-ms", "600001"}, 2, "analyze_wait"},
		{[]string{"--analyze", "host", "--analyze-wait-ms", "250"}, 5, "E_GATE"},
	} {
		rc, _, e := runCLI(append(append([]string{}, base...), c.extra...)...)
		if rc == 1 || rc != c.rc || !strings.Contains(e, c.tok) {
			t.Fatalf("%v rc=%d %s", c.extra, rc, e)
		}
	}
}

// CF-01: the driver clock is CLOCK_MONOTONIC (the actuator in the same kernel reads the same t0 from START).
func TestMonoNsIsClockMonotonic(t *testing.T) {
	var ts unix.Timespec
	a := monoNs()
	unix.ClockGettime(unix.CLOCK_MONOTONIC, &ts)
	b := ts.Nano()
	if a <= 0 || b < a || b-a > int64(time.Second) {
		t.Fatalf("monoNs=%d CLOCK_MONOTONIC=%d", a, b)
	}
}

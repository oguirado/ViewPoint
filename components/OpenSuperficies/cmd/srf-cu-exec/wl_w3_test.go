package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"opensuperficies.local/srf/src/cubook/exec"
)

const prereg2 = "../../bench/wl/prereg_wl_v2.json"

type tclock struct{ now int64 }

func (c *tclock) NowNs() int64 { return c.now }
func (c *tclock) SleepNs(ns int64) {
	if ns > 0 {
		c.now += ns
	}
}

// useClock injects a monotonic clock; onSleep runs after every sleep (e.g. wlcap writing START late).
func useClock(t *testing.T, c exec.MonoClock) {
	t.Helper()
	old := newClock
	t.Cleanup(func() { newClock = old })
	newClock = func() exec.MonoClock { return c }
}

func writeStart(t *testing.T, path string, t0 int64) {
	t.Helper()
	b, _ := json.Marshal(map[string]any{"schema": "os.cu.wlcap-start/v1", "clock": "CLOCK_MONOTONIC", "t0_ns": t0,
		"delta_ns": 16666667, "frames": 360, "w": 1920, "h": 1080, "chunk_frames": 60, "mode": "wl"})
	if err := os.WriteFile(path, b, 0o640); err != nil {
		t.Fatal(err)
	}
}

func rowsOf(t *testing.T, path string) []exec.WLActRow {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var out []exec.WLActRow
	for _, ln := range strings.Split(strings.TrimRight(string(b), "\n"), "\n") {
		if ln == "" {
			continue
		}
		var r exec.WLActRow
		if err := json.Unmarshal([]byte(ln), &r); err != nil {
			t.Fatalf("%q: %v", ln, err)
		}
		out = append(out, r)
	}
	return out
}

func TestRunPreregFakeWritesActStepsRelativeToStart(t *testing.T) {
	t.Setenv("OS_CU_TEST", "1")
	d := t.TempDir()
	st, out := filepath.Join(d, "START"), filepath.Join(d, "act_steps.jsonl")
	writeStart(t, st, 9_000_000_000)
	useClock(t, &tclock{now: 9_000_100_000})
	var o, e bytes.Buffer
	r := run([]string{"run", "--prereg", prereg2, "--item", "WL-1", "--start", st, "--out", out, "--mock"}, &o, &e)
	if r != 0 {
		t.Fatalf("rc %d: %s", r, e.String())
	}
	rows := rowsOf(t, out)
	if len(rows) != 3 || rows[0].TUS != 1_000_000 || rows[2].TUS != 5_000_000 || rows[2].ActuatorSeq != 3 || rows[1].Item != "WL-1" {
		t.Fatalf("rows %+v", rows)
	}
	fi, _ := os.Stat(out)
	if fi.Mode().Perm() != 0o640 {
		t.Errorf("act_steps mode %o want 640", fi.Mode().Perm())
	}
	if !strings.Contains(o.String(), "ok item=WL-1 events=3") {
		t.Errorf("stdout %q", o.String())
	}
	// the out file is never appended to or overwritten (one actuator run per AJSON)
	if r := run([]string{"run", "--prereg", prereg2, "--item", "WL-1", "--start", st, "--out", out, "--mock"}, &o, &e); r != 2 {
		t.Errorf("existing --out rc %d want 2", r)
	}
}

func TestRunPreregWaitsBoundedForStart(t *testing.T) {
	t.Setenv("OS_CU_TEST", "1")
	d := t.TempDir()
	st, out := filepath.Join(d, "START"), filepath.Join(d, "act.jsonl")
	useClock(t, &tclock{now: 1_000_000_000})
	var o, e bytes.Buffer
	if r := run([]string{"run", "--prereg", prereg2, "--item", "WL-1", "--start", st, "--out", out, "--mock", "--start-wait-ms", "200"}, &o, &e); r != 5 {
		t.Fatalf("no START rc %d want 5 (%s)", r, e.String())
	}
	if !strings.Contains(e.String(), "start_timeout") {
		t.Errorf("stderr %q", e.String())
	}
	if _, err := os.Stat(out); err == nil {
		t.Errorf("act_steps written without START")
	}
	// START appears while the actuator is polling: it starts, events relative to the written t0
	c := &lateClock{tclock: tclock{now: 2_000_000_000}, after: 3, write: func() { writeStart(t, st, 2_000_500_000) }}
	useClock(t, c)
	if r := run([]string{"run", "--prereg", prereg2, "--item", "WL-1", "--start", st, "--out", out, "--mock", "--start-wait-ms", "5000"}, &o, &e); r != 0 {
		t.Fatalf("late START rc %d: %s", r, e.String())
	}
	if rows := rowsOf(t, out); len(rows) != 3 || rows[0].TUS != 1_000_000 {
		t.Errorf("rows %+v", rows)
	}
	for _, bad := range []string{"0", "-1", "600001"} {
		if r := run([]string{"run", "--prereg", prereg2, "--item", "WL-1", "--start", st, "--out", out + bad, "--mock", "--start-wait-ms", bad}, &o, &e); r != 2 {
			t.Errorf("--start-wait-ms %s rc %d want 2", bad, r)
		}
	}
}

type lateClock struct {
	tclock
	n, after int
	write    func()
}

func (c *lateClock) SleepNs(ns int64) {
	c.tclock.SleepNs(ns)
	if c.n++; c.n == c.after {
		c.write()
	}
}

func TestRunPreregUsageAndSchemaRc2(t *testing.T) {
	t.Setenv("OS_CU_TEST", "1")
	d := t.TempDir()
	st := filepath.Join(d, "START")
	writeStart(t, st, 5_000_000_000)
	useClock(t, &tclock{now: 5_000_000_000})
	cases := [][]string{
		{"run", "--prereg", prereg2, "--item", "WL-1", "--start", st, "--mock"},                                 // no --out
		{"run", "--prereg", prereg2, "--start", st, "--out", filepath.Join(d, "a"), "--mock"},                   // no --item
		{"run", "--prereg", prereg2, "--item", "WL-1", "--out", filepath.Join(d, "b"), "--mock"},                // no --start
		{"run", "--prereg", prereg2, "--item", "WL-1", "--start", st, "--out", filepath.Join(d, "c")},           // fake without --mock
		{"run", "--prereg", prereg2, "--item", "WL-9", "--start", st, "--out", filepath.Join(d, "d"), "--mock"}, // unknown item
		{"run", "--prereg", "/nonexistent.json", "--item", "WL-1", "--start", st, "--out", filepath.Join(d, "e"), "--mock"},
		{"run", "--prereg", prereg2, "--plan", planPath, "--item", "WL-1", "--start", st, "--out", filepath.Join(d, "f"), "--mock"},
		{"run", "--prereg", prereg2, "--item", "WL-1", "--start", st, "--out", filepath.Join(d, "g"), "--mock", "--frames", framesPath},
	}
	for _, c := range cases {
		var o, e bytes.Buffer
		if r := run(c, &o, &e); r != 2 {
			t.Errorf("%v -> rc %d want 2 (%s)", c[1:], r, e.String())
		}
	}
	bad := filepath.Join(d, "BADSTART")
	os.WriteFile(bad, []byte(`{"t0_ns":"x"}`), 0o640)
	var o, e bytes.Buffer
	if r := run([]string{"run", "--prereg", prereg2, "--item", "WL-1", "--start", bad, "--out", filepath.Join(d, "h"), "--mock", "--start-wait-ms", "100"}, &o, &e); r != 2 {
		t.Errorf("bad START rc %d want 2", r)
	}
}

func TestRunPreregProdUsesXTestAndHostRefused(t *testing.T) {
	d := t.TempDir()
	st := filepath.Join(d, "START")
	writeStart(t, st, 7_000_000_000)
	useClock(t, &tclock{now: 7_000_000_000})
	x, opens := &recX{}, 0
	fakeDeps(t, true, x, &opens, map[string]string{"OS_CU_TEST": "", "DISPLAY": ":100", "VQA_ISO": goodIso})
	var o, e bytes.Buffer
	if r := run([]string{"run", "--prereg", prereg2, "--item", "WL-7", "--start", st, "--out", filepath.Join(d, "p.jsonl")}, &o, &e); r != 0 {
		t.Fatalf("prod rc %d: %s", r, e.String())
	}
	if opens != 1 || x.ev != 8 {
		t.Errorf("XTEST opens %d events %d want 1/8", opens, x.ev)
	}
	if rows := rowsOf(t, filepath.Join(d, "p.jsonl")); len(rows) != 8 || rows[7].TUS != 4_500_000 {
		t.Errorf("rows %+v", rows)
	}
	x2, opens2 := &recX{}, 0
	fakeDeps(t, false, x2, &opens2, map[string]string{"OS_CU_TEST": "", "DISPLAY": ":100", "VQA_ISO": goodIso})
	if r := run([]string{"run", "--prereg", prereg2, "--item", "WL-7", "--start", st, "--out", filepath.Join(d, "h.jsonl")}, &o, &e); r != 5 {
		t.Errorf("host rc %d want 5", r)
	}
	if opens2 != 0 || x2.ev != 0 {
		t.Errorf("host opened XTEST %d / %d events", opens2, x2.ev)
	}
	if _, err := os.Stat(filepath.Join(d, "h.jsonl")); err == nil {
		t.Errorf("host run created act_steps")
	}
}

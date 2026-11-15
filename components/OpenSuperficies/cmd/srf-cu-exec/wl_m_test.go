package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

// W4 lane M: the START reader never follows a link, never blocks on a FIFO, caps the size (AW4-06) and checks (w,h) (AW4-07).

func runBounded(t *testing.T, args []string) (int, string) {
	t.Helper()
	type res struct {
		r int
		e string
	}
	ch := make(chan res, 1)
	go func() {
		var o, e bytes.Buffer
		r := run(args, &o, &e)
		ch <- res{r, e.String()}
	}()
	select {
	case r := <-ch:
		return r.r, r.e
	case <-time.After(3 * time.Second):
		t.Fatal("srf-cu-exec run blocked > 3 s")
		return -1, ""
	}
}

func TestStartFifoSymlinkRc5(t *testing.T) {
	t.Setenv("OS_CU_TEST", "1")
	d := t.TempDir()
	useClock(t, &tclock{now: 1_000_000_000})
	good := filepath.Join(d, "good")
	writeStart(t, good, 7_000_000_000)
	// The hardlink case gets its own target so that the symlink case always follows a regular nlink-1 file
	// (map order is random; sharing "good" let the nlink check mask a missing O_NOFOLLOW, mutant M10).
	good2 := filepath.Join(d, "good2")
	writeStart(t, good2, 7_000_000_000)
	mk := map[string]func(p string){
		"fifo":     func(p string) { syscall.Mkfifo(p, 0o600) },
		"symlink":  func(p string) { os.Symlink(good, p) },
		"oversize": func(p string) { os.WriteFile(p, []byte(strings.Repeat(" ", 5000)+"{}"), 0o640) },
		"hardlink": func(p string) { os.Link(good2, p) },
		"empty":    func(p string) { os.WriteFile(p, nil, 0o640) },
	}
	for name, f := range mk {
		st := filepath.Join(d, "START_"+name)
		f(st)
		r, e := runBounded(t, []string{"run", "--prereg", prereg2, "--item", "WL-1", "--start", st, "--out", filepath.Join(d, "act_"+name), "--mock", "--start-wait-ms", "200"})
		if r != 5 {
			t.Errorf("%s START: rc %d want 5 (%s)", name, r, e)
		}
		if _, err := os.Stat(filepath.Join(d, "act_"+name)); err == nil {
			t.Errorf("%s START: act_steps written", name)
		}
	}
	// control: a plain START still runs
	plain := filepath.Join(d, "START_plain")
	writeStart(t, plain, 7_000_000_000)
	if r, e := runBounded(t, []string{"run", "--prereg", prereg2, "--item", "WL-1", "--start", plain, "--out", filepath.Join(d, "act_ok"), "--mock", "--start-wait-ms", "200"}); r != 0 {
		t.Fatalf("plain START rc %d: %s", r, e)
	}
}

func TestStartResolutionMismatchRc5(t *testing.T) {
	t.Setenv("OS_CU_TEST", "1")
	d := t.TempDir()
	useClock(t, &tclock{now: 1_000_000_000})
	st := filepath.Join(d, "START")
	os.WriteFile(st, []byte(`{"schema":"os.cu.wlcap-start/v1","clock":"CLOCK_MONOTONIC","t0_ns":7000000000,"w":3840,"h":2160}`), 0o640)
	if r, e := runBounded(t, []string{"run", "--prereg", prereg2, "--item", "WL-1", "--start", st, "--out", filepath.Join(d, "a"), "--mock", "--start-wait-ms", "200"}); r != 5 {
		t.Fatalf("3840x2160 START against a 1920x1080 prereg: rc %d (%s)", r, e)
	}
}

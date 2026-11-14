package main

// run --prereg: execute one os.cu.prereg-wl/v2 item (CF-01). The actuator
// waits (bounded) for the wlcap START file, fires every event at t0 + t_ms on
// CLOCK_MONOTONIC and writes os.cu.wl-act-step/v1 lines to --out (created
// 0o640, never overwritten). Prod needs prodgate.Check (capsule, :100, iso);
// OS_CU_TEST=1 --mock uses the FakeActuator. No observer: verdicts are not
// part of a WL capture, the frames are the capture itself.

import (
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"syscall"

	"opensuperficies.local/srf/src/cubook/exec"
	"opensuperficies.local/srf/src/cubook/prodgate"
	"opensuperficies.local/srf/src/cubook/rc"
)

const (
	maxStartWaitMS = 600000
	startMax       = 4096 // START is one small JSON line (W4 marker contract)
)

type wlArgs struct {
	prereg, item, start, out, stop, iso, plan, frames string
	mock                                              bool
	startWaitMS                                       int
}

// newClock is CLOCK_MONOTONIC; tests replace it.
var newClock = func() exec.MonoClock { return exec.RealMono{} }

// readStart is the actuator side of the marker contract (AW4-06): open O_RDONLY|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC straight
// away, fstat S_ISREG with st_nlink == 1 and 0 < size <= startMax, one bounded read. A missing file is (nil, false, nil);
// a symlink, FIFO, hardlink, empty or oversize file is rc 5 and never blocks.
func readStart(path string) ([]byte, bool, error) {
	f, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK|syscall.O_CLOEXEC, 0)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, rc.New("E_GATE", "start_unsafe: "+err.Error())
	}
	defer f.Close()
	var st syscall.Stat_t
	if err := syscall.Fstat(int(f.Fd()), &st); err != nil {
		return nil, false, rc.New("E_GATE", "start_unsafe: "+err.Error())
	}
	if st.Mode&syscall.S_IFMT != syscall.S_IFREG || st.Nlink != 1 || st.Size <= 0 || st.Size > startMax {
		return nil, false, rc.New("E_GATE", "start_unsafe: not a single-link regular file of 1..4096 bytes")
	}
	b, err := io.ReadAll(io.LimitReader(f, startMax+1))
	if err != nil || len(b) > startMax {
		return nil, false, rc.New("E_GATE", "start_unsafe: unreadable or oversize")
	}
	return b, true, nil
}

// waitStart polls the START file every 10 ms until it parses or waitMS elapses.
func waitStart(path string, clk exec.MonoClock, waitMS int) (exec.StartInfo, error) {
	deadline := clk.NowNs() + int64(waitMS)*1_000_000
	for {
		b, ok, err := readStart(path)
		if err != nil {
			return exec.StartInfo{}, err
		}
		if ok {
			return exec.ParseStartInfo(b) // content errors stay E_SCHEMA rc 2 (a safe file with bad JSON)
		}
		if clk.NowNs() >= deadline {
			return exec.StartInfo{}, rc.New("E_GATE", "start_timeout: no START within "+fmt.Sprint(waitMS)+" ms")
		}
		clk.SleepNs(10_000_000)
	}
}

func runWL(a wlArgs, stdout, stderr io.Writer) int {
	if a.out == "" || a.item == "" || a.start == "" || a.plan != "" || a.frames != "" ||
		a.startWaitMS <= 0 || a.startWaitMS > maxStartWaitMS {
		return usage(stderr)
	}
	if _, err := os.Lstat(a.out); err == nil {
		return fail(stderr, rc.New("E_IO", "--out exists: one actuator run per act_steps file"))
	}
	b, err := os.ReadFile(a.prereg)
	if err != nil {
		return fail(stderr, rc.New("E_IO", err.Error()))
	}
	it, err := exec.LoadWLItem(b, a.item)
	if err != nil {
		return fail(stderr, err)
	}
	env := func(k string) string {
		if k == "VQA_ISO" && a.iso != "" {
			return a.iso
		}
		return os.Getenv(k)
	}
	deps := newDeps(env)
	mode, err := prodgate.Check(deps.Env, deps.Probe, deps.Iso)
	if err != nil {
		return fail(stderr, err)
	}
	var act exec.WLActuator
	if mode == prodgate.TestFake {
		if !a.mock {
			return usage(stderr)
		}
		act = &exec.FakeActuator{}
	} else {
		x, closeX, err := deps.XTest(":100")
		if err != nil {
			return fail(stderr, err)
		}
		defer closeX()
		act = &exec.XTestActuator{X: x}
	}
	clk := newClock()
	si, err := waitStart(a.start, clk, a.startWaitMS)
	if err != nil {
		return fail(stderr, err)
	}
	t0 := si.T0Ns
	// START's measured screen must be a prereg resolution, and every positioned event must lie inside it (AW4-07)
	if len(it.Resolutions) > 0 {
		match := false
		for _, r := range it.Resolutions {
			match = match || r == [2]int{si.W, si.H}
		}
		if !match {
			return fail(stderr, rc.New("E_GATE", fmt.Sprintf("START screen %dx%d is not a prereg resolution", si.W, si.H)))
		}
	}
	if err := it.CheckBounds(si.W, si.H); err != nil {
		return fail(stderr, err)
	}
	f, err := os.OpenFile(a.out, os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_NOFOLLOW, 0o640)
	if err != nil {
		return fail(stderr, rc.New("E_IO", err.Error()))
	}
	if err := f.Chmod(0o640); err != nil { // explicit mode, umask-independent (AW3-07)
		f.Close()
		return fail(stderr, rc.New("E_IO", err.Error()))
	}
	var stop func() bool
	if a.stop != "" {
		stop = func() bool { _, err := os.Stat(a.stop); return err == nil }
	}
	r, rerr := exec.RunWL(it, act, clk, t0, f, stop)
	if cerr := f.Close(); cerr != nil && rerr == nil {
		r, rerr = rc.Usage, rc.New("E_IO", cerr.Error())
	}
	if rerr != nil {
		fmt.Fprintln(stderr, "srf-cu-exec:", rerr)
		return r
	}
	fmt.Fprintf(stdout, "ok item=%s events=%d t0_ns=%d\n", it.ID, len(it.Events), t0)
	return rc.OK
}

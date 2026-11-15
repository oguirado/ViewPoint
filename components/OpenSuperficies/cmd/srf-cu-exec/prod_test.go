package main

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"

	"opensuperficies.local/srf/src/cubook/capture"
	"opensuperficies.local/srf/src/cubook/exec"
	"opensuperficies.local/srf/src/cubook/prodgate"
)

const goodIso = "0123456789abcdef0123456789abcdef"

type recX struct{ ev int }

func (r *recX) MovePointer(x, y int16) error { r.ev++; return nil }
func (r *recX) ButtonDown(b byte) error      { r.ev++; return nil }
func (r *recX) ButtonUp(b byte) error        { r.ev++; return nil }
func (r *recX) KeyDown(k byte) error         { r.ev++; return nil }
func (r *recX) KeyUp(k byte) error           { r.ev++; return nil }

type okIso struct{}

func (okIso) Read(string) (string, error) { return goodIso, nil }

const planPath = "../../testdata/cu/plans/click_effect.plan.json"
const framesPath = "../../testdata/cu/plans/effect.frames.jsonl"

// fakeDeps is a capsule-shaped environment over in-memory effects; opens counts XTEST dials.
func fakeDeps(t *testing.T, inCapsule bool, x *recX, opens *int, env map[string]string) {
	t.Helper()
	old := newDeps
	t.Cleanup(func() { newDeps = old })
	newDeps = func(e func(string) string) prodgate.Deps {
		return prodgate.Deps{
			Env:   func(k string) string { return e(k) },
			Probe: capture.CapsuleProbe{Exists: func(p string) bool { return inCapsule && p == "/.dockerenv" }},
			Iso:   okIso{},
			XTest: func(string) (exec.XTestAPI, func(), error) { *opens++; return x, func() {}, nil },
			NewObserver: func(fb, ram, disp string) (exec.Observer, func(), error) {
				b, _ := os.ReadFile(framesPath)
				fm, _ := exec.ParseFrames(b)
				var seq []exec.Frame
				for ri := uint32(0); len(seq) < len(fm); ri++ {
					if f, ok := fm[ri]; ok {
						seq = append(seq, f)
					}
				}
				return &exec.SeqObserver{Frames: seq}, func() {}, nil
			},
		}
	}
	for k, v := range env {
		t.Setenv(k, v)
	}
}

func TestExecRunProdUsesXTestActuator(t *testing.T) {
	x, opens := &recX{}, 0
	fakeDeps(t, true, x, &opens, map[string]string{"OS_CU_TEST": "", "DISPLAY": ":100"})
	out := filepath.Join(t.TempDir(), "steps.jsonl")
	var o, e bytes.Buffer
	r := run([]string{"run", "--plan", planPath, "--out", out, "--fb", "/x/Xvfb_screen0", "--ramws-dir", "/dev/shm/x", "--iso", goodIso}, &o, &e)
	if opens != 1 || x.ev == 0 {
		t.Fatalf("rc %d: XTEST opens=%d events=%d (%s)", r, opens, x.ev, e.String())
	}
	if r < 0 || r > 7 || r == 1 {
		t.Fatalf("rc %d", r)
	}
	// A frames file in prod mode is refused: fakes are never reachable there.
	opens, x.ev = 0, 0
	o.Reset()
	if r := run([]string{"run", "--plan", planPath, "--out", out + "2", "--frames", framesPath, "--fb", "a", "--ramws-dir", "b", "--iso", goodIso}, &o, &e); r != 2 || x.ev != 0 {
		t.Fatalf("--frames in prod -> rc %d events %d", r, x.ev)
	}
}

func TestExecRunOutsideCapsuleRc5ZeroEvents(t *testing.T) {
	x, opens := &recX{}, 0
	fakeDeps(t, false, x, &opens, map[string]string{"OS_CU_TEST": "", "DISPLAY": ":100"})
	out := filepath.Join(t.TempDir(), "steps.jsonl")
	for _, argv := range [][]string{
		{"run", "--plan", planPath, "--out", out, "--fb", "a", "--ramws-dir", "b", "--iso", goodIso},
		{"run", "--plan", planPath, "--out", out, "--frames", framesPath, "--mock"},
	} {
		var o, e bytes.Buffer
		if r := run(argv, &o, &e); r != 5 {
			t.Fatalf("%v -> rc %d (%s)", argv, r, e.String())
		}
	}
	if opens != 0 || x.ev != 0 {
		t.Fatalf("outside the capsule: opens=%d events=%d", opens, x.ev)
	}
	if _, err := os.Stat(out); err == nil {
		t.Fatal("refused run created the step log")
	}
}

func TestExecFakeNeedsTestEnvAndNeverTouchesX(t *testing.T) {
	x, opens := &recX{}, 0
	fakeDeps(t, true, x, &opens, map[string]string{"OS_CU_TEST": "1", "DISPLAY": ""})
	out := filepath.Join(t.TempDir(), "steps.jsonl")
	var o, e bytes.Buffer
	if r := run([]string{"run", "--plan", planPath, "--out", out, "--frames", framesPath, "--mock"}, &o, &e); r != 0 {
		t.Fatalf("rc %d %s", r, e.String())
	}
	if opens != 0 || x.ev != 0 {
		t.Fatalf("fake mode dialled X: opens=%d events=%d", opens, x.ev)
	}
}

// ADW1-05: the fake world needs OS_CU_TEST=1 and an explicit --mock; --frames alone is a usage error.
func TestExecFramesRequiresMock(t *testing.T) {
	x, opens := &recX{}, 0
	fakeDeps(t, true, x, &opens, map[string]string{"OS_CU_TEST": "1"})
	out := filepath.Join(t.TempDir(), "steps.jsonl")
	var o, e bytes.Buffer
	if r := run([]string{"run", "--plan", planPath, "--out", out, "--frames", framesPath}, &o, &e); r != 2 {
		t.Fatalf("--frames without --mock -> rc %d", r)
	}
}

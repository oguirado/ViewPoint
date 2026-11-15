package capture

import (
	"os"
	"path/filepath"
	"syscall"
	"testing"
)

// AMEND_W2 C5: the capsule liveness probe is LOCK_SH|LOCK_NB on the read-only .lock, released at once.
func probeRun(t *testing.T) (PathGuard, string, *os.File) {
	t.Helper()
	root := t.TempDir()
	run := filepath.Join(root, "r1")
	os.Mkdir(run, 0o700)
	os.WriteFile(filepath.Join(run, "owner.json"), []byte("{}"), 0o600)
	lk, err := os.OpenFile(filepath.Join(run, ".lock"), os.O_RDWR|os.O_CREATE, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { lk.Close() })
	return PathGuard{Root: root, FSType: fsOf("tmpfs")}, run, lk
}

func tryEX(t *testing.T, run string) bool {
	t.Helper()
	f, err := os.OpenFile(filepath.Join(run, ".lock"), os.O_RDWR, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		return false
	}
	syscall.Flock(int(f.Fd()), syscall.LOCK_UN)
	return true
}

func TestProbeLiveWhileHostHoldsEX(t *testing.T) {
	g, run, lk := probeRun(t)
	if err := syscall.Flock(int(lk.Fd()), syscall.LOCK_EX); err != nil {
		t.Fatal(err)
	}
	if err := g.CheckRunDir(run); err != nil {
		t.Fatalf("host holds LOCK_EX but probe reported not live: %v", err)
	}
}

func TestProbeLeavesNoLockHeld(t *testing.T) {
	g, run, lk := probeRun(t)
	syscall.Flock(int(lk.Fd()), syscall.LOCK_EX)
	if err := g.CheckRunDir(run); err != nil {
		t.Fatal(err)
	}
	syscall.Flock(int(lk.Fd()), syscall.LOCK_UN)
	_ = g.CheckRunDir(run) // probe of an unheld lock too
	if !tryEX(t, run) {
		t.Fatal("LOCK_EX attempt failed right after the probe: the probe left a lock held")
	}
}

func TestProbeUnheldIsUnmanaged(t *testing.T) {
	g, run, _ := probeRun(t)
	wantGate(t, g.CheckRunDir(run), "unmanaged_run_dir")
}

// A shared holder (e.g. another reader's probe) is not the host session: only EX means live.
func TestProbeSharedHolderIsNotLive(t *testing.T) {
	g, run, lk := probeRun(t)
	if err := syscall.Flock(int(lk.Fd()), syscall.LOCK_SH); err != nil {
		t.Fatal(err)
	}
	wantGate(t, g.CheckRunDir(run), "unmanaged_run_dir")
}

func openFDs(t *testing.T) int {
	t.Helper()
	ents, err := os.ReadDir("/proc/self/fd")
	if err != nil {
		t.Skip("no /proc/self/fd")
	}
	return len(ents)
}

// The probe must close its .lock descriptor (kills "delete defer lk.Close()").
func TestProbeClosesLockFD(t *testing.T) {
	g, run, lk := probeRun(t)
	syscall.Flock(int(lk.Fd()), syscall.LOCK_EX)
	before := openFDs(t)
	for i := 0; i < 5; i++ {
		if err := g.CheckRunDir(run); err != nil {
			t.Fatal(err)
		}
	}
	if after := openFDs(t); after != before {
		t.Fatalf("fd leak: %d open before, %d after the probe", before, after)
	}
}

// With the probing descriptor still open, the shared lock must already be released (kills "delete LOCK_UN").
func TestProbeLiveReleasesWhileFDOpen(t *testing.T) {
	_, run, _ := probeRun(t)
	f, err := os.Open(filepath.Join(run, ".lock"))
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	live, err := probeLive(f)
	if err != nil || live {
		t.Fatalf("unheld lock: live=%v err=%v", live, err)
	}
	if !tryEX(t, run) {
		t.Fatal("probeLive left LOCK_SH held on an open descriptor")
	}
}

// Only EWOULDBLOCK means live; any other flock error is not "live" (ADC-W2-07).
func TestProbeFlockErrorIsNotLive(t *testing.T) {
	_, run, _ := probeRun(t)
	f, err := os.Open(filepath.Join(run, ".lock"))
	if err != nil {
		t.Fatal(err)
	}
	f.Close()
	if live, err := probeLive(f); live || err == nil {
		t.Fatalf("closed fd: live=%v err=%v", live, err)
	}
}

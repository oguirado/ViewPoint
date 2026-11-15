package main

import (
	"os"
	"path/filepath"
	"syscall"
	"testing"
)

// ADC-W3-06: srf-cu-snap sets umask 027 and writes explicit 0o750 dirs / 0o640 files (no world bits).
func TestSnapUmask027AndExplicitModes(t *testing.T) {
	old := syscall.Umask(0) // permissive on purpose: the modes must not depend on the caller's umask
	defer syscall.Umask(old)
	dir := managedRun(t, "fx1fc-modes0")
	out := filepath.Join(t.TempDir(), "evidence", "nested")
	c, _, se := runCLI("--fake-fb", "--frames", "2", "--ramws-dir", dir, "--out", out)
	if c != 0 {
		t.Fatalf("rc %d %s", c, se)
	}
	if got := syscall.Umask(0o022); got != 0o027 {
		t.Fatalf("umask after run = %04o, want 0027", got)
	}
	for _, d := range []string{out, filepath.Dir(out)} {
		if st, err := os.Stat(d); err != nil || st.Mode().Perm() != 0o750 {
			t.Fatalf("dir %s mode %v err %v, want 0750", filepath.Base(d), st.Mode().Perm(), err)
		}
	}
	for _, f := range []string{"frames.manifest.json", "capture_receipt.json"} {
		if st, err := os.Stat(filepath.Join(out, f)); err != nil || st.Mode().Perm() != 0o640 {
			t.Fatalf("%s mode %v err %v, want 0640", f, st.Mode().Perm(), err)
		}
	}
}

package capture

import (
	"os"
	"path/filepath"
	"syscall"
	"testing"
)

// AW2-01: a run dir created by the host user leaves .lock 0600 owned by the host uid; the capsule (uid 10001) can
// only open it read-only. CheckRunDir must still see the held flock through O_RDONLY.
func TestCheckRunDirReadOnlyLock(t *testing.T) {
	root := t.TempDir()
	g := PathGuard{Root: root, FSType: fsOf("tmpfs")}
	run := filepath.Join(root, "r1")
	os.Mkdir(run, 0o700)
	os.WriteFile(filepath.Join(run, "owner.json"), []byte("{}"), 0o600)
	lkPath := filepath.Join(run, ".lock")
	lk, err := os.OpenFile(lkPath, os.O_RDWR|os.O_CREATE, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	defer lk.Close()
	if err := syscall.Flock(int(lk.Fd()), syscall.LOCK_EX); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(lkPath, 0o400); err != nil { // read-only for everybody, as the capsule uid would see it
		t.Fatal(err)
	}
	if os.Geteuid() == 0 {
		t.Skip("root bypasses file modes")
	}
	if err := g.CheckRunDir(run); err != nil {
		t.Fatalf("read-only .lock held by the session was refused: %v", err)
	}
	syscall.Flock(int(lk.Fd()), syscall.LOCK_UN)
	wantGate(t, g.CheckRunDir(run), "unmanaged_run_dir")
}

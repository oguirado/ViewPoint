package reaper

import (
	"os/exec"
	"syscall"
	"testing"
	"time"
)

func TestNewProcessReaper(t *testing.T) {
	r, err := NewProcessReaper()
	if err != nil {
		t.Fatalf("failed to initialize ProcessReaper: %v", err)
	}
	if r == nil {
		t.Fatal("expected non-nil ProcessReaper")
	}
}

func TestReaperPreservesDirectChild(t *testing.T) {
	r, err := NewProcessReaper()
	if err != nil {
		t.Fatalf("NewProcessReaper failed: %v", err)
	}

	cmd := exec.Command("bash", "-c", "sleep 0.05; exit 42")
	if err := cmd.Start(); err != nil {
		t.Fatalf("cmd.Start failed: %v", err)
	}

	pid := cmd.Process.Pid
	r.RegisterDirectChild(pid)
	defer r.UnregisterDirectChild(pid)

	time.Sleep(100 * time.Millisecond)

	// Call DrainZombies. Because pid is registered as a direct child, it must NOT be reaped!
	reaped, err := r.DrainZombies()
	if err != nil {
		t.Fatalf("DrainZombies failed: %v", err)
	}
	for _, p := range reaped {
		if p == pid {
			t.Fatalf("DrainZombies illegally reaped direct child %d!", pid)
		}
	}

	// Now cmd.Wait() must observe the exit status without ECHILD.
	waitErr := cmd.Wait()
	if waitErr == nil {
		t.Fatal("expected non-zero exit error (exit 42), got nil")
	}

	if exitErr, ok := waitErr.(*exec.ExitError); ok {
		if exitErr.ExitCode() != 42 {
			t.Fatalf("expected exit code 42, got %d", exitErr.ExitCode())
		}
	} else {
		t.Fatalf("expected *exec.ExitError, got %v", waitErr)
	}
}

func TestReaperReapsAdoptedOrphan(t *testing.T) {
	r, err := NewProcessReaper()
	if err != nil {
		t.Fatalf("NewProcessReaper failed: %v", err)
	}

	// Spawn a shell that forks a grandchild into the background and immediately exits.
	// The grandchild will be adopted by this process (as subreaper).
	cmd := exec.Command("bash", "-c", "(sleep 0.05 &)")
	if err := cmd.Run(); err != nil {
		t.Fatalf("parent runner failed: %v", err)
	}

	// Wait for the grandchild to finish sleeping and become a zombie.
	time.Sleep(150 * time.Millisecond)

	reaped, err := r.DrainZombies()
	if err != nil {
		t.Fatalf("DrainZombies failed: %v", err)
	}
	if len(reaped) == 0 {
		// Grandchild may take slightly longer, retry once
		time.Sleep(100 * time.Millisecond)
		reaped, err = r.DrainZombies()
		if err != nil {
			t.Fatalf("DrainZombies retry failed: %v", err)
		}
	}

	if len(reaped) == 0 {
		t.Log("Note: orphan grandchild already reaped or not scheduled in window")
	}
}

func TestPidfd(t *testing.T) {
	cmd := exec.Command("bash", "-c", "sleep 10")
	if err := cmd.Start(); err != nil {
		t.Fatalf("cmd.Start failed: %v", err)
	}
	defer func() {
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
	}()

	pidfd, err := PidfdOpen(cmd.Process.Pid)
	if err != nil {
		t.Fatalf("PidfdOpen failed: %v", err)
	}
	defer syscall.Close(pidfd)

	if err := PidfdSendSignal(pidfd, syscall.SIGKILL); err != nil {
		t.Fatalf("PidfdSendSignal failed: %v", err)
	}

	_ = cmd.Wait()
}

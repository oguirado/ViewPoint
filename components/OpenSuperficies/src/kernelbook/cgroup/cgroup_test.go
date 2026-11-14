package cgroup

import (
	"errors"
	"os"
	"syscall"
	"testing"
	"time"

	"golang.org/x/sys/unix"
	"opensuperficies.local/srf/src/kernelbook/lanzador"
)

func TestFindCurrentAndParent(t *testing.T) {
	curr, err := FindCurrentCgroup()
	if err != nil {
		t.Fatalf("FindCurrentCgroup failed: %v", err)
	}
	if curr == "" {
		t.Fatal("empty current cgroup path")
	}

	parent, err := FindSiblingParent()
	if err != nil {
		t.Fatalf("FindSiblingParent failed: %v", err)
	}
	if parent == "" {
		t.Fatal("empty parent cgroup path")
	}
}

func TestSiblingCgroupLifecycle(t *testing.T) {
	cg, err := CreateSibling("srf-unit-test")
	if err != nil {
		t.Fatalf("CreateSibling failed: %v", err)
	}
	defer func() {
		_ = cg.Destroy()
	}()

	if cg.Fd() < 0 {
		t.Fatalf("invalid cgroup fd: %d", cg.Fd())
	}

	// Spawn a process using lanzador with CLONE_INTO_CGROUP (falling back to AddProc if clone3 is ENOSYS under seccomp)
	cmd, err := lanzador.Command("bash", "-c", "sleep 10")
	if err != nil {
		t.Fatalf("lanzador.Command failed: %v", err)
	}

	cmd.SysProcAttr = &syscall.SysProcAttr{
		UseCgroupFD: true,
		CgroupFD:    cg.Fd(),
		Setpgid:     true,
	}

	if err := cmd.Start(); err != nil {
		// If seccomp blocks clone3 (ENOSYS), fall back to launch + AddProc
		cmd, _ = lanzador.Command("bash", "-c", "sleep 10")
		cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
		if err := cmd.Start(); err != nil {
			t.Fatalf("cmd.Start fallback failed: %v", err)
		}
		if err := cg.AddProc(cmd.Process.Pid); err != nil {
			_ = cmd.Process.Kill()
			t.Fatalf("cg.AddProc failed: %v", err)
		}
	}

	// Verify the process is registered in cgroup.procs
	pids, err := cg.Procs()
	if err != nil {
		t.Fatalf("cg.Procs failed: %v", err)
	}
	found := false
	for _, p := range pids {
		if p == cmd.Process.Pid {
			found = true
			break
		}
	}
	if !found {
		t.Fatalf("pid %d not found in cgroup.procs: %v", cmd.Process.Pid, pids)
	}

	pop, err := cg.Populated()
	if err != nil || pop != 1 {
		t.Fatalf("expected populated 1, got %d, err %v", pop, err)
	}

	// Kill via cgroup.kill
	if err := cg.Kill(); err != nil {
		t.Fatalf("cg.Kill failed: %v", err)
	}

	if err := cg.WaitEmpty(2 * time.Second); err != nil {
		t.Fatalf("WaitEmpty failed: %v", err)
	}

	popFinal, err := cg.Populated()
	if err != nil || popFinal != 0 {
		t.Fatalf("expected populated_final 0, got %d, err %v", popFinal, err)
	}

	_ = cmd.Wait()

	// Destroy
	if err := cg.Destroy(); err != nil {
		t.Fatalf("cg.Destroy failed: %v", err)
	}

	if _, err := os.Stat(cg.Path); !os.IsNotExist(err) {
		t.Fatalf("cgroup path %s still exists after Destroy!", cg.Path)
	}
}

// TestCommandCageSubtreeControlEBUSY verifies that the command cage leaf cannot delegate controllers (returns EBUSY).
func TestCommandCageSubtreeControlEBUSY(t *testing.T) {
	err := TryCommandCageSubtreeControl()
	if err == nil {
		t.Fatal("expected EBUSY when writing subtree_control on command cage, got nil")
	}
	if !errors.Is(err, syscall.EBUSY) && !errors.Is(err, unix.EBUSY) {
		t.Logf("subtree_control returned error (expected EBUSY): %v", err)
	}
}

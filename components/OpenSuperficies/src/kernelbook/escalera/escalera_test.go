package escalera

import (
	"fmt"
	"os"
	"os/exec"
	"sync"
	"testing"
	"time"

	"golang.org/x/sys/unix"
	"opensuperficies.local/srf/src/kernelbook/cgroup"
	"opensuperficies.local/srf/src/kernelbook/reaper"
)

func TestLanzarYDerribarSimple(t *testing.T) {
	id := fmt.Sprintf("test-simple-%d", time.Now().UnixNano()%1000000)
	sesion, err := LanzarSesion(SesionConfig{
		ID:        id,
		DobleFork: false,
	})
	if err != nil {
		t.Fatalf("LanzarSesion failed: %v", err)
	}

	pop, err := sesion.Cgroup.IsPopulated()
	if err != nil {
		t.Fatalf("IsPopulated failed: %v", err)
	}
	if !pop {
		t.Fatalf("expected cgroup to be populated after launch")
	}

	if err := sesion.Derribar(); err != nil {
		t.Fatalf("Derribar failed: %v", err)
	}

	// Verify cgroup was destroyed
	huerfanos, zombis, populated, restantes, err := CensoEstado(sesion.ID)
	if err != nil {
		t.Fatalf("CensoEstado failed: %v", err)
	}
	if huerfanos != 0 || zombis != 0 || populated != 0 || restantes != 0 {
		t.Fatalf("residues detected: huerfanos=%d zombis=%d populated=%d restantes=%d",
			huerfanos, zombis, populated, restantes)
	}
}

func TestLanzarYDerribarDobleFork(t *testing.T) {
	id := fmt.Sprintf("test-dfork-%d", time.Now().UnixNano()%1000000)
	sesion, err := LanzarSesion(SesionConfig{
		ID:        id,
		DobleFork: true,
	})
	if err != nil {
		t.Fatalf("LanzarSesion failed: %v", err)
	}

	pids, err := sesion.Cgroup.Procs()
	if err != nil {
		t.Fatalf("Procs failed: %v", err)
	}
	if len(pids) < 2 {
		t.Logf("warning: expected >= 2 pids in cgroup, got %v", pids)
	}

	if err := sesion.Derribar(); err != nil {
		t.Fatalf("Derribar failed: %v", err)
	}

	huerfanos, zombis, populated, restantes, err := CensoEstado(sesion.ID)
	if err != nil {
		t.Fatalf("CensoEstado failed: %v", err)
	}
	if huerfanos != 0 || zombis != 0 || populated != 0 || restantes != 0 {
		t.Fatalf("residues detected after teardown: huerfanos=%d zombis=%d populated=%d restantes=%d",
			huerfanos, zombis, populated, restantes)
	}
}

func TestPurgar(t *testing.T) {
	id := fmt.Sprintf("test-purgar-%d", time.Now().UnixNano()%1000000)
	sesion, err := LanzarSesion(SesionConfig{
		ID:        id,
		DobleFork: true,
	})
	if err != nil {
		t.Fatalf("LanzarSesion failed: %v", err)
	}
	defer sesion.PTY.Close()

	// Do NOT call Derribar. Verify cgroup exists and has processes.
	huerfanos, _, populated, restantes, err := CensoEstado(sesion.ID)
	if err != nil {
		t.Fatalf("CensoEstado failed: %v", err)
	}
	if restantes == 0 || populated == 0 {
		t.Fatalf("expected active cgroup before purge, got restantes=%d populated=%d huerfanos=%d",
			restantes, populated, huerfanos)
	}

	// Purge
	if err := Purgar(sesion.ID); err != nil {
		t.Fatalf("Purgar failed: %v", err)
	}

	// Verify all cleaned up
	huerfanosPost, zombisPost, populatedPost, restantesPost, err := CensoEstado(sesion.ID)
	if err != nil {
		t.Fatalf("CensoEstado after purge failed: %v", err)
	}
	if huerfanosPost != 0 || zombisPost != 0 || populatedPost != 0 || restantesPost != 0 {
		t.Fatalf("residues remaining after Purgar: huerfanos=%d zombis=%d populated=%d restantes=%d",
			huerfanosPost, zombisPost, populatedPost, restantesPost)
	}
}

func TestU10A_SubreaperNoExitTheft(t *testing.T) {
	rp, err := reaper.NewProcessReaper()
	if err != nil {
		t.Fatalf("NewProcessReaper: %v", err)
	}
	SetLiveReaper(rp)

	cmd := exec.Command("bash", "-c", "sleep 0.05; exit 42")
	if err := StartDirectChild(cmd, rp); err != nil {
		t.Fatalf("StartDirectChild: %v", err)
	}

	pid := cmd.Process.Pid
	defer rp.UnregisterDirectChild(pid)
	t.Logf("Parent %d spawned registered direct child %d exiting with status 42", os.Getpid(), pid)

	time.Sleep(100 * time.Millisecond)

	// Call Purgar, exactly as production does (U10-A2, U10-A3)
	if err := Purgar("test-purgar-prod-", rp); err != nil {
		t.Fatalf("Purgar failed: %v", err)
	}
	t.Logf("Purgar executed via production path, direct child %d preserved", pid)

	// Parent Wait() must still return 42, not ECHILD or -1
	waitErr := cmd.Wait()
	if waitErr == nil {
		t.Fatal("expected non-zero exit error, got nil")
	}
	exitErr, ok := waitErr.(*exec.ExitError)
	if !ok {
		t.Fatalf("expected *exec.ExitError, got %v", waitErr)
	}
	t.Logf("Parent cmd.Wait() returned exit code %d (not ECHILD, not -1)", exitErr.ExitCode())
	if exitErr.ExitCode() != 42 {
		t.Fatalf("expected exit code 42, got %d", exitErr.ExitCode())
	}

	// Then show an unregistered orphan is still reaped
	orphanCmd := exec.Command("bash", "-c", "setsid sleep 0.05 & exit 0")
	if err := orphanCmd.Run(); err != nil {
		t.Fatalf("orphanCmd.Run: %v", err)
	}
	time.Sleep(100 * time.Millisecond)

	reaped := DrainZombiesSafe(rp)
	t.Logf("DrainZombiesSafe reaped unregistered orphan zombies: %v", reaped)
	_, zombis, _, _, err := CensoEstado("test-purgar-orphan-")
	if err != nil {
		t.Fatalf("CensoEstado: %v", err)
	}
	if zombis != 0 {
		t.Fatalf("expected 0 zombies after orphan reap, got %d", zombis)
	}
	t.Logf("Census verified 0 zombies remaining after orphan reap")
}

func TestU10A2_DrainZombiesSafeNilFailsSafe(t *testing.T) {
	// With rp == nil, DrainZombiesSafe must fail safe and refuse to consume anything
	reaped := DrainZombiesSafe(nil)
	if len(reaped) != 0 {
		t.Fatalf("expected DrainZombiesSafe(nil) to refuse to consume anything, got %v", reaped)
	}
}

func TestU10A4_ConcurrentDrainNoLostStatus(t *testing.T) {
	rp, err := reaper.NewProcessReaper()
	if err != nil {
		t.Fatalf("NewProcessReaper: %v", err)
	}
	SetLiveReaper(rp)

	stopDrain := make(chan struct{})
	var drainWg sync.WaitGroup

	// Hammer a drain concurrently across the start-to-register window
	drainWg.Add(1)
	go func() {
		defer drainWg.Done()
		for {
			select {
			case <-stopDrain:
				return
			default:
				_ = DrainZombiesSafe(rp)
				_ = Purgar("srf-term-", rp)
				time.Sleep(50 * time.Microsecond)
			}
		}
	}()

	const iterations = 50 // at least 30 iterations required by contract
	lostStatuses := 0

	for i := 0; i < iterations; i++ {
		cmd := exec.Command("bash", "-c", "exit 42")
		if err := StartDirectChild(cmd, rp); err != nil {
			t.Fatalf("iteration %d: StartDirectChild: %v", i, err)
		}
		pid := cmd.Process.Pid

		waitErr := cmd.Wait()
		rp.UnregisterDirectChild(pid)

		if waitErr == nil {
			lostStatuses++
			t.Errorf("iteration %d: expected non-nil exit error, got nil", i)
			continue
		}
		exitErr, ok := waitErr.(*exec.ExitError)
		if !ok {
			lostStatuses++
			t.Errorf("iteration %d: lost status (waitid returned error): %v", i, waitErr)
			continue
		}
		if exitErr.ExitCode() != 42 {
			lostStatuses++
			t.Errorf("iteration %d: expected exit code 42, got %d", i, exitErr.ExitCode())
		}
	}

	close(stopDrain)
	drainWg.Wait()

	t.Logf("Tested %d iterations across start-to-register window: lost_statuses=%d", iterations, lostStatuses)
	if lostStatuses > 0 {
		t.Fatalf("detected %d lost statuses out of %d iterations", lostStatuses, iterations)
	}
}

func TestU10C_CensusDetectsEscapee(t *testing.T) {
	id := fmt.Sprintf("test-escape-%d", time.Now().UnixNano()%1000000)
	sesion, err := LanzarSesion(SesionConfig{
		ID:        id,
		DobleFork: true,
	})
	if err != nil {
		t.Fatalf("LanzarSesion: %v", err)
	}
	t.Logf("Session %s launched with DobleFork: true", sesion.ID)

	// Find the grandchild PID
	pids, err := sesion.Cgroup.Procs()
	if err != nil || len(pids) < 2 {
		t.Fatalf("expected >= 2 pids in cgroup, got %v (err=%v)", pids, err)
	}

	grandchildPid := pids[1]
	if grandchildPid == sesion.Cmd.Process.Pid && len(pids) > 1 {
		grandchildPid = pids[0]
	}

	// Create an escape sibling cgroup and migrate the grandchild there (adversarial escape)
	escapeCgName := fmt.Sprintf("srf-escape-dest-%d", time.Now().UnixNano()%1000000)
	escapeCg, err := cgroup.CreateSibling(escapeCgName)
	if err != nil {
		t.Fatalf("CreateSibling escape dest failed: %v", err)
	}
	defer func() {
		_ = escapeCg.Destroy()
	}()

	if err := escapeCg.AddProc(grandchildPid); err != nil {
		t.Fatalf("migrating grandchild to escape cgroup failed: %v", err)
	}
	t.Logf("Grandchild %d migrated to sibling cgroup %s (adversarial escape)", grandchildPid, escapeCg.Name)

	// Teardown the session cgroup. This destroys the srf-term-<id> directory.
	if err := sesion.Derribar(); err != nil {
		t.Fatalf("Derribar failed: %v", err)
	}
	t.Logf("Session %s torn down and directory destroyed", sesion.ID)

	// The session cgroup directory no longer exists!
	// CensoEstado must still detect and report the escapee!
	huerfanos, _, _, restantes, err := CensoEstado(sesion.ID)
	if err != nil {
		t.Fatalf("CensoEstado: %v", err)
	}
	t.Logf("CensoEstado(%s) reported: huerfanos=%d, cgroups_restantes=%d", sesion.ID, huerfanos, restantes)
	if restantes != 0 {
		t.Fatalf("expected 0 remaining cgroups for session, got %d", restantes)
	}
	if huerfanos < 1 {
		t.Fatalf("expected census to report escaped orphan (huerfanos >= 1), got %d", huerfanos)
	}

	// Cleanup the escaped process
	_ = unix.Kill(grandchildPid, unix.SIGKILL)
	time.Sleep(20 * time.Millisecond)
	_ = DrainZombiesSafe(sesion.Reaper)
	UnregisterTrackedPID(grandchildPid)
}

func TestU10C2_EscapeeWithoutTrackedPIDs(t *testing.T) {
	prefix := fmt.Sprintf("srf-term-%d-", os.Getpid())
	id := fmt.Sprintf("%stest-escapee-%d", prefix, time.Now().UnixNano()%1000000)

	sesion, err := LanzarSesion(SesionConfig{
		ID:        id,
		DobleFork: true,
	})
	if err != nil {
		t.Fatalf("LanzarSesion: %v", err)
	}
	t.Logf("Session %s launched with DobleFork: true", sesion.ID)

	pids, err := sesion.Cgroup.Procs()
	if err != nil || len(pids) < 2 {
		t.Fatalf("expected >= 2 pids in cgroup, got %v (err=%v)", pids, err)
	}

	grandchildPid := pids[1]
	if grandchildPid == sesion.Cmd.Process.Pid && len(pids) > 1 {
		grandchildPid = pids[0]
	}

	// Create an escape sibling cgroup and migrate the grandchild there (adversarial escape)
	escapeCgName := fmt.Sprintf("srf-escape-dest-%d", time.Now().UnixNano()%1000000)
	escapeCg, err := cgroup.CreateSibling(escapeCgName)
	if err != nil {
		t.Fatalf("CreateSibling escape dest failed: %v", err)
	}
	defer func() {
		_ = escapeCg.Destroy()
	}()

	if err := escapeCg.AddProc(grandchildPid); err != nil {
		t.Fatalf("migrating grandchild to escape cgroup failed: %v", err)
	}
	t.Logf("Grandchild %d migrated to sibling cgroup %s", grandchildPid, escapeCg.Name)

	// Teardown the session cgroup
	if err := sesion.Derribar(); err != nil {
		t.Fatalf("Derribar failed: %v", err)
	}

	// Explicitly remove grandchildPid from trackedPIDs so census cannot rely on it
	UnregisterTrackedPID(grandchildPid)
	if s := getTrackedSession(grandchildPid); s != "" {
		t.Fatalf("expected trackedSession to be empty for %d, got %s", grandchildPid, s)
	}
	t.Logf("Grandchild %d explicitly unregistered from trackedPIDs", grandchildPid)

	// CensoEstado with real oracle prefix format must discover the escapee WITHOUT relying on trackedPIDs
	huerfanos, _, _, restantes, err := CensoEstado(prefix)
	if err != nil {
		t.Fatalf("CensoEstado: %v", err)
	}
	t.Logf("CensoEstado(%s) reported: huerfanos=%d, cgroups_restantes=%d", prefix, huerfanos, restantes)
	if huerfanos < 1 {
		t.Fatalf("expected census to report escaped orphan (huerfanos >= 1) without trackedPIDs, got %d", huerfanos)
	}

	// Cleanup the escaped process
	_ = unix.Kill(grandchildPid, unix.SIGKILL)
	time.Sleep(20 * time.Millisecond)
	_ = DrainZombiesSafe(sesion.Reaper)
	UnregisterTrackedPID(grandchildPid)
}

func TestU10D_ZombieProbeMultipleZombies(t *testing.T) {
	prefix := fmt.Sprintf("test-zombies-%d-", time.Now().UnixNano()%1000000)
	rp, err := reaper.NewProcessReaper()
	if err != nil {
		t.Fatalf("NewProcessReaper: %v", err)
	}

	// Launch two sessions
	s1, err := LanzarSesion(SesionConfig{
		ID:        prefix + "01",
		DobleFork: false,
		Reaper:    rp,
	})
	if err != nil {
		t.Fatalf("LanzarSesion 1: %v", err)
	}
	s2, err := LanzarSesion(SesionConfig{
		ID:        prefix + "02",
		DobleFork: false,
		Reaper:    rp,
	})
	if err != nil {
		t.Fatalf("LanzarSesion 2: %v", err)
	}

	// Kill their session processes without reaping them so they become zombies
	_ = unix.Kill(s1.PGID, unix.SIGKILL)
	_ = unix.Kill(s2.PGID, unix.SIGKILL)
	time.Sleep(50 * time.Millisecond)

	// Census must detect both zombies (not capped at 1)
	_, zombis, _, _, err := CensoEstado(prefix)
	if err != nil {
		t.Fatalf("CensoEstado: %v", err)
	}
	if zombis < 2 {
		t.Fatalf("expected zombie probe to detect multiple zombies (>= 2), got %d", zombis)
	}

	// Cleanup
	_ = s1.Derribar()
	_ = s2.Derribar()
	_ = Purgar(prefix)
}

func TestU10G_NoFdLeakWithoutDerribar(t *testing.T) {
	id := fmt.Sprintf("test-fdleak-%d", time.Now().UnixNano()%1000000)
	sesion, err := LanzarSesion(SesionConfig{
		ID:        id,
		DobleFork: false,
	})
	if err != nil {
		t.Fatalf("LanzarSesion: %v", err)
	}

	masterFd := sesion.PTY.Master.Fd()
	slaveFd := sesion.PTY.Slave.Fd()

	// Path without --derribar: close PTY descriptors in parent
	if sesion.PTY != nil {
		_ = sesion.PTY.Close()
	}

	// Verify that neither master nor slave descriptor is still open
	if err := unix.Fsync(int(masterFd)); err != unix.EBADF {
		t.Fatalf("expected master PTY FD %d to be closed (EBADF), got %v", masterFd, err)
	}
	if err := unix.Fsync(int(slaveFd)); err != unix.EBADF {
		t.Fatalf("expected slave PTY FD %d to be closed (EBADF), got %v", slaveFd, err)
	}

	// Cleanup
	_ = sesion.Derribar()
}

func TestU10F_LaunchFailureReporting(t *testing.T) {
	// An invalid cgroup name (with path traversal) fails launch
	_, err := LanzarSesion(SesionConfig{
		ID: "../invalid/escape",
	})
	if err == nil {
		t.Fatal("expected launch failure on invalid cgroup ID, got nil")
	}
}


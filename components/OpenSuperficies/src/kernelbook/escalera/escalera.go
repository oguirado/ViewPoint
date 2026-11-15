package escalera

import (
	"bufio"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/unix"
	"opensuperficies.local/srf/src/kernelbook/cgroup"
	"opensuperficies.local/srf/src/kernelbook/lanzador"
	"opensuperficies.local/srf/src/kernelbook/pty"
	"opensuperficies.local/srf/src/kernelbook/reaper"
)

var mutanteSinCgroupKill = false

// SesionConfig specifies session creation parameters.
type SesionConfig struct {
	ID        string
	DobleFork bool
	Reaper    *reaper.ProcessReaper
}

// Sesion represents an active terminal session bound to a sibling cgroup v2.
type Sesion struct {
	ID        string
	Cgroup    *cgroup.Cgroup
	Cmd       *exec.Cmd
	PTY       *pty.PTY
	Reaper    *reaper.ProcessReaper
	PGID      int
	DobleFork bool
	mu        sync.Mutex
	derribada bool
}

var (
	trackerMu    sync.RWMutex
	trackedPIDs  = make(map[int]string) // PID -> session ID
	liveReaperMu sync.RWMutex
	liveReaper   *reaper.ProcessReaper
	spawnMu      sync.RWMutex
)

// SetLiveReaper sets the active reaper registry for the process.
func SetLiveReaper(r *reaper.ProcessReaper) {
	liveReaperMu.Lock()
	defer liveReaperMu.Unlock()
	liveReaper = r
}

// GetLiveReaper retrieves the active reaper registry for the process.
func GetLiveReaper() *reaper.ProcessReaper {
	liveReaperMu.RLock()
	defer liveReaperMu.RUnlock()
	return liveReaper
}

// StartDirectChild starts an exec.Cmd and registers it directly with rp
// atomically under spawnMu, eliminating any window where a concurrent drain
// could steal its exit status.
func StartDirectChild(cmd *exec.Cmd, rp *reaper.ProcessReaper) error {
	if rp == nil {
		rp = GetLiveReaper()
	}
	spawnMu.Lock()
	defer spawnMu.Unlock()
	if err := cmd.Start(); err != nil {
		return err
	}
	if rp != nil {
		rp.RegisterDirectChild(cmd.Process.Pid)
	}
	return nil
}


// RegisterTrackedPID associates a PID with a session ID for census accounting.
func RegisterTrackedPID(pid int, sessionID string) {
	trackerMu.Lock()
	defer trackerMu.Unlock()
	trackedPIDs[pid] = sessionID
}

// UnregisterTrackedPID removes a PID from census tracking.
func UnregisterTrackedPID(pid int) {
	trackerMu.Lock()
	defer trackerMu.Unlock()
	delete(trackedPIDs, pid)
}

func getTrackedPIDs(prefix string) []int {
	trackerMu.RLock()
	defer trackerMu.RUnlock()
	var pids []int
	for pid, id := range trackedPIDs {
		if strings.HasPrefix(id, prefix) {
			pids = append(pids, pid)
		}
	}
	return pids
}

func getTrackedSession(pid int) string {
	trackerMu.RLock()
	defer trackerMu.RUnlock()
	return trackedPIDs[pid]
}

// LanzarSesion initiates a terminal session inside a dedicated sibling cgroup.
// It executes Rung L0: Containment (birth via CLONE_INTO_CGROUP or coordinated migration,
// plus subreaper declaration via PR_SET_CHILD_SUBREAPER).
func LanzarSesion(cfg SesionConfig) (*Sesion, error) {
	if cfg.Reaper == nil {
		var err error
		cfg.Reaper, err = reaper.NewProcessReaper()
		if err != nil {
			return nil, fmt.Errorf("escalera: subreaper setup: %w", err)
		}
	}
	SetLiveReaper(cfg.Reaper)

	cgroupName := cfg.ID
	if !strings.HasPrefix(cgroupName, "srf-term-") {
		cgroupName = "srf-term-" + cgroupName
	}
	cg, err := cgroup.CreateSibling(cgroupName)
	if err != nil {
		return nil, fmt.Errorf("escalera: create sibling cgroup %s: %w", cgroupName, err)
	}

	p, err := pty.Open()
	if err != nil {
		_ = cg.Destroy()
		return nil, fmt.Errorf("escalera: open pty: %w", err)
	}

	script := "read -r ; exec sleep 120"
	if cfg.DobleFork {
		// Adversarial escape: fork a grandchild that runs setsid in a separate process group
		script = "read -r ; setsid -f sleep 120 & exec sleep 120"
	}

	cmd, err := lanzador.Command("bash", "-c", script)
	if err != nil {
		_ = p.Close()
		_ = cg.Destroy()
		return nil, fmt.Errorf("escalera: lanzador validate/command: %w", err)
	}

	cmd.Stdin = p.Slave
	cmd.Stdout = p.Slave
	cmd.Stderr = p.Slave

	// First attempt: atomic birth inside cgroup v2 slice via SysProcAttr.CgroupFD
	cmd.SysProcAttr = &syscall.SysProcAttr{
		UseCgroupFD: true,
		CgroupFD:    cg.Fd(),
		Setpgid:     true,
	}

	spawnMu.Lock()
	if err := cmd.Start(); err == nil {
		pgid := cmd.Process.Pid
		cfg.Reaper.RegisterDirectChild(pgid)
		RegisterTrackedPID(pgid, cgroupName)
		spawnMu.Unlock()
		// Release the blocked read
		_, _ = p.Master.Write([]byte("\n"))
	} else {
		spawnMu.Unlock()
		// Fallback to coordinated migration (when clone3/UseCgroupFD returns ENOSYS)
		cmd, err = lanzador.Command("bash", "-c", script)
		if err != nil {
			_ = p.Close()
			_ = cg.Destroy()
			return nil, fmt.Errorf("escalera: fallback command: %w", err)
		}
		cmd.Stdin = p.Slave
		cmd.Stdout = p.Slave
		cmd.Stderr = p.Slave
		cmd.SysProcAttr = &syscall.SysProcAttr{
			Setpgid: true,
		}
		spawnMu.Lock()
		if err := cmd.Start(); err != nil {
			spawnMu.Unlock()
			_ = p.Close()
			_ = cg.Destroy()
			return nil, fmt.Errorf("escalera: start command: %w", err)
		}
		pgid := cmd.Process.Pid
		cfg.Reaper.RegisterDirectChild(pgid)
		RegisterTrackedPID(pgid, cgroupName)
		spawnMu.Unlock()

		if err := cg.AddProc(cmd.Process.Pid); err != nil {
			cfg.Reaper.UnregisterDirectChild(cmd.Process.Pid)
			UnregisterTrackedPID(cmd.Process.Pid)
			_ = p.Close()
			_ = cmd.Process.Kill()
			_ = cmd.Wait()
			_ = cg.Destroy()
			return nil, fmt.Errorf("escalera: AddProc to cgroup %s: %w", cgroupName, err)
		}
		// Release the blocked read now that the process is securely in the sibling cgroup
		_, _ = p.Master.Write([]byte("\n"))
	}

	pgid := cmd.Process.Pid

	sesion := &Sesion{
		ID:        cgroupName,
		Cgroup:    cg,
		Cmd:       cmd,
		PTY:       p,
		Reaper:    cfg.Reaper,
		PGID:      pgid,
		DobleFork: cfg.DobleFork,
	}

	if cfg.DobleFork {
		// Await the double-forked setsid child birth inside the cgroup
		deadline := time.Now().Add(200 * time.Millisecond)
		for time.Now().Before(deadline) {
			pids, err := cg.Procs()
			if err == nil {
				for _, pid := range pids {
					RegisterTrackedPID(pid, cgroupName)
				}
				if len(pids) >= 2 {
					break
				}
			}
			time.Sleep(5 * time.Millisecond)
		}
	}

	return sesion, nil
}

// siginfoKernel represents the true Linux kernel siginfo_t layout on 64-bit architectures.
type siginfoKernel struct {
	Signo  int32
	Errno  int32
	Code   int32
	_pad0  int32
	Pid    int32
	Uid    uint32
	Status int32
	_tail  [25]int32
}

// DrainZombiesSafe inspects waitable children using WNOWAIT without exit theft.
// Registered direct children are protected and never reaped.
// Adopted orphan grandchildren are reaped by exact PID via P_PID.
// If rp is nil, it fails safe and refuses to reap anything (U10-A2).
func DrainZombiesSafe(rp *reaper.ProcessReaper) []int {
	if rp == nil {
		return nil
	}
	spawnMu.RLock()
	defer spawnMu.RUnlock()

	var reaped []int
	for {
		var info siginfoKernel
		_, _, errno := unix.Syscall6(
			unix.SYS_WAITID,
			uintptr(unix.P_ALL),
			0,
			uintptr(unsafe.Pointer(&info)),
			uintptr(unix.WEXITED|unix.WNOHANG|unix.WNOWAIT),
			0,
			0,
		)
		if errno != 0 || info.Pid <= 0 {
			break
		}
		pid := int(info.Pid)
		if rp.IsDirectChild(pid) {
			// Direct child protected: do not steal exit status!
			break
		}

		// Adopted orphan grandchild: reap authoritatively by exact PID
		var reapInfo siginfoKernel
		_, _, reapErr := unix.Syscall6(
			unix.SYS_WAITID,
			uintptr(unix.P_PID),
			uintptr(pid),
			uintptr(unsafe.Pointer(&reapInfo)),
			uintptr(unix.WEXITED),
			0,
			0,
		)
		if reapErr != 0 {
			break
		}
		UnregisterTrackedPID(pid)
		reaped = append(reaped, pid)
	}
	return reaped
}

// Derribar executes the 7-rung zero-orphan teardown ladder.
func (s *Sesion) Derribar() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.derribada {
		return nil
	}

	// Snapshot all active PIDs in cgroup before teardown commences (U10-C)
	if procs, err := s.Cgroup.Procs(); err == nil {
		for _, p := range procs {
			RegisterTrackedPID(p, s.ID)
		}
	}

	// Rung L1: Protocol - graceful shutdown: send SIGTERM to initial PGID
	if s.PGID > 0 {
		_ = unix.Kill(-s.PGID, unix.SIGTERM)
	}

	// MUTATOR BRANCH: build tag mutante_sin_cgroup_kill stops at SIGTERM to the PGID;
	// double-forked setsid children survive.
	if mutanteSinCgroupKill {
		s.derribada = true
		_ = s.Cmd.Wait()
		s.Reaper.UnregisterDirectChild(s.PGID)
		UnregisterTrackedPID(s.PGID)
		if s.PTY != nil {
			_ = s.PTY.Close() // Close PTY descriptors to prevent FD leak (U10-G)
		}
		// Stop here: skip L2..L6 so double-forked setsid children survive
		return nil
	}

	// Rung L2: Sweep - bounded grace period to allow cooperative exit
	time.Sleep(20 * time.Millisecond)

	// Rung L3: Hard Kill - recursive cgroup.kill terminates all descendants regardless of setsid
	_ = s.Cgroup.Kill()

	// Rung L4: Quiescence - poll cgroup.events until populated 0
	_ = s.Cgroup.WaitEmpty(2 * time.Second)

	// Rung L5: Drain Reap - reap direct child and all adopted orphan grandchildren without exit theft
	_ = s.Cmd.Wait()
	s.Reaper.UnregisterDirectChild(s.PGID)
	UnregisterTrackedPID(s.PGID)
	_ = DrainZombiesSafe(s.Reaper)

	// Rung L6: Removal - close PTY descriptors and bottom-up rmdir of cgroup directory
	if s.PTY != nil {
		_ = s.PTY.Close()
	}
	_ = s.Cgroup.Destroy()

	s.derribada = true
	return nil
}

// CensoEstado performs a live count of surviving orphans, zombies, populated cgroups,
// and remaining cgroups under the sibling slice.
// It inspects cgroup directories, tracked session PIDs, and process state without
// depending on cgroup directories still existing.
func CensoEstado(prefix string) (huerfanos int, zombis int, populated int, cgroupsRestantes int, err error) {
	parent, err := cgroup.FindSiblingParent()
	if err != nil {
		return 0, 0, 0, 0, err
	}

	searchPrefix := prefix
	if !strings.HasPrefix(searchPrefix, "srf-term-") {
		searchPrefix = "srf-term-" + searchPrefix
	}
	matches, err := filepath.Glob(filepath.Join(parent, searchPrefix+"*"))
	if err != nil {
		return 0, 0, 0, 0, err
	}
	if len(matches) == 0 && searchPrefix != prefix {
		if m, err := filepath.Glob(filepath.Join(parent, prefix+"*")); err == nil {
			matches = m
		}
	}
	cgroupsRestantes = len(matches)

	seenPids := make(map[int]bool)

	// 1. Inspect matching cgroup directories
	for _, d := range matches {
		eventsFile := filepath.Join(d, "cgroup.events")
		data, err := os.ReadFile(eventsFile)
		if err == nil && strings.Contains(string(data), "populated 1") {
			populated++
		}

		procsFile := filepath.Join(d, "cgroup.procs")
		f, err := os.Open(procsFile)
		if err == nil {
			scanner := bufio.NewScanner(f)
			for scanner.Scan() {
				line := strings.TrimSpace(scanner.Text())
				if line == "" {
					continue
				}
				pid, err := strconv.Atoi(line)
				if err != nil || pid <= 0 || seenPids[pid] {
					continue
				}
				seenPids[pid] = true

				if err := unix.Kill(pid, 0); err == nil {
					statusFile := filepath.Join("/proc", strconv.Itoa(pid), "status")
					statusData, err := os.ReadFile(statusFile)
					if err == nil && strings.Contains(string(statusData), "State:\tZ (zombie)") {
						zombis++
					} else {
						huerfanos++
					}
				}
			}
			f.Close()
		}
	}

	// 2. Inspect tracked session PIDs (detects escapees even after cgroup destruction)
	tracked := getTrackedPIDs(prefix)
	if len(tracked) == 0 && searchPrefix != prefix {
		tracked = getTrackedPIDs(searchPrefix)
	}
	for _, pid := range tracked {
		if seenPids[pid] {
			continue
		}
		if err := unix.Kill(pid, 0); err == nil {
			seenPids[pid] = true
			statusFile := filepath.Join("/proc", strconv.Itoa(pid), "status")
			statusData, err := os.ReadFile(statusFile)
			if err == nil && strings.Contains(string(statusData), "State:\tZ (zombie)") {
				zombis++
			} else {
				huerfanos++
			}
		}
	}

	// 3. Scan /proc for any adopted orphan descendants of current process
	myPid := os.Getpid()
	if entries, err := os.ReadDir("/proc"); err == nil {
		for _, entry := range entries {
			if !entry.IsDir() {
				continue
			}
			pid, err := strconv.Atoi(entry.Name())
			if err != nil || pid <= 0 || seenPids[pid] {
				continue
			}

			statusFile := filepath.Join("/proc", entry.Name(), "status")
			statusData, err := os.ReadFile(statusFile)
			if err != nil {
				continue
			}
			content := string(statusData)

			ppid := -1
			for _, line := range strings.Split(content, "\n") {
				if strings.HasPrefix(line, "PPid:") {
					fields := strings.Fields(line)
					if len(fields) >= 2 {
						ppid, _ = strconv.Atoi(fields[1])
					}
					break
				}
			}

			if ppid == myPid {
				cgroupFile := filepath.Join("/proc", entry.Name(), "cgroup")
				cgData, _ := os.ReadFile(cgroupFile)
				cgStr := string(cgData)

				if strings.Contains(cgStr, prefix) || strings.Contains(cgStr, searchPrefix) || strings.HasPrefix(prefix, "srf-term-") || strings.Contains(cgStr, "srf-") || getTrackedSession(pid) != "" {
					seenPids[pid] = true
					if strings.Contains(content, "State:\tZ (zombie)") {
						zombis++
					} else {
						if liveRp := GetLiveReaper(); liveRp != nil && liveRp.IsDirectChild(pid) {
							continue
						}
						huerfanos++
					}
				}
			}
		}
	}

	// 4. Probe waitable zombies via waitid in a loop (U10-D)
	for {
		var info siginfoKernel
		_, _, errno := unix.Syscall6(
			unix.SYS_WAITID,
			uintptr(unix.P_ALL),
			0,
			uintptr(unsafe.Pointer(&info)),
			uintptr(unix.WEXITED|unix.WNOHANG|unix.WNOWAIT),
			0,
			0,
		)
		if errno != 0 || info.Pid <= 0 {
			break
		}
		pid := int(info.Pid)
		if seenPids[pid] {
			break
		}
		seenPids[pid] = true
		zombis++
	}

	return huerfanos, zombis, populated, cgroupsRestantes, nil
}

// Purgar terminates all processes in matching sibling cgroups, cleans tracked PIDs, and removes directories.
func Purgar(prefix string, rpOptional ...*reaper.ProcessReaper) error {
	parent, err := cgroup.FindSiblingParent()
	if err != nil {
		return err
	}

	var rp *reaper.ProcessReaper
	if len(rpOptional) > 0 && rpOptional[0] != nil {
		rp = rpOptional[0]
	} else {
		rp = GetLiveReaper()
	}
	if rp == nil {
		if r, err := reaper.NewProcessReaper(); err == nil {
			SetLiveReaper(r)
			rp = r
		}
	}

	searchPrefix := prefix
	if !strings.HasPrefix(searchPrefix, "srf-term-") {
		searchPrefix = "srf-term-" + searchPrefix
	}
	matches, err := filepath.Glob(filepath.Join(parent, searchPrefix+"*"))
	if err != nil {
		return err
	}
	if len(matches) == 0 && searchPrefix != prefix {
		if m, err := filepath.Glob(filepath.Join(parent, prefix+"*")); err == nil {
			matches = m
		}
	}

	// 1. Send recursive kill to all matching cgroups and unregister direct children being purged
	for _, d := range matches {
		killFile := filepath.Join(d, "cgroup.kill")
		_ = os.WriteFile(killFile, []byte("1\n"), 0644)

		procsFile := filepath.Join(d, "cgroup.procs")
		if f, err := os.Open(procsFile); err == nil {
			scanner := bufio.NewScanner(f)
			for scanner.Scan() {
				if pid, err := strconv.Atoi(strings.TrimSpace(scanner.Text())); err == nil && pid > 0 {
					if rp != nil {
						rp.UnregisterDirectChild(pid)
					}
					UnregisterTrackedPID(pid)
				}
			}
			f.Close()
		}
	}

	// Kill and unregister tracked PIDs matching prefix or searchPrefix
	trackedList := append(getTrackedPIDs(prefix), getTrackedPIDs(searchPrefix)...)
	for _, pid := range trackedList {
		if rp != nil {
			rp.UnregisterDirectChild(pid)
		}
		UnregisterTrackedPID(pid)
		_ = unix.Kill(pid, unix.SIGKILL)
	}

	// 2. Wait for cgroups to empty
	deadline := time.Now().Add(2 * time.Second)
	for _, d := range matches {
		eventsFile := filepath.Join(d, "cgroup.events")
		for time.Now().Before(deadline) {
			data, err := os.ReadFile(eventsFile)
			if err != nil || strings.Contains(string(data), "populated 0") {
				break
			}
			time.Sleep(10 * time.Millisecond)
		}
	}

	// 3. Drain any local zombies safely using the live reaper registry (U10-A2)
	_ = DrainZombiesSafe(rp)

	// 4. Unlink cgroup directories
	for _, d := range matches {
		deadline := time.Now().Add(1 * time.Second)
		for {
			err := unix.Rmdir(d)
			if err == nil || os.IsNotExist(err) {
				break
			}
			if time.Now().After(deadline) {
				break
			}
			time.Sleep(15 * time.Millisecond)
		}
	}

	return nil
}


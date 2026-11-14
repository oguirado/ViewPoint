package reaper

import (
	"fmt"
	"sync"
	"syscall"
	"unsafe"

	"golang.org/x/sys/unix"
)

// SiginfoWait matches Linux struct siginfo_t layout for SYS_WAITID on 64-bit architectures.
type SiginfoWait struct {
	Signo  int32
	Errno  int32
	Code   int32
	_pad   [3]int32
	Pid    int32
	Uid    uint32
	Status int32
	_tail  [26]int32 // Total 128 bytes
}

// ProcessReaper manages child process reaping without stealing direct child exit statuses.
type ProcessReaper struct {
	mu           sync.RWMutex
	directPIDs   map[int]struct{}
	orphanReaped chan int
}

// NewProcessReaper configures the current process as child subreaper via PR_SET_CHILD_SUBREAPER.
func NewProcessReaper() (*ProcessReaper, error) {
	if err := unix.Prctl(unix.PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0); err != nil {
		return nil, fmt.Errorf("reaper: PR_SET_CHILD_SUBREAPER failed: %w", err)
	}
	return &ProcessReaper{
		directPIDs:   make(map[int]struct{}),
		orphanReaped: make(chan int, 1024),
	}, nil
}

// RegisterDirectChild marks a PID as directly owned by an active command waiter (e.g. os/exec.Cmd).
func (r *ProcessReaper) RegisterDirectChild(pid int) {
	r.mu.Lock()
	r.directPIDs[pid] = struct{}{}
	r.mu.Unlock()
}

// UnregisterDirectChild removes a PID from the direct child registry once Wait() has completed.
func (r *ProcessReaper) UnregisterDirectChild(pid int) {
	r.mu.Lock()
	delete(r.directPIDs, pid)
	r.mu.Unlock()
}

// IsDirectChild returns true if the PID is registered as a direct child.
func (r *ProcessReaper) IsDirectChild(pid int) bool {
	r.mu.RLock()
	defer r.mu.RUnlock()
	_, ok := r.directPIDs[pid]
	return ok
}

// OrphanReapedChan returns the receive-only channel of reaped orphan PIDs.
func (r *ProcessReaper) OrphanReapedChan() <-chan int {
	return r.orphanReaped
}

// DrainZombies inspects waitable children non-destructively using WNOWAIT.
// Direct children are skipped to avoid exit theft (ECHILD in cmd.Wait).
// Adopted grandchildren / orphans are reaped authoritatively by exact PID.
// Returns the list of reaped orphan PIDs.
func (r *ProcessReaper) DrainZombies() ([]int, error) {
	var reaped []int
	for {
		var info SiginfoWait
		_, _, errno := unix.Syscall6(
			unix.SYS_WAITID,
			uintptr(unix.P_ALL),
			0,
			uintptr(unsafe.Pointer(&info)),
			uintptr(unix.WEXITED|unix.WNOHANG|unix.WNOWAIT),
			0,
			0,
		)

		if errno != 0 {
			if errno == unix.ECHILD {
				return reaped, nil
			}
			return reaped, errno
		}

		if info.Pid == 0 {
			return reaped, nil
		}

		pid := int(info.Pid)
		if r.IsDirectChild(pid) {
			// Direct child owned by active waiter goroutine: DO NOT REAP.
			return reaped, nil
		}

		// Adopted orphan grandchild: reap definitively by exact PID.
		var reapInfo SiginfoWait
		_, _, reapErr := unix.Syscall6(
			unix.SYS_WAITID,
			uintptr(unix.P_PID),
			uintptr(pid),
			uintptr(unsafe.Pointer(&reapInfo)),
			uintptr(unix.WEXITED),
			0,
			0,
		)

		if reapErr == 0 {
			reaped = append(reaped, pid)
			select {
			case r.orphanReaped <- pid:
			default:
			}
		} else {
			break
		}
	}
	return reaped, nil
}

// PidfdOpen returns a file descriptor referring to the process specified by pid.
func PidfdOpen(pid int) (int, error) {
	fd, err := unix.PidfdOpen(pid, 0)
	if err != nil {
		return -1, fmt.Errorf("pidfd_open: %w", err)
	}
	return fd, nil
}

// PidfdSendSignal sends a signal to the process referred to by pidfd.
func PidfdSendSignal(pidfd int, sig syscall.Signal) error {
	if err := unix.PidfdSendSignal(pidfd, sig, nil, 0); err != nil {
		return fmt.Errorf("pidfd_send_signal: %w", err)
	}
	return nil
}

package headless

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unsafe"
)

const cgroupRoot = "/sys/fs/cgroup"
const cgroup2Magic = 0x63677270
const pidfdSendSignal = 424 // Linux arm64 and amd64 syscall number.

type cgroupIdentity struct {
	Path   string `json:"path"`
	Device uint64 `json:"device"`
	Inode  uint64 `json:"inode"`
}

type scopedGroup struct {
	cgroupIdentity
	fd   *os.File
	root *os.Root
}

func openScopedGroup(path string) (*scopedGroup, error) {
	if !strings.HasPrefix(path, cgroupRoot+"/") || canonical(path) != nil {
		return nil, errors.New("canonical cgroup leaf required")
	}
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	var st syscall.Stat_t
	var fs syscall.Statfs_t
	if err := syscall.Fstat(int(f.Fd()), &st); err != nil {
		f.Close()
		return nil, err
	}
	if err := syscall.Fstatfs(int(f.Fd()), &fs); err != nil || fs.Type != cgroup2Magic || st.Mode&syscall.S_IFMT != syscall.S_IFDIR {
		f.Close()
		return nil, errors.New("cgroup v2 directory required")
	}
	root, err := os.OpenRoot(fmt.Sprintf("/proc/self/fd/%d", f.Fd()))
	if err != nil {
		f.Close()
		return nil, err
	}
	return &scopedGroup{cgroupIdentity{path, uint64(st.Dev), st.Ino}, f, root}, nil
}

func (g *scopedGroup) close() {
	g.root.Close()
	g.fd.Close()
}

func (g *scopedGroup) verifyPath() error {
	var st syscall.Stat_t
	if canonical(g.Path) != nil || syscall.Lstat(g.Path, &st) != nil ||
		uint64(st.Dev) != g.Device || st.Ino != g.Inode || st.Mode&syscall.S_IFMT != syscall.S_IFDIR {
		return errors.New("cgroup identity changed")
	}
	return nil
}

func readCgroupValue(root *os.Root, name string) (string, error) {
	raw, err := root.ReadFile(name)
	return strings.TrimSpace(string(raw)), err
}

type scopedLimits struct {
	Path        string `json:"path"`
	MemoryHigh  int64  `json:"memory_high"`
	MemoryMax   int64  `json:"memory_max"`
	SwapMax     int64  `json:"swap_max"`
	PIDsMax     int    `json:"pids_max"`
	AllowedCPUs string `json:"allowed_cpus"`
}

func verifyScope(scope, leaf *scopedGroup) error {
	_, err := readScopeLimits(scope, leaf)
	return err
}

func readScopeLimits(scope, leaf *scopedGroup) (scopedLimits, error) {
	var limits scopedLimits
	if leaf.Path != scope.Path && !strings.HasPrefix(leaf.Path, scope.Path+"/") {
		return limits, errors.New("attempt leaf outside run scope")
	}
	limits = scopedLimits{Path: leaf.Path, MemoryHigh: 6 << 30, MemoryMax: 8 << 30}
	for _, limit := range []struct {
		name  string
		value int64
	}{
		{"memory.high", limits.MemoryHigh}, {"memory.max", limits.MemoryMax}, {"memory.swap.max", limits.SwapMax},
	} {
		if actual, err := readCgroupValue(scope.root, limit.name); err != nil || actual != strconv.FormatInt(limit.value, 10) {
			return limits, fmt.Errorf("run scope %s unproved", limit.name)
		}
	}
	tasks, err := readCgroupValue(scope.root, "pids.max")
	if err != nil {
		return limits, fmt.Errorf("read run scope pids.max: %w", err)
	}
	limits.PIDsMax, err = strconv.Atoi(tasks)
	if err != nil || limits.PIDsMax < 1 || limits.PIDsMax > 256 {
		return limits, errors.New("run scope pids.max unproved or excessive")
	}
	if err := scope.verifyPath(); err != nil {
		return limits, err
	}
	if err := leaf.verifyPath(); err != nil {
		return limits, err
	}
	current, err := procGroup(os.Getpid())
	if err != nil {
		return limits, err
	}
	if group := leaf.Path[len(cgroupRoot):]; current == group || strings.HasPrefix(current, group+"/") {
		return limits, errors.New("supervisor inside managed cgroup")
	}
	// A leaf without a local cpuset controller inherits the nearest effective mask.
	for path := leaf.Path; path != cgroupRoot; path = filepath.Dir(path) {
		raw, err := os.ReadFile(filepath.Join(path, "cpuset.cpus.effective"))
		if errors.Is(err, os.ErrNotExist) {
			continue
		}
		if err != nil {
			return limits, err
		}
		limits.AllowedCPUs = strings.TrimSpace(string(raw))
		return limits, verifyCPUSet(limits.AllowedCPUs)
	}
	return limits, errors.New("effective cpuset unavailable")
}

func verifyCPUSet(value string) error {
	for _, segment := range strings.Split(value, ",") {
		low, high, isRange := strings.Cut(segment, "-")
		n, err := strconv.Atoi(low)
		if err != nil || n < 0 || n > 15 {
			return fmt.Errorf("effective cpuset outside CPUs 0-15: %q", value)
		}
		if isRange {
			m, err := strconv.Atoi(high)
			if err != nil || m < n || m > 15 {
				return fmt.Errorf("effective cpuset outside CPUs 0-15: %q", value)
			}
		}
	}
	return nil
}

func procGroup(pid int) (string, error) {
	raw, err := os.ReadFile(fmt.Sprintf("/proc/%d/cgroup", pid))
	if err != nil {
		return "", err
	}
	path, ok := strings.CutPrefix(strings.TrimSpace(string(raw)), "0::")
	if !ok || !filepath.IsAbs(path) || filepath.Clean(path) != path || strings.ContainsRune(path, '\n') {
		return "", errors.New("invalid unified cgroup membership")
	}
	return path, nil
}

func startTicks(pid int) (uint64, error) {
	raw, err := os.ReadFile(fmt.Sprintf("/proc/%d/stat", pid))
	if err != nil {
		return 0, err
	}
	line := string(raw)
	end := strings.LastIndex(line, ") ")
	if end < 0 || !strings.HasPrefix(line, strconv.Itoa(pid)+" (") {
		return 0, errors.New("invalid proc stat")
	}
	fields := strings.Fields(line[end+2:])
	if len(fields) < 20 {
		return 0, errors.New("missing process start ticks")
	}
	ticks, err := strconv.ParseUint(fields[19], 10, 64)
	if err != nil || ticks == 0 {
		return 0, errors.New("invalid process start ticks")
	}
	return ticks, nil
}

type scopedPID struct {
	Role       string `json:"role"`
	PID        int    `json:"pid"`
	StartTicks uint64 `json:"start_ticks"`
	Cgroup     string `json:"cgroup_path"`
}

func (g *scopedGroup) identity(role string, pid int) (scopedPID, error) {
	ticks, err := startTicks(pid)
	if err != nil {
		return scopedPID{}, err
	}
	path, err := procGroup(pid)
	if err != nil || path != g.Path[len(cgroupRoot):] {
		return scopedPID{}, fmt.Errorf("PID %d outside attempt leaf", pid)
	}
	again, err := startTicks(pid)
	if err != nil || again != ticks {
		return scopedPID{}, fmt.Errorf("PID %d changed while observing custody", pid)
	}
	return scopedPID{role, pid, ticks, path}, nil
}

func (g *scopedGroup) members() ([]int, error) {
	dir, err := g.root.Open(".")
	if err != nil {
		return nil, err
	}
	defer dir.Close()
	items, err := dir.ReadDir(-1)
	if err != nil {
		return nil, err
	}
	for _, item := range items {
		if item.IsDir() {
			return nil, errors.New("nested cgroup membership unproved")
		}
	}
	raw, err := readCgroupValue(g.root, "cgroup.procs")
	if err != nil {
		return nil, err
	}
	pids := make([]int, 0)
	for _, field := range strings.Fields(raw) {
		pid, err := strconv.Atoi(field)
		if err != nil || pid <= 1 {
			return nil, errors.New("invalid cgroup PID")
		}
		pids = append(pids, pid)
	}
	slices.Sort(pids)
	return slices.Compact(pids), g.verifyPath()
}

func (g *scopedGroup) writeFreeze(value string) error {
	f, err := g.root.OpenFile("cgroup.freeze", os.O_WRONLY, 0)
	if err != nil {
		return err
	}
	_, err = f.WriteString(value)
	return errors.Join(err, f.Close())
}

func (g *scopedGroup) waitEvent(ctx context.Context, key, value string) error {
	ticker := time.NewTicker(10 * time.Millisecond)
	defer ticker.Stop()
	for {
		raw, err := readCgroupValue(g.root, "cgroup.events")
		if err != nil {
			return err
		}
		if slices.Contains(strings.Split(raw, "\n"), key+" "+value) {
			return g.verifyPath()
		}
		select {
		case <-ticker.C:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

func signalScopedPID(fd *os.File, signal syscall.Signal) error {
	if fd == nil {
		return errors.New("retained pidfd absent")
	}
	raw, err := fd.SyscallConn()
	if err != nil {
		return err
	}
	var signalErr syscall.Errno
	err = raw.Control(func(handle uintptr) {
		_, _, signalErr = syscall.Syscall6(pidfdSendSignal, handle, uintptr(signal), 0, 0, 0, 0)
	})
	if err != nil {
		return err
	}
	if signalErr != 0 {
		return signalErr
	}
	return nil
}

func readMonotonicClock(ts *syscall.Timespec) error {
	const clockMonotonic = 1
	if _, _, errno := syscall.Syscall(syscall.SYS_CLOCK_GETTIME, clockMonotonic, uintptr(unsafe.Pointer(ts)), 0); errno != 0 {
		return errno
	}
	return nil
}

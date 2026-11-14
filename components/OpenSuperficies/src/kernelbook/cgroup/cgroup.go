package cgroup

import (
	"bufio"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"golang.org/x/sys/unix"
)

const cgroupRoot = "/sys/fs/cgroup"

// Cgroup represents a cgroup v2 directory under the sibling slice (ia.slice).
type Cgroup struct {
	Name    string
	Path    string
	dirFile *os.File
}

// FindCurrentCgroup parses /proc/self/cgroup and returns the absolute cgroup v2 path under /sys/fs/cgroup.
func FindCurrentCgroup() (string, error) {
	data, err := os.ReadFile("/proc/self/cgroup")
	if err != nil {
		return "", fmt.Errorf("cgroup: read /proc/self/cgroup failed: %w", err)
	}

	for _, line := range strings.Split(strings.TrimSpace(string(data)), "\n") {
		parts := strings.SplitN(line, ":", 3)
		if len(parts) == 3 && parts[0] == "0" && parts[1] == "" {
			rel := strings.TrimPrefix(parts[2], "/")
			return filepath.Join(cgroupRoot, rel), nil
		}
	}

	return "", errors.New("cgroup: cgroup v2 unified hierarchy entry not found in /proc/self/cgroup")
}

// FindSiblingParent returns the parent slice directory under which sibling cgroups are created (e.g. ia.slice).
func FindSiblingParent() (string, error) {
	curr, err := FindCurrentCgroup()
	if err != nil {
		return "", err
	}
	parent := filepath.Dir(curr)
	if parent == curr || parent == cgroupRoot || parent == "/" {
		return "", fmt.Errorf("cgroup: invalid parent directory for sibling creation: %s", parent)
	}
	return parent, nil
}

// CreateSibling creates and opens a new sibling cgroup directory under the parent slice.
// If the directory already exists from a previous run, it is cleaned up first.
func CreateSibling(name string) (*Cgroup, error) {
	parent, err := FindSiblingParent()
	if err != nil {
		return nil, err
	}

	target := filepath.Join(parent, name)

	// Clean up existing stale directory if present
	if info, err := os.Stat(target); err == nil && info.IsDir() {
		_ = killAndRemove(target)
	}

	if err := os.Mkdir(target, 0755); err != nil {
		return nil, fmt.Errorf("cgroup: mkdir sibling %s failed: %w", target, err)
	}

	f, err := os.Open(target)
	if err != nil {
		_ = os.Remove(target)
		return nil, fmt.Errorf("cgroup: open sibling dir %s failed: %w", target, err)
	}

	return &Cgroup{
		Name:    name,
		Path:    target,
		dirFile: f,
	}, nil
}

// Fd returns the directory file descriptor suitable for SysProcAttr.CgroupFD.
func (c *Cgroup) Fd() int {
	if c == nil || c.dirFile == nil {
		return -1
	}
	return int(c.dirFile.Fd())
}

// AddProc moves the given PID into the cgroup via cgroup.procs.
func (c *Cgroup) AddProc(pid int) error {
	procsFile := filepath.Join(c.Path, "cgroup.procs")
	return os.WriteFile(procsFile, []byte(strconv.Itoa(pid)+"\n"), 0644)
}

// Procs returns all PIDs currently assigned to this cgroup.
func (c *Cgroup) Procs() ([]int, error) {
	procsFile := filepath.Join(c.Path, "cgroup.procs")
	f, err := os.Open(procsFile)
	if err != nil {
		return nil, err
	}
	defer f.Close()

	var pids []int
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		text := strings.TrimSpace(scanner.Text())
		if text == "" {
			continue
		}
		pid, err := strconv.Atoi(text)
		if err == nil {
			pids = append(pids, pid)
		}
	}
	return pids, scanner.Err()
}

// IsPopulated returns true if the cgroup currently contains any processes (populated 1 in cgroup.events).
func (c *Cgroup) IsPopulated() (bool, error) {
	eventsFile := filepath.Join(c.Path, "cgroup.events")
	data, err := os.ReadFile(eventsFile)
	if err != nil {
		return false, err
	}

	for _, line := range strings.Split(string(data), "\n") {
		fields := strings.Fields(line)
		if len(fields) == 2 && fields[0] == "populated" {
			return fields[1] == "1", nil
		}
	}
	return false, errors.New("cgroup: 'populated' key not found in cgroup.events")
}

// Populated returns 1 if populated, 0 if empty.
func (c *Cgroup) Populated() (int, error) {
	pop, err := c.IsPopulated()
	if err != nil {
		return -1, err
	}
	if pop {
		return 1, nil
	}
	return 0, nil
}

// Kill recursively signals all processes in the cgroup via cgroup.kill.
func (c *Cgroup) Kill() error {
	killFile := filepath.Join(c.Path, "cgroup.kill")
	return os.WriteFile(killFile, []byte("1\n"), 0644)
}

// WaitEmpty polls cgroup.events until populated becomes 0 or timeout expires.
func (c *Cgroup) WaitEmpty(timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	for {
		pop, err := c.IsPopulated()
		if err == nil && !pop {
			return nil
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("cgroup: timeout waiting for cgroup %s to become empty", c.Path)
		}
		time.Sleep(15 * time.Millisecond)
	}
}

// Destroy closes the directory descriptor and removes the cgroup directory.
func (c *Cgroup) Destroy() error {
	if c == nil {
		return nil
	}
	if c.dirFile != nil {
		_ = c.dirFile.Close()
		c.dirFile = nil
	}
	return killAndRemove(c.Path)
}

func killAndRemove(dir string) error {
	killFile := filepath.Join(dir, "cgroup.kill")
	if _, err := os.Stat(killFile); err == nil {
		_ = os.WriteFile(killFile, []byte("1\n"), 0644)
	}

	deadline := time.Now().Add(2 * time.Second)
	var lastErr error
	for {
		lastErr = unix.Rmdir(dir)
		if lastErr == nil || errors.Is(lastErr, unix.ENOENT) {
			return nil
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("cgroup: rmdir %s timed out: %w", dir, lastErr)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

// TryCommandCageSubtreeControl attempts to enable controllers on the current command cage.
// Under cgroup v2, this fails with EBUSY because the leaf cage already contains processes.
func TryCommandCageSubtreeControl() error {
	curr, err := FindCurrentCgroup()
	if err != nil {
		return err
	}
	scFile := filepath.Join(curr, "cgroup.subtree_control")
	return os.WriteFile(scFile, []byte("+memory +pids\n"), 0644)
}

package microvm

import (
	"bufio"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"golang.org/x/sys/unix"
)

var (
	// DefaultX925Cores represents the measured Cortex-X925 logical CPUs on NVIDIA GB10
	DefaultX925Cores = []int{5, 6, 7, 8, 9, 15, 16, 17, 18, 19}
	vcpuCommRegex    = regexp.MustCompile(`^CPU\s+(\d+)/KVM$`)
)

// VCPUThread represents an identified QEMU vCPU thread and its assigned physical CPU.
type VCPUThread struct {
	Index       int
	TID         int
	AssignedCPU int
}

// DiscoverX925CPUs scans sysfs MIDR registers to find all online Cortex-X925 cores (Part 0xd85).
func DiscoverX925CPUs() []int {
	return DiscoverX925CPUsFromSysfs("/sys/devices/system/cpu")
}

// DiscoverX925CPUsFromSysfs scans sysfs MIDR registers under the given sysfs CPU directory.
func DiscoverX925CPUsFromSysfs(sysfsBase string) []int {
	var x925CPUs []int

	matches, err := filepath.Glob(filepath.Join(sysfsBase, "cpu[0-9]*"))
	if err != nil || len(matches) == 0 {
		return append([]int(nil), DefaultX925Cores...)
	}

	for _, cpuDir := range matches {
		cpuBase := filepath.Base(cpuDir)
		cpuIDStr := strings.TrimPrefix(cpuBase, "cpu")
		cpuID, err := strconv.Atoi(cpuIDStr)
		if err != nil {
			continue
		}

		// Check if CPU is online
		onlineFile := filepath.Join(cpuDir, "online")
		if data, err := os.ReadFile(onlineFile); err == nil {
			if strings.TrimSpace(string(data)) == "0" {
				continue
			}
		}

		regFile := filepath.Join(cpuDir, "regs", "identification", "midr_el1")
		if _, err := os.Stat(regFile); os.IsNotExist(err) {
			regFile = filepath.Join(cpuDir, "regs", "identification", "midr")
		}

		data, err := os.ReadFile(regFile)
		if err != nil {
			continue
		}

		midrStr := strings.TrimSpace(string(data))
		midr, err := strconv.ParseUint(strings.TrimPrefix(midrStr, "0x"), 16, 64)
		if err != nil {
			continue
		}

		part := (midr >> 4) & 0xfff
		if part == 0xd85 {
			x925CPUs = append(x925CPUs, cpuID)
		}
	}

	if len(x925CPUs) == 0 {
		return append([]int(nil), DefaultX925Cores...)
	}

	sort.Ints(x925CPUs)
	return x925CPUs
}

// SelectVCPUs selects vcpuCount X925 CPUs. For vcpus=4, returns {5, 6, 7, 8}.
func SelectVCPUs(vcpuCount int) ([]int, error) {
	available := DiscoverX925CPUs()
	if len(available) < vcpuCount {
		return nil, fmt.Errorf("fijacion: insufficient X925 CPUs: requested %d, found %d", vcpuCount, len(available))
	}
	selected := make([]int, vcpuCount)
	copy(selected, available[:vcpuCount])
	return selected, nil
}

// SetThreadAffinity sets the CPU affinity mask of a given TID to a single target CPU.
func SetThreadAffinity(tid int, cpu int) error {
	var mask unix.CPUSet
	mask.Zero()
	mask.Set(cpu)
	return unix.SchedSetaffinity(tid, &mask)
}

// SetProcessAffinity sets the CPU affinity mask of a PID to a list of CPUs.
func SetProcessAffinity(pid int, cpus []int) error {
	var mask unix.CPUSet
	mask.Zero()
	for _, c := range cpus {
		mask.Set(c)
	}
	return unix.SchedSetaffinity(pid, &mask)
}

// DiscoverVCPUThreads scans /proc/<pid>/task/ to discover all "CPU n/KVM" threads.
func DiscoverVCPUThreads(pid int) ([]VCPUThread, error) {
	taskDir := fmt.Sprintf("/proc/%d/task", pid)
	entries, err := os.ReadDir(taskDir)
	if err != nil {
		return nil, fmt.Errorf("fijacion: read task dir %s: %w", taskDir, err)
	}

	var threads []VCPUThread
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		tid, err := strconv.Atoi(entry.Name())
		if err != nil {
			continue
		}

		commPath := filepath.Join(taskDir, entry.Name(), "comm")
		commBytes, err := os.ReadFile(commPath)
		if err != nil {
			continue
		}
		comm := strings.TrimSpace(string(commBytes))
		sub := vcpuCommRegex.FindStringSubmatch(comm)
		if len(sub) == 2 {
			idx, _ := strconv.Atoi(sub[1])
			threads = append(threads, VCPUThread{
				Index: idx,
				TID:   tid,
			})
		}
	}

	sort.Slice(threads, func(i, j int) bool {
		return threads[i].Index < threads[j].Index
	})

	return threads, nil
}

// PinVCPUThreads waits for QEMU vCPU threads to be created, then pins each 1:1 to an X925 core.
func PinVCPUThreads(pid int, assignedCPUs []int, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	expectedCount := len(assignedCPUs)

	var threads []VCPUThread
	for {
		var err error
		threads, err = DiscoverVCPUThreads(pid)
		if err == nil && len(threads) == expectedCount {
			break
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("fijacion: timed out waiting for %d vCPU threads (found %d)", expectedCount, len(threads))
		}
		time.Sleep(10 * time.Millisecond)
	}

	// 1. Pin main QEMU emulator thread to reserved core 9 (or last assigned + 1)
	mainLoopCPU := 9
	if err := SetThreadAffinity(pid, mainLoopCPU); err != nil {
		return fmt.Errorf("fijacion: failed to set affinity for main thread (PID %d) to CPU %d: %w", pid, mainLoopCPU, err)
	}
	mainStatusFile := fmt.Sprintf("/proc/%d/task/%d/status", pid, pid)
	mainAllowed, err := readCPUsAllowedList(mainStatusFile)
	if err != nil {
		return fmt.Errorf("fijacion: failed to verify main thread status for PID %d: %w", pid, err)
	}
	if mainAllowed != strconv.Itoa(mainLoopCPU) {
		return fmt.Errorf("fijacion: main thread (PID %d) affinity mismatch: expected %q, got %q",
			pid, strconv.Itoa(mainLoopCPU), mainAllowed)
	}

	// 2. Pin each vCPU thread 1:1 to assigned physical CPU
	for i := range threads {
		targetCPU := assignedCPUs[i]
		threads[i].AssignedCPU = targetCPU
		if err := SetThreadAffinity(threads[i].TID, targetCPU); err != nil {
			return fmt.Errorf("fijacion: failed to set affinity for vCPU %d (TID %d) to CPU %d: %w",
				threads[i].Index, threads[i].TID, targetCPU, err)
		}
	}

	// 3. Verify that each vCPU allowed list contains strictly the single assigned core
	for _, th := range threads {
		statusFile := fmt.Sprintf("/proc/%d/task/%d/status", pid, th.TID)
		allowed, err := readCPUsAllowedList(statusFile)
		if err != nil {
			return fmt.Errorf("fijacion: failed to verify status for TID %d: %w", th.TID, err)
		}
		expectedStr := strconv.Itoa(th.AssignedCPU)
		if allowed != expectedStr {
			return fmt.Errorf("fijacion: vCPU %d (TID %d) affinity mismatch: expected %q, got %q",
				th.Index, th.TID, expectedStr, allowed)
		}
	}

	return nil
}

func readCPUsAllowedList(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()

	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := scanner.Text()
		if strings.HasPrefix(line, "Cpus_allowed_list:") {
			parts := strings.SplitN(line, ":", 2)
			if len(parts) == 2 {
				return strings.TrimSpace(parts[1]), nil
			}
		}
	}
	return "", fmt.Errorf("Cpus_allowed_list not found in %s", path)
}

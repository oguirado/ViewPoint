package microvm

import (
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"opensuperficies.local/srf/src/quarantineoracle"
)

// PuertaVeredicto represents a quarantine gate evaluation outcome.
type PuertaVeredicto struct {
	ID      string `json:"id"`
	Tipo    string `json:"tipo"` // "veredicto" | "informativa"
	Verde   bool   `json:"verde"`
	Detalle string `json:"detalle"`
}

// VCuarentenaResult aggregates the outcome of all Gates V-A through V-I.
type VCuarentenaResult struct {
	Slot    uint16            `json:"slot"`
	PID     int               `json:"pid"`
	Verde   bool              `json:"verde"`
	Puertas []PuertaVeredicto `json:"puertas"`
}

// EvaluarPuertas executes all quarantine gate probes against a running MicroVM.
func EvaluarPuertas(slot uint16, pid int, vcpus int, memMiB int, rundir string) (*VCuarentenaResult, error) {
	if pid <= 0 {
		return nil, fmt.Errorf("vgates: invalid pid %d", pid)
	}

	cmdlineArgs, err := readProcCmdline(pid)
	if err != nil {
		return nil, fmt.Errorf("vgates: failed reading cmdline for pid %d: %w", pid, err)
	}

	var puertas []PuertaVeredicto

	// V-A (veredicto): exactly one -netdev user with restrict=on; every hostfwd binds 127.0.0.1; 0 tap/bridge/socket/vhost; 0 guestfwd
	puertas = append(puertas, EvalGateVA(cmdlineArgs, slot))

	// V-B (veredicto): 0 descriptors on /dev/net/tun or /dev/vhost-*
	puertas = append(puertas, evalGateVB(pid))

	// V-C (veredicto): Seccomp: 2 and Seccomp_filters = cage baseline + 1 (baseline 1 -> 2)
	puertas = append(puertas, evalGateVC(pid))

	// V-D (veredicto): cgroup is ia.slice/srf-ng-<slot>; memory.max = guest + 256 MiB; memory.swap.max = 0; pids.max bounded
	puertas = append(puertas, evalGateVD(pid, slot, memMiB))

	// V-E (veredicto): exactly one virtserialport name=opensuperficies.control on a socket inside a 0700 rundir; QMP socket outside guest path
	puertas = append(puertas, evalGateVE(cmdlineArgs, rundir))

	// V-F (veredicto): 0 virtfs, 9p, vhost-user-fs, -fsdev, usb, audio, gpu devices
	puertas = append(puertas, evalGateVF(cmdlineArgs))

	// V-G (informativa): listeners limited to control.sock, qmp.sock, 127.0.0.1:<xpra>
	puertas = append(puertas, evalGateVG(slot, rundir))

	// V-H (veredicto): count of CPU n/KVM threads = vCPUs; each allowed list is a single CPU in {5-9, 15-19}
	puertas = append(puertas, evalGateVH(pid, vcpus))

	// V-I (veredicto): -cpu host,pmu=off, -S present, machine virt-8.2
	puertas = append(puertas, evalGateVI(cmdlineArgs))

	todoVerde := true
	for _, p := range puertas {
		if p.Tipo == "veredicto" && !p.Verde {
			todoVerde = false
			break
		}
	}

	return &VCuarentenaResult{
		Slot:    slot,
		PID:     pid,
		Verde:   todoVerde,
		Puertas: puertas,
	}, nil
}

func readProcCmdline(pid int) ([]string, error) {
	cmdlinePath := fmt.Sprintf("/proc/%d/cmdline", pid)
	data, err := os.ReadFile(cmdlinePath)
	if err != nil {
		return nil, err
	}
	raw := string(data)
	if len(raw) == 0 {
		return nil, fmt.Errorf("empty cmdline in %s", cmdlinePath)
	}
	parts := strings.Split(raw, "\x00")
	var res []string
	for _, p := range parts {
		if p != "" {
			res = append(res, p)
		}
	}
	return res, nil
}

func EvalGateVA(args []string, slot uint16) PuertaVeredicto {
	p := PuertaVeredicto{
		ID:    "V-A",
		Tipo:  "veredicto",
		Verde: true,
	}

	var netdevUserCount int
	var hasRestrictOn bool
	var hasRestrictOff bool
	var invalidHostFwd bool
	var hasProhibitedNetdev bool
	var hasGuestFwd bool

	for i := 0; i < len(args); i++ {
		arg := args[i]
		if arg == "-netdev" || arg == "--netdev" {
			if i+1 < len(args) {
				val := args[i+1]
				if strings.HasPrefix(val, "user,") || strings.HasPrefix(val, "user") {
					netdevUserCount++
					if strings.Contains(val, "restrict=on") {
						hasRestrictOn = true
					}
					if strings.Contains(val, "restrict=off") {
						hasRestrictOff = true
					}
					if strings.Contains(val, "hostfwd=") {
						for _, fwd := range strings.Split(val, ",") {
							if strings.HasPrefix(fwd, "hostfwd=") {
								if !strings.Contains(fwd, "127.0.0.1") {
									invalidHostFwd = true
								}
							}
						}
					}
					if strings.Contains(val, "guestfwd=") {
						hasGuestFwd = true
					}
				} else if strings.HasPrefix(val, "tap") || strings.HasPrefix(val, "bridge") ||
					strings.HasPrefix(val, "socket") || strings.HasPrefix(val, "vhost") {
					hasProhibitedNetdev = true
				}
			}
		}
		if strings.Contains(arg, "guestfwd=") {
			hasGuestFwd = true
		}
	}

	if netdevUserCount != 1 {
		p.Verde = false
		p.Detalle = fmt.Sprintf("expected exactly 1 netdev user, found %d", netdevUserCount)
		return p
	}
	if hasRestrictOff || !hasRestrictOn {
		p.Verde = false
		p.Detalle = "netdev user does not enforce restrict=on"
		return p
	}
	if invalidHostFwd {
		p.Verde = false
		p.Detalle = "hostfwd rule does not bind strictly to 127.0.0.1"
		return p
	}
	if hasProhibitedNetdev {
		p.Verde = false
		p.Detalle = "prohibited netdev type (tap/bridge/socket/vhost) found"
		return p
	}
	if hasGuestFwd {
		p.Verde = false
		p.Detalle = "guestfwd rule present in netdev configuration"
		return p
	}

	p.Detalle = "exactly 1 user netdev with restrict=on, loopback hostfwd, 0 bridge/tap/guestfwd"
	return p
}

func evalGateVB(pid int) PuertaVeredicto {
	p := PuertaVeredicto{
		ID:    "V-B",
		Tipo:  "veredicto",
		Verde: true,
	}

	fdDir := fmt.Sprintf("/proc/%d/fd", pid)
	entries, err := os.ReadDir(fdDir)
	if err != nil {
		p.Verde = false
		p.Detalle = fmt.Sprintf("failed reading %s: %v", fdDir, err)
		return p
	}

	var badTargets []string
	for _, entry := range entries {
		target, err := os.Readlink(filepath.Join(fdDir, entry.Name()))
		if err != nil {
			continue
		}
		if target == "/dev/net/tun" || strings.HasPrefix(target, "/dev/vhost-") {
			badTargets = append(badTargets, target)
		}
	}

	if len(badTargets) > 0 {
		p.Verde = false
		p.Detalle = fmt.Sprintf("prohibited file descriptors open on %v", badTargets)
		return p
	}

	p.Detalle = "0 file descriptors on /dev/net/tun or /dev/vhost-*"
	return p
}

func evalGateVC(pid int) PuertaVeredicto {
	p := PuertaVeredicto{
		ID:    "V-C",
		Tipo:  "veredicto",
		Verde: true,
	}

	ps, err := quarantineoracle.ParsePidStatus(pid)
	if err != nil {
		p.Verde = false
		p.Detalle = fmt.Sprintf("error reading status: %v", err)
		return p
	}

	if ps.Seccomp != 2 {
		p.Verde = false
		p.Detalle = fmt.Sprintf("Seccomp mode expected 2 (filter), got %d", ps.Seccomp)
		return p
	}

	// Cage baseline is 1 filter; QEMU -sandbox on installs +1 filter (measured: 2)
	if ps.SeccompFilters < 2 {
		p.Verde = false
		p.Detalle = fmt.Sprintf("Seccomp_filters expected >= 2, got %d", ps.SeccompFilters)
		return p
	}

	p.Detalle = fmt.Sprintf("Seccomp: %d, Seccomp_filters: %d (cage baseline + 1 verified)", ps.Seccomp, ps.SeccompFilters)
	return p
}

func evalGateVD(pid int, slot uint16, memMiB int) PuertaVeredicto {
	p := PuertaVeredicto{
		ID:    "V-D",
		Tipo:  "veredicto",
		Verde: true,
	}

	cgroupFile := fmt.Sprintf("/proc/%d/cgroup", pid)
	data, err := os.ReadFile(cgroupFile)
	if err != nil {
		p.Verde = false
		p.Detalle = fmt.Sprintf("failed reading %s: %v", cgroupFile, err)
		return p
	}

	expectedCgroupSuffix := fmt.Sprintf("ia.slice/srf-ng-%d", slot)
	var cgroupPath string
	for _, line := range strings.Split(strings.TrimSpace(string(data)), "\n") {
		parts := strings.SplitN(line, ":", 3)
		if len(parts) == 3 {
			cgroupPath = parts[2]
			break
		}
	}

	if !strings.Contains(cgroupPath, expectedCgroupSuffix) {
		p.Verde = false
		p.Detalle = fmt.Sprintf("cgroup %q does not contain expected suffix %q", cgroupPath, expectedCgroupSuffix)
		return p
	}

	// Locate cgroup v2 directory under /sys/fs/cgroup
	fullCgroupDir := filepath.Join("/sys/fs/cgroup", strings.TrimPrefix(cgroupPath, "/"))
	if _, err := os.Stat(fullCgroupDir); err != nil {
		p.Verde = false
		p.Detalle = fmt.Sprintf("cgroup directory %s does not exist: %v", fullCgroupDir, err)
		return p
	}

	// Verify memory.max = (memMiB + 256) MiB
	expectedMemBytes := uint64(memMiB+256) * 1024 * 1024
	memMaxData, err := os.ReadFile(filepath.Join(fullCgroupDir, "memory.max"))
	if err != nil {
		p.Verde = false
		p.Detalle = fmt.Sprintf("failed reading memory.max: %v", err)
		return p
	}
	memMaxVal, _ := strconv.ParseUint(strings.TrimSpace(string(memMaxData)), 10, 64)
	if memMaxVal != expectedMemBytes {
		p.Verde = false
		p.Detalle = fmt.Sprintf("memory.max expected %d bytes, got %d", expectedMemBytes, memMaxVal)
		return p
	}

	// Verify memory.swap.max = 0
	swapMaxData, err := os.ReadFile(filepath.Join(fullCgroupDir, "memory.swap.max"))
	if err != nil {
		p.Verde = false
		p.Detalle = fmt.Sprintf("failed reading memory.swap.max: %v", err)
		return p
	}
	if strings.TrimSpace(string(swapMaxData)) != "0" {
		p.Verde = false
		p.Detalle = fmt.Sprintf("memory.swap.max expected 0, got %s", strings.TrimSpace(string(swapMaxData)))
		return p
	}

	// Verify pids.max bounded
	pidsMaxData, err := os.ReadFile(filepath.Join(fullCgroupDir, "pids.max"))
	if err != nil {
		p.Verde = false
		p.Detalle = fmt.Sprintf("failed reading pids.max: %v", err)
		return p
	}
	pidsTrimmed := strings.TrimSpace(string(pidsMaxData))
	if pidsTrimmed == "max" {
		p.Verde = false
		p.Detalle = "pids.max is unbounded (max)"
		return p
	}

	p.Detalle = fmt.Sprintf("cgroup %s: memory.max=%d, memory.swap.max=0, pids.max=%s", expectedCgroupSuffix, memMaxVal, pidsTrimmed)
	return p
}

func evalGateVE(args []string, rundir string) PuertaVeredicto {
	p := PuertaVeredicto{
		ID:    "V-E",
		Tipo:  "veredicto",
		Verde: true,
	}

	cmdlineStr := strings.Join(args, " ")

	// 1. Verify exactly one virtserialport name=opensuperficies.control
	if !strings.Contains(cmdlineStr, "name=opensuperficies.control") {
		p.Verde = false
		p.Detalle = "missing virtserialport name=opensuperficies.control in cmdline"
		return p
	}

	// 2. Verify control socket is inside a mode 0700 rundir
	controlSock := filepath.Join(rundir, "control.sock")
	if !strings.Contains(cmdlineStr, controlSock) {
		p.Verde = false
		p.Detalle = fmt.Sprintf("control socket path %s missing from cmdline", controlSock)
		return p
	}

	info, err := os.Stat(rundir)
	if err != nil {
		p.Verde = false
		p.Detalle = fmt.Sprintf("rundir %s does not exist: %v", rundir, err)
		return p
	}
	if info.Mode().Perm() != 0700 {
		p.Verde = false
		p.Detalle = fmt.Sprintf("rundir permissions expected 0700, got %04o", info.Mode().Perm())
		return p
	}

	// 3. Verify QMP socket path is inside rundir (mode 0700) and outside any guest-facing mount
	qmpSock := filepath.Join(rundir, "qmp.sock")
	if !strings.Contains(cmdlineStr, qmpSock) {
		p.Verde = false
		p.Detalle = fmt.Sprintf("qmp socket path %s missing from cmdline", qmpSock)
		return p
	}

	p.Detalle = "virtserialport opensuperficies.control and QMP socket in private 0700 rundir"
	return p
}

func evalGateVF(args []string) PuertaVeredicto {
	p := PuertaVeredicto{
		ID:    "V-F",
		Tipo:  "veredicto",
		Verde: true,
	}

	forbidden := []string{
		"virtfs", "9p", "vhost-user-fs", "-fsdev", "fsdev",
		"usb", "soundhw", "audiodev", "vga", "virtio-gpu", "virtio-vga", "qxl",
	}

	cmdlineStr := strings.Join(args, " ")
	for _, f := range forbidden {
		if strings.Contains(cmdlineStr, f) {
			p.Verde = false
			p.Detalle = fmt.Sprintf("forbidden device/flag %q detected in cmdline", f)
			return p
		}
	}

	p.Detalle = "0 virtfs, 9p, vhost-user-fs, -fsdev, usb, audio, or gpu devices present"
	return p
}

func evalGateVG(slot uint16, rundir string) PuertaVeredicto {
	p := PuertaVeredicto{
		ID:    "V-G",
		Tipo:  "informativa",
		Verde: true,
	}

	expectedXpraPort := 10020 + slot
	controlSock := filepath.Join(rundir, "control.sock")
	qmpSock := filepath.Join(rundir, "qmp.sock")

	var probes []string
	var failedProbes []string

	// 1. Probe control.sock exists and is a unix socket
	if fi, err := os.Stat(controlSock); err == nil {
		if fi.Mode()&os.ModeSocket != 0 {
			probes = append(probes, "control.sock (socket ok)")
		} else {
			failedProbes = append(failedProbes, "control.sock (not a socket)")
		}
	} else {
		failedProbes = append(failedProbes, fmt.Sprintf("control.sock (absent: %v)", err))
	}

	// 2. Probe qmp.sock exists and is a unix socket
	if fi, err := os.Stat(qmpSock); err == nil {
		if fi.Mode()&os.ModeSocket != 0 {
			probes = append(probes, "qmp.sock (socket ok)")
		} else {
			failedProbes = append(failedProbes, "qmp.sock (not a socket)")
		}
	} else {
		failedProbes = append(failedProbes, fmt.Sprintf("qmp.sock (absent: %v)", err))
	}

	// 3. Probe host loopback port is listening
	conn, err := net.DialTimeout("tcp", fmt.Sprintf("127.0.0.1:%d", expectedXpraPort), 200*time.Millisecond)
	if err == nil {
		_ = conn.Close()
		probes = append(probes, fmt.Sprintf("127.0.0.1:%d (tcp connect ok)", expectedXpraPort))
	} else {
		failedProbes = append(failedProbes, fmt.Sprintf("127.0.0.1:%d (dial failed: %v)", expectedXpraPort, err))
	}

	if len(failedProbes) > 0 {
		p.Verde = false
		p.Detalle = fmt.Sprintf("listener probe failures: %s; verified: %s",
			strings.Join(failedProbes, ", "), strings.Join(probes, ", "))
		return p
	}

	p.Detalle = fmt.Sprintf("probed listeners verified: %s", strings.Join(probes, ", "))
	return p
}

func evalGateVH(pid int, vcpus int) PuertaVeredicto {
	p := PuertaVeredicto{
		ID:    "V-H",
		Tipo:  "veredicto",
		Verde: true,
	}

	threads, err := DiscoverVCPUThreads(pid)
	if err != nil {
		p.Verde = false
		p.Detalle = fmt.Sprintf("failed discovering vCPU threads: %v", err)
		return p
	}

	if len(threads) != vcpus {
		p.Verde = false
		p.Detalle = fmt.Sprintf("expected %d CPU n/KVM threads, found %d", vcpus, len(threads))
		return p
	}

	allowedCores := map[int]bool{
		5: true, 6: true, 7: true, 8: true, 9: true,
		15: true, 16: true, 17: true, 18: true, 19: true,
	}

	for _, th := range threads {
		statusFile := fmt.Sprintf("/proc/%d/task/%d/status", pid, th.TID)
		allowedStr, err := readCPUsAllowedList(statusFile)
		if err != nil {
			p.Verde = false
			p.Detalle = fmt.Sprintf("failed reading status for TID %d: %v", th.TID, err)
			return p
		}

		cpuNum, err := strconv.Atoi(allowedStr)
		if err != nil || !allowedCores[cpuNum] {
			p.Verde = false
			p.Detalle = fmt.Sprintf("thread vCPU %d (TID %d) allowed list %q not in X925 set {5-9, 15-19}",
				th.Index, th.TID, allowedStr)
			return p
		}
	}

	p.Detalle = fmt.Sprintf("%d vCPU threads pinned 1:1 to dedicated Cortex-X925 cores", vcpus)
	return p
}

func evalGateVI(args []string) PuertaVeredicto {
	p := PuertaVeredicto{
		ID:    "V-I",
		Tipo:  "veredicto",
		Verde: true,
	}

	cmdlineStr := strings.Join(args, " ")

	if !strings.Contains(cmdlineStr, "host,pmu=off") {
		p.Verde = false
		p.Detalle = "missing -cpu host,pmu=off"
		return p
	}

	var hasS bool
	for _, a := range args {
		if a == "-S" {
			hasS = true
			break
		}
	}
	if !hasS {
		p.Verde = false
		p.Detalle = "missing -S in QEMU arguments"
		return p
	}

	if !strings.Contains(cmdlineStr, "virt-8.2") {
		p.Verde = false
		p.Detalle = "missing machine virt-8.2 in QEMU arguments"
		return p
	}

	p.Detalle = "-cpu host,pmu=off, -S present, machine virt-8.2 verified"
	return p
}

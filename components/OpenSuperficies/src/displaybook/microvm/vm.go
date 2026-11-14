package microvm

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/unix"
	"opensuperficies.local/srf/src/displaybook/puertos"
	"opensuperficies.local/srf/src/kernelbook/cgroup"
	"opensuperficies.local/srf/src/kernelbook/lanzador"
)

var (
	// RestrictOption is "on" by default and set to "off" under mutante_restrict_off build tag
	RestrictOption = "on"

	activeLocksMu sync.Mutex
	activeLocks   = make(map[uint16]*puertos.SlotLock)
)

// MicroVMInfo contains live status metrics of an active MicroVM capsule.
type MicroVMInfo struct {
	Slot          uint16 `json:"slot"`
	Clase         string `json:"clase"`
	Contenedor    string `json:"contenedor"`
	Estado        string `json:"estado"`
	EstadoQMP     string `json:"estado_qmp"`
	AgenteControl string `json:"agente_control"`
	HostfwdTCP    bool   `json:"hostfwd_tcp"`
	PuertoXpra    uint16 `json:"puerto_xpra"`
	PID           int    `json:"pid"`
	VCPUs         int    `json:"vcpus"`
	MemMiB        int    `json:"mem_mib"`
	Rundir        string `json:"rundir"`
}

// DerriboInfo details the teardown outcome of a MicroVM capsule.
type DerriboInfo struct {
	Slot            uint16 `json:"slot"`
	Clase           string `json:"clase"`
	Estado          string `json:"estado"`
	RC              int    `json:"rc"`
	Populated       int    `json:"populated"`
	RundirEliminado bool   `json:"rundir_eliminado"`
}

// Metadata holds runtime parameters serialized inside the capsule rundir.
type Metadata struct {
	Slot       uint16 `json:"slot"`
	Clase      string `json:"clase"`
	Estado     string `json:"estado"`
	EstadoQMP  string `json:"estado_qmp,omitempty"`
	PID        int    `json:"pid"`
	Pidfd      int    `json:"pidfd"`
	VCPUs      int    `json:"vcpus"`
	MemMiB     int    `json:"mem_mib"`
	Rundir     string `json:"rundir"`
	CgroupName string `json:"cgroup_name"`
	CgroupPath string `json:"cgroup_path"`
	PuertoXpra uint16 `json:"puerto_xpra"`
}

// GetRundir returns the canonical mode 0700 runtime directory for a slot.
func GetRundir(slot uint16) string {
	base := os.Getenv("XDG_RUNTIME_DIR")
	if base == "" {
		base = "/run/user/1000"
		if _, err := os.Stat(base); err != nil {
			base = "/tmp"
		}
	}
	return filepath.Join(base, "opensuperficies", fmt.Sprintf("srf-ng-%d", slot))
}

// IniciarOpciones provides configuration parameters for launching a MicroVM capsule.
type IniciarOpciones struct {
	VCPUs         int           `json:"vcpus"`
	MemMiB        int           `json:"mem_mib"`
	Pausado       bool          `json:"pausado"`
	AgenteTimeout time.Duration `json:"agente_timeout"`
	Informe       string        `json:"informe"`
}

// SondaHostfwd probes the host-side SLIRP hostfwd TCP port with bounded attempts and timeout.
// Proves that the hostfwd rule is installed by SLIRP on the host loopback, NOT that a guest service answered.
func SondaHostfwd(port uint16, attempts int, timeout time.Duration) (bool, error) {
	var lastDialErr error
	for i := 0; i < attempts; i++ {
		conn, err := net.DialTimeout("tcp", fmt.Sprintf("127.0.0.1:%d", port), timeout)
		if err == nil {
			_ = conn.Close()
			return true, nil
		}
		lastDialErr = err
		time.Sleep(20 * time.Millisecond)
	}
	return false, lastDialErr
}

type siginfoKernel struct {
	Signo int32
	Errno int32
	Code  int32
	_pad0 int32
	Pid   int32
}

// Iniciar launches a hardened, pinned QEMU MicroVM capsule inside a dedicated cgroup slice.
func Iniciar(ctx context.Context, slot uint16, opts IniciarOpciones) (*MicroVMInfo, error) {
	// Clean up any stale or previous running instance on this slot
	if meta, err := CargarMetadata(slot); err == nil && meta.PID > 0 {
		_, _ = Detener(ctx, slot)
	}

	vcpus := opts.VCPUs
	if vcpus <= 0 {
		vcpus = 4
	}
	memMiB := opts.MemMiB
	if memMiB <= 0 {
		memMiB = 4096
	}
	agenteTimeout := opts.AgenteTimeout
	if agenteTimeout <= 0 {
		agenteTimeout = 15 * time.Second
	}

	// 1. Acquire advisory slot lock
	slotLock, err := puertos.ReservarSlot("", slot)
	if err != nil {
		return nil, fmt.Errorf("iniciar microvm: slot lock failed: %w", err)
	}
	activeLocksMu.Lock()
	activeLocks[slot] = slotLock
	activeLocksMu.Unlock()

	xpraPort := 10020 + slot
	cgroupName := fmt.Sprintf("srf-ng-%d", slot)
	rundir := GetRundir(slot)

	// 2. Prepare mode 0700 runtime directory
	if err := os.MkdirAll(rundir, 0700); err != nil {
		return nil, fmt.Errorf("iniciar microvm: mkdir rundir %s: %w", rundir, err)
	}
	if err := os.Chmod(rundir, 0700); err != nil {
		return nil, fmt.Errorf("iniciar microvm: chmod rundir %s: %w", rundir, err)
	}

	controlSock := filepath.Join(rundir, "control.sock")
	qmpSock := filepath.Join(rundir, "qmp.sock")
	_ = os.Remove(controlSock)
	_ = os.Remove(qmpSock)

	// 3. Create sibling cgroup under ia.slice
	cg, err := cgroup.CreateSibling(cgroupName)
	if err != nil {
		return nil, fmt.Errorf("iniciar microvm: create sibling cgroup %s: %w", cgroupName, err)
	}

	// Configure cgroup resource limits
	memMaxBytes := uint64(memMiB+256) * 1024 * 1024
	if err := os.WriteFile(filepath.Join(cg.Path, "memory.max"), []byte(strconv.FormatUint(memMaxBytes, 10)+"\n"), 0644); err != nil {
		_ = cg.Destroy()
		return nil, fmt.Errorf("iniciar microvm: write memory.max: %w", err)
	}
	if err := os.WriteFile(filepath.Join(cg.Path, "memory.swap.max"), []byte("0\n"), 0644); err != nil {
		_ = cg.Destroy()
		return nil, fmt.Errorf("iniciar microvm: write memory.swap.max: %w", err)
	}
	if err := os.WriteFile(filepath.Join(cg.Path, "pids.max"), []byte("512\n"), 0644); err != nil {
		_ = cg.Destroy()
		return nil, fmt.Errorf("iniciar microvm: write pids.max: %w", err)
	}

	// 4. Select dedicated Cortex-X925 physical cores
	assignedCPUs, err := SelectVCPUs(vcpus)
	if err != nil {
		_ = cg.Destroy()
		return nil, fmt.Errorf("iniciar microvm: select vcpus: %w", err)
	}

	// 5. Construct QEMU command arguments complying strictly with Gates V-A..V-I
	kernelPath := "guest/Image"
	rootPath := "guest/guest-root.raw"

	qemuArgs := []string{
		"-name", fmt.Sprintf("opensuperficies-%s,debug-threads=on", cgroupName),
		"-machine", "virt-8.2,accel=kvm,gic-version=host",
		"-cpu", "host,pmu=off",
		"-smp", fmt.Sprintf("cpus=%d,maxcpus=%d", vcpus, vcpus),
		"-m", strconv.Itoa(memMiB),
		"-nodefaults",
		"-no-user-config",
		"-no-reboot",
		"-display", "none",
		"-monitor", "none",
		"-serial", "none",
		"-S",
		"-kernel", kernelPath,
		"-append", "console=hvc0 root=/dev/vda init=/init rw quiet loglevel=3 panic=1 oops=panic mitigations=auto",
		"-netdev", fmt.Sprintf("user,id=net0,ipv4=on,ipv6=off,restrict=%s,hostfwd=tcp:127.0.0.1:%d-:14500", RestrictOption, xpraPort),
		"-device", "virtio-net-device,netdev=net0",
		"-object", "rng-random,id=rng0,filename=/dev/urandom",
		"-device", "virtio-rng-device,rng=rng0",
		"-drive", fmt.Sprintf("if=none,id=root,file=%s,format=raw,readonly=on", rootPath),
		"-device", "virtio-blk-device,drive=root",
		"-chardev", fmt.Sprintf("socket,id=control,path=%s,server=on,wait=off", controlSock),
		"-device", "virtio-serial-device",
		"-device", "virtserialport,chardev=control,name=opensuperficies.control",
		"-qmp", fmt.Sprintf("unix:%s,server=on,wait=off", qmpSock),
		"-sandbox", "on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny",
		"-msg", "timestamp=on",
	}

	// 6. Launch QEMU through validated launcher inside cgroup
	cmd, err := lanzador.CommandContext(ctx, "qemu-system-aarch64", qemuArgs...)
	if err != nil {
		_ = cg.Destroy()
		return nil, fmt.Errorf("iniciar microvm: lanzador validation failed: %w", err)
	}

	cmd.SysProcAttr = &syscall.SysProcAttr{
		Setpgid: true,
	}

	stdoutFile, err := os.OpenFile(filepath.Join(rundir, "qemu.stdout.log"), os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0644)
	if err != nil {
		_ = cg.Destroy()
		return nil, fmt.Errorf("iniciar microvm: create stdout log: %w", err)
	}
	defer stdoutFile.Close()

	stderrFile, err := os.OpenFile(filepath.Join(rundir, "qemu.stderr.log"), os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0644)
	if err != nil {
		_ = stdoutFile.Close()
		_ = cg.Destroy()
		return nil, fmt.Errorf("iniciar microvm: create stderr log: %w", err)
	}
	defer stderrFile.Close()

	cmd.Stdout = stdoutFile
	cmd.Stderr = stderrFile

	if err := cmd.Start(); err != nil {
		_ = cg.Destroy()
		return nil, fmt.Errorf("iniciar microvm: start qemu failed: %w", err)
	}

	pid := cmd.Process.Pid

	writeAbortReport := func(est, ctrl, qmpSt string, hostfwd bool) {
		if opts.Informe == "" {
			return
		}
		fInfo := &MicroVMInfo{
			Slot:          slot,
			Clase:         "microvm",
			Contenedor:    cgroupName,
			Estado:        est,
			EstadoQMP:     qmpSt,
			AgenteControl: ctrl,
			HostfwdTCP:    hostfwd,
			PuertoXpra:    xpraPort,
			PID:           pid,
			VCPUs:         vcpus,
			MemMiB:        memMiB,
			Rundir:        rundir,
		}
		_ = os.MkdirAll(filepath.Dir(opts.Informe), 0755)
		if rBytes, errR := json.MarshalIndent(fInfo, "", "  "); errR == nil {
			_ = os.WriteFile(opts.Informe, rBytes, 0644)
		}
	}

	if err := cg.AddProc(pid); err != nil {
		writeAbortReport("error", "abort", "none", false)
		_ = unix.Kill(pid, unix.SIGKILL)
		_ = cg.Destroy()
		return nil, fmt.Errorf("iniciar microvm: add pid to cgroup failed: %w", err)
	}
	pidfd, err := unix.PidfdOpen(pid, 0)
	if err != nil {
		pidfd = -1
	}

	// Set initial affinity of QEMU process to X925 core set
	if err := SetProcessAffinity(pid, DefaultX925Cores); err != nil {
		writeAbortReport("error", "abort", "none", false)
		_ = unix.Kill(pid, unix.SIGKILL)
		_ = cg.Destroy()
		return nil, fmt.Errorf("iniciar microvm: set process affinity: %w", err)
	}

	// 7. Pin vCPU threads 1:1 to dedicated X925 physical cores
	if err := PinVCPUThreads(pid, assignedCPUs, 5*time.Second); err != nil {
		writeAbortReport("error", "abort", "none", false)
		_ = unix.Kill(pid, unix.SIGKILL)
		_ = cg.Destroy()
		return nil, fmt.Errorf("iniciar microvm: pinning vcpu threads failed: %w", err)
	}

	// 8. Connect to QMP and issue 'cont'
	qmpClient, err := DialQMP(qmpSock, 5*time.Second)
	if err != nil {
		writeAbortReport("error", "abort", "disconnected", false)
		_ = unix.Kill(pid, unix.SIGKILL)
		_ = cg.Destroy()
		return nil, fmt.Errorf("iniciar microvm: qmp dial failed: %w", err)
	}

	if err := qmpClient.Capabilities(); err != nil {
		writeAbortReport("error", "abort", "error", false)
		_ = qmpClient.Close()
		_ = unix.Kill(pid, unix.SIGKILL)
		_ = cg.Destroy()
		return nil, fmt.Errorf("iniciar microvm: qmp capabilities failed: %w", err)
	}

	// 8b. Connect to virtio-serial control socket BEFORE resuming guest execution
	ctrlClient, err := DialControl(controlSock, 8*time.Second)
	if err != nil {
		writeAbortReport("error", "abort", "connected", false)
		_ = qmpClient.Close()
		_ = unix.Kill(pid, unix.SIGKILL)
		_ = cg.Destroy()
		return nil, fmt.Errorf("iniciar microvm: control dial failed: %w", err)
	}

	if !opts.Pausado {
		if err := qmpClient.Cont(); err != nil {
			writeAbortReport("error", "abort", "error", false)
			_ = ctrlClient.Close()
			_ = qmpClient.Close()
			_ = unix.Kill(pid, unix.SIGKILL)
			_ = cg.Destroy()
			return nil, fmt.Errorf("iniciar microvm: qmp cont failed: %w", err)
		}
	}

	// Derive estado from QMP query-status
	var estado string
	qmpStatus, err := qmpClient.QueryStatus()
	if err != nil {
		writeAbortReport("error", "abort", "error", false)
		_ = ctrlClient.Close()
		_ = qmpClient.Close()
		_ = unix.Kill(pid, unix.SIGKILL)
		_ = cg.Destroy()
		return nil, fmt.Errorf("iniciar microvm: qmp query-status failed: %w", err)
	}
	_ = qmpClient.Close()

	estadoQMP := qmpStatus.Status
	if qmpStatus.Running {
		estado = "listo"
	} else if qmpStatus.Status == "prelaunch" {
		estado = "paused"
	} else {
		estado = qmpStatus.Status
	}

	// 9. Await PID1 readiness message and derive agente_control
	var agenteControl string
	var agenteErr error
	if opts.Pausado {
		agenteControl = "pausado"
		_ = ctrlClient.Close()
	} else {
		if err := ctrlClient.WaitForPID1(agenteTimeout); err != nil {
			agenteControl = "timeout"
			agenteErr = fmt.Errorf("iniciar microvm: waiting for PID1 failed: %w", err)
		} else {
			agenteControl = "listo"
		}
		_ = ctrlClient.Close()
	}

	if agenteErr != nil {
		// Stop guest so it enters paused state and remains quarantined
		if qmpC, errQ := DialQMP(qmpSock, 1*time.Second); errQ == nil {
			_ = qmpC.Capabilities()
			_ = qmpC.Stop()
			if st, errS := qmpC.QueryStatus(); errS == nil {
				estadoQMP = st.Status
				estado = st.Status
			} else {
				estadoQMP = "unknown"
				estado = "error"
			}
			_ = qmpC.Close()
		} else {
			estadoQMP = "error"
			estado = "error"
		}

		// Probe hostfwd on failure path as well (real measurement, not fabricated false)
		hostfwdTCP, _ := SondaHostfwd(xpraPort, 5, 50*time.Millisecond)

		failInfo := &MicroVMInfo{
			Slot:          slot,
			Clase:         "microvm",
			Contenedor:    cgroupName,
			Estado:        estado,
			EstadoQMP:     estadoQMP,
			AgenteControl: agenteControl,
			HostfwdTCP:    hostfwdTCP,
			PuertoXpra:    xpraPort,
			PID:           pid,
			VCPUs:         vcpus,
			MemMiB:        memMiB,
			Rundir:        rundir,
		}

		meta := Metadata{
			Slot:       slot,
			Clase:      "microvm",
			Estado:     estado,
			EstadoQMP:  estadoQMP,
			PID:        pid,
			Pidfd:      pidfd,
			VCPUs:      vcpus,
			MemMiB:     memMiB,
			Rundir:     rundir,
			CgroupName: cgroupName,
			CgroupPath: cg.Path,
			PuertoXpra: xpraPort,
		}
		if metaBytes, errM := json.MarshalIndent(meta, "", "  "); errM == nil {
			_ = os.WriteFile(filepath.Join(rundir, "metadata.json"), metaBytes, 0600)
			_ = os.WriteFile(filepath.Join(rundir, "pid"), []byte(strconv.Itoa(pid)+"\n"), 0600)
			_ = os.MkdirAll("/tmp/srf-capsula", 0755)
			_ = os.WriteFile(filepath.Join("/tmp/srf-capsula", fmt.Sprintf("slot-%d.json", slot)), metaBytes, 0644)
		}

		if opts.Informe != "" {
			_ = os.MkdirAll(filepath.Dir(opts.Informe), 0755)
			if reportBytes, errR := json.MarshalIndent(failInfo, "", "  "); errR == nil {
				_ = os.WriteFile(opts.Informe, reportBytes, 0644)
			}
		}

		return failInfo, agenteErr
	}

	// 10. Verify hostfwd rule is installed by SLIRP on host loopback port.
	// NOTE (INVESTIGACION opus-229): A successful TCP connect to a hostfwd port proves
	// that the hostfwd rule is installed by SLIRP on the host loopback, NOT that a guest
	// service answered (SLIRP performs host-side bind/listen during rule installation).
	hostfwdTCP, lastDialErr := SondaHostfwd(xpraPort, 50, 200*time.Millisecond)
	if !hostfwdTCP {
		writeAbortReport("error", agenteControl, estadoQMP, false)
		_ = unix.Kill(pid, unix.SIGKILL)
		_ = cg.Destroy()
		return nil, fmt.Errorf("iniciar microvm: hostfwd listener dial failed on port %d after 50 attempts: %w", xpraPort, lastDialErr)
	}

	// 11. Record metadata
	meta := Metadata{
		Slot:       slot,
		Clase:      "microvm",
		Estado:     estado,
		EstadoQMP:  estadoQMP,
		PID:        pid,
		Pidfd:      pidfd,
		VCPUs:      vcpus,
		MemMiB:     memMiB,
		Rundir:     rundir,
		CgroupName: cgroupName,
		CgroupPath: cg.Path,
		PuertoXpra: xpraPort,
	}
	metaBytes, err := json.MarshalIndent(meta, "", "  ")
	if err != nil {
		writeAbortReport("error", agenteControl, estadoQMP, hostfwdTCP)
		_ = unix.Kill(pid, unix.SIGKILL)
		_ = cg.Destroy()
		return nil, fmt.Errorf("iniciar microvm: marshal metadata: %w", err)
	}
	if err := os.WriteFile(filepath.Join(rundir, "metadata.json"), metaBytes, 0600); err != nil {
		writeAbortReport("error", agenteControl, estadoQMP, hostfwdTCP)
		_ = unix.Kill(pid, unix.SIGKILL)
		_ = cg.Destroy()
		return nil, fmt.Errorf("iniciar microvm: write metadata.json: %w", err)
	}
	if err := os.WriteFile(filepath.Join(rundir, "pid"), []byte(strconv.Itoa(pid)+"\n"), 0600); err != nil {
		writeAbortReport("error", agenteControl, estadoQMP, hostfwdTCP)
		_ = unix.Kill(pid, unix.SIGKILL)
		_ = cg.Destroy()
		return nil, fmt.Errorf("iniciar microvm: write pid file: %w", err)
	}

	// Save global runtime slot metadata
	if err := os.MkdirAll("/tmp/srf-capsula", 0755); err != nil {
		writeAbortReport("error", agenteControl, estadoQMP, hostfwdTCP)
		_ = unix.Kill(pid, unix.SIGKILL)
		_ = cg.Destroy()
		return nil, fmt.Errorf("iniciar microvm: mkdir /tmp/srf-capsula: %w", err)
	}
	if err := os.WriteFile(filepath.Join("/tmp/srf-capsula", fmt.Sprintf("slot-%d.json", slot)), metaBytes, 0644); err != nil {
		writeAbortReport("error", agenteControl, estadoQMP, hostfwdTCP)
		_ = unix.Kill(pid, unix.SIGKILL)
		_ = cg.Destroy()
		return nil, fmt.Errorf("iniciar microvm: write slot metadata in /tmp/srf-capsula: %w", err)
	}

	info := &MicroVMInfo{
		Slot:          slot,
		Clase:         "microvm",
		Contenedor:    cgroupName,
		Estado:        estado,
		EstadoQMP:     estadoQMP,
		AgenteControl: agenteControl,
		HostfwdTCP:    hostfwdTCP,
		PuertoXpra:    xpraPort,
		PID:           pid,
		VCPUs:         vcpus,
		MemMiB:        memMiB,
		Rundir:        rundir,
	}

	if opts.Informe != "" {
		_ = os.MkdirAll(filepath.Dir(opts.Informe), 0755)
		if reportBytes, err := json.MarshalIndent(info, "", "  "); err == nil {
			_ = os.WriteFile(opts.Informe, reportBytes, 0644)
		}
	}

	return info, nil
}

// Detener executes a deterministic 7-step teardown of the MicroVM capsule.
func Detener(ctx context.Context, slot uint16) (*DerriboInfo, error) {
	rundir := GetRundir(slot)
	cgroupName := fmt.Sprintf("srf-ng-%d", slot)

	var pid int
	var pidfd int
	var meta Metadata

	// Read metadata if available
	metaData, err := os.ReadFile(filepath.Join(rundir, "metadata.json"))
	if err == nil {
		if err := json.Unmarshal(metaData, &meta); err == nil {
			pid = meta.PID
			pidfd = meta.Pidfd
		}
	}
	if pid == 0 {
		pidBytes, err := os.ReadFile(filepath.Join(rundir, "pid"))
		if err == nil {
			pid, _ = strconv.Atoi(strings.TrimSpace(string(pidBytes)))
		}
	}

	// 1. Send QMP system_powerdown and quit if socket exists
	qmpSock := filepath.Join(rundir, "qmp.sock")
	if _, err := os.Stat(qmpSock); err == nil {
		if qmpClient, err := DialQMP(qmpSock, 500*time.Millisecond); err == nil {
			_ = qmpClient.Capabilities()
			_ = qmpClient.Quit()
			_ = qmpClient.Close()
		}
	}

	// 2. Open pidfd if not already held, signal process
	if pid > 0 {
		if pidfd <= 0 {
			pidfd, _ = unix.PidfdOpen(pid, 0)
		}
		if pidfd > 0 {
			_ = unix.PidfdSendSignal(pidfd, unix.SIGTERM, nil, 0)
		} else {
			_ = unix.Kill(pid, unix.SIGTERM)
		}
	}

	// 3. Signal cgroup v2 subtree via cgroup.kill
	var cgPath string
	if meta.CgroupPath != "" {
		cgPath = meta.CgroupPath
	} else if parent, err := cgroup.FindSiblingParent(); err == nil {
		cgPath = filepath.Join(parent, cgroupName)
	}

	// 4. Poll cgroup.events until populated == 0
	populated := -1
	if cgPath != "" {
		if _, err := os.Stat(cgPath); err == nil {
			killFile := filepath.Join(cgPath, "cgroup.kill")
			_ = os.WriteFile(killFile, []byte("1\n"), 0644)

			eventsFile := filepath.Join(cgPath, "cgroup.events")
			deadline := time.Now().Add(3 * time.Second)
			for {
				data, err := os.ReadFile(eventsFile)
				if err == nil {
					for _, line := range strings.Split(string(data), "\n") {
						fields := strings.Fields(line)
						if len(fields) == 2 && fields[0] == "populated" {
							if fields[1] == "0" {
								populated = 0
								goto CgroupEmpty
							} else if fields[1] == "1" {
								populated = 1
							}
						}
					}
				}
				if time.Now().After(deadline) {
					break
				}
				time.Sleep(20 * time.Millisecond)
			}
		CgroupEmpty:
		}
	}

	// 5. Reap process with waitid on pidfd or waitpid
	if pidfd > 0 {
		var siginfo siginfoKernel
		_, _, _ = unix.Syscall6(
			unix.SYS_WAITID,
			uintptr(unix.P_PIDFD),
			uintptr(pidfd),
			uintptr(unsafe.Pointer(&siginfo)),
			uintptr(unix.WEXITED|unix.WNOHANG),
			0,
			0,
		)
		_ = unix.Close(pidfd)
	} else if pid > 0 {
		var ws unix.WaitStatus
		_, _ = unix.Wait4(pid, &ws, unix.WNOHANG, nil)
	}

	// 6. Destroy sibling cgroup directory
	if cgPath != "" {
		_ = unix.Rmdir(cgPath)
	}

	// 7. Remove rundir completely and verify removal
	_ = os.RemoveAll(rundir)
	rundirEliminado := false
	if _, err := os.Stat(rundir); os.IsNotExist(err) {
		rundirEliminado = true
	}

	// Remove runtime slot files
	_ = os.Remove(filepath.Join("/tmp/srf-capsula", fmt.Sprintf("slot-%d.json", slot)))

	// Release slot lock
	activeLocksMu.Lock()
	if lock, ok := activeLocks[slot]; ok && lock != nil {
		_ = lock.Release()
		delete(activeLocks, slot)
	}
	activeLocksMu.Unlock()

	rc := 0
	if populated != 0 || !rundirEliminado {
		rc = 1
	}

	info := &DerriboInfo{
		Slot:            slot,
		Clase:           "microvm",
		Estado:          "detenido",
		RC:              rc,
		Populated:       populated,
		RundirEliminado: rundirEliminado,
	}

	if rc != 0 {
		return info, fmt.Errorf("detener microvm slot %d: teardown incomplete (populated=%d, rundir_eliminado=%v)", slot, populated, rundirEliminado)
	}
	return info, nil
}

// CargarMetadata reads runtime metadata for a given slot.
func CargarMetadata(slot uint16) (*Metadata, error) {
	rundir := GetRundir(slot)
	metaPath := filepath.Join(rundir, "metadata.json")
	data, err := os.ReadFile(metaPath)
	if err != nil {
		// Fallback to /tmp/srf-capsula
		altPath := filepath.Join("/tmp/srf-capsula", fmt.Sprintf("slot-%d.json", slot))
		data, err = os.ReadFile(altPath)
		if err != nil {
			return nil, fmt.Errorf("microvm: metadata for slot %d not found: %w", slot, err)
		}
	}

	var meta Metadata
	if err := json.Unmarshal(data, &meta); err != nil {
		return nil, fmt.Errorf("microvm: unmarshal metadata for slot %d: %w", slot, err)
	}
	return &meta, nil
}

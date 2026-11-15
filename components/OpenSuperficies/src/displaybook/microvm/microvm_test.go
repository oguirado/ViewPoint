package microvm

import (
	"bufio"
	"context"
	"encoding/json"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

func TestQMPRecordedMock(t *testing.T) {
	fixturePath := filepath.Join("..", "..", "..", "fixture", "ng", "microvm", "qmp-grabado.jsonl")
	data, err := os.ReadFile(fixturePath)
	if err != nil {
		t.Fatalf("failed to read fixture %s: %v", fixturePath, err)
	}

	lines := strings.Split(strings.TrimSpace(string(data)), "\n")
	if len(lines) < 6 {
		t.Fatalf("expected at least 6 lines in recorded QMP fixture, got %d", len(lines))
	}

	tmpDir := t.TempDir()
	sockPath := filepath.Join(tmpDir, "qmp.sock")

	listener, err := net.Listen("unix", sockPath)
	if err != nil {
		t.Fatalf("failed to listen on mock qmp socket: %v", err)
	}
	defer listener.Close()

	serverDone := make(chan struct{})
	go func() {
		defer close(serverDone)
		conn, err := listener.Accept()
		if err != nil {
			return
		}
		defer conn.Close()

		// Send greeting (first line of fixture)
		_, _ = conn.Write([]byte(lines[0] + "\r\n"))

		reader := bufio.NewReader(conn)
		for {
			cmdLine, err := reader.ReadString('\n')
			if err != nil {
				return
			}
			cmdLine = strings.TrimSpace(cmdLine)
			if cmdLine == "" {
				continue
			}

			var req map[string]interface{}
			if err := json.Unmarshal([]byte(cmdLine), &req); err != nil {
				continue
			}

			execCmd, _ := req["execute"].(string)
			switch execCmd {
			case "qmp_capabilities":
				// lines[2]
				_, _ = conn.Write([]byte(lines[2] + "\r\n"))
			case "query-chardev":
				// lines[4]
				_, _ = conn.Write([]byte(lines[4] + "\r\n"))
			case "query-cpus-fast":
				// lines[6]
				_, _ = conn.Write([]byte(lines[6] + "\r\n"))
			case "cont":
				// lines[8]
				_, _ = conn.Write([]byte(lines[8] + "\r\n"))
			case "quit":
				// lines[10]
				_, _ = conn.Write([]byte(lines[10] + "\r\n"))
				return
			default:
				_, _ = conn.Write([]byte(`{"return":{}}` + "\r\n"))
			}
		}
	}()

	client, err := DialQMP(sockPath, 3*time.Second)
	if err != nil {
		t.Fatalf("DialQMP failed: %v", err)
	}
	defer client.Close()

	if err := client.Capabilities(); err != nil {
		t.Fatalf("Capabilities failed: %v", err)
	}

	chardevs, err := client.QueryChardev()
	if err != nil {
		t.Fatalf("QueryChardev failed: %v", err)
	}
	if len(chardevs) != 2 || chardevs[0].ID != "control" || chardevs[1].ID != "boot_console" {
		t.Fatalf("unexpected chardevs parsed from fixture: %+v", chardevs)
	}

	cpus, err := client.QueryCPUsFast()
	if err != nil {
		t.Fatalf("QueryCPUsFast failed: %v", err)
	}
	if len(cpus) != 4 || cpus[0].CPUIndex != 0 || cpus[0].ThreadID != 12345 || cpus[3].ThreadID != 12348 {
		t.Fatalf("unexpected cpus parsed from fixture: %+v", cpus)
	}

	if err := client.Cont(); err != nil {
		t.Fatalf("Cont failed: %v", err)
	}

	if err := client.Quit(); err != nil {
		t.Fatalf("Quit failed: %v", err)
	}

	select {
	case <-serverDone:
	case <-time.After(2 * time.Second):
		t.Errorf("server did not shut down cleanly")
	}
}

func TestDiscoverX925CPUs(t *testing.T) {
	// 1. Live scan on GB10 host
	cpus := DiscoverX925CPUs()
	if len(cpus) == 0 {
		t.Fatalf("expected at least one Cortex-X925 core on GB10, got 0")
	}

	t.Logf("discovered %d Cortex-X925 cores: %v", len(cpus), cpus)
	if len(cpus) != 10 {
		t.Fatalf("expected 10 X925 cores on GB10, got %d: %v", len(cpus), cpus)
	}
	for _, cpu := range cpus {
		if (cpu >= 5 && cpu <= 9) || (cpu >= 15 && cpu <= 19) {
			continue
		}
		t.Errorf("unexpected core %d outside X925 clusters (5-9, 15-19)", cpu)
	}

	// 2. Synthetic sysfs test: discriminates against constant fallback
	tmpSysfs := t.TempDir()
	cpu2Dir := filepath.Join(tmpSysfs, "cpu2", "regs", "identification")
	if err := os.MkdirAll(cpu2Dir, 0755); err != nil {
		t.Fatalf("mkdir cpu2: %v", err)
	}
	if err := os.WriteFile(filepath.Join(cpu2Dir, "midr_el1"), []byte("0x00000000410fd851\n"), 0644); err != nil {
		t.Fatalf("write cpu2 midr: %v", err)
	}

	cpu3Dir := filepath.Join(tmpSysfs, "cpu3", "regs", "identification")
	if err := os.MkdirAll(cpu3Dir, 0755); err != nil {
		t.Fatalf("mkdir cpu3: %v", err)
	}
	if err := os.WriteFile(filepath.Join(cpu3Dir, "midr_el1"), []byte("0x00000000410fd871\n"), 0644); err != nil {
		t.Fatalf("write cpu3 midr: %v", err)
	}

	synthCPUs := DiscoverX925CPUsFromSysfs(tmpSysfs)
	if len(synthCPUs) != 1 || synthCPUs[0] != 2 {
		t.Fatalf("synthetic sysfs scan expected [2], got %v", synthCPUs)
	}
}

func TestControlSocketWait(t *testing.T) {
	tmpDir := t.TempDir()
	sockPath := filepath.Join(tmpDir, "ctrl.sock")

	l, err := net.Listen("unix", sockPath)
	if err != nil {
		t.Fatalf("failed to listen on mock control socket: %v", err)
	}
	defer l.Close()

	go func() {
		conn, err := l.Accept()
		if err != nil {
			return
		}
		defer conn.Close()
		_, _ = conn.Write([]byte("SRF1 PID1\n"))
	}()

	client, err := DialControl(sockPath, 2*time.Second)
	if err != nil {
		t.Fatalf("DialControl failed: %v", err)
	}
	defer client.Close()

	err = client.WaitForPID1(2 * time.Second)
	if err != nil {
		t.Fatalf("WaitForPID1 failed: %v", err)
	}
}

func TestGateVA_MutantCheck(t *testing.T) {
	argsStandard := []string{
		"qemu-system-aarch64",
		"-netdev",
		"user,id=net0,ipv4=on,ipv6=off,restrict=on,hostfwd=tcp:127.0.0.1:10023-:14500",
	}
	resStandard := EvalGateVA(argsStandard, 3)
	if !resStandard.Verde {
		t.Errorf("expected standard restrict=on cmdline to be green, got: %s", resStandard.Detalle)
	}

	argsMutant := []string{
		"qemu-system-aarch64",
		"-netdev",
		"user,id=net0,ipv4=on,ipv6=off,restrict=off,hostfwd=tcp:127.0.0.1:10023-:14500",
	}
	resMutant := EvalGateVA(argsMutant, 3)
	if resMutant.Verde {
		t.Errorf("expected mutant restrict=off cmdline to be red, but was green")
	}
}

func TestQMPQueryStatus(t *testing.T) {
	tmpDir := t.TempDir()
	sockPath := filepath.Join(tmpDir, "qmp.sock")

	listener, err := net.Listen("unix", sockPath)
	if err != nil {
		t.Fatalf("failed to listen on mock qmp socket: %v", err)
	}
	defer listener.Close()

	go func() {
		conn, err := listener.Accept()
		if err != nil {
			return
		}
		defer conn.Close()

		_, _ = conn.Write([]byte(`{"QMP": {"version": {"qemu": {"micro": 2, "minor": 2, "major": 8}}, "capabilities": []}}` + "\r\n"))
		reader := bufio.NewReader(conn)
		isPaused := false
		for {
			line, err := reader.ReadString('\n')
			if err != nil {
				return
			}
			var req map[string]interface{}
			if err := json.Unmarshal([]byte(line), &req); err != nil {
				continue
			}
			cmd, _ := req["execute"].(string)
			switch cmd {
			case "qmp_capabilities":
				_, _ = conn.Write([]byte(`{"return":{}}` + "\r\n"))
			case "query-status":
				if isPaused {
					_, _ = conn.Write([]byte(`{"return":{"running":false,"singlestep":false,"status":"paused"}}` + "\r\n"))
				} else {
					_, _ = conn.Write([]byte(`{"return":{"running":true,"singlestep":false,"status":"running"}}` + "\r\n"))
				}
			case "stop":
				isPaused = true
				_, _ = conn.Write([]byte(`{"return":{}}` + "\r\n"))
			default:
				_, _ = conn.Write([]byte(`{"return":{}}` + "\r\n"))
			}
		}
	}()

	client, err := DialQMP(sockPath, 2*time.Second)
	if err != nil {
		t.Fatalf("DialQMP failed: %v", err)
	}
	defer client.Close()

	if err := client.Capabilities(); err != nil {
		t.Fatalf("Capabilities failed: %v", err)
	}

	st, err := client.QueryStatus()
	if err != nil {
		t.Fatalf("QueryStatus failed: %v", err)
	}
	if !st.Running || st.Status != "running" {
		t.Errorf("expected running=true, status=running, got: %+v", st)
	}

	if err := client.Stop(); err != nil {
		t.Fatalf("Stop failed: %v", err)
	}

	stPaused, err := client.QueryStatus()
	if err != nil {
		t.Fatalf("QueryStatus after stop failed: %v", err)
	}
	if stPaused.Running || stPaused.Status != "paused" {
		t.Errorf("expected running=false, status=paused after stop, got: %+v", stPaused)
	}
}

func TestHostfwdTCPProbe(t *testing.T) {
	// 1. Success test: active TCP listener connects via production SondaHostfwd
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("failed to listen on mock TCP port: %v", err)
	}
	defer l.Close()

	_, portStr, err := net.SplitHostPort(l.Addr().String())
	if err != nil {
		t.Fatalf("split host port failed: %v", err)
	}
	port, _ := strconv.Atoi(portStr)

	ok, err := SondaHostfwd(uint16(port), 5, 100*time.Millisecond)
	if err != nil || !ok {
		t.Fatalf("expected successful connect via SondaHostfwd, got ok=%v, err=%v", ok, err)
	}

	// 2. Failure test: closed port returns error via production SondaHostfwd
	_ = l.Close() // Close listener so port is closed
	okClosed, errClosed := SondaHostfwd(uint16(port), 2, 50*time.Millisecond)
	if okClosed || errClosed == nil {
		t.Fatalf("expected connection failure on closed port via SondaHostfwd, got ok=%v, err=%v", okClosed, errClosed)
	}
}

func TestGateVG_Probes(t *testing.T) {
	tmpDir := t.TempDir()
	ctrlSock := filepath.Join(tmpDir, "control.sock")
	qmpSock := filepath.Join(tmpDir, "qmp.sock")

	// Create mock unix sockets
	lCtrl, err := net.Listen("unix", ctrlSock)
	if err != nil {
		t.Fatalf("failed to create ctrl socket: %v", err)
	}
	defer lCtrl.Close()

	lQmp, err := net.Listen("unix", qmpSock)
	if err != nil {
		t.Fatalf("failed to create qmp socket: %v", err)
	}
	defer lQmp.Close()

	// Bind ephemeral loopback TCP port to avoid slot collision, derive matching slot
	lTCP, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("cannot bind loopback TCP in test: %v", err)
	}
	defer lTCP.Close()

	tcpPort := lTCP.Addr().(*net.TCPAddr).Port
	slot := uint16(tcpPort - 10020)

	res := evalGateVG(slot, tmpDir)
	if !res.Verde {
		t.Fatalf("expected Gate V-G to be green with valid listeners, got: %s", res.Detalle)
	}
	if !strings.Contains(res.Detalle, "probed listeners verified") {
		t.Errorf("expected probed listeners verified in detail, got: %s", res.Detalle)
	}
}

func TestGateVG_FailsWhenMissing(t *testing.T) {
	tmpDir := t.TempDir()
	// No sockets or TCP listener created
	res := evalGateVG(99, tmpDir)
	if res.Verde {
		t.Fatalf("expected Gate V-G to be false when listeners absent, but was green")
	}
	if !strings.Contains(res.Detalle, "listener probe failures") {
		t.Errorf("expected listener probe failures in detail, got: %s", res.Detalle)
	}
}

func TestDetenerDerriboInfo(t *testing.T) {
	// 1. Call Detener on an uninitialized slot to verify dynamic teardown error and rc 1
	res, err := Detener(context.Background(), 999)
	if err == nil {
		t.Fatalf("expected error stopping uninitialized slot 999, got nil")
	}
	if res == nil || res.RC != 1 {
		t.Fatalf("expected rc 1 on uninitialized slot teardown, got %+v", res)
	}
	if res.Populated == 0 {
		t.Fatalf("expected non-zero populated on uninitialized slot teardown, got 0")
	}
}

func TestControlSocketWaitForPID1Failure(t *testing.T) {
	tmpDir := t.TempDir()
	sockPath := filepath.Join(tmpDir, "ctrl.sock")

	l, err := net.Listen("unix", sockPath)
	if err != nil {
		t.Fatalf("failed to listen on mock control socket: %v", err)
	}
	defer l.Close()

	client, err := DialControl(sockPath, 2*time.Second)
	if err != nil {
		t.Fatalf("DialControl failed: %v", err)
	}
	defer client.Close()

	// Timeout expected when PID1 message is never sent
	err = client.WaitForPID1(50 * time.Millisecond)
	if err == nil {
		t.Fatalf("expected error waiting for PID1 with no message sent, got nil")
	}
}

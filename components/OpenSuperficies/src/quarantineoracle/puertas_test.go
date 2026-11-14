package quarantineoracle

import (
	"bytes"
	"context"
	"encoding/binary"
	"fmt"
	"net/http"
	"strings"
	"testing"
)

type mockContainerEngine struct {
	inspectFunc func(ctx context.Context, nameOrID string) (*ContainerInspect, error)
	execFunc    func(ctx context.Context, nameOrID string, cmd []string) (int, string, error)
}

func (m *mockContainerEngine) Inspect(ctx context.Context, nameOrID string) (*ContainerInspect, error) {
	if m.inspectFunc != nil {
		return m.inspectFunc(ctx, nameOrID)
	}
	return nil, nil
}

func (m *mockContainerEngine) Exec(ctx context.Context, nameOrID string, cmd []string) (int, string, error) {
	if m.execFunc != nil {
		return m.execFunc(ctx, nameOrID, cmd)
	}
	return 127, "", nil
}

type mockHTTPProber struct {
	probeFunc func(ctx context.Context, url string) (int, error)
}

func (m *mockHTTPProber) Probe(ctx context.Context, url string) (int, error) {
	if m.probeFunc != nil {
		return m.probeFunc(ctx, url)
	}
	return http.StatusOK, nil
}

func getBaseCleanInspect() *ContainerInspect {
	return &ContainerInspect{
		Id:   "test-container-id",
		Name: "/test-container",
		State: ContainerState{
			Status:   "running",
			Running:  true,
			Pid:      12345,
			ExitCode: 0,
		},
		HostConfig: HostConfig{
			ShmSize:     2147483648, // 2GB
			CapDrop:     []string{"ALL"},
			SecurityOpt: []string{"no-new-privileges"},
		},
		Mounts: []ContainerMount{
			{
				Type:        "volume",
				Source:      "/var/lib/docker/volumes/test/_data",
				Destination: "/data/profile",
			},
			{
				Type:        "tmpfs",
				Source:      "",
				Destination: "/tmp",
			},
		},
		Config: ContainerConfig{
			Env: []string{
				"DBUS_SESSION_BUS_ADDRESS=disabled:",
				"DBUS_SYSTEM_BUS_ADDRESS=disabled:",
				"XDG_RUNTIME_DIR=/run/newton-tenant",
				"XDG_SESSION_TYPE=x11",
				"GTK_USE_PORTAL=0",
				"GDK_BACKEND=x11",
				"QT_QPA_PLATFORM=xcb",
				"NO_AT_BRIDGE=1",
				"GIO_USE_VFS=local",
				"DISPLAY=:100",
				"LIBGL_ALWAYS_SOFTWARE=1",
				"GALLIUM_DRIVER=llvmpipe",
			},
		},
		NetworkSettings: ContainerNetworkSettings{
			Networks: map[string]ContainerNetwork{
				"newton-srf-probe": {
					IPAddress: "172.19.0.2",
					NetworkID: "net-test-id",
				},
			},
			Ports: map[string][]PortBinding{
				"10000/tcp": {
					{HostIp: "127.0.0.1", HostPort: "10099"},
				},
				"9222/tcp": {
					{HostIp: "127.0.0.1", HostPort: "9299"},
				},
			},
		},
	}
}

func TestPuertasCleanBrowser(t *testing.T) {
	mockEng := &mockContainerEngine{
		inspectFunc: func(ctx context.Context, nameOrID string) (*ContainerInspect, error) {
			return getBaseCleanInspect(), nil
		},
		execFunc: func(ctx context.Context, nameOrID string, cmd []string) (int, string, error) {
			cmdStr := strings.Join(cmd, " ")
			if strings.Contains(cmdStr, "DISPLAY=:1") {
				return 1, "xdpyinfo: unable to open display \":1\".", nil
			}
			if strings.Contains(cmdStr, "[ -e /tmp/.X11-unix ]") {
				return 0, "__AUSENTE__", nil
			}
			if strings.Contains(cmdStr, "xdpyinfo") {
				return 0, "name of display: :100\n", nil
			}
			if strings.Contains(cmdStr, "glxinfo") {
				return 0, "OpenGL renderer string: llvmpipe (LLVM 15.0.6)\n", nil
			}
			if strings.Contains(cmdStr, "/proc/net/unix") {
				return 0, "Num RefCount Protocol Flags Type St Inode Path\n00: 00 00 00 01 01 1234 @/tmp/.X11-unix/X100\n", nil
			}
			return 127, "", nil
		},
	}
	mockProb := &mockHTTPProber{
		probeFunc: func(ctx context.Context, url string) (int, error) {
			return http.StatusOK, nil
		},
	}

	oracle := NewOracle(mockEng, mockProb)
	res, err := oracle.Evaluar(context.Background(), "srf-probe", "browser")
	if err != nil {
		t.Fatalf("Evaluar returned unexpected error: %v", err)
	}

	if len(res.Puertas) != 14 {
		t.Fatalf("expected 14 puertas, got %d", len(res.Puertas))
	}

	if !res.Verde {
		t.Errorf("expected overall capsule verde=true, got false")
		for _, p := range res.Puertas {
			if p.Tipo == "veredicto" && !p.Verde {
				t.Errorf("puerta %s (%s) falló: %s", p.Puerta, p.Nombre, p.Detalle)
			}
		}
	}
}

func TestPuertasPoisonMount(t *testing.T) {
	insp := getBaseCleanInspect()
	// Añadir veneno de montaje X11
	insp.Mounts = append(insp.Mounts, ContainerMount{
		Type:        "bind",
		Source:      "/tmp/.X11-unix",
		Destination: "/tmp/.X11-unix",
	})

	mockEng := &mockContainerEngine{
		inspectFunc: func(ctx context.Context, nameOrID string) (*ContainerInspect, error) {
			return insp, nil
		},
	}
	oracle := NewOracle(mockEng, &mockHTTPProber{})
	res, err := oracle.Evaluar(context.Background(), "srf-probe-poison", "browser")
	if err != nil {
		t.Fatalf("Evaluar error: %v", err)
	}

	if res.Verde {
		t.Errorf("expected poisoned container to have verde=false")
	}

	// Puerta A debe estar en rojo
	var puertaA *PuertaResultado
	for i := range res.Puertas {
		if res.Puertas[i].Puerta == "A" {
			puertaA = &res.Puertas[i]
			break
		}
	}
	if puertaA == nil || puertaA.Verde {
		t.Errorf("expected Puerta A to fail on poison mount, got %+v", puertaA)
	}
}

func TestPuertasNotRunning(t *testing.T) {
	insp := getBaseCleanInspect()
	insp.State.Running = false
	insp.State.Status = "exited"

	mockEng := &mockContainerEngine{
		inspectFunc: func(ctx context.Context, nameOrID string) (*ContainerInspect, error) {
			return insp, nil
		},
	}
	oracle := NewOracle(mockEng, &mockHTTPProber{})
	res, err := oracle.Evaluar(context.Background(), "srf-stopped", "browser")
	if err != nil {
		t.Fatalf("Evaluar error: %v", err)
	}

	if res.Verde {
		t.Errorf("expected stopped container to have verde=false")
	}
}

func TestPuertasDesktopParameterized(t *testing.T) {
	insp := getBaseCleanInspect()
	insp.Config.Env = []string{
		"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/newton-tenant/bus",
		"DBUS_SYSTEM_BUS_ADDRESS=disabled:",
		"XDG_RUNTIME_DIR=/run/newton-tenant",
		"XDG_SESSION_TYPE=x11",
		"GTK_USE_PORTAL=0",
		"GDK_BACKEND=x11",
		"QT_QPA_PLATFORM=xcb",
		"NO_AT_BRIDGE=0",
		"GIO_USE_VFS=local",
		"DISPLAY=:100",
		"LIBGL_ALWAYS_SOFTWARE=1",
		"GALLIUM_DRIVER=llvmpipe",
	}

	mockEng := &mockContainerEngine{
		inspectFunc: func(ctx context.Context, nameOrID string) (*ContainerInspect, error) {
			return insp, nil
		},
		execFunc: func(ctx context.Context, nameOrID string, cmd []string) (int, string, error) {
			cmdStr := strings.Join(cmd, " ")
			if strings.Contains(cmdStr, "DISPLAY=:1") {
				return 1, "xdpyinfo: unable to open display \":1\".", nil
			}
			if strings.Contains(cmdStr, "[ -e /tmp/.X11-unix ]") {
				return 0, "__AUSENTE__", nil
			}
			if strings.Contains(cmdStr, "/proc/net/unix") {
				return 0, "Num RefCount Protocol Flags Type St Inode Path\n00: 00 00 00 01 01 1234 @/tmp/.X11-unix/X100\n", nil
			}
			return 0, "name of display: :100\nOpenGL renderer string: llvmpipe\n", nil
		},
	}
	mockProb := &mockHTTPProber{
		probeFunc: func(ctx context.Context, url string) (int, error) {
			return http.StatusOK, nil
		},
	}

	oracle := NewOracle(mockEng, mockProb)
	res, err := oracle.Evaluar(context.Background(), "srf-desktop", "desktop")
	if err != nil {
		t.Fatalf("Evaluar error: %v", err)
	}

	var puertaH *PuertaResultado
	for i := range res.Puertas {
		if res.Puertas[i].Puerta == "H" {
			puertaH = &res.Puertas[i]
			break
		}
	}
	if puertaH == nil || !puertaH.Verde {
		t.Errorf("expected Puerta H to be green for desktop parameterized env, got %+v", puertaH)
	}
}

func TestPuertasMissingEnv(t *testing.T) {
	insp := getBaseCleanInspect()
	// Eliminar una variable de cuarentena obligatoria
	insp.Config.Env = []string{
		"DISPLAY=:100",
	}

	mockEng := &mockContainerEngine{
		inspectFunc: func(ctx context.Context, nameOrID string) (*ContainerInspect, error) {
			return insp, nil
		},
	}
	oracle := NewOracle(mockEng, &mockHTTPProber{})
	res, err := oracle.Evaluar(context.Background(), "srf-missing-env", "browser")
	if err != nil {
		t.Fatalf("Evaluar error: %v", err)
	}

	var puertaH *PuertaResultado
	for i := range res.Puertas {
		if res.Puertas[i].Puerta == "H" {
			puertaH = &res.Puertas[i]
			break
		}
	}
	if puertaH == nil || puertaH.Verde {
		t.Errorf("expected Puerta H to fail with missing env vars, got %+v", puertaH)
	}
}

func TestPuertasLowShm(t *testing.T) {
	insp := getBaseCleanInspect()
	insp.HostConfig.ShmSize = 67108864 // 64MB (< 2GB)

	mockEng := &mockContainerEngine{
		inspectFunc: func(ctx context.Context, nameOrID string) (*ContainerInspect, error) {
			return insp, nil
		},
	}
	oracle := NewOracle(mockEng, &mockHTTPProber{})
	res, err := oracle.Evaluar(context.Background(), "srf-low-shm", "browser")
	if err != nil {
		t.Fatalf("Evaluar error: %v", err)
	}

	var puertaI *PuertaResultado
	for i := range res.Puertas {
		if res.Puertas[i].Puerta == "I" {
			puertaI = &res.Puertas[i]
			break
		}
	}
	if puertaI == nil || puertaI.Verde {
		t.Errorf("expected Puerta I to fail on low shm size, got %+v", puertaI)
	}
}

func TestPuertasDefaultBridge(t *testing.T) {
	insp := getBaseCleanInspect()
	insp.NetworkSettings.Networks = map[string]ContainerNetwork{
		"bridge": {
			IPAddress: "172.17.0.2",
			NetworkID: "default-bridge-id",
		},
	}

	mockEng := &mockContainerEngine{
		inspectFunc: func(ctx context.Context, nameOrID string) (*ContainerInspect, error) {
			return insp, nil
		},
	}
	oracle := NewOracle(mockEng, &mockHTTPProber{})
	res, err := oracle.Evaluar(context.Background(), "srf-bridge", "browser")
	if err != nil {
		t.Fatalf("Evaluar error: %v", err)
	}

	var puertaN *PuertaResultado
	for i := range res.Puertas {
		if res.Puertas[i].Puerta == "N" {
			puertaN = &res.Puertas[i]
			break
		}
	}
	if puertaN == nil || puertaN.Verde {
		t.Errorf("expected Puerta N to fail on default bridge, got %+v", puertaN)
	}
}

func TestDemuxDockerStream(t *testing.T) {
	var raw bytes.Buffer
	msg := "hello world\n"
	hdr := make([]byte, 8)
	hdr[0] = 1 // stdout
	binary.BigEndian.PutUint32(hdr[4:8], uint32(len(msg)))
	raw.Write(hdr)
	raw.WriteString(msg)

	demuxed := demuxDockerStream(raw.Bytes())
	if demuxed != msg {
		t.Errorf("expected %q, got %q", msg, demuxed)
	}
}

func TestProcStatusParser(t *testing.T) {
	sample := `Name:	chromium
State:	S (sleeping)
Pid:	1234
PPid:	1
TracerPid:	0
Uid:	10001	10001	10001	10001
Gid:	0	0	0	0
FDSize:	256
Groups:	0 
CapInh:	0000000000000000
CapPrm:	0000000000000000
CapEff:	0000000000000000
CapBnd:	0000000000000000
CapAmb:	0000000000000000
NoNewPrivs:	1
Seccomp:	2
Seccomp_filters:	1
Threads:	10
`
	ps, err := ParseStatus(strings.NewReader(sample))
	if err != nil {
		t.Fatalf("ParseStatus error: %v", err)
	}
	if ps.Name != "chromium" {
		t.Errorf("expected Name chromium, got %s", ps.Name)
	}
	if ps.NoNewPrivs != 1 {
		t.Errorf("expected NoNewPrivs 1, got %d", ps.NoNewPrivs)
	}
	if ps.Seccomp != 2 {
		t.Errorf("expected Seccomp 2, got %d", ps.Seccomp)
	}
	if ps.CapEff != 0 {
		t.Errorf("expected CapEff 0, got %x", ps.CapEff)
	}
}

func TestMountinfoParser(t *testing.T) {
	sample := `117 116 259:2 / / rw,relatime - ext4 /dev/nvme0n1p2 rw,errors=remount-ro
273 117 0:6 / /dev rw,nosuid,relatime - devtmpfs udev rw,size=63686288k,nr_inodes=15921572,mode=755,inode64
13522 273 0:31 / /dev/shm rw,nosuid,nodev - tmpfs tmpfs rw,inode64
`
	mounts, err := GetMountsFromReader(strings.NewReader(sample))
	if err != nil {
		t.Fatalf("GetMountsFromReader error: %v", err)
	}
	if len(mounts) != 3 {
		t.Fatalf("expected 3 mounts, got %d", len(mounts))
	}
	if mounts[0].MountPoint != "/" || mounts[0].FSType != "ext4" {
		t.Errorf("unexpected mount 0: %+v", mounts[0])
	}
	if mounts[2].MountPoint != "/dev/shm" || mounts[2].FSType != "tmpfs" {
		t.Errorf("unexpected mount 2: %+v", mounts[2])
	}

	if !HasMountWithSubstring(mounts, "shm") {
		t.Errorf("expected HasMountWithSubstring('shm') to be true")
	}

	leaks := FindSensitiveMountLeaks(mounts, []string{"/tmp/.X11-unix", "/run/user"})
	if len(leaks) != 0 {
		t.Errorf("expected 0 sensitive leaks, got %v", leaks)
	}
}

func TestPuertaA_A1RcZero(t *testing.T) {
	mockEng := &mockContainerEngine{
		inspectFunc: func(ctx context.Context, nameOrID string) (*ContainerInspect, error) {
			return getBaseCleanInspect(), nil
		},
		execFunc: func(ctx context.Context, nameOrID string, cmd []string) (int, string, error) {
			cmdStr := strings.Join(cmd, " ")
			if strings.Contains(cmdStr, "DISPLAY=:1") {
				return 0, "name of display: :1\n", nil
			}
			if strings.Contains(cmdStr, "[ -e /tmp/.X11-unix ]") {
				return 0, "__AUSENTE__", nil
			}
			return 0, "", nil
		},
	}
	oracle := NewOracle(mockEng, &mockHTTPProber{})
	res, err := oracle.Evaluar(context.Background(), "srf-a1-leak", "browser")
	if err != nil {
		t.Fatalf("Evaluar error: %v", err)
	}
	if res.Verde {
		t.Errorf("expected container to be red when A.1 has rc 0")
	}
	var pA *PuertaResultado
	for i := range res.Puertas {
		if res.Puertas[i].Puerta == "A" {
			pA = &res.Puertas[i]
			break
		}
	}
	if pA == nil || pA.Verde {
		t.Errorf("expected Puerta A to fail when A.1 returns rc 0, got: %+v", pA)
	}
}

func TestPuertaA_A2NonEmptyListing(t *testing.T) {
	mockEng := &mockContainerEngine{
		inspectFunc: func(ctx context.Context, nameOrID string) (*ContainerInspect, error) {
			return getBaseCleanInspect(), nil
		},
		execFunc: func(ctx context.Context, nameOrID string, cmd []string) (int, string, error) {
			cmdStr := strings.Join(cmd, " ")
			if strings.Contains(cmdStr, "DISPLAY=:1") {
				return 1, "xdpyinfo: unable to open display \":1\".", nil
			}
			if strings.Contains(cmdStr, "[ -e /tmp/.X11-unix ]") {
				return 0, "X0\nX1\n", nil
			}
			return 0, "", nil
		},
	}
	oracle := NewOracle(mockEng, &mockHTTPProber{})
	res, err := oracle.Evaluar(context.Background(), "srf-a2-leak", "browser")
	if err != nil {
		t.Fatalf("Evaluar error: %v", err)
	}
	if res.Verde {
		t.Errorf("expected container to be red when A.2 listing is non-empty")
	}
	var pA *PuertaResultado
	for i := range res.Puertas {
		if res.Puertas[i].Puerta == "A" {
			pA = &res.Puertas[i]
			break
		}
	}
	if pA == nil || pA.Verde {
		t.Errorf("expected Puerta A to fail when A.2 listing is non-empty, got: %+v", pA)
	}
}

func TestPuertaM_ExecError(t *testing.T) {
	mockEng := &mockContainerEngine{
		inspectFunc: func(ctx context.Context, nameOrID string) (*ContainerInspect, error) {
			return getBaseCleanInspect(), nil
		},
		execFunc: func(ctx context.Context, nameOrID string, cmd []string) (int, string, error) {
			cmdStr := strings.Join(cmd, " ")
			if strings.Contains(cmdStr, "DISPLAY=:1") {
				return 1, "xdpyinfo: unable to open display \":1\".", nil
			}
			if strings.Contains(cmdStr, "[ -e /tmp/.X11-unix ]") {
				return 0, "__AUSENTE__", nil
			}
			if strings.Contains(cmdStr, "xdpyinfo") {
				return 0, "name of display: :100\n", nil
			}
			if strings.Contains(cmdStr, "glxinfo") {
				return 0, "OpenGL renderer string: llvmpipe (LLVM 15.0.6)\n", nil
			}
			if strings.Contains(cmdStr, "/proc/net/unix") {
				return -1, "", fmt.Errorf("simulated exec error")
			}
			return 127, "", nil
		},
	}
	oracle := NewOracle(mockEng, &mockHTTPProber{})
	res, err := oracle.Evaluar(context.Background(), "srf-m-err", "browser")
	if err != nil {
		t.Fatalf("Evaluar error: %v", err)
	}
	if res.Verde {
		t.Errorf("expected container to be red when Puerta M exec errors")
	}
	var pM *PuertaResultado
	for i := range res.Puertas {
		if res.Puertas[i].Puerta == "M" {
			pM = &res.Puertas[i]
			break
		}
	}
	if pM == nil || pM.Verde {
		t.Errorf("expected Puerta M to fail when exec errors, got: %+v", pM)
	}
}

func TestDockerSocketClientExecError(t *testing.T) {
	client := NewDockerSocketClient("/nonexistent/docker.sock")
	rc, _, err := client.Exec(context.Background(), "nonexistent-ctr", []string{"ls"})
	if err == nil {
		t.Errorf("expected error connecting to nonexistent docker socket, got nil")
	}
	if rc != -1 {
		t.Errorf("expected rc -1 on exec error, got %d", rc)
	}
}

func TestPuertasInformativeBCD(t *testing.T) {
	mockEng := &mockContainerEngine{
		inspectFunc: func(ctx context.Context, nameOrID string) (*ContainerInspect, error) {
			return getBaseCleanInspect(), nil
		},
		execFunc: func(ctx context.Context, nameOrID string, cmd []string) (int, string, error) {
			cmdStr := strings.Join(cmd, " ")
			if strings.Contains(cmdStr, "DISPLAY=:1") {
				return 1, "xdpyinfo: unable to open display \":1\".", nil
			}
			if strings.Contains(cmdStr, "[ -e /tmp/.X11-unix ]") {
				return 0, "__AUSENTE__", nil
			}
			if strings.Contains(cmdStr, "xdpyinfo") {
				return 0, "name of display: :100\n", nil
			}
			if strings.Contains(cmdStr, "glxinfo") {
				return 0, "OpenGL renderer string: llvmpipe (LLVM 15.0.6)\n", nil
			}
			if strings.Contains(cmdStr, "/proc/net/unix") {
				return 0, "Num RefCount Protocol Flags Type St Inode Path\n", nil
			}
			return 0, "", nil
		},
	}
	oracle := NewOracle(mockEng, &mockHTTPProber{})
	res, err := oracle.Evaluar(context.Background(), "srf-test", "browser")
	if err != nil {
		t.Fatalf("Evaluar error: %v", err)
	}
	for _, letter := range []string{"B", "C", "D"} {
		var found *PuertaResultado
		for i := range res.Puertas {
			if res.Puertas[i].Puerta == letter {
				found = &res.Puertas[i]
				break
			}
		}
		if found == nil {
			t.Fatalf("Puerta %s not found in results", letter)
		}
		if found.Tipo != "informativa" {
			t.Errorf("Puerta %s tipo expected informativa, got %s", letter, found.Tipo)
		}
		if !found.Verde {
			t.Errorf("Puerta %s verde expected true, got false", letter)
		}
		if found.Detalle == "" {
			t.Errorf("Puerta %s detalle is empty", letter)
		}
	}
}


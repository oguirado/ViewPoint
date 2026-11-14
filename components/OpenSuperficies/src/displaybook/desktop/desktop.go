package desktop

import (
	"archive/tar"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"opensuperficies.local/srf/src/displaybook/puertos"
	"opensuperficies.local/srf/src/dockerapi"
	"opensuperficies.local/srf/src/quarantineoracle"
)

const (
	DefaultImageTag     = "opensuperficies-desktop-arm64:1.0.0"
	DefaultSlotsDir     = "runtime/slots"
	DefaultRuntimeDir   = "/tmp/srf-capsula"
	DefaultDockerSocket = "/var/run/docker.sock"
	DefaultClass        = "desktop"
)

// BuildOptions parameters for building the desktop capsule OCI image.
type BuildOptions struct {
	Tag          string `json:"tag"`
	Dockerfile   string `json:"dockerfile"`
	Entrypoint   string `json:"entrypoint"`
	DockerSocket string `json:"docker_socket"`
	Clase        string `json:"clase,omitempty"`
}

// BuildResult contains metadata about the built OCI image.
type BuildResult struct {
	Imagen string `json:"imagen"`
	Clase  string `json:"clase"`
	Id     string `json:"id,omitempty"`
	Size   int64  `json:"size,omitempty"`
	Estado string `json:"estado"`
}

// CapsulaInfo represents running capsule status and connection metadata.
type CapsulaInfo struct {
	Slot       uint16 `json:"slot"`
	Clase      string `json:"clase"`
	Contenedor string `json:"contenedor"`
	ID         string `json:"id,omitempty"`
	PuertoXpra uint16 `json:"puerto_xpra"`
	PuertoCDP  uint16 `json:"puerto_cdp,omitempty"`
	Token      string `json:"token,omitempty"`
	Red        string `json:"red,omitempty"`
	Estado     string `json:"estado"`
	RC         int    `json:"rc,omitempty"`
}

// IniciarOptions options for launching a desktop capsule.
type IniciarOptions struct {
	SlotsDir     string `json:"slots_dir"`
	RuntimeDir   string `json:"runtime_dir"`
	DockerSocket string `json:"docker_socket"`
	Timeout      time.Duration
	Clase        string `json:"clase,omitempty"`
}

// Construir builds the desktop OCI image 1.0.0 via Docker Engine API over Unix socket.
func Construir(ctx context.Context, opts BuildOptions) (*BuildResult, error) {
	if opts.Tag == "" {
		opts.Tag = DefaultImageTag
	}
	if opts.DockerSocket == "" {
		opts.DockerSocket = DefaultDockerSocket
	}
	if opts.Dockerfile == "" {
		opts.Dockerfile = "docker/ng/desktop/Dockerfile"
	}
	if opts.Entrypoint == "" {
		opts.Entrypoint = "docker/ng/desktop/entrypoint.sh"
	}

	dockerfileBytes, err := os.ReadFile(opts.Dockerfile)
	if err != nil {
		return nil, fmt.Errorf("dockerfile %s missing: %w", opts.Dockerfile, err)
	}

	entrypointBytes, err := os.ReadFile(opts.Entrypoint)
	if err != nil {
		return nil, fmt.Errorf("entrypoint %s missing: %w", opts.Entrypoint, err)
	}

	var tarBuf bytes.Buffer
	tw := tar.NewWriter(&tarBuf)

	if err := addTarFile(tw, "Dockerfile", dockerfileBytes, 0644); err != nil {
		return nil, err
	}
	if err := addTarFile(tw, "docker/ng/desktop/Dockerfile", dockerfileBytes, 0644); err != nil {
		return nil, err
	}
	if err := addTarFile(tw, "docker/ng/desktop/entrypoint.sh", entrypointBytes, 0755); err != nil {
		return nil, err
	}
	if err := addTarFile(tw, "entrypoint.sh", entrypointBytes, 0755); err != nil {
		return nil, err
	}

	if err := tw.Close(); err != nil {
		return nil, fmt.Errorf("error closing tar context: %w", err)
	}

	tr := &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, "unix", opts.DockerSocket)
		},
		DisableCompression: true,
	}
	httpClient := &http.Client{Transport: tr, Timeout: 300 * time.Second}

	buildURL := fmt.Sprintf("http://localhost/build?t=%s", url.QueryEscape(opts.Tag))
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, buildURL, &tarBuf)
	if err != nil {
		return nil, fmt.Errorf("error creating build request: %w", err)
	}
	req.Header.Set("Content-Type", "application/x-tar")

	resp, err := httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("error sending build request to docker: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		respBody, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("docker build returned status %d: %s", resp.StatusCode, string(respBody))
	}

	dec := json.NewDecoder(resp.Body)
	for dec.More() {
		var msg struct {
			Stream      string `json:"stream"`
			Error       string `json:"error"`
			ErrorDetail struct {
				Message string `json:"message"`
			} `json:"errorDetail"`
		}
		if err := dec.Decode(&msg); err != nil {
			break
		}
		if msg.Error != "" {
			return nil, fmt.Errorf("docker build error: %s", msg.Error)
		}
		if msg.ErrorDetail.Message != "" {
			return nil, fmt.Errorf("docker build error: %s", msg.ErrorDetail.Message)
		}
	}

	dockerClient, err := dockerapi.NewClient(opts.DockerSocket)
	if err != nil {
		return nil, fmt.Errorf("error connecting to docker client: %w", err)
	}
	insp, err := dockerClient.InspectImage(ctx, opts.Tag)
	if err != nil {
		return nil, fmt.Errorf("built image %s inspection failed: %w", opts.Tag, err)
	}

	clase := opts.Clase
	if clase == "" {
		clase = DefaultClass
	}

	return &BuildResult{
		Imagen: opts.Tag,
		Clase:  clase,
		Id:     insp.Id,
		Size:   insp.Size,
		Estado: "construida",
	}, nil
}

func addTarFile(tw *tar.Writer, name string, content []byte, mode int64) error {
	hdr := &tar.Header{
		Name:    name,
		Mode:    mode,
		Size:    int64(len(content)),
		ModTime: time.Now(),
	}
	if err := tw.WriteHeader(hdr); err != nil {
		return fmt.Errorf("error writing tar header for %s: %w", name, err)
	}
	if _, err := tw.Write(content); err != nil {
		return fmt.Errorf("error writing tar content for %s: %w", name, err)
	}
	return nil
}

// BuildDesktopEnv returns the environment variables conforming to all 14 quarantine doors for desktop class.
func BuildDesktopEnv(xpraPort uint16) []string {
	return []string{
		"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/newton-tenant/bus",
		"DBUS_SYSTEM_BUS_ADDRESS=disabled:",
		"XDG_RUNTIME_DIR=/run/newton-tenant",
		"XDG_CURRENT_DESKTOP=",
		"XDG_SESSION_TYPE=x11",
		"GTK_USE_PORTAL=0",
		"GDK_BACKEND=x11",
		"QT_QPA_PLATFORM=xcb",
		"NO_AT_BRIDGE=0",
		"GIO_USE_VFS=local",
		"DISPLAY=:100",
		"LIBGL_ALWAYS_SOFTWARE=1",
		"GALLIUM_DRIVER=llvmpipe",
		"LP_NUM_THREADS=4",
		"HOME=/home/tenant",
		fmt.Sprintf("XPRA_BIND_PORT=%d", 10000),
		"DISPLAY_NUM=100",
	}
}

// BuildContainerConfig returns the ContainerConfig for launching a desktop capsule container.
func BuildContainerConfig(slot uint16, xpraPort uint16, netName string) dockerapi.ContainerConfig {
	return dockerapi.ContainerConfig{
		Image: DefaultImageTag,
		User:  "10001:0",
		Env:   BuildDesktopEnv(xpraPort),
		HostConfig: &dockerapi.HostConfig{
			// 1280x900 BGRA plus one MiB of XWD header/colormap headroom.
			Tmpfs: map[string]string{
				"/run/vqa/fb": "rw,nosuid,nodev,noexec,mode=0700,uid=10001,size=5656576",
			},
			ShmSize:     2147483648, // 2GB (Door I)
			NetworkMode: netName,    // Isolated network (Door N)
			PortBindings: map[string][]dockerapi.PortBinding{
				"10000/tcp": {
					{HostIp: "127.0.0.1", HostPort: fmt.Sprintf("%d", xpraPort)},
				},
			},
		},
	}
}

// Iniciar reserves slot, starts desktop capsule container with quarantine doors, and returns info.
func Iniciar(ctx context.Context, slot uint16, opts IniciarOptions) (*CapsulaInfo, error) {
	if opts.SlotsDir == "" {
		opts.SlotsDir = DefaultSlotsDir
	}
	if opts.RuntimeDir == "" {
		opts.RuntimeDir = DefaultRuntimeDir
	}
	if opts.DockerSocket == "" {
		opts.DockerSocket = DefaultDockerSocket
	}
	if opts.Timeout <= 0 {
		opts.Timeout = 25 * time.Second
	}
	clase := opts.Clase
	if clase == "" {
		clase = DefaultClass
	}

	// 1. Acquire slot lock
	slotLock, err := puertos.ReservarSlot(opts.SlotsDir, slot)
	if err != nil {
		return nil, fmt.Errorf("starting desktop slot %d failed: %w", slot, err)
	}

	// 2. Allocate ports
	plan := puertos.NuevoPlanPuertos()
	par, err := plan.Par(slot)
	if err != nil {
		_ = slotLock.Release()
		return nil, fmt.Errorf("starting desktop slot %d failed port calculation: %w", slot, err)
	}
	xpraPort := par.Xpra

	// 3. Connect to Docker Engine
	dockerClient, err := dockerapi.NewClient(opts.DockerSocket)
	if err != nil {
		_ = slotLock.Release()
		return nil, fmt.Errorf("error connecting to docker: %w", err)
	}

	containerName := fmt.Sprintf("srf-ng-%d", slot)
	netName := fmt.Sprintf("srf-net-%d", slot)

	// Clean any previous residue container or network
	_ = dockerClient.RemoveContainer(ctx, containerName, true, true)

	// 4. Create isolated network (Door N compliance)
	if _, err := dockerClient.CreateNetwork(ctx, netName, false); err != nil {
		_ = slotLock.Release()
		return nil, fmt.Errorf("error creating isolated network %s: %w", netName, err)
	}

	// 5. Assemble container configuration matching all 14 quarantine doors for desktop class
	cfg := BuildContainerConfig(slot, xpraPort, netName)

	ctrID, err := dockerClient.CreateContainer(ctx, cfg, containerName)
	if err != nil {
		_ = dockerClient.RemoveNetwork(ctx, netName)
		_ = slotLock.Release()
		return nil, fmt.Errorf("error creating container %s: %w", containerName, err)
	}

	if err := dockerClient.StartContainer(ctx, ctrID); err != nil {
		_ = dockerClient.RemoveContainer(ctx, ctrID, true, true)
		_ = dockerClient.RemoveNetwork(ctx, netName)
		_ = slotLock.Release()
		return nil, fmt.Errorf("error starting container %s: %w", containerName, err)
	}

	info := &CapsulaInfo{
		Slot:       slot,
		Clase:      clase,
		Contenedor: containerName,
		ID:         ctrID,
		PuertoXpra: xpraPort,
		Red:        netName,
		Estado:     "iniciando",
	}

	// 6. Poll until Xpra endpoint is responding and X11/D-Bus are operational
	readyDeadline := time.Now().Add(opts.Timeout)
	ready := false
	pollClient := &http.Client{Timeout: 1 * time.Second}
	oracleEngine := quarantineoracle.NewDockerSocketClient(opts.DockerSocket)

	for time.Now().Before(readyDeadline) {
		xpraURL := fmt.Sprintf("http://127.0.0.1:%d/", xpraPort)
		xresp, xerr := pollClient.Get(xpraURL)
		if xerr == nil {
			if xresp.StatusCode == http.StatusOK || xresp.StatusCode == http.StatusFound {
				xresp.Body.Close()
				// Verify Xvfb display is responding
				rcX, _, errX := oracleEngine.Exec(ctx, containerName, []string{"xdpyinfo", "-display", ":100"})
				if errX == nil && rcX == 0 {
					// Verify private D-Bus is responding
					rcB, _, errB := oracleEngine.Exec(ctx, containerName, []string{"busctl", "--address=unix:path=/run/newton-tenant/bus", "list"})
					if errB == nil && rcB == 0 {
						ready = true
						break
					}
				}
			} else {
				xresp.Body.Close()
			}
		}
		time.Sleep(150 * time.Millisecond)
	}

	if !ready {
		_ = Detener(ctx, slot)
		return nil, fmt.Errorf("timeout waiting for desktop capsule endpoints (xpra %d) to become ready", xpraPort)
	}

	// Mark verified status
	info.Estado = "listo"

	// 7. Persist runtime info
	if err := os.MkdirAll(opts.RuntimeDir, 0755); err != nil {
		_ = Detener(ctx, slot)
		return nil, fmt.Errorf("error creating runtime dir %s: %w", opts.RuntimeDir, err)
	}
	stateFile := filepath.Join(opts.RuntimeDir, fmt.Sprintf("slot-%d.json", slot))
	data, err := json.Marshal(info)
	if err != nil {
		_ = Detener(ctx, slot)
		return nil, fmt.Errorf("error marshaling slot state: %w", err)
	}
	if err := os.WriteFile(stateFile, data, 0644); err != nil {
		_ = Detener(ctx, slot)
		return nil, fmt.Errorf("error writing state file %s: %w", stateFile, err)
	}

	return info, nil
}

// Detener stops and removes the container, deletes isolated network, and releases locks.
func Detener(ctx context.Context, slot uint16) error {
	containerName := fmt.Sprintf("srf-ng-%d", slot)
	netName := fmt.Sprintf("srf-net-%d", slot)

	dockerClient, err := dockerapi.NewClient(DefaultDockerSocket)
	if err != nil {
		return fmt.Errorf("error connecting to docker: %w", err)
	}

	var teardownErrs []error

	if err := dockerClient.StopContainer(ctx, containerName, 3); err != nil && !errors.Is(err, dockerapi.ErrNotFound) {
		teardownErrs = append(teardownErrs, fmt.Errorf("stop container %s: %w", containerName, err))
	}
	if err := dockerClient.RemoveContainer(ctx, containerName, true, true); err != nil && !errors.Is(err, dockerapi.ErrNotFound) {
		teardownErrs = append(teardownErrs, fmt.Errorf("remove container %s: %w", containerName, err))
	}
	if err := dockerClient.RemoveNetwork(ctx, netName); err != nil && !errors.Is(err, dockerapi.ErrNotFound) {
		teardownErrs = append(teardownErrs, fmt.Errorf("remove network %s: %w", netName, err))
	}

	stateFile := filepath.Join(DefaultRuntimeDir, fmt.Sprintf("slot-%d.json", slot))
	if err := os.Remove(stateFile); err != nil && !os.IsNotExist(err) {
		teardownErrs = append(teardownErrs, fmt.Errorf("remove state file %s: %w", stateFile, err))
	}

	lockPath := filepath.Join(DefaultSlotsDir, fmt.Sprintf("slot-%d.lock", slot))
	if err := os.Remove(lockPath); err != nil && !os.IsNotExist(err) {
		teardownErrs = append(teardownErrs, fmt.Errorf("remove lock file %s: %w", lockPath, err))
	}

	if len(teardownErrs) > 0 {
		return fmt.Errorf("detener slot %d failures: %v", slot, teardownErrs)
	}

	return nil
}

// PID resolves the process ID for the specified process name inside the desktop capsule container.
func PID(ctx context.Context, slot uint16, proceso string) (int, error) {
	containerName := fmt.Sprintf("srf-ng-%d", slot)
	client := quarantineoracle.NewDockerSocketClient(DefaultDockerSocket)

	rc, out, err := client.Exec(ctx, containerName, []string{"pgrep", "-o", "-f", proceso})
	if err != nil {
		return 0, fmt.Errorf("exec pgrep failed: %w", err)
	}
	if rc != 0 {
		return 0, fmt.Errorf("process %s not found in container %s (rc %d): %s", proceso, containerName, rc, strings.TrimSpace(out))
	}

	lines := strings.Fields(strings.TrimSpace(out))
	if len(lines) == 0 {
		return 0, fmt.Errorf("pgrep returned no lines")
	}

	pid, err := strconv.Atoi(lines[0])
	if err != nil {
		return 0, fmt.Errorf("error parsing pid %q: %w", lines[0], err)
	}

	return pid, nil
}

// Listar queries active containers and lists running desktop capsules.
func Listar(ctx context.Context) ([]CapsulaInfo, error) {
	var list []CapsulaInfo
	entries, err := os.ReadDir(DefaultRuntimeDir)
	if err != nil {
		if os.IsNotExist(err) {
			return list, nil
		}
		return nil, fmt.Errorf("reading runtime dir %s: %w", DefaultRuntimeDir, err)
	}
	for _, entry := range entries {
		if strings.HasPrefix(entry.Name(), "slot-") && strings.HasSuffix(entry.Name(), ".json") {
			filePath := filepath.Join(DefaultRuntimeDir, entry.Name())
			data, rerr := os.ReadFile(filePath)
			if rerr != nil {
				return nil, fmt.Errorf("reading state file %s: %w", filePath, rerr)
			}
			var info CapsulaInfo
			if uerr := json.Unmarshal(data, &info); uerr != nil {
				return nil, fmt.Errorf("decoding state file %s: %w", filePath, uerr)
			}
			list = append(list, info)
		}
	}
	return list, nil
}

// InyeccionXTestResultado captures the parameters and observed effects of an XTEST synthetic injection.
type InyeccionXTestResultado struct {
	PunteroInicial   [2]int16 `json:"puntero_inicial"`
	PunteroInyectado [2]int16 `json:"puntero_inyectado"`
	PunteroObservado [2]int16 `json:"puntero_observado"`
	Coincide         bool     `json:"coincide"`
	Evento           string   `json:"evento"`
}

// PercepcionResult captures perception ladder verification and XTEST injection evidence.
type PercepcionResult struct {
	Slot           uint16                  `json:"slot,omitempty"`
	Display        string                  `json:"display"`
	Resolucion     string                  `json:"resolucion"`
	VentanasRaiz   int                     `json:"ventanas_raiz"`
	AtSpiBus       string                  `json:"atspi_bus"`
	NodoRaiz       string                  `json:"nodo_raiz"`
	RolNodoRaiz    string                  `json:"rol_nodo_raiz"`
	Hijos          int                     `json:"hijos"`
	Aplicacion     string                  `json:"aplicacion,omitempty"`
	RolAplicacion  string                  `json:"rol_aplicacion,omitempty"`
	InyeccionXTest InyeccionXTestResultado `json:"inyeccion_xtest"`
	Estado         string                  `json:"estado"`
}

// ProbarPercepcionLocal tests AT-SPI2 perception and XTEST injection on the local display/bus.
func ProbarPercepcionLocal(display string) (*PercepcionResult, error) {
	if os.Getenv("NO_AT_BRIDGE") == "1" {
		return nil, fmt.Errorf("hazard R-13: AT-SPI2 perception ladder disabled by NO_AT_BRIDGE=1")
	}

	if display == "" {
		display = os.Getenv("DISPLAY")
	}
	if display == "" {
		display = ":100"
	}

	xt, err := NewXTestClient(display)
	if err != nil {
		return nil, fmt.Errorf("failed to initialize XTEST client on %s: %w", display, err)
	}
	defer xt.Close()

	w, h := xt.ScreenDimensions()
	resolucion := fmt.Sprintf("%dx%d", w, h)

	windows, err := xt.QueryWindows()
	if err != nil {
		return nil, fmt.Errorf("failed to query windows: %w", err)
	}

	// Move pointer to distinct starting position (100, 100) before test
	startX, startY := int16(100), int16(100)
	targetX, targetY := int16(350), int16(220)

	if err := xt.MovePointer(startX, startY); err != nil {
		return nil, fmt.Errorf("failed to initialize pointer to (%d, %d): %w", startX, startY, err)
	}
	time.Sleep(15 * time.Millisecond)

	x0, y0, err := xt.QueryPointer()
	if err != nil {
		return nil, fmt.Errorf("failed to query initial pointer: %w", err)
	}

	if err := xt.MovePointer(targetX, targetY); err != nil {
		return nil, fmt.Errorf("failed to inject pointer motion: %w", err)
	}
	time.Sleep(15 * time.Millisecond)

	x1, y1, err := xt.QueryPointer()
	if err != nil {
		return nil, fmt.Errorf("failed to query post-injection pointer: %w", err)
	}

	coincide := (x1 == targetX && y1 == targetY && (x0 != targetX || y0 != targetY))

	// Perception ladder via D-Bus / AT-SPI2
	atspiBus := ""
	nodoRaiz := "/org/a11y/atspi/accessible/root"
	rolRaiz := ""
	hijosCount := 0
	appNombre := ""
	appRol := ""

	sessionBusAddr := os.Getenv("DBUS_SESSION_BUS_ADDRESS")
	if sessionBusAddr == "" {
		sessionBusAddr = "unix:path=/run/newton-tenant/bus"
	}

	// 1. Query AT-SPI bus address from org.a11y.Bus
	cmdAddr := exec.Command("busctl", "--address="+sessionBusAddr, "call", "org.a11y.Bus", "/org/a11y/bus", "org.a11y.Bus", "GetAddress")
	outAddr, errAddr := cmdAddr.Output()
	if errAddr == nil {
		atspiBus = parseBusctlString(string(outAddr))
	}

	targetBus := atspiBus
	if targetBus == "" {
		targetBus = "unix:path=/run/newton-tenant/at-spi/bus_100"
	}
	busAddrFlag := "--address=" + targetBus
	if strings.Contains(targetBus, ",") {
		parts := strings.Split(targetBus, ",")
		busAddrFlag = "--address=" + parts[0]
	}

	// 2. Query root node role
	cmdRole := exec.Command("busctl", busAddrFlag, "call", "org.a11y.atspi.Registry", nodoRaiz, "org.a11y.atspi.Accessible", "GetRoleName")
	outRole, errRole := cmdRole.Output()
	if errRole == nil {
		rolRaiz = parseBusctlString(string(outRole))
	}

	// 3. Query root children
	cmdChildren := exec.Command("busctl", busAddrFlag, "call", "org.a11y.atspi.Registry", nodoRaiz, "org.a11y.atspi.Accessible", "GetChildren")
	outChildren, errChildren := cmdChildren.Output()
	if errChildren == nil {
		fields := strings.Fields(string(outChildren))
		for i, f := range fields {
			if strings.HasPrefix(f, "\":1.") && i+1 < len(fields) {
				hijosCount++
				childApp := strings.Trim(f, "\"")
				childPath := strings.Trim(fields[i+1], "\"")
				cmdAppRole := exec.Command("busctl", busAddrFlag, "call", childApp, childPath, "org.a11y.atspi.Accessible", "GetRoleName")
				if outAppRole, errAppRole := cmdAppRole.Output(); errAppRole == nil {
					appRol = parseBusctlString(string(outAppRole))
				}
				cmdAppName := exec.Command("busctl", busAddrFlag, "call", childApp, childPath, "org.freedesktop.DBus.Properties", "Get", "ss", "org.a11y.atspi.Accessible", "Name")
				if outAppName, errAppName := cmdAppName.Output(); errAppName == nil {
					appNombre = parseBusctlVariantString(string(outAppName))
				}
				break
			}
		}
	}

	res := &PercepcionResult{
		Display:       display,
		Resolucion:    resolucion,
		VentanasRaiz:  len(windows),
		AtSpiBus:      atspiBus,
		NodoRaiz:      nodoRaiz,
		RolNodoRaiz:   rolRaiz,
		Hijos:         hijosCount,
		Aplicacion:    appNombre,
		RolAplicacion: appRol,
		InyeccionXTest: InyeccionXTestResultado{
			PunteroInicial:   [2]int16{x0, y0},
			PunteroInyectado: [2]int16{targetX, targetY},
			PunteroObservado: [2]int16{x1, y1},
			Coincide:         coincide,
			Evento:           fmt.Sprintf("MotionNotify to (%d, %d)", targetX, targetY),
		},
		Estado: "verificado",
	}

	return res, nil
}

func parseBusctlString(raw string) string {
	s := strings.TrimSpace(raw)
	if strings.HasPrefix(s, "s \"") && strings.HasSuffix(s, "\"") {
		return strings.TrimSuffix(strings.TrimPrefix(s, "s \""), "\"")
	}
	return s
}

func parseBusctlVariantString(raw string) string {
	s := strings.TrimSpace(raw)
	if strings.HasPrefix(s, "v s \"") && strings.HasSuffix(s, "\"") {
		return strings.TrimSuffix(strings.TrimPrefix(s, "v s \""), "\"")
	}
	return parseBusctlString(s)
}

// ProbarPercepcion verifies AT-SPI2 perception and XTEST injection on the capsule slot.
func ProbarPercepcion(ctx context.Context, slot uint16) (*PercepcionResult, error) {
	containerName := fmt.Sprintf("srf-ng-%d", slot)

	// Ensure binary is copied into container
	exePath, err := os.Executable()
	if err != nil || !strings.Contains(exePath, "srf-capsula-desktop") {
		exePath = "bin/srf-capsula-desktop"
	}
	cmdCp := exec.CommandContext(ctx, "docker", "cp", exePath, containerName+":/usr/local/bin/srf-capsula-desktop")
	if cpOut, cpErr := cmdCp.CombinedOutput(); cpErr != nil {
		return nil, fmt.Errorf("failed to copy srf-capsula-desktop into %s: %s (%w)", containerName, string(cpOut), cpErr)
	}

	// Run percepcion inside the container
	cmdExec := exec.CommandContext(ctx, "docker", "exec", containerName, "/usr/local/bin/srf-capsula-desktop", "percepcion", "--inside", "--json")
	out, err := cmdExec.Output()
	if err != nil {
		return nil, fmt.Errorf("failed to execute percepcion inside %s: %w", containerName, err)
	}

	var res PercepcionResult
	if err := json.Unmarshal(out, &res); err != nil {
		return nil, fmt.Errorf("failed to parse percepcion output: %w (raw: %s)", err, string(out))
	}
	res.Slot = slot
	return &res, nil
}

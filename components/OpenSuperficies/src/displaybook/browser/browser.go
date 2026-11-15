package browser

import (
	"archive/tar"
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"opensuperficies.local/srf/src/displaybook/puertos"
	"opensuperficies.local/srf/src/dockerapi"
	"opensuperficies.local/srf/src/quarantineoracle"
)

const (
	DefaultImageTag     = "opensuperficies-browser-arm64:2.0.0"
	DefaultSlotsDir     = "runtime/slots"
	DefaultRuntimeDir   = "/tmp/srf-capsula"
	DefaultDockerSocket = "/var/run/docker.sock"
)

// BuildOptions parameters for building the browser capsule OCI image.
type BuildOptions struct {
	Tag            string `json:"tag"`
	Dockerfile     string `json:"dockerfile"`
	Entrypoint     string `json:"entrypoint"`
	RelayBin       string `json:"relay_bin"`
	BaseRepoDir    string `json:"base_repo_dir"`
	DockerSocket   string `json:"docker_socket"`
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
	Slot        uint16 `json:"slot"`
	Clase       string `json:"clase"`
	Contenedor  string `json:"contenedor"`
	ID          string `json:"id,omitempty"`
	PuertoXpra  uint16 `json:"puerto_xpra"`
	PuertoCDP   uint16 `json:"puerto_cdp"`
	Token       string `json:"token"`
	Red         string `json:"red,omitempty"`
	Estado      string `json:"estado"`
	RC          int    `json:"rc,omitempty"`
}

// IniciarOptions options for launching a browser capsule.
type IniciarOptions struct {
	SlotsDir     string `json:"slots_dir"`
	RuntimeDir   string `json:"runtime_dir"`
	DockerSocket string `json:"docker_socket"`
	Timeout      time.Duration
}

// Construir builds the OCI image 2.0.0 via Docker Engine API over Unix socket.
func Construir(ctx context.Context, opts BuildOptions) (*BuildResult, error) {
	if opts.Tag == "" {
		opts.Tag = DefaultImageTag
	}
	if opts.DockerSocket == "" {
		opts.DockerSocket = DefaultDockerSocket
	}
	if opts.Dockerfile == "" {
		opts.Dockerfile = "docker/ng/browser/Dockerfile"
	}
	if opts.Entrypoint == "" {
		opts.Entrypoint = "docker/ng/browser/entrypoint.sh"
	}
	if opts.RelayBin == "" {
		opts.RelayBin = "bin/srf-relay"
	}

	// 1. Verify bin/srf-relay exists
	relayBytes, err := os.ReadFile(opts.RelayBin)
	if err != nil {
		return nil, fmt.Errorf("relay binary %s missing: %w", opts.RelayBin, err)
	}

	dockerfileBytes, err := os.ReadFile(opts.Dockerfile)
	if err != nil {
		return nil, fmt.Errorf("dockerfile %s missing: %w", opts.Dockerfile, err)
	}

	entrypointBytes, err := os.ReadFile(opts.Entrypoint)
	if err != nil {
		return nil, fmt.Errorf("entrypoint %s missing: %w", opts.Entrypoint, err)
	}

	// 2. Build tar context in memory
	var tarBuf bytes.Buffer
	tw := tar.NewWriter(&tarBuf)

	// Add Dockerfile
	if err := addTarFile(tw, "Dockerfile", dockerfileBytes, 0644); err != nil {
		return nil, err
	}
	// Add docker/ng/browser/Dockerfile
	if err := addTarFile(tw, "docker/ng/browser/Dockerfile", dockerfileBytes, 0644); err != nil {
		return nil, err
	}
	// Add bin/srf-relay
	if err := addTarFile(tw, "bin/srf-relay", relayBytes, 0755); err != nil {
		return nil, err
	}
	// Add srf-relay
	if err := addTarFile(tw, "srf-relay", relayBytes, 0755); err != nil {
		return nil, err
	}
	// Add docker/ng/browser/entrypoint.sh
	if err := addTarFile(tw, "docker/ng/browser/entrypoint.sh", entrypointBytes, 0755); err != nil {
		return nil, err
	}
	// Add entrypoint.sh
	if err := addTarFile(tw, "entrypoint.sh", entrypointBytes, 0755); err != nil {
		return nil, err
	}

	if err := tw.Close(); err != nil {
		return nil, fmt.Errorf("error closing tar context: %w", err)
	}

	// 3. POST tar archive to Docker Engine API /build
	tr := &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, "unix", opts.DockerSocket)
		},
		DisableCompression: true,
	}
	httpClient := &http.Client{Transport: tr, Timeout: 180 * time.Second}

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

	// Consume streaming JSON response
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

	// Inspect built image to ensure presence
	dockerClient, err := dockerapi.NewClient(opts.DockerSocket)
	if err != nil {
		return nil, fmt.Errorf("error connecting to docker client: %w", err)
	}
	insp, err := dockerClient.InspectImage(ctx, opts.Tag)
	if err != nil {
		return nil, fmt.Errorf("built image %s inspection failed: %w", opts.Tag, err)
	}

	return &BuildResult{
		Imagen: opts.Tag,
		Clase:  "browser",
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

// Iniciar reserves slot, generates token, configures quarantine, and starts browser capsule.
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

	// 1. Acquire slot lock
	slotLock, err := puertos.ReservarSlot(opts.SlotsDir, slot)
	if err != nil {
		return nil, fmt.Errorf("starting slot %d failed: %w", slot, err)
	}
	// Note: We intentionally hold the file lock while the container runs

	// 2. Allocate ports
	plan := puertos.NuevoPlanPuertos()
	par, err := plan.Par(slot)
	if err != nil {
		_ = slotLock.Release()
		return nil, fmt.Errorf("starting slot %d failed port calculation: %w", slot, err)
	}
	xpraPort := par.Xpra
	cdpPort := par.CDP

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
	_, _ = dockerClient.CreateNetwork(ctx, netName, false)

	// 5. Generate secure Bearer authorization token
	tokenBytes := make([]byte, 16)
	if _, err := rand.Read(tokenBytes); err != nil {
		_ = slotLock.Release()
		return nil, fmt.Errorf("error generating random bearer token: %w", err)
	}
	token := hex.EncodeToString(tokenBytes)

	// 6. Assemble container configuration matching all 14 quarantine doors
	cfg := dockerapi.ContainerConfig{
		Image: DefaultImageTag,
		User:  "10001:0",
		Env: []string{
			"DBUS_SESSION_BUS_ADDRESS=disabled:",
			"DBUS_SYSTEM_BUS_ADDRESS=disabled:",
			"XDG_RUNTIME_DIR=/run/newton-tenant",
			"XDG_CURRENT_DESKTOP=",
			"XDG_SESSION_TYPE=x11",
			"GTK_USE_PORTAL=0",
			"GDK_BACKEND=x11",
			"QT_QPA_PLATFORM=xcb",
			"NO_AT_BRIDGE=1",
			"GIO_USE_VFS=local",
			"DISPLAY=:100",
			"LIBGL_ALWAYS_SOFTWARE=1",
			"GALLIUM_DRIVER=llvmpipe",
			"LP_NUM_THREADS=4",
			"HOME=/home/tenant",
			fmt.Sprintf("SRF_RELAY_TOKEN=%s", token),
			fmt.Sprintf("RELAY_PORT=%d", cdpPort),
			fmt.Sprintf("CDP_PORT=%d", 9222),
			fmt.Sprintf("XPRA_BIND_PORT=%d", 10000),
			"DISPLAY_NUM=100",
		},
		HostConfig: &dockerapi.HostConfig{
			ShmSize:     2147483648, // 2GB (Door I)
			NetworkMode: netName,    // Isolated network (Door N)
			PortBindings: map[string][]dockerapi.PortBinding{
				"10000/tcp": {
					{HostIp: "127.0.0.1", HostPort: fmt.Sprintf("%d", xpraPort)},
				},
				fmt.Sprintf("%d/tcp", cdpPort): {
					{HostIp: "127.0.0.1", HostPort: fmt.Sprintf("%d", cdpPort)},
				},
			},
		},
	}

	ctrID, err := dockerClient.CreateContainer(ctx, cfg, containerName)
	if err != nil {
		_ = slotLock.Release()
		return nil, fmt.Errorf("error creating container %s: %w", containerName, err)
	}

	if err := dockerClient.StartContainer(ctx, ctrID); err != nil {
		_ = dockerClient.RemoveContainer(ctx, ctrID, true, true)
		_ = slotLock.Release()
		return nil, fmt.Errorf("error starting container %s: %w", containerName, err)
	}

	info := &CapsulaInfo{
		Slot:       slot,
		Clase:      "browser",
		Contenedor: containerName,
		ID:         ctrID,
		PuertoXpra: xpraPort,
		PuertoCDP:  cdpPort,
		Token:      token,
		Red:        netName,
		Estado:     "listo",
	}

	// 7. Persist runtime info in runtime directory
	_ = os.MkdirAll(opts.RuntimeDir, 0755)
	stateFile := filepath.Join(opts.RuntimeDir, fmt.Sprintf("slot-%d.json", slot))
	if data, err := json.Marshal(info); err == nil {
		_ = os.WriteFile(stateFile, data, 0644)
	}

	// 8. Poll until both CDP relay and Xpra endpoints are responding
	readyDeadline := time.Now().Add(opts.Timeout)
	ready := false
	pollClient := &http.Client{Timeout: 1 * time.Second}

	for time.Now().Before(readyDeadline) {
		cdpURL := fmt.Sprintf("http://127.0.0.1:%d/json/version", cdpPort)
		creq, err := http.NewRequestWithContext(ctx, http.MethodGet, cdpURL, nil)
		if err == nil {
			creq.Header.Set("Authorization", "Bearer "+token)
			cresp, cerr := pollClient.Do(creq)
			if cerr == nil {
				if cresp.StatusCode == http.StatusOK {
					xpraURL := fmt.Sprintf("http://127.0.0.1:%d/", xpraPort)
					xresp, xerr := pollClient.Get(xpraURL)
					if xerr == nil {
						if xresp.StatusCode == http.StatusOK || xresp.StatusCode == http.StatusFound {
							xresp.Body.Close()
							cresp.Body.Close()
							ready = true
							break
						}
						xresp.Body.Close()
					}
				}
				cresp.Body.Close()
			}
		}
		time.Sleep(150 * time.Millisecond)
	}

	if !ready {
		_ = Detener(ctx, slot)
		return nil, fmt.Errorf("timeout waiting for capsule endpoints (cdp %d, xpra %d) to become ready", cdpPort, xpraPort)
	}

	return info, nil
}

// Detener stops and removes the container, deletes isolated network, and releases locks.
func Detener(ctx context.Context, slot uint16) error {
	containerName := fmt.Sprintf("srf-ng-%d", slot)
	netName := fmt.Sprintf("srf-net-%d", slot)

	dockerClient, err := dockerapi.NewClient(DefaultDockerSocket)
	if err == nil {
		_ = dockerClient.StopContainer(ctx, containerName, 3)
		_ = dockerClient.RemoveContainer(ctx, containerName, true, true)
		_ = dockerClient.RemoveNetwork(ctx, netName)
	}

	stateFile := filepath.Join(DefaultRuntimeDir, fmt.Sprintf("slot-%d.json", slot))
	_ = os.Remove(stateFile)

	lockPath := filepath.Join(DefaultSlotsDir, fmt.Sprintf("slot-%d.lock", slot))
	_ = os.Remove(lockPath)

	return nil
}

// Token retrieves the Bearer authorization token for a given slot.
func Token(ctx context.Context, slot uint16) (string, error) {
	// First check cached runtime metadata file
	stateFile := filepath.Join(DefaultRuntimeDir, fmt.Sprintf("slot-%d.json", slot))
	if data, err := os.ReadFile(stateFile); err == nil {
		var info CapsulaInfo
		if err := json.Unmarshal(data, &info); err == nil && info.Token != "" {
			return info.Token, nil
		}
	}

	// Fallback to inspecting container environment directly via Docker API
	dockerClient, err := dockerapi.NewClient(DefaultDockerSocket)
	if err != nil {
		return "", fmt.Errorf("error connecting to docker: %w", err)
	}

	containerName := fmt.Sprintf("srf-ng-%d", slot)
	insp, err := dockerClient.InspectContainer(ctx, containerName)
	if err != nil {
		return "", fmt.Errorf("error inspecting container %s: %w", containerName, err)
	}

	for _, e := range insp.Config.Env {
		if strings.HasPrefix(e, "SRF_RELAY_TOKEN=") {
			return strings.TrimPrefix(e, "SRF_RELAY_TOKEN="), nil
		}
	}

	return "", fmt.Errorf("token not found for slot %d", slot)
}

// PID resolves the process ID for the specified process name inside the capsule container.
func PID(ctx context.Context, slot uint16, proceso string) (int, error) {
	containerName := fmt.Sprintf("srf-ng-%d", slot)
	client := quarantineoracle.NewDockerSocketClient(DefaultDockerSocket)

	var rc int
	var out string
	var err error

	if proceso == "chromium" {
		rc, out, err = client.Exec(ctx, containerName, []string{"pgrep", "-o", "-f", "remote-debugging-port"})
		if err != nil {
			return 0, fmt.Errorf("exec pgrep failed: %w", err)
		}
		if rc != 0 {
			rc, out, err = client.Exec(ctx, containerName, []string{"pgrep", "-o", "-f", "chromium"})
			if err != nil {
				return 0, fmt.Errorf("exec pgrep failed: %w", err)
			}
		}
	} else {
		rc, out, err = client.Exec(ctx, containerName, []string{"pgrep", "-o", "-f", proceso})
		if err != nil {
			return 0, fmt.Errorf("exec pgrep failed: %w", err)
		}
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

// Listar queries active containers and lists running capsules.
func Listar(ctx context.Context) ([]CapsulaInfo, error) {
	var list []CapsulaInfo
	entries, err := os.ReadDir(DefaultRuntimeDir)
	if err == nil {
		for _, entry := range entries {
			if strings.HasPrefix(entry.Name(), "slot-") && strings.HasSuffix(entry.Name(), ".json") {
				data, rerr := os.ReadFile(filepath.Join(DefaultRuntimeDir, entry.Name()))
				if rerr == nil {
					var info CapsulaInfo
					if json.Unmarshal(data, &info) == nil {
						list = append(list, info)
					}
				}
			}
		}
	}
	return list, nil
}

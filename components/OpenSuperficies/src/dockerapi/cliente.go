package dockerapi

import (
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
	"strings"
	"time"
)

// DefaultSocketPath is the standard Docker Engine API Unix domain socket location.
const DefaultSocketPath = "/var/run/docker.sock"

// ErrNotFound indicates the requested Docker resource was not found.
var ErrNotFound = errors.New("dockerapi: recurso no encontrado")

// ErrDockerAPI indicates an error response returned by Docker Engine API.
var ErrDockerAPI = errors.New("dockerapi: error del motor docker")

// Client provides an HTTP client communicating directly over the Docker Engine UDS.
type Client struct {
	socketPath string
	httpClient *http.Client
}

// ImageInspect represents metadata returned by GET /images/{name}/json.
type ImageInspect struct {
	Id          string            `json:"Id"`
	RepoTags    []string          `json:"RepoTags"`
	Size        int64             `json:"Size"`
	VirtualSize int64             `json:"VirtualSize,omitempty"`
	Created     string            `json:"Created"`
	DockerVersion string          `json:"DockerVersion,omitempty"`
	Architecture string           `json:"Architecture,omitempty"`
	Os          string            `json:"Os,omitempty"`
	Config      *ContainerConfig  `json:"Config,omitempty"`
}

// ContainerState represents the lifecycle status of a container.
type ContainerState struct {
	Status     string `json:"Status"`
	Running    bool   `json:"Running"`
	Paused     bool   `json:"Paused"`
	Restarting bool   `json:"Restarting"`
	OOMKilled  bool   `json:"OOMKilled"`
	Dead       bool   `json:"Dead"`
	Pid        int    `json:"Pid"`
	ExitCode   int    `json:"ExitCode"`
	Error      string `json:"Error"`
	StartedAt  string `json:"StartedAt"`
	FinishedAt string `json:"FinishedAt"`
}

// ContainerInspect represents metadata returned by GET /containers/{id}/json.
type ContainerInspect struct {
	Id         string          `json:"Id"`
	Created    string          `json:"Created"`
	Path       string          `json:"Path"`
	Args       []string        `json:"Args"`
	State      ContainerState  `json:"State"`
	Image      string          `json:"Image"`
	Name       string          `json:"Name"`
	Config     ContainerConfig `json:"Config"`
	HostConfig HostConfig      `json:"HostConfig"`
}

// PortBinding represents a host port mapping.
type PortBinding struct {
	HostIp   string `json:"HostIp"`
	HostPort string `json:"HostPort"`
}

// HostConfig holds container host-level isolation and resource constraints.
type HostConfig struct {
	NetworkMode   string                   `json:"NetworkMode,omitempty"`
	Binds         []string                 `json:"Binds,omitempty"`
	PortBindings  map[string][]PortBinding `json:"PortBindings,omitempty"`
	Memory        int64                    `json:"Memory,omitempty"`
	MemorySwap    int64                    `json:"MemorySwap,omitempty"`
	NanoCpus      int64                    `json:"NanoCpus,omitempty"`
	PidsLimit     *int64                   `json:"PidsLimit,omitempty"`
	CapDrop       []string                 `json:"CapDrop,omitempty"`
	SecurityOpt   []string                 `json:"SecurityOpt,omitempty"`
	ShmSize       int64                    `json:"ShmSize,omitempty"`
	Tmpfs         map[string]string        `json:"Tmpfs,omitempty"`
	AutoRemove    bool                     `json:"AutoRemove,omitempty"`
}

// ContainerConfig defines container creation parameters.
type ContainerConfig struct {
	Image        string      `json:"Image"`
	Cmd          []string    `json:"Cmd,omitempty"`
	Env          []string    `json:"Env,omitempty"`
	User         string      `json:"User,omitempty"`
	Tty          bool        `json:"Tty,omitempty"`
	OpenStdin    bool        `json:"OpenStdin,omitempty"`
	AttachStdout bool        `json:"AttachStdout,omitempty"`
	AttachStderr bool        `json:"AttachStderr,omitempty"`
	HostConfig   *HostConfig `json:"HostConfig,omitempty"`
}

// NewClient initializes a Docker Engine API client communicating through a Unix socket.
func NewClient(socketPath string) (*Client, error) {
	if socketPath == "" {
		if env := os.Getenv("DOCKER_HOST"); env != "" {
			socketPath = strings.TrimPrefix(env, "unix://")
		} else {
			socketPath = DefaultSocketPath
		}
	}

	transport := &http.Transport{
		DialContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
			var d net.Dialer
			return d.DialContext(ctx, "unix", socketPath)
		},
		DisableCompression: true,
	}

	return &Client{
		socketPath: socketPath,
		httpClient: &http.Client{
			Transport: transport,
			Timeout:   120 * time.Second,
		},
	}, nil
}

// SocketPath returns the Unix socket path configured for the client.
func (c *Client) SocketPath() string {
	return c.socketPath
}

// Ping checks whether the Docker Engine daemon is accessible and responding.
func (c *Client) Ping(ctx context.Context) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://localhost/_ping", nil)
	if err != nil {
		return err
	}
	resp, err := c.httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("%w: ping status %d", ErrDockerAPI, resp.StatusCode)
	}
	return nil
}

// InspectRaw performs a GET request against the specified path and returns the raw response body.
func (c *Client) InspectRaw(ctx context.Context, path string) ([]byte, error) {
	if !strings.HasPrefix(path, "/") {
		path = "/" + path
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://localhost"+path, nil)
	if err != nil {
		return nil, err
	}
	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode == http.StatusNotFound {
		return nil, fmt.Errorf("%w: %s", ErrNotFound, path)
	}
	if resp.StatusCode >= 400 {
		return nil, fmt.Errorf("%w: status %d: %s", ErrDockerAPI, resp.StatusCode, string(body))
	}
	return body, nil
}

// InspectImageRaw retrieves the unparsed JSON inspection document for the named image.
func (c *Client) InspectImageRaw(ctx context.Context, imageName string) ([]byte, error) {
	path := fmt.Sprintf("/images/%s/json", url.PathEscape(imageName))
	return c.InspectRaw(ctx, path)
}

// InspectImage inspects a Docker image and parses the metadata into ImageInspect.
func (c *Client) InspectImage(ctx context.Context, imageName string) (*ImageInspect, error) {
	data, err := c.InspectImageRaw(ctx, imageName)
	if err != nil {
		return nil, err
	}
	var inspect ImageInspect
	if err := json.Unmarshal(data, &inspect); err != nil {
		return nil, fmt.Errorf("error deserializing image inspect: %w", err)
	}
	return &inspect, nil
}

// InspectContainer retrieves container state and configuration by ID or name.
func (c *Client) InspectContainer(ctx context.Context, containerID string) (*ContainerInspect, error) {
	path := fmt.Sprintf("/containers/%s/json", url.PathEscape(containerID))
	data, err := c.InspectRaw(ctx, path)
	if err != nil {
		return nil, err
	}
	var inspect ContainerInspect
	if err := json.Unmarshal(data, &inspect); err != nil {
		return nil, fmt.Errorf("error deserializing container inspect: %w", err)
	}
	return &inspect, nil
}

// CreateContainer creates a container and returns its ID.
func (c *Client) CreateContainer(ctx context.Context, cfg ContainerConfig, name string) (string, error) {
	path := "/containers/create"
	if name != "" {
		path += "?name=" + url.QueryEscape(name)
	}
	bodyData, err := json.Marshal(cfg)
	if err != nil {
		return "", err
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, "http://localhost"+path, bytes.NewReader(bodyData))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()

	respBytes, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", err
	}
	if resp.StatusCode != http.StatusCreated && resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("%w: status %d: %s", ErrDockerAPI, resp.StatusCode, string(respBytes))
	}

	var res struct {
		Id       string   `json:"Id"`
		Warnings []string `json:"Warnings"`
	}
	if err := json.Unmarshal(respBytes, &res); err != nil {
		return "", err
	}
	return res.Id, nil
}

// StartContainer starts a created container by ID.
func (c *Client) StartContainer(ctx context.Context, containerID string) error {
	path := fmt.Sprintf("/containers/%s/start", url.PathEscape(containerID))
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, "http://localhost"+path, nil)
	if err != nil {
		return err
	}
	resp, err := c.httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusNoContent && resp.StatusCode != http.StatusNotModified {
		body, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("%w: start status %d: %s", ErrDockerAPI, resp.StatusCode, string(body))
	}
	return nil
}

// StopContainer requests graceful container termination with a deadline.
func (c *Client) StopContainer(ctx context.Context, containerID string, timeoutSeconds int) error {
	path := fmt.Sprintf("/containers/%s/stop?t=%d", url.PathEscape(containerID), timeoutSeconds)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, "http://localhost"+path, nil)
	if err != nil {
		return err
	}
	resp, err := c.httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusNoContent && resp.StatusCode != http.StatusNotModified {
		body, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("%w: stop status %d: %s", ErrDockerAPI, resp.StatusCode, string(body))
	}
	return nil
}

// RemoveContainer deletes a container, optionally forcing removal and cleaning volumes.
func (c *Client) RemoveContainer(ctx context.Context, containerID string, force, removeVolumes bool) error {
	path := fmt.Sprintf("/containers/%s?force=%t&v=%t", url.PathEscape(containerID), force, removeVolumes)
	req, err := http.NewRequestWithContext(ctx, http.MethodDelete, "http://localhost"+path, nil)
	if err != nil {
		return err
	}
	resp, err := c.httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusNoContent {
		body, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("%w: remove status %d: %s", ErrDockerAPI, resp.StatusCode, string(body))
	}
	return nil
}

// CreateNetwork creates an isolated Docker network.
func (c *Client) CreateNetwork(ctx context.Context, name string, internal bool) (string, error) {
	path := "/networks/create"
	payload := map[string]interface{}{
		"Name":     name,
		"Internal": internal,
		"CheckDuplicate": true,
	}
	bodyData, err := json.Marshal(payload)
	if err != nil {
		return "", err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, "http://localhost"+path, bytes.NewReader(bodyData))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.httpClient.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	respBytes, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", err
	}
	if resp.StatusCode != http.StatusCreated && resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("%w: create network status %d: %s", ErrDockerAPI, resp.StatusCode, string(respBytes))
	}
	var res struct {
		Id string `json:"Id"`
	}
	if err := json.Unmarshal(respBytes, &res); err != nil {
		return "", err
	}
	return res.Id, nil
}

// RemoveNetwork removes an existing Docker network by name or ID.
func (c *Client) RemoveNetwork(ctx context.Context, networkID string) error {
	path := fmt.Sprintf("/networks/%s", url.PathEscape(networkID))
	req, err := http.NewRequestWithContext(ctx, http.MethodDelete, "http://localhost"+path, nil)
	if err != nil {
		return err
	}
	resp, err := c.httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusNoContent {
		body, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("%w: remove network status %d: %s", ErrDockerAPI, resp.StatusCode, string(body))
	}
	return nil
}

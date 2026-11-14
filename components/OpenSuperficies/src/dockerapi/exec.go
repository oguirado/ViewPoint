package dockerapi

import (
	"bufio"
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
)

// Standard Docker multiplexing stream types.
const (
	StreamStdin  byte = 0
	StreamStdout byte = 1
	StreamStderr byte = 2
)

// ErrCabeceraInvalida indicates that the multiplexing header is corrupt or unrecognized.
var ErrCabeceraInvalida = errors.New("cabecera de multiplexación inválida")

// headerSize defines the byte length of the multiplexing header.
// Default is 8 bytes. The mutante_demux build tag sets this to 4 bytes.
var headerSize = 8

// readFrameHeader reads the multiplexing header from the input stream.
func readFrameHeader(r io.Reader) (byte, uint32, error) {
	hdr := make([]byte, headerSize)
	if _, err := io.ReadFull(r, hdr); err != nil {
		return 0, 0, err
	}
	if headerSize == 8 {
		streamType := hdr[0]
		if streamType != StreamStdout && streamType != StreamStderr {
			return 0, 0, fmt.Errorf("%w: tipo de flujo desconocido %d", ErrCabeceraInvalida, streamType)
		}
		if hdr[1] != 0 || hdr[2] != 0 || hdr[3] != 0 {
			return 0, 0, fmt.Errorf("%w: bytes reservados no nulos [%x %x %x]", ErrCabeceraInvalida, hdr[1], hdr[2], hdr[3])
		}
		size := binary.BigEndian.Uint32(hdr[4:8])
		return streamType, size, nil
	}

	// Mutant: reads 4-byte header instead of 8-byte multiplexing header
	streamType := hdr[0]
	if streamType != StreamStdout && streamType != StreamStderr {
		return 0, 0, fmt.Errorf("%w: mutante tipo de flujo desconocido %d", ErrCabeceraInvalida, streamType)
	}
	size := uint32(hdr[1])<<16 | uint32(hdr[2])<<8 | uint32(hdr[3])
	return streamType, size, nil
}

// Demux reads a Docker multiplexed stream and writes payloads to the matching stdout/stderr writers.
func Demux(r io.Reader, stdout, stderr io.Writer) error {
	for {
		streamType, size, err := readFrameHeader(r)
		if errors.Is(err, io.EOF) {
			return nil
		}
		if err != nil {
			return err
		}

		payload := make([]byte, size)
		if _, err := io.ReadFull(r, payload); err != nil {
			return err
		}

		switch streamType {
		case StreamStdout:
			if stdout != nil {
				if _, err := stdout.Write(payload); err != nil {
					return err
				}
			}
		case StreamStderr:
			if stderr != nil {
				if _, err := stderr.Write(payload); err != nil {
					return err
				}
			}
		default:
			return fmt.Errorf("%w: flujo %d no direccionable", ErrCabeceraInvalida, streamType)
		}
	}
}

// WriteFrame writes a single standard 8-byte multiplexed Docker frame to the writer.
func WriteFrame(w io.Writer, streamType byte, payload []byte) error {
	hdr := make([]byte, 8)
	hdr[0] = streamType
	binary.BigEndian.PutUint32(hdr[4:8], uint32(len(payload)))
	if _, err := w.Write(hdr); err != nil {
		return err
	}
	_, err := w.Write(payload)
	return err
}

// ExecConfig specifies options for creating an exec instance.
type ExecConfig struct {
	AttachStdin  bool     `json:"AttachStdin,omitempty"`
	AttachStdout bool     `json:"AttachStdout,omitempty"`
	AttachStderr bool     `json:"AttachStderr,omitempty"`
	Tty          bool     `json:"Tty,omitempty"`
	Cmd          []string `json:"Cmd"`
	Env          []string `json:"Env,omitempty"`
	Privileged   bool     `json:"Privileged,omitempty"`
	User         string   `json:"User,omitempty"`
}

// ExecInspect contains details of an exec instance.
type ExecInspect struct {
	ID       string `json:"ID"`
	Running  bool   `json:"Running"`
	ExitCode int    `json:"ExitCode"`
	Pid      int    `json:"Pid"`
}

// CreateExec initializes an exec instance inside a running container.
func (c *Client) CreateExec(ctx context.Context, containerID string, cfg ExecConfig) (string, error) {
	path := fmt.Sprintf("/containers/%s/exec", url.PathEscape(containerID))
	bodyBytes, err := json.Marshal(cfg)
	if err != nil {
		return "", err
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, "http://localhost"+path, bytes.NewReader(bodyBytes))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()

	respData, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", err
	}
	if resp.StatusCode != http.StatusCreated && resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("%w: create exec status %d: %s", ErrDockerAPI, resp.StatusCode, string(respData))
	}

	var res struct {
		Id string `json:"Id"`
	}
	if err := json.Unmarshal(respData, &res); err != nil {
		return "", err
	}
	return res.Id, nil
}

// StartExecHijack initiates the exec process over a hijacked raw stream.
func (c *Client) StartExecHijack(ctx context.Context, execID string) (net.Conn, *bufio.Reader, error) {
	var d net.Dialer
	conn, err := d.DialContext(ctx, "unix", c.socketPath)
	if err != nil {
		return nil, nil, fmt.Errorf("error dialing docker socket for exec hijack: %w", err)
	}

	body := []byte(`{"Detach":false,"Tty":false}`)
	reqStr := fmt.Sprintf("POST /exec/%s/start HTTP/1.1\r\n"+
		"Host: localhost\r\n"+
		"Content-Type: application/json\r\n"+
		"Connection: Upgrade\r\n"+
		"Upgrade: tcp\r\n"+
		"Content-Length: %d\r\n\r\n%s",
		url.PathEscape(execID), len(body), string(body))

	if _, err := conn.Write([]byte(reqStr)); err != nil {
		conn.Close()
		return nil, nil, fmt.Errorf("error writing exec start request: %w", err)
	}

	reader := bufio.NewReader(conn)
	resp, err := http.ReadResponse(reader, nil)
	if err != nil {
		conn.Close()
		return nil, nil, fmt.Errorf("error reading exec upgrade response: %w", err)
	}

	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusSwitchingProtocols {
		conn.Close()
		return nil, nil, fmt.Errorf("%w: exec upgrade status %d", ErrDockerAPI, resp.StatusCode)
	}

	return conn, reader, nil
}

// InspectExec checks the status and exit code of an exec instance.
func (c *Client) InspectExec(ctx context.Context, execID string) (*ExecInspect, error) {
	path := fmt.Sprintf("/exec/%s/json", url.PathEscape(execID))
	data, err := c.InspectRaw(ctx, path)
	if err != nil {
		return nil, err
	}
	var inspect ExecInspect
	if err := json.Unmarshal(data, &inspect); err != nil {
		return nil, fmt.Errorf("error deserializing exec inspect: %w", err)
	}
	return &inspect, nil
}

// ExecRun creates, starts and demultiplexes an exec process inside a container.
func (c *Client) ExecRun(ctx context.Context, containerID string, cmd []string, stdout, stderr io.Writer) error {
	execID, err := c.CreateExec(ctx, containerID, ExecConfig{
		AttachStdout: stdout != nil,
		AttachStderr: stderr != nil,
		Cmd:          cmd,
	})
	if err != nil {
		return err
	}

	conn, reader, err := c.StartExecHijack(ctx, execID)
	if err != nil {
		return err
	}
	defer conn.Close()

	return Demux(reader, stdout, stderr)
}

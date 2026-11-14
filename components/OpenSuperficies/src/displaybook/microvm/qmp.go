package microvm

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"strings"
	"sync"
	"time"
)

// QMPClient manages a JSON-based QEMU Machine Protocol connection over a Unix domain socket.
type QMPClient struct {
	conn   net.Conn
	reader *bufio.Reader
	mu     sync.Mutex
}

// ChardevInfo describes a character device reported by QMP query-chardev.
type ChardevInfo struct {
	ID       string `json:"id"`
	Filename string `json:"filename"`
}

// CPUFastInfo describes a vCPU reported by QMP query-cpus-fast.
type CPUFastInfo struct {
	CPUIndex int    `json:"cpu-index"`
	QOMPath  string `json:"qom-path"`
	ThreadID int    `json:"thread-id"`
}

type qmpCommand struct {
	Execute   string      `json:"execute"`
	Arguments interface{} `json:"arguments,omitempty"`
}

type qmpResponse struct {
	Return json.RawMessage `json:"return,omitempty"`
	Error  *qmpError       `json:"error,omitempty"`
	Event  string          `json:"event,omitempty"`
}

type qmpError struct {
	Class string `json:"class"`
	Desc  string `json:"desc"`
}

// DialQMP connects to the QMP Unix socket, reads the initial greeting, and returns an initialized QMPClient.
func DialQMP(socketPath string, timeout time.Duration) (*QMPClient, error) {
	deadline := time.Now().Add(timeout)
	var conn net.Conn
	var err error

	for {
		conn, err = net.DialTimeout("unix", socketPath, 200*time.Millisecond)
		if err == nil {
			break
		}
		if time.Now().After(deadline) {
			return nil, fmt.Errorf("qmp: failed to connect to socket %s within %v: %w", socketPath, timeout, err)
		}
		time.Sleep(20 * time.Millisecond)
	}

	return NewQMPClient(conn)
}

// NewQMPClient wraps an existing net.Conn, verifies the QMP greeting banner, and returns a QMPClient.
func NewQMPClient(conn net.Conn) (*QMPClient, error) {
	reader := bufio.NewReader(conn)
	greetingLine, err := reader.ReadString('\n')
	if err != nil {
		_ = conn.Close()
		return nil, fmt.Errorf("qmp: failed reading greeting: %w", err)
	}

	var greeting map[string]interface{}
	if err := json.Unmarshal([]byte(greetingLine), &greeting); err != nil || greeting["QMP"] == nil {
		_ = conn.Close()
		return nil, fmt.Errorf("qmp: invalid greeting response: %s", strings.TrimSpace(greetingLine))
	}

	return &QMPClient{
		conn:   conn,
		reader: reader,
	}, nil
}

// Close closes the underlying socket connection.
func (q *QMPClient) Close() error {
	q.mu.Lock()
	defer q.mu.Unlock()
	if q.conn != nil {
		return q.conn.Close()
	}
	return nil
}

// Execute sends a command to QEMU and returns the raw JSON payload of the "return" field.
// Asynchronous "event" lines are ignored while waiting for the command response.
func (q *QMPClient) Execute(cmd string, args interface{}) (json.RawMessage, error) {
	q.mu.Lock()
	defer q.mu.Unlock()

	req := qmpCommand{
		Execute:   cmd,
		Arguments: args,
	}

	data, err := json.Marshal(req)
	if err != nil {
		return nil, fmt.Errorf("qmp: marshal command %s: %w", cmd, err)
	}
	data = append(data, '\n')

	if _, err := q.conn.Write(data); err != nil {
		return nil, fmt.Errorf("qmp: write command %s: %w", cmd, err)
	}

	for {
		line, err := q.reader.ReadString('\n')
		if err != nil {
			return nil, fmt.Errorf("qmp: read response for %s: %w", cmd, err)
		}

		var resp qmpResponse
		if err := json.Unmarshal([]byte(line), &resp); err != nil {
			return nil, fmt.Errorf("qmp: parse response for %s (%s): %w", cmd, strings.TrimSpace(line), err)
		}

		// Asynchronous notification, drain and continue waiting
		if resp.Event != "" {
			continue
		}

		if resp.Error != nil {
			return nil, fmt.Errorf("qmp: command %s error [%s]: %s", cmd, resp.Error.Class, resp.Error.Desc)
		}

		return resp.Return, nil
	}
}

// Capabilities negotiates QMP capabilities (qmp_capabilities).
func (q *QMPClient) Capabilities() error {
	_, err := q.Execute("qmp_capabilities", nil)
	return err
}

// QueryChardev retrieves character device details from QEMU.
func (q *QMPClient) QueryChardev() ([]ChardevInfo, error) {
	raw, err := q.Execute("query-chardev", nil)
	if err != nil {
		return nil, err
	}
	var list []ChardevInfo
	if err := json.Unmarshal(raw, &list); err != nil {
		return nil, fmt.Errorf("qmp: parse query-chardev result: %w", err)
	}
	return list, nil
}

// QueryCPUsFast retrieves thread and architecture details for online vCPUs.
func (q *QMPClient) QueryCPUsFast() ([]CPUFastInfo, error) {
	raw, err := q.Execute("query-cpus-fast", nil)
	if err != nil {
		return nil, err
	}
	var list []CPUFastInfo
	if err := json.Unmarshal(raw, &list); err != nil {
		return nil, fmt.Errorf("qmp: parse query-cpus-fast result: %w", err)
	}
	return list, nil
}

// StatusInfo describes the guest running state reported by QMP query-status.
type StatusInfo struct {
	Running    bool   `json:"running"`
	Singlestep bool   `json:"singlestep"`
	Status     string `json:"status"`
}

// QueryStatus queries QEMU execution status via query-status.
func (q *QMPClient) QueryStatus() (*StatusInfo, error) {
	raw, err := q.Execute("query-status", nil)
	if err != nil {
		return nil, err
	}
	var s StatusInfo
	if err := json.Unmarshal(raw, &s); err != nil {
		return nil, fmt.Errorf("qmp: parse query-status result: %w", err)
	}
	return &s, nil
}

// Cont resumes guest execution from paused state.
func (q *QMPClient) Cont() error {
	_, err := q.Execute("cont", nil)
	return err
}

// Stop pauses guest execution.
func (q *QMPClient) Stop() error {
	_, err := q.Execute("stop", nil)
	return err
}

// SystemPowerdown requests an orderly ACPI/PSCI powerdown.
func (q *QMPClient) SystemPowerdown() error {
	_, err := q.Execute("system_powerdown", nil)
	return err
}

// Quit requests QEMU termination.
func (q *QMPClient) Quit() error {
	_, err := q.Execute("quit", nil)
	if errors.Is(err, net.ErrClosed) {
		return nil
	}
	return err
}

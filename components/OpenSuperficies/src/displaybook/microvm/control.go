package microvm

import (
	"bufio"
	"fmt"
	"net"
	"strings"
	"sync"
	"time"
)

// ControlClient manages communication over the host-side virtio-serial Unix socket.
type ControlClient struct {
	conn   net.Conn
	reader *bufio.Reader
	mu     sync.Mutex
}

// DialControl connects to the virtio-serial Unix socket exposed by QEMU.
func DialControl(socketPath string, timeout time.Duration) (*ControlClient, error) {
	deadline := time.Now().Add(timeout)
	var conn net.Conn
	var err error

	for {
		conn, err = net.DialTimeout("unix", socketPath, 200*time.Millisecond)
		if err == nil {
			break
		}
		if time.Now().After(deadline) {
			return nil, fmt.Errorf("control: failed connecting to control socket %s within %v: %w", socketPath, timeout, err)
		}
		time.Sleep(20 * time.Millisecond)
	}

	return &ControlClient{
		conn:   conn,
		reader: bufio.NewReader(conn),
	}, nil
}

// Close closes the control socket connection.
func (c *ControlClient) Close() error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.conn != nil {
		return c.conn.Close()
	}
	return nil
}

// WaitForPID1 blocks until the guest static init emits "SRF1 PID1".
func (c *ControlClient) WaitForPID1(timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	_ = c.conn.SetReadDeadline(deadline)

	for {
		line, err := c.reader.ReadString('\n')
		if err != nil {
			return fmt.Errorf("control: read error waiting for PID1 ready: %w", err)
		}
		trimmed := strings.TrimSpace(line)
		if strings.HasPrefix(trimmed, "SRF1 PID1") {
			return nil
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("control: timeout waiting for SRF1 PID1 readiness message")
		}
	}
}

// WaitForShutdown blocks until the guest static init emits "SRF1 POWER_OFF" or socket closes.
func (c *ControlClient) WaitForShutdown(timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	_ = c.conn.SetReadDeadline(deadline)

	for {
		line, err := c.reader.ReadString('\n')
		if err != nil {
			return nil // Socket closed / EOF upon power off
		}
		trimmed := strings.TrimSpace(line)
		if strings.HasPrefix(trimmed, "SRF1 POWER_OFF") {
			return nil
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("control: timeout waiting for SRF1 POWER_OFF message")
		}
	}
}

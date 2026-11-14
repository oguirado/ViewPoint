package pty

import (
	"bytes"
	"io"
	"strings"
	"sync"
	"testing"
)

func TestPTYOpenAndEcho(t *testing.T) {
	p, err := Open()
	if err != nil {
		t.Fatalf("Open failed: %v", err)
	}
	defer p.Close()

	if p.Master == nil || p.Slave == nil {
		t.Fatalf("expected non-nil master and slave fds")
	}

	num, err := p.Number()
	if err != nil {
		t.Fatalf("Number failed: %v", err)
	}
	if num < 0 {
		t.Fatalf("expected non-negative PTY number, got %d", num)
	}

	name, err := p.PtsName()
	if err != nil {
		t.Fatalf("PtsName failed: %v", err)
	}
	if !strings.HasPrefix(name, "/dev/pts/") {
		t.Fatalf("expected PtsName starting with /dev/pts/, got %s", name)
	}

	// Test Winsize resize and retrieval
	if err := p.Resize(120, 40); err != nil {
		t.Fatalf("Resize failed: %v", err)
	}
	ws, err := p.Winsize()
	if err != nil {
		t.Fatalf("Winsize failed: %v", err)
	}
	if ws.Cols != 120 || ws.Rows != 40 {
		t.Fatalf("expected 120x40 winsize, got %dx%d", ws.Cols, ws.Rows)
	}

	// Test bi-directional byte transmission
	payload := []byte("hello kernelbook pty\n")
	if _, err := p.Slave.Write(payload); err != nil {
		t.Fatalf("write to slave failed: %v", err)
	}

	buf := make([]byte, 128)
	n, err := p.Master.Read(buf)
	if err != nil && err != io.EOF {
		t.Fatalf("read from master failed: %v", err)
	}
	if !bytes.Contains(buf[:n], []byte("hello kernelbook pty")) {
		t.Fatalf("expected payload in master read, got %q", string(buf[:n]))
	}
}

func TestTriRingThrottleBasic(t *testing.T) {
	ring := NewTriRingThrottle()
	payload := []byte("test-terminal-stream-payload")

	n := ring.WriteIngress(payload)
	if n != len(payload) {
		t.Fatalf("expected %d bytes written, got %d", len(payload), n)
	}

	// Swap 1: Ingress -> Staging (returned publish is empty initial buffer)
	pub1 := ring.SwapAndFlush()
	if pub1 == nil {
		t.Fatalf("unexpected nil publish buffer on swap 1")
	}
	if pub1.Len() != 0 {
		t.Fatalf("expected empty publish on first swap, got %d bytes", pub1.Len())
	}

	// Swap 2: Staging -> Publish (contains payload)
	pub2 := ring.SwapAndFlush()
	if pub2 == nil {
		t.Fatalf("unexpected nil publish buffer on swap 2")
	}
	if pub2.Len() != len(payload) {
		t.Fatalf("expected %d bytes on second swap, got %d", len(payload), pub2.Len())
	}
	if !bytes.Equal(pub2.Bytes(), payload) {
		t.Fatalf("payload mismatch: got %q, want %q", string(pub2.Bytes()), string(payload))
	}

	if ring.Epoch() != 2 {
		t.Fatalf("expected epoch 2, got %d", ring.Epoch())
	}
}

func TestTriRingThrottleOverflow(t *testing.T) {
	ring := NewTriRingThrottle()

	largeData := make([]byte, RingBufferSize+4096)
	for i := range largeData {
		largeData[i] = 'A'
	}

	n := ring.WriteIngress(largeData)
	if n != RingBufferSize {
		t.Fatalf("expected write capped at %d, got %d", RingBufferSize, n)
	}
	if ring.Overflow() != 1 {
		t.Fatalf("expected overflow count 1, got %d", ring.Overflow())
	}

	// Further writes must also overflow and write 0
	n2 := ring.WriteIngress([]byte("extra"))
	if n2 != 0 {
		t.Fatalf("expected 0 bytes written to full ring, got %d", n2)
	}
	if ring.Overflow() != 2 {
		t.Fatalf("expected overflow count 2, got %d", ring.Overflow())
	}
}

func TestTriRingThrottleConcurrency(t *testing.T) {
	ring := NewTriRingThrottle()
	var wg sync.WaitGroup

	stop := make(chan struct{})

	// Writer goroutine
	wg.Add(1)
	go func() {
		defer wg.Done()
		chunk := []byte("concurrency-sample-data\n")
		for {
			select {
			case <-stop:
				return
			default:
				ring.WriteIngress(chunk)
			}
		}
	}()

	// Flusher goroutine (simulating 60 Hz ticker)
	wg.Add(1)
	go func() {
		defer wg.Done()
		for i := 0; i < 50; i++ {
			_ = ring.SwapAndFlush()
		}
		close(stop)
	}()

	wg.Wait()

	if ring.Epoch() < 50 {
		t.Fatalf("expected at least 50 epochs, got %d", ring.Epoch())
	}
}

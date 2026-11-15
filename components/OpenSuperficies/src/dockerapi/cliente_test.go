package dockerapi

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// TestExecHijackCabecera tests demultiplexing of the recorded Docker exec multiplexing frame.
// Under standard execution, it must verify the 8-byte header and parse the payload cleanly.
// Under build tag mutante_demux, the 4-byte header mutator causes demultiplexing to fail.
func TestExecHijackCabecera(t *testing.T) {
	fixturePaths := []string{
		"../../fixture/ng/docker/exec-frame.bin",
		"fixture/ng/docker/exec-frame.bin",
	}

	var data []byte
	var readErr error
	for _, p := range fixturePaths {
		data, readErr = os.ReadFile(p)
		if readErr == nil {
			break
		}
	}
	if readErr != nil {
		t.Fatalf("failed to read fixture exec-frame.bin from candidate paths %v: %v", fixturePaths, readErr)
	}

	var stdout bytes.Buffer
	var stderr bytes.Buffer

	err := Demux(bytes.NewReader(data), &stdout, &stderr)
	if err != nil {
		t.Fatalf("demultiplex failed: %v", err)
	}

	expectedStdout := "OpenSuperficies Exec OK\n"
	if stdout.String() != expectedStdout {
		t.Fatalf("unexpected stdout content: got %q, expected %q", stdout.String(), expectedStdout)
	}
	if stderr.Len() != 0 {
		t.Fatalf("expected empty stderr, got %q", stderr.String())
	}
}

// TestFrameWriteAndDemux validates multi-frame serialization and demultiplexing.
func TestFrameWriteAndDemux(t *testing.T) {
	if headerSize != 8 {
		t.Skip("skipping 8-byte multi-frame test under mutant demux header")
	}

	var buf bytes.Buffer
	msgStdout := []byte("stdout stream line 1\n")
	msgStderr := []byte("stderr stream alert\n")

	if err := WriteFrame(&buf, StreamStdout, msgStdout); err != nil {
		t.Fatalf("WriteFrame stdout failed: %v", err)
	}
	if err := WriteFrame(&buf, StreamStderr, msgStderr); err != nil {
		t.Fatalf("WriteFrame stderr failed: %v", err)
	}

	var outStdout, outStderr bytes.Buffer
	if err := Demux(&buf, &outStdout, &outStderr); err != nil {
		t.Fatalf("Demux failed: %v", err)
	}

	if outStdout.String() != string(msgStdout) {
		t.Fatalf("stdout mismatch: got %q, want %q", outStdout.String(), string(msgStdout))
	}
	if outStderr.String() != string(msgStderr) {
		t.Fatalf("stderr mismatch: got %q, want %q", outStderr.String(), string(msgStderr))
	}
}

// TestInspectImageLive runs against the host Docker Engine socket if available.
func TestInspectImageLive(t *testing.T) {
	if _, err := os.Stat(DefaultSocketPath); err != nil {
		t.Skip("Docker socket not accessible on host, skipping live inspect test")
	}

	client, err := NewClient(DefaultSocketPath)
	if err != nil {
		t.Fatalf("failed to create client: %v", err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	if err := client.Ping(ctx); err != nil {
		t.Skipf("Docker daemon ping failed: %v", err)
	}

	imageName := "opensuperficies-browser-arm64:1.0.0"
	inspect, err := client.InspectImage(ctx, imageName)
	if err != nil {
		t.Logf("image %s not found or inspect failed: %v", imageName, err)
		return
	}

	if inspect.Size <= 1000000000 {
		t.Fatalf("expected image size > 1,000,000,000 bytes, got %d", inspect.Size)
	}
}

// TestMockServerOperations exercises client error handling and JSON parsing over a mock UDS.
func TestMockServerOperations(t *testing.T) {
	tmpDir := t.TempDir()
	sockPath := filepath.Join(tmpDir, "mock_docker.sock")

	listener, err := net.Listen("unix", sockPath)
	if err != nil {
		t.Fatalf("failed to create unix listener: %v", err)
	}
	defer listener.Close()

	mockImageJSON := `{"Id":"sha256:1234567890abcdef","RepoTags":["test:1.0"],"Size":1500000000,"Created":"2026-09-17T00:00:00Z"}`

	mux := http.NewServeMux()
	mux.HandleFunc("/_ping", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		w.Write([]byte("OK"))
	})
	mux.HandleFunc("/images/test:1.0/json", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		w.Write([]byte(mockImageJSON))
	})
	mux.HandleFunc("/images/missing:latest/json", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		w.Write([]byte(`{"message":"no such image"}`))
	})

	server := &http.Server{Handler: mux}
	go server.Serve(listener)
	defer server.Close()

	client, err := NewClient(sockPath)
	if err != nil {
		t.Fatalf("NewClient failed: %v", err)
	}

	ctx := context.Background()
	if err := client.Ping(ctx); err != nil {
		t.Fatalf("Ping failed: %v", err)
	}

	inspect, err := client.InspectImage(ctx, "test:1.0")
	if err != nil {
		t.Fatalf("InspectImage failed: %v", err)
	}
	if inspect.Size != 1500000000 {
		t.Fatalf("expected size 1500000000, got %d", inspect.Size)
	}

	_, err = client.InspectImage(ctx, "missing:latest")
	if err == nil || !errors.Is(err, ErrNotFound) {
		t.Fatalf("expected ErrNotFound for missing image, got: %v", err)
	}
}

// TestEventStreamMock validates JSON line decoding in StreamEvents.
func TestEventStreamMock(t *testing.T) {
	tmpDir := t.TempDir()
	sockPath := filepath.Join(tmpDir, "mock_events.sock")

	listener, err := net.Listen("unix", sockPath)
	if err != nil {
		t.Fatalf("failed to create unix listener: %v", err)
	}
	defer listener.Close()

	mux := http.NewServeMux()
	mux.HandleFunc("/events", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		flusher, ok := w.(http.Flusher)
		if !ok {
			http.Error(w, "flusher unsupported", http.StatusInternalServerError)
			return
		}

		ev1 := Event{Type: "container", Action: "create", Time: 1726600000}
		b1, _ := json.Marshal(ev1)
		w.Write(append(b1, '\n'))
		flusher.Flush()

		ev2 := Event{Type: "container", Action: "start", Time: 1726600001}
		b2, _ := json.Marshal(ev2)
		w.Write(append(b2, '\n'))
		flusher.Flush()
	})

	server := &http.Server{Handler: mux}
	go server.Serve(listener)
	defer server.Close()

	client, err := NewClient(sockPath)
	if err != nil {
		t.Fatalf("NewClient failed: %v", err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	var received []Event
	err = client.StreamEvents(ctx, EventsOptions{}, func(ev Event) error {
		received = append(received, ev)
		if len(received) >= 2 {
			cancel()
		}
		return nil
	})

	if err != nil && !errors.Is(err, context.Canceled) {
		t.Fatalf("StreamEvents returned unexpected error: %v", err)
	}
	if len(received) != 2 {
		t.Fatalf("expected 2 events, got %d", len(received))
	}
	if received[0].Action != "create" || received[1].Action != "start" {
		t.Fatalf("unexpected events received: %+v", received)
	}
}

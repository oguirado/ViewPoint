package w8muse

import (
	"context"
	"io"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// This is a fake UDS peer, never the installed seat or a Docker daemon.
func TestSocketSeatWireCarriesOnlyDeclaredStreams(t *testing.T) {
	root := scratch(t)
	path := filepath.Join(root, "peer.sock")
	listener, e := net.Listen("unix", path)
	if e != nil {
		t.Fatal(e)
	}
	defer listener.Close()
	done := make(chan error, 1)
	go func() {
		conn, e := listener.Accept()
		if e != nil {
			done <- e
			return
		}
		defer conn.Close()
		_ = conn.SetDeadline(time.Now().Add(2 * time.Second))
		var req Request
		if e = ReadFrame(conn, &req); e != nil {
			done <- e
			return
		}
		b := make([]byte, req.BundleLen+req.PatchLen)
		_, e = io.ReadFull(conn, b)
		if e == nil && (req.Verb != "submit" || string(b) != "bundlepatch") {
			e = io.ErrUnexpectedEOF
		}
		if e == nil {
			e = WriteFrame(conn, Response{ExitCode: 0, Handle: "fake-handle"})
		}
		done <- e
	}()
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	response, e := (SocketSeat{Path: path, Timeout: time.Second}).Exchange(ctx, Request{Verb: "submit", WorkerTask: []byte(`{}`), BundleLen: 6, PatchLen: 5}, strings.NewReader("bundle"), []byte("patch"))
	if e != nil || response.Handle != "fake-handle" {
		t.Fatalf("%+v %v", response, e)
	}
	if e = <-done; e != nil {
		t.Fatal(e)
	}
}
func TestLiveEncodeBeforeCanonRc4(t *testing.T) {
	cfg, item, seat, _, _ := fixture(t)
	// The registered writer surface is valid for dry execution but never live.
	cfg.Dry = false
	_, e := Run(context.Background(), cfg, item)
	if ExitCode(e) != 4 || len(seat.calls) != 0 {
		t.Fatalf("%v calls=%v", e, seat.calls)
	}
}
func TestCollectionModeAndAckOrdering(t *testing.T) {
	cfg, item, s, _, _ := fixture(t)
	cfg.Seat = seatFunc(func(ctx context.Context, req Request, b io.Reader, p []byte) (Response, error) {
		if req.Verb == "ack" {
			dir := filepath.Join(cfg.CampaignDir, "w8-muse", "receipts", item.TaskID, item.Attempt)
			for _, name := range []string{"receipt.json", "report.md", "driver.json"} {
				st, e := os.Stat(filepath.Join(dir, name))
				if e != nil || st.Mode().Perm() != 0644 {
					t.Errorf("ack before durable 0644 file: %s %v", name, e)
				}
			}
		}
		return s.Exchange(ctx, req, b, p)
	})
	_, e := Run(context.Background(), cfg, item)
	if e != nil {
		t.Fatal(e)
	}
}

// A seat that refuses after the header answers and closes before reading the
// bundle; the driver's write then fails, but the reply is authoritative.
func TestSocketSeatEarlyRefusalIsReadNotSeatLost(t *testing.T) {
	for _, reply := range []bool{true, false} {
		root := scratch(t)
		path := filepath.Join(root, "peer.sock")
		listener, e := net.Listen("unix", path)
		if e != nil {
			t.Fatal(e)
		}
		go func() {
			conn, e := listener.Accept()
			if e != nil {
				return
			}
			var req Request
			if ReadFrame(conn, &req) == nil && reply {
				_ = WriteFrame(conn, Response{ExitCode: 2, Reason: "contract"})
			}
			_ = conn.Close()
		}()
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		big := strings.Repeat("b", 32<<20)
		response, e := (SocketSeat{Path: path, Timeout: 5 * time.Second}).Exchange(ctx, Request{Verb: "submit", WorkerTask: []byte(`{}`), BundleLen: int64(len(big))}, strings.NewReader(big), nil)
		cancel()
		listener.Close()
		if reply && (e != nil || response.ExitCode != 2 || response.Reason != "contract") {
			t.Fatalf("early refusal lost: %+v %v", response, e)
		}
		if !reply && e == nil {
			t.Fatalf("silent close must stay a transport error: %+v", response)
		}
	}
}

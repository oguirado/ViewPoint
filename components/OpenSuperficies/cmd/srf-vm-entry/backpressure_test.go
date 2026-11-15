package main

import (
	"bytes"
	"context"
	"encoding/json"
	"net"
	"testing"
	"time"

	"opensuperficies.local/srf/src/vmsurface/proto"
)

func TestHubSlowPairBackpressure(t *testing.T) {
	for _, overrun := range []bool{false, true} {
		t.Run(map[bool]string{false: "exact", true: "overrun"}[overrun], func(t *testing.T) {
			guest, peer := net.Pipe()
			h := newHub(guest, 4)
			defer h.close()
			defer peer.Close()
			local, shim := net.Pipe()
			defer shim.Close()
			ctx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
			defer cancel()
			go relayOne(ctx, local, h)
			_ = peer.SetDeadline(time.Now().Add(4 * time.Second))
			_ = shim.SetDeadline(time.Now().Add(4 * time.Second))
			// Exact live pair size, including both length headers.
			pair := capPair(4609676)
			for i := 8; i < 8+4609676; i++ {
				pair[i] = byte(i)
			}
			if overrun {
				pair = append(pair, 42)
			}
			finished := make(chan struct{})
			go func() {
				defer close(finished)
				f, err := proto.ReadFrame(peer)
				if err != nil {
					return
				}
				for off := 0; off < len(pair); off += 64 << 10 {
					end := off + 64<<10
					if end > len(pair) {
						end = len(pair)
					}
					if proto.WriteFrame(peer, proto.Frame{Type: proto.TStdout, Stream: f.Stream, Payload: pair[off:end]}) != nil {
						return
					}
				}
				_ = proto.WriteFrame(peer, proto.Frame{Type: proto.TExit, Stream: f.Stream, Payload: []byte(`{"rc":0}`)})
				// Drain the relay's cancellation so its deferred send cannot linger.
				_, _ = proto.ReadFrame(peer)
			}()
			defer func() {
				peer.Close()
				select {
				case <-finished:
				case <-ctx.Done():
					t.Error("guest goroutine survived")
				}
			}()
			p, _ := json.Marshal(proto.Open{Argv: []string{"srf-fbread", "pair", ":100", "0"}})
			if err := proto.WriteFrame(shim, proto.Frame{Type: proto.TOpen, Stream: 1, Payload: p}); err != nil {
				t.Fatal(err)
			}
			var out bytes.Buffer
			for {
				f, err := proto.ReadFrame(shim)
				if err != nil {
					t.Fatal(err)
				}
				if f.Type == proto.TExit {
					want := 0
					if overrun {
						want = 5
					}
					var exit proto.Exit
					if json.Unmarshal(f.Payload, &exit) != nil || exit.RC != want {
						t.Fatalf("exit=%s want rc %d", f.Payload, want)
					}
					if !overrun && !bytes.Equal(pair, out.Bytes()) {
						t.Fatalf("pair changed: received %d of %d", out.Len(), len(pair))
					}
					break
				}
				if f.Type != proto.TStdout {
					t.Fatalf("unexpected type %d", f.Type)
				}
				out.Write(f.Payload)
				time.Sleep(5 * time.Millisecond)
			}
		})
	}
}

func TestHubBackpressureRemainingDeadline(t *testing.T) {
	a, peer := net.Pipe()
	h := newHub(a, 4)
	defer h.close()
	defer peer.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()
	s, err := h.openContext(ctx)
	if err != nil {
		t.Fatal(err)
	}
	_ = peer.SetDeadline(time.Now().Add(time.Second))
	start := time.Now()
	for i := 0; i < 17; i++ {
		if err := proto.WriteFrame(peer, proto.Frame{Type: proto.TStdout, Stream: s.id, Payload: make([]byte, proto.MaxPayload)}); err != nil {
			t.Fatal(err)
		}
	}
	select {
	case <-s.done:
	case <-time.After(time.Second):
		t.Fatal("remaining deadline ignored")
	}
	if elapsed := time.Since(start); elapsed < 150*time.Millisecond || elapsed > 900*time.Millisecond {
		t.Fatalf("wait=%v", elapsed)
	}
	if s.terminal.Load() != 6 || s.queued.Load() != 16<<20 {
		t.Fatalf("terminal=%d queued=%d", s.terminal.Load(), s.queued.Load())
	}
	// A stream behind the stalled one is delayed, but its payload is intact.
	other, err := h.open()
	if err != nil {
		t.Fatal(err)
	}
	if err := proto.WriteFrame(peer, proto.Frame{Type: proto.TStdout, Stream: other.id, Payload: []byte("intact")}); err != nil {
		t.Fatal(err)
	}
	select {
	case f := <-other.frames:
		if string(f.Payload) != "intact" {
			t.Fatal("other stream corrupted")
		}
	case <-time.After(time.Second):
		t.Fatal("other stream not resumed")
	}
}

func TestHubFullQueueResumesWithoutDropping(t *testing.T) {
	a, peer := net.Pipe()
	h := newHub(a, 4)
	defer h.close()
	defer peer.Close()
	s, err := h.open()
	if err != nil {
		t.Fatal(err)
	}
	_ = peer.SetDeadline(time.Now().Add(3 * time.Second))
	sent := make(chan error, 1)
	go func() {
		for i := 0; i < 18; i++ {
			p := bytes.Repeat([]byte{byte(i)}, proto.MaxPayload)
			if err := proto.WriteFrame(peer, proto.Frame{Type: proto.TStdout, Stream: s.id, Payload: p}); err != nil {
				sent <- err
				return
			}
		}
		sent <- proto.WriteFrame(peer, proto.Frame{Type: proto.TExit, Stream: s.id, Payload: []byte(`{"rc":0}`)})
	}()
	defer func() {
		peer.Close()
		select {
		case <-sent:
		case <-time.After(time.Second):
			t.Error("producer survived")
		}
	}()
	// Wait until the byte cap is reached without relying on goroutine timing.
	until := time.NewTimer(time.Second)
	defer until.Stop()
	tick := time.NewTicker(time.Millisecond)
	defer tick.Stop()
	for s.queued.Load() != 16<<20 {
		select {
		case <-tick.C:
		case <-until.C:
			t.Fatal("queue did not reach byte cap")
		}
	}
	for i := 0; i < 19; i++ {
		select {
		case f := <-s.frames:
			if i < 18 {
				if f.Type != proto.TStdout || !bytes.Equal(f.Payload, bytes.Repeat([]byte{byte(i)}, proto.MaxPayload)) {
					t.Fatalf("frame %d changed", i)
				}
			} else if f.Type != proto.TExit {
				t.Fatal("EXIT not last")
			}
			s.consumed(len(f.Payload))
		case <-s.done:
			t.Fatalf("stream terminated rc=%d", s.terminal.Load())
		case <-time.After(time.Second):
			t.Fatal("queue did not resume")
		}
	}
}

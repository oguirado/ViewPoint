package main

import (
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"io"
	"net"
	"strings"
	"testing"
	"time"

	"opensuperficies.local/srf/src/vmsurface/proto"
)

func capPair(n int) []byte {
	p := make([]byte, 16+2*n)
	binary.BigEndian.PutUint64(p, uint64(n))
	binary.BigEndian.PutUint64(p[8+n:], uint64(n))
	return p
}

// Both paths face a hostile peer directly: client tests cannot rely on the hub,
// and relay tests cannot rely on a cooperative shim or guest-side policy.
func TestHostStdoutCaps(t *testing.T) {
	big := make([]byte, 8)
	binary.BigEndian.PutUint64(big, 20<<20)
	for _, path := range []string{"client", "hub"} {
		for _, tc := range []struct {
			name    string
			argv    []string
			data    []byte
			forever bool
			want    int
		}{
			{"generic-forever", []string{"xdotool", "getwindowfocus"}, bytes.Repeat([]byte("x"), 32768), true, 5},
			{"pair-forever", []string{"srf-fbread", "pair", ":100", "0"}, capPair(4), true, 5},
			{"pair-large-header", []string{"srf-fbread", "pair", ":100", "0"}, big, false, 5},
			{"pair-truncated", []string{"srf-fbread", "pair", ":100", "0"}, capPair(4)[:12], false, 5},
			{"pair-valid", []string{"srf-fbread", "pair", ":100", "0"}, capPair(4609676), false, 0},
		} {
			t.Run(path+"/"+tc.name, func(t *testing.T) {
				ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
				defer cancel()
				a, peer := net.Pipe()
				defer peer.Close()
				_ = peer.SetDeadline(time.Now().Add(3 * time.Second))
				if path == "hub" {
					h := newHub(a, 4)
					defer h.close()
					local, shim := net.Pipe()
					go relayOne(ctx, local, h)
					a = shim
				}
				consumed := make(chan struct{}, 1)
				cancelled := make(chan struct{})
				guestDone := make(chan struct{})
				go func() {
					defer close(guestDone)
					f, err := proto.ReadFrame(peer)
					if err != nil {
						return
					}
					go func() {
						for {
							f, err := proto.ReadFrame(peer)
							if err != nil {
								return
							}
							if f.Type == proto.TCancel {
								close(cancelled)
								return
							}
						}
					}()
					for {
						for off := 0; off < len(tc.data); {
							end := off + 32768
							if end > len(tc.data) {
								end = len(tc.data)
							}
							if proto.WriteFrame(peer, proto.Frame{Type: proto.TStdout, Stream: f.Stream, Payload: tc.data[off:end]}) != nil {
								return
							}
							off = end
							if !tc.forever {
								select {
								case <-consumed:
								case <-cancelled:
									return
								case <-ctx.Done():
									return
								}
							}
						}
						if !tc.forever {
							break
						}
					}
					_ = proto.WriteFrame(peer, proto.Frame{Type: proto.TExit, Stream: f.Stream, Payload: []byte(`{"rc":0}`)})
				}()
				var out bytes.Buffer
				output := writerFunc(func(p []byte) (int, error) {
					if out.Len()+len(p) > pairStdoutLimit {
						return 0, io.ErrShortWrite
					}
					n, err := out.Write(p)
					if !tc.forever {
						consumed <- struct{}{}
					}
					return n, err
				})
				var rc int
				if path == "client" {
					rc = client(ctx, a, proto.Open{Argv: tc.argv}, io.NopCloser(strings.NewReader("")), output, io.Discard)
				} else {
					p, _ := json.Marshal(proto.Open{Argv: tc.argv})
					_ = a.SetDeadline(time.Now().Add(3 * time.Second))
					if err := proto.WriteFrame(a, proto.Frame{Type: proto.TOpen, Stream: 1, Payload: p}); err != nil {
						t.Fatal(err)
					}
					for {
						f, err := proto.ReadFrame(a)
						if err != nil {
							t.Fatal(err)
						}
						if f.Type == proto.TStdout {
							output.Write(f.Payload)
						}
						if f.Type == proto.TExit {
							var x proto.Exit
							_ = json.Unmarshal(f.Payload, &x)
							rc = x.RC
							break
						}
					}
					a.Close()
				}
				if rc != tc.want {
					t.Fatalf("rc=%d want=%d bytes=%d", rc, tc.want, out.Len())
				}
				if tc.want == 0 && !bytes.Equal(out.Bytes(), tc.data) {
					t.Fatal("pair changed")
				}
				if tc.want == 5 {
					select {
					case <-cancelled:
					case <-ctx.Done():
						t.Fatal("missing CANCEL")
					}
				}
				peer.Close()
				select {
				case <-guestDone:
				case <-ctx.Done():
					t.Fatal("guest survived")
				}
			})
		}
	}
}

func TestHubBufferedPayloadBound(t *testing.T) {
	a, peer := net.Pipe()
	h := newHub(a, 4)
	defer h.close()
	defer peer.Close()
	_ = peer.SetDeadline(time.Now().Add(4 * time.Second))
	s, err := h.open()
	if err != nil {
		t.Fatal(err)
	}
	payload := make([]byte, proto.MaxPayload)
	start := time.Now()
	for i := 0; i < 17; i++ {
		if err := proto.WriteFrame(peer, proto.Frame{Type: proto.TStdout, Stream: s.id, Payload: payload}); err != nil {
			t.Fatal(err)
		}
	}
	select {
	case <-s.done:
	case <-time.After(3 * time.Second):
		t.Fatal("endless guest not stopped")
	}
	if elapsed := time.Since(start); elapsed < 1800*time.Millisecond || elapsed > 3500*time.Millisecond {
		t.Fatalf("default back-pressure wait=%v", elapsed)
	}
	if rc := s.terminal.Load(); rc != 6 {
		t.Fatalf("rc=%d", rc)
	}
	// queued includes the frame currently being relayed; ReadFrame holds at
	// most one additional MaxPayload frame. No whole stdout is accumulated.
	buffered := s.queued.Load() + proto.MaxPayload
	if buffered > (16<<20)+proto.MaxPayload {
		t.Fatalf("buffered=%d", buffered)
	}
}

func TestHubOwnDeadline(t *testing.T) {
	a, peer := net.Pipe()
	h := newHub(a, 4)
	defer h.close()
	defer peer.Close()
	local, shim := net.Pipe()
	defer shim.Close()
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	go relayOne(ctx, local, h)
	cancelled := make(chan struct{})
	go func() {
		for {
			f, err := proto.ReadFrame(peer)
			if err != nil {
				return
			}
			if f.Type == proto.TCancel {
				close(cancelled)
				return
			}
		}
	}()
	_ = shim.SetDeadline(time.Now().Add(time.Second))
	p, _ := json.Marshal(proto.Open{Argv: []string{"srf-fbread", "pair", ":100", "0"}, TimeoutMS: 20})
	if err := proto.WriteFrame(shim, proto.Frame{Type: proto.TOpen, Stream: 1, Payload: p}); err != nil {
		t.Fatal(err)
	}
	f, err := proto.ReadFrame(shim)
	if err != nil || f.Type != proto.TExit || string(f.Payload) != `{"rc":6}` {
		t.Fatalf("%+v %v", f, err)
	}
	select {
	case <-cancelled:
	case <-ctx.Done():
		t.Fatal("deadline did not CANCEL guest")
	}
}

func TestStdoutBoundFragmentedHeadersAndRecord(t *testing.T) {
	p := capPair(31)
	b := newStdoutBound(proto.Open{Argv: []string{"srf-fbread"}})
	for _, v := range p {
		if !b.accept([]byte{v}) {
			t.Fatal("split header rejected")
		}
	}
	if !b.complete() || b.accept([]byte{0}) {
		t.Fatal("pair completion/trailing bytes")
	}
	b = newStdoutBound(proto.Open{StdinCap: -1})
	chunk := make([]byte, proto.MaxPayload)
	for i := 0; i < 32; i++ {
		if !b.accept(chunk) {
			t.Fatal("record total capped")
		}
	}
	if b.total != 0 {
		t.Fatal("unbounded record counter")
	}
}

func TestStdoutBoundCombinedLengths(t *testing.T) {
	for _, n := range []uint64{pairStdoutLimit, ^uint64(0)} {
		b := newStdoutBound(proto.Open{Argv: []string{"srf-fbread"}})
		var header [8]byte
		// Even an empty first leg leaves both framing headers in the budget.
		if !b.accept(header[:]) {
			t.Fatal("first header")
		}
		binary.BigEndian.PutUint64(header[:], n)
		if b.accept(header[:]) {
			t.Fatalf("accepted second length %d", n)
		}
	}
	b := newStdoutBound(proto.Open{})
	if !b.accept(make([]byte, proto.MaxPayload)) || b.accept([]byte{0}) {
		t.Fatal("generic stdout boundary")
	}
}

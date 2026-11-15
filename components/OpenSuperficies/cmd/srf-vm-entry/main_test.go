package main

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net"
	"opensuperficies.local/srf/src/vmsurface/proto"
	"reflect"
	"strings"
	"testing"
	"time"
)

func TestSettings(t *testing.T) {
	base := map[string]string{"SRF_PROGRAM": "notepad-vm", "SRF_WINDOWS": "1", "SRF_TILES": `[{"id":"w1","x":0,"y":0,"w":400,"h":300}]`, "SRF_GUEST_CPUS": "4", "SRF_GUEST_MEM_MIB": "6144", "SRF_PROFILE_DISK_MIB": "1024", "NO_AT_BRIDGE": "0"}
	c, err := settings(base)
	if err != nil {
		t.Fatal(err)
	}
	if c.CPUs != 4 || c.MemoryMiB != 6144 || c.DiskMiB != 1024 {
		t.Fatal(c)
	}
	for _, key := range []string{"SRF_PROGRAM", "SRF_WINDOWS", "SRF_TILES", "SRF_GUEST_CPUS", "SRF_GUEST_MEM_MIB", "SRF_PROFILE_DISK_MIB", "NO_AT_BRIDGE"} {
		bad := map[string]string{}
		for k, v := range base {
			bad[k] = v
		}
		bad[key] = "bad"
		if _, err := settings(bad); err == nil {
			t.Fatal("accepted", key)
		}
	}
	bad := map[string]string{}
	for k, v := range base {
		bad[k] = v
	}
	bad["SRF_TILES"] = `[{"id":"w1","x":0,"y":0,"w":400,"h":300,"other":0}]`
	if _, err := settings(bad); err == nil {
		t.Fatal("unknown tile field")
	}
}
func TestReadinessOrderedSingleWrites(t *testing.T) {
	var writes []string
	w := writerFunc(func(p []byte) (int, error) { writes = append(writes, string(p)); return len(p), nil })
	src := "SRF1 OUT PROGRAM_READY {\"windows\":[]}\nSRF1 OUT xpra layer up\nSRF1 OUT xpra layer up\n"
	if err := relayControl(strings.NewReader(src), w); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(writes, []string{"xpra layer up\n", "PROGRAM_READY {\"windows\":[]}\n"}) {
		t.Fatal(writes)
	}
}
func TestClientFidelity(t *testing.T) {
	a, b := net.Pipe()
	defer b.Close()
	go func() {
		f, e := proto.ReadFrame(b)
		if e != nil {
			return
		}
		var o proto.Open
		_ = json.Unmarshal(f.Payload, &o)
		var input []byte
		for {
			f, e = proto.ReadFrame(b)
			if e != nil {
				return
			}
			if f.Type == proto.TStdinEOF {
				break
			}
			input = append(input, f.Payload...)
		}
		_ = proto.WriteFrame(b, proto.Frame{Type: proto.TStdout, Stream: 1, Payload: input})
		_ = proto.WriteFrame(b, proto.Frame{Type: proto.TStderr, Stream: 1, Payload: []byte(strings.Repeat("x", 5000))})
		p, _ := json.Marshal(proto.Exit{RC: 7})
		_ = proto.WriteFrame(b, proto.Frame{Type: proto.TExit, Stream: 1, Payload: p})
	}()
	var out, errout bytes.Buffer
	rc := client(context.Background(), a, proto.Open{Argv: []string{"xdotool", "getwindowfocus"}}, io.NopCloser(strings.NewReader("payload")), &out, &errout)
	if rc != 7 || out.String() != "payload" || errout.Len() != 4096 {
		t.Fatalf("rc %d out %q stderr %d", rc, out.String(), errout.Len())
	}
}
func TestClientDeadPeer(t *testing.T) {
	a, b := net.Pipe()
	b.Close()
	if rc := client(context.Background(), a, proto.Open{}, io.NopCloser(strings.NewReader("")), io.Discard, io.Discard); rc != 6 {
		t.Fatal(rc)
	}
}
func TestMuxConcurrent(t *testing.T) {
	a, b := net.Pipe()
	h := newHub(a, 32)
	defer h.close()
	defer b.Close()
	s1, e := h.open()
	if e != nil {
		t.Fatal(e)
	}
	s2, e := h.open()
	if e != nil {
		t.Fatal(e)
	}
	go func() {
		_ = proto.WriteFrame(b, proto.Frame{Type: proto.TViewData, Stream: s2.id, Payload: []byte("second")})
		_ = proto.WriteFrame(b, proto.Frame{Type: proto.TViewData, Stream: s1.id, Payload: []byte("first")})
	}()
	for _, s := range []*stream{s1, s2} {
		select {
		case f := <-s.frames:
			if f.Stream != s.id {
				t.Fatal(f)
			}
		case <-time.After(time.Second):
			t.Fatal("blocked stream")
		}
	}
	for i := 0; i < 30; i++ {
		if _, err := h.open(); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := h.open(); err == nil {
		t.Fatal("view cap exceeded")
	}
}

type writerFunc func([]byte) (int, error)

func (f writerFunc) Write(p []byte) (int, error) { return f(p) }

func TestRelayStreamsAndCancel(t *testing.T) {
	guest, peer := net.Pipe()
	h := newHub(guest, 128)
	defer h.close()
	defer peer.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	local, shim := net.Pipe()
	done := make(chan struct{})
	go func() { relayOne(ctx, local, h); close(done) }()
	guestCancel := make(chan bool, 1)
	go func() {
		open, e := proto.ReadFrame(peer)
		if e != nil {
			return
		}
		_ = proto.WriteFrame(peer, proto.Frame{Type: proto.TStdout, Stream: open.Stream, Payload: []byte("from guest")})
		for {
			f, e := proto.ReadFrame(peer)
			if e != nil {
				return
			}
			if f.Type == proto.TCancel {
				guestCancel <- true
				return
			}
		}
	}()
	if e := proto.WriteFrame(shim, proto.Frame{Type: proto.TOpen, Stream: 99, Payload: []byte(`{"argv":["xdotool","getwindowfocus"]}`)}); e != nil {
		t.Fatal(e)
	}
	f, e := proto.ReadFrame(shim)
	if e != nil || f.Stream != 99 || string(f.Payload) != "from guest" {
		t.Fatal(f, e)
	}
	shim.Close()
	select {
	case <-guestCancel:
	case <-time.After(time.Second):
		t.Fatal("no CANCEL after local EOF")
	}
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("relay outlived local peer")
	}
}
func TestViewRoundTrip(t *testing.T) {
	guest, peer := net.Pipe()
	h := newHub(guest, 32)
	defer h.close()
	defer peer.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	local, browser := net.Pipe()
	done := make(chan struct{})
	go func() { relayView(ctx, local, h); close(done) }()
	go func() {
		f, e := proto.ReadFrame(peer)
		if e != nil || f.Type != proto.TViewOpen {
			return
		}
		f, e = proto.ReadFrame(peer)
		if e != nil {
			return
		}
		f.Payload = append([]byte("reply:"), f.Payload...)
		_ = proto.WriteFrame(peer, f)
		for {
			f, e = proto.ReadFrame(peer)
			if e != nil || f.Type == proto.TViewClose {
				return
			}
		}
	}()
	if _, e := browser.Write([]byte("GET /")); e != nil {
		t.Fatal(e)
	}
	b := make([]byte, 11)
	if _, e := io.ReadFull(browser, b); e != nil || string(b) != "reply:GET /" {
		t.Fatal(string(b), e)
	}
	browser.Close()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("view did not close")
	}
}
func TestClientCancellation(t *testing.T) {
	a, b := net.Pipe()
	defer b.Close()
	ctx, cancel := context.WithCancel(context.Background())
	seen := make(chan struct{})
	go func() { _, _ = proto.ReadFrame(b); close(seen); _, _ = proto.ReadFrame(b) }()
	in, writer := io.Pipe()
	defer writer.Close()
	done := make(chan int, 1)
	go func() { done <- client(ctx, a, proto.Open{}, in, io.Discard, io.Discard) }()
	<-seen
	cancel()
	select {
	case rc := <-done:
		if rc != 6 {
			t.Fatal(rc)
		}
	case <-time.After(time.Second):
		t.Fatal("cancel hung")
	}
}

func TestExactTileNegative(t *testing.T) {
	for _, raw := range []string{`{"id":"w","y":0,"w":1,"h":1}`, `{"id":"w","x":0,"x":1,"y":0,"w":1,"h":1}`, `{"ID":"w","x":0,"y":0,"w":1,"h":1}`, `{"id":"w","x":null,"y":0,"w":1,"h":1}`} {
		if exactTile([]byte(raw)) {
			t.Fatal("accepted", raw)
		}
	}
}

func TestMalformedGuestFrameReturnsFive(t *testing.T) {
	guest, peer := net.Pipe()
	h := newHub(guest, 128)
	defer h.close()
	defer peer.Close()
	local, shim := net.Pipe()
	defer shim.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go relayOne(ctx, local, h)
	go func() {
		_, e := proto.ReadFrame(peer)
		if e == nil {
			_, _ = peer.Write([]byte("BAD!\x04\x00\x00\x00\x01\x00\x00\x00\x00"))
		}
	}()
	_ = proto.WriteFrame(shim, proto.Frame{Type: proto.TOpen, Stream: 4, Payload: []byte(`{"argv":["xdotool","getwindowfocus"]}`)})
	_ = shim.SetReadDeadline(time.Now().Add(time.Second))
	f, e := proto.ReadFrame(shim)
	if e != nil || f.Type != proto.TExit || string(f.Payload) != `{"rc":5}` {
		t.Fatal(f, e)
	}
}
func TestMalformedLocalFrameReturnsFive(t *testing.T) {
	guest, peer := net.Pipe()
	h := newHub(guest, 128)
	defer h.close()
	defer peer.Close()
	local, shim := net.Pipe()
	defer shim.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go relayOne(ctx, local, h)
	seen := make(chan struct{})
	go func() { _, _ = proto.ReadFrame(peer); close(seen); _, _ = proto.ReadFrame(peer) }()
	_ = proto.WriteFrame(shim, proto.Frame{Type: proto.TOpen, Stream: 4, Payload: []byte(`{"argv":["xdotool","getwindowfocus"]}`)})
	<-seen
	_, _ = shim.Write([]byte("VMA1\xff\x00\x00\x00\x04\x00\x00\x00\x00"))
	_ = shim.SetReadDeadline(time.Now().Add(time.Second))
	f, e := proto.ReadFrame(shim)
	if e != nil || f.Type != proto.TExit || string(f.Payload) != `{"rc":5}` {
		t.Fatal(f, e)
	}
}

func TestShimRejectsBeforeDial(t *testing.T) {
	for _, test := range []struct {
		name string
		args []string
	}{
		{"xdotool", []string{"exec", "/bin/sh"}}, {"python3", []string{"-I", "-c", "print('arbitrary')"}},
		{"srf-fbread", []string{"pair", ":101", "0"}}, {"identity-entrypoint.sh", []string{"boot"}},
	} {
		if rc := run(context.Background(), test.name, test.args); rc != 2 {
			t.Fatalf("%s: %d", test.name, rc)
		}
	}
}

func TestRecordInitialEOFStaysAliveUntilCancel(t *testing.T) {
	a, b := net.Pipe()
	defer b.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	open := make(chan struct{})
	frames := make(chan proto.Type, 2)
	go func() {
		_, _ = proto.ReadFrame(b)
		close(open)
		for {
			f, e := proto.ReadFrame(b)
			if e != nil {
				return
			}
			frames <- f.Type
			if f.Type == proto.TCancel {
				return
			}
		}
	}()
	done := make(chan int, 1)
	go func() {
		done <- client(ctx, a, proto.Open{StdinCap: -1}, io.NopCloser(strings.NewReader("")), io.Discard, io.Discard)
	}()
	<-open
	select {
	case typ := <-frames:
		t.Fatalf("initial record EOF sent %d", typ)
	case <-time.After(30 * time.Millisecond):
	}
	cancel()
	select {
	case typ := <-frames:
		if typ != proto.TCancel {
			t.Fatal(typ)
		}
	case <-time.After(time.Second):
		t.Fatal("missing record CANCEL")
	}
	select {
	case rc := <-done:
		if rc != 6 {
			t.Fatal(rc)
		}
	case <-time.After(time.Second):
		t.Fatal("record shim survived cancellation")
	}
}

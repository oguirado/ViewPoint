package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/binary"
	"errors"
	"io"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"testing/iotest"
	"time"

	"opensuperficies.local/srf/src/ramcap"
)

// streamLeg builds an XWD leg; header (name, byte order) varies independently
// of the canonical RGB24 pixels.
func streamLeg(w, h int, order uint32, name string, rgb []byte) []byte {
	nm := append([]byte(name), 0)
	fields := []uint32{uint32(100 + len(nm)), 7, 2, 24, uint32(w), uint32(h), 0, order, 32, order, 32, 32, uint32(4 * w), 4, 0xff0000, 0xff00, 0xff, 8, 0, 0, uint32(w), uint32(h), 0, 0, 0}
	out := make([]byte, 100+len(nm)+4*w*h)
	for i, v := range fields {
		binary.BigEndian.PutUint32(out[4*i:], v)
	}
	copy(out[100:], nm)
	px := out[100+len(nm):]
	for i := 0; i < w*h; i++ {
		if order == 0 {
			px[4*i+2], px[4*i+1], px[4*i] = rgb[3*i], rgb[3*i+1], rgb[3*i+2]
		} else {
			px[4*i+1], px[4*i+2], px[4*i+3] = rgb[3*i], rgb[3*i+1], rgb[3*i+2]
		}
	}
	return out
}

func streamRGB(w, h int, salt byte) []byte {
	rgb := make([]byte, 3*w*h)
	for i := range rgb {
		rgb[i] = byte(i*5) + salt
	}
	return rgb
}

// streamFake is a race-safe imageServer that counts grabs and checks that
// Image is only called while grabbed.
type streamFake struct {
	mu                        sync.Mutex
	grabbed                   bool
	grabs, ungrabs, closes    int
	ungrabErr                 error
	img                       func(n int) []byte
	imageOutsideGrab, imagesN int
}

func (f *streamFake) Grab() error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.grabs++
	f.grabbed = true
	return nil
}
func (f *streamFake) Ungrab() error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.ungrabs++
	f.grabbed = false
	return f.ungrabErr
}
func (f *streamFake) Image(uint32) ([]byte, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if !f.grabbed {
		f.imageOutsideGrab++
	}
	f.imagesN++
	return f.img(f.imagesN), nil
}
func (f *streamFake) Close() { f.mu.Lock(); f.closes++; f.mu.Unlock() }
func (f *streamFake) counts() (int, int) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.grabs, f.ungrabs
}

const sw, sh = 4, 3

func streamBindingMap() map[string]any {
	return map[string]any{
		"schema": "w8u-binding/1", "run_id": "r1", "lease_id": "l1", "seat_grant_sha256": strings.Repeat("a", 64),
		"container_id": "c1", "target_handle": map[string]any{"kind": "capsule", "canonical_id": "app", "generation": 3},
		"epoch": 2, "display": ":100", "xid": 7, "width": sw, "height": sh,
		"frames_root": ramcap.StreamFramesRoot, "frame_run_root": ramcap.StreamRunParent + "/r1",
		"image_sha256": strings.Repeat("b", 64), "reader_sha256": strings.Repeat("c", 64), "exec_allowlist_sha256": strings.Repeat("d", 64),
		"max_record_bytes": ramcap.RecordCap(sw, sh), "max_inflight_frames": 2, "tick_mode": "unavailable",
	}
}

// rawBinding returns canonical binding bytes; ParseSeatBinding is applied
// separately so tests can also send bindings the sender must refuse.
func rawBinding(t *testing.T, mutate func(map[string]any)) ([]byte, ramcap.SeatBinding) {
	t.Helper()
	m := streamBindingMap()
	if mutate != nil {
		mutate(m)
	}
	raw, err := ramcap.CanonicalJSON(m)
	if err != nil {
		t.Fatal(err)
	}
	b, _ := ramcap.ParseSeatBinding(raw)
	return raw, b
}

type harness struct {
	t      *testing.T
	ctlW   *io.PipeWriter
	dec    *ramcap.StreamDecoder
	hc     *ramcap.HostControl
	rc     chan int
	srv    *streamFake
	opens  atomic.Int32
	reads  atomic.Int32
	frag   bool
	stderr *lockedBuf
}

type lockedBuf struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (l *lockedBuf) Write(p []byte) (int, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.b.Write(p)
}

// start runs the sender over an in-memory duplex. read returns leg A for the
// n-th framebuffer read; the fake server returns leg B.
func start(t *testing.T, bd ramcap.SeatBinding, srv *streamFake, read func(n int) ([]byte, error), frag bool) *harness {
	t.Helper()
	ctlR, ctlW := io.Pipe()
	outR, outW := io.Pipe()
	h := &harness{t: t, ctlW: ctlW, hc: ramcap.NewHostControl(bd), rc: make(chan int, 1), srv: srv, frag: frag, stderr: &lockedBuf{}}
	var src io.Reader = outR
	if frag {
		src = iotest.OneByteReader(outR)
	}
	h.dec = ramcap.NewStreamDecoder(src, bd, h.hc.Ledger())
	var mono atomic.Uint64
	deps := dependencies{
		stdin: ctlR,
		open:  func() (imageServer, error) { h.opens.Add(1); return srv, nil },
		read:  func() ([]byte, error) { return read(int(h.reads.Add(1))) },
		mono:  func() uint64 { return 1000 + mono.Add(1) },
	}
	go func() {
		rc := run([]string{"stream", ":100", "7"}, outW, h.stderr, deps)
		_ = outW.Close()
		h.rc <- rc
	}()
	t.Cleanup(func() { _ = ctlW.Close(); _ = outR.Close() })
	return h
}

func (h *harness) send(b []byte) {
	h.t.Helper()
	if !h.frag {
		if _, err := h.ctlW.Write(b); err != nil {
			h.t.Fatal(err)
		}
		return
	}
	for i := range b {
		if _, err := h.ctlW.Write(b[i : i+1]); err != nil {
			h.t.Fatal(err)
		}
	}
}

// sendAsync is for records the sender may refuse before consuming them whole.
func (h *harness) sendAsync(b []byte) {
	go func() { _, _ = h.ctlW.Write(b) }()
}

func (h *harness) next(want string) ramcap.StreamRecord {
	h.t.Helper()
	r, err := h.dec.Next()
	if err != nil || r.Magic != want {
		h.t.Fatalf("want %s, got %q err %v", want, r.Magic, err)
	}
	return r
}

func (h *harness) ack(seq, total uint64) {
	h.t.Helper()
	b, err := h.hc.Ack(seq, total)
	if err != nil {
		h.t.Fatal(err)
	}
	h.send(b)
}

// end expects a terminal W8UE with code, then EOF, then the exit code.
func (h *harness) end(code ramcap.StreamCode, last uint64, rc int) {
	h.t.Helper()
	r := h.next(ramcap.MagicEnd)
	if r.End.ErrorCode != code || r.End.LastSeq != last || r.End.Sent != last {
		h.t.Fatalf("END %+v, want code %s last %d", *r.End, ramcap.StreamCodeName(code), last)
	}
	if _, err := h.dec.Next(); err != io.EOF {
		h.t.Fatal("no EOF after END", err)
	}
	select {
	case got := <-h.rc:
		if got != rc {
			h.t.Fatalf("rc %d want %d", got, rc)
		}
	case <-time.After(5 * time.Second):
		h.t.Fatal("sender did not exit")
	}
}

func goodLegs(n int) []byte { return streamLeg(sw, sh, 0, "Xvfb screen0", streamRGB(sw, sh, byte(n))) }
func goodImage(n int) []byte {
	return streamLeg(sw, sh, 1, "srf-fbread xid=7", streamRGB(sw, sh, byte(n)))
}

func TestW8UStreamCreditedPairsOverOneChannel(t *testing.T) {
	for _, frag := range []bool{true, false} {
		raw, bd := rawBinding(t, nil)
		srv := &streamFake{img: goodImage}
		h := start(t, bd, srv, func(n int) ([]byte, error) { return goodLegs(n), nil }, frag)
		if !bytes.Equal(h.hc.Hello()[44:], raw) {
			t.Fatal("HELLO body")
		}
		if frag {
			h.send(h.hc.Hello())
		} else {
			// Coalesced: HELLO and the first ACK in one write.
			ack, _ := h.hc.Ack(0, 2)
			h.send(append(h.hc.Hello(), ack...))
		}
		h.next(ramcap.MagicHelloConfirm)
		if g, _ := srv.counts(); g != 0 && frag {
			t.Fatal("HELLO granted credit")
		}
		if frag {
			h.ack(0, 2)
		}
		for seq := 1; seq <= 2; seq++ {
			f := h.next(ramcap.MagicFrame).Frame
			if f.Header.Seq != uint64(seq) || !bytes.Equal(f.RGB24, streamRGB(sw, sh, byte(seq))) || f.Header.CaptureMonoNS != uint64(1000+seq) || f.Header.Flags != ramcap.FlagPairUnderGrab || f.Header.Tick != 0 {
				t.Fatalf("frame %d header %+v", seq, f.Header)
			}
			if bytes.Equal(f.LegA[:120], f.LegB[:120]) {
				t.Fatal("legs not distinct XWDs")
			}
		}
		time.Sleep(30 * time.Millisecond)
		if g, u := srv.counts(); g != 2 || u != 2 {
			t.Fatalf("credit exhaustion did not pause before grab: grabs %d ungrabs %d", g, u)
		}
		h.ack(2, 4)
		for seq := 3; seq <= 4; seq++ {
			if f := h.next(ramcap.MagicFrame).Frame; f.Header.Seq != uint64(seq) {
				t.Fatal("seq", f.Header.Seq)
			}
		}
		h.send(h.hc.Stop())
		h.end(ramcap.StreamOK, 4, 0)
		if g, _ := srv.counts(); g != 4 || h.opens.Load() != 1 || srv.imageOutsideGrab != 0 || srv.closes == 0 {
			t.Fatalf("grabs %d opens %d outside-grab %d closes %d", g, h.opens.Load(), srv.imageOutsideGrab, srv.closes)
		}
	}
}

func TestW8UStreamCaptureMustFail(t *testing.T) {
	for _, tc := range []struct {
		name   string
		read   func(n int) ([]byte, error)
		ungrab error
		mut    func(map[string]any)
		want   ramcap.StreamCode
	}{
		{"leg changes between reads", func(n int) ([]byte, error) { return nil, errors.New("E_TORN") }, nil, nil, ramcap.StreamTorn},
		{"legs differ", func(n int) ([]byte, error) { return goodLegs(n + 1), nil }, nil, nil, ramcap.StreamTorn},
		{"ungrab failure", func(n int) ([]byte, error) { return goodLegs(n), nil }, errors.New("BadAccess"), nil, ramcap.StreamTorn},
		{"geometry mismatch", func(n int) ([]byte, error) { return goodLegs(n), nil }, nil, func(m map[string]any) { m["width"] = sw + 1; m["max_record_bytes"] = ramcap.RecordCap(sw+1, sh) }, ramcap.StreamGeometry},
		{"record above negotiated cap", func(n int) ([]byte, error) { return goodLegs(n), nil }, nil, func(m map[string]any) { m["max_record_bytes"] = 200 }, ramcap.StreamLength},
		{"invalid XWD leg", func(n int) ([]byte, error) { return []byte("not XWD"), nil }, nil, nil, ramcap.StreamWire},
	} {
		_, bd := rawBinding(t, tc.mut)
		srv := &streamFake{img: goodImage, ungrabErr: tc.ungrab}
		h := start(t, bd, srv, tc.read, false)
		h.send(h.hc.Hello())
		h.next(ramcap.MagicHelloConfirm)
		h.ack(0, 2)
		h.end(tc.want, 0, 5)
		if g, u := srv.counts(); g != 1 || u != 1 {
			t.Fatalf("%s: grabs %d ungrabs %d", tc.name, g, u)
		}
		if h.stderr.b.Len() == 0 || h.stderr.b.Len() > 1100 {
			t.Fatalf("%s: diagnostics %d bytes", tc.name, h.stderr.b.Len())
		}
	}
}

func TestW8UStreamHelloMustFail(t *testing.T) {
	good := func(n int) ([]byte, error) { return goodLegs(n), nil }
	raw, bd := rawBinding(t, nil)
	forged := ramcap.EncodeControl(ramcap.MagicControl, ramcap.KindHello, [32]byte{1}, raw)
	committedRaw, _ := rawBinding(t, func(m map[string]any) { m["tick_mode"] = "committed" })
	committed := ramcap.EncodeControl(ramcap.MagicControl, ramcap.KindHello, sha(committedRaw), committedRaw)
	otherXID, _ := rawBinding(t, func(m map[string]any) { m["xid"] = 8 })
	legacy, _ := rawBinding(t, func(m map[string]any) { m["frames_root"] = "/dev/shm/w8/frames" })
	big := ramcap.EncodeControl(ramcap.MagicControl, ramcap.KindHello, bd.SHA256, nil)
	binary.BigEndian.PutUint32(big[8:], ramcap.MaxHelloBodyBytes+1)
	spaced := append(bytes.Clone(raw), ' ')
	for _, tc := range []struct {
		name string
		msg  []byte
		want ramcap.StreamCode
	}{
		{"wrong binding sha", forged, ramcap.StreamBinding},
		{"tick_mode committed", committed, ramcap.StreamTick},
		{"xid differs from argv", ramcap.EncodeControl(ramcap.MagicControl, ramcap.KindHello, sha(otherXID), otherXID), ramcap.StreamBinding},
		{"legacy frame root", ramcap.EncodeControl(ramcap.MagicControl, ramcap.KindHello, sha(legacy), legacy), ramcap.StreamRoot},
		{"oversize HELLO", big, ramcap.StreamLength},
		{"ACK before HELLO", ramcap.EncodeControl(ramcap.MagicControl, ramcap.KindAck, bd.SHA256, ramcap.EncodeAckBody(0, 2)), ramcap.StreamWire},
		{"bad magic", append([]byte("W8UX\x00\x01\x00\x01\x00\x00\x00\x00"), make([]byte, 32)...), ramcap.StreamWire},
		{"bad version", append([]byte("W8UC\x00\x02\x00\x01\x00\x00\x00\x00"), make([]byte, 32)...), ramcap.StreamWire},
		{"non-canonical binding", ramcap.EncodeControl(ramcap.MagicControl, ramcap.KindHello, sha(spaced), spaced), ramcap.StreamBinding},
	} {
		srv := &streamFake{img: goodImage}
		h := start(t, bd, srv, good, false)
		h.sendAsync(tc.msg)
		h.end(tc.want, 0, 5)
		if g, _ := srv.counts(); g != 0 || h.opens.Load() != 0 {
			t.Fatalf("%s: X touched before an authenticated HELLO", tc.name)
		}
	}
	// Control channel closed before HELLO.
	srv := &streamFake{img: goodImage}
	h := start(t, bd, srv, good, false)
	_ = h.ctlW.Close()
	h.end(ramcap.StreamWire, 0, 5)
}

func sha(b []byte) [32]byte { return sha256.Sum256(b) }

func TestW8UStreamControlMustFail(t *testing.T) {
	good := func(n int) ([]byte, error) { return goodLegs(n), nil }
	_, bd := rawBinding(t, nil)
	rawAck := func(seq, total uint64) []byte {
		return ramcap.EncodeControl(ramcap.MagicControl, ramcap.KindAck, bd.SHA256, ramcap.EncodeAckBody(seq, total))
	}
	for _, tc := range []struct {
		name string
		pre  [][2]uint64 // valid ACKs, each followed by reading the frames it grants
		bad  []byte
		last uint64
		want ramcap.StreamCode
		rc   int
	}{
		{"ACK inflation", nil, rawAck(0, 3), 0, ramcap.StreamCredit, 5},
		{"first ACK not seq 0", nil, rawAck(1, 2), 0, ramcap.StreamCredit, 5},
		{"ACK regression", [][2]uint64{{0, 1}, {1, 2}}, rawAck(0, 2), 2, ramcap.StreamCredit, 5},
		{"ACK credit regression", [][2]uint64{{0, 2}}, rawAck(2, 1), 2, ramcap.StreamCredit, 5},
		{"ACK of unsent seq", [][2]uint64{{0, 2}}, rawAck(3, 4), 2, ramcap.StreamCredit, 5},
		{"ACK overflow", [][2]uint64{{0, 2}}, rawAck(^uint64(0), ^uint64(0)), 2, ramcap.StreamCredit, 5},
		{"ACK wrong binding", [][2]uint64{{0, 2}}, ramcap.EncodeControl(ramcap.MagicControl, ramcap.KindAck, [32]byte{9}, ramcap.EncodeAckBody(2, 4)), 2, ramcap.StreamCredit, 5},
		{"STOP wrong binding", nil, ramcap.EncodeControl(ramcap.MagicControl, ramcap.KindStop, [32]byte{9}, nil), 0, ramcap.StreamBinding, 5},
		{"second HELLO", nil, nil, 0, ramcap.StreamWire, 5},
		{"unknown kind", nil, ramcap.EncodeControl(ramcap.MagicControl, 7, bd.SHA256, nil), 0, ramcap.StreamWire, 5},
		{"STOP before credit", nil, ramcap.EncodeControl(ramcap.MagicControl, ramcap.KindStop, bd.SHA256, nil), 0, ramcap.StreamOK, 0},
		{"duplicate ACK then STOP", [][2]uint64{{0, 2}, {2, 2}}, ramcap.EncodeControl(ramcap.MagicControl, ramcap.KindStop, bd.SHA256, nil), 2, ramcap.StreamOK, 0},
	} {
		srv := &streamFake{img: goodImage}
		h := start(t, bd, srv, good, true)
		h.send(h.hc.Hello())
		h.next(ramcap.MagicHelloConfirm)
		var granted uint64
		for _, a := range tc.pre {
			h.ack(a[0], a[1])
			for ; granted < a[1]; granted++ {
				h.next(ramcap.MagicFrame)
			}
		}
		if tc.name == "duplicate ACK then STOP" {
			h.send(rawAck(2, 2))
		}
		bad := tc.bad
		if bad == nil {
			bad = h.hc.Hello()
		}
		h.sendAsync(bad)
		h.end(tc.want, tc.last, tc.rc)
		if g, _ := srv.counts(); uint64(g) != tc.last {
			t.Fatalf("%s: grabs %d", tc.name, g)
		}
	}
}

func TestW8UStreamArgv(t *testing.T) {
	if r, err := parseArgs([]string{"stream", ":100", "7"}); err != nil || r.sub != "stream" || r.xid != 7 {
		t.Fatal(r, err)
	}
	for _, args := range [][]string{{"stream", ":0", "7"}, {"stream", ":100", "07"}, {"stream", ":100"}, {"stream", ":100", "7", "x"}} {
		var out, errout bytes.Buffer
		if rc := run(args, &out, &errout, dependencies{}); rc != 2 || out.Len() != 0 {
			t.Fatalf("%v rc %d", args, rc)
		}
	}
	var out, errout bytes.Buffer
	if rc := run([]string{"stream", ":100", "7"}, &out, &errout, dependencies{}); rc != 2 || out.Len() != 0 {
		t.Fatal("missing stream dependencies", rc)
	}
}

type renewingServer struct {
	imageServer
	renewed int
}

func (r *renewingServer) Renew(time.Duration) error { r.renewed++; return nil }

// The resident stream must re-arm the X socket deadline before every pair
// (live: openServer's absolute 2 s deadline killed the long-lived connection).
func TestStreamRenewsDeadlinePerPair(t *testing.T) {
	r := &renewingServer{}
	if e := renewDeadline(r); e != nil || r.renewed != 1 {
		t.Fatalf("renew %d %v", r.renewed, e)
	}
}

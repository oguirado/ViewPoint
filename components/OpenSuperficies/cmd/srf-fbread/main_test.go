package main

import (
	"bytes"
	"encoding/binary"
	"errors"
	"golang.org/x/sys/unix"
	"io"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func fixture() []byte {
	b := make([]byte, 108)
	fields := []uint32{104, 7, 2, 24, 1, 1, 0, 0, 32, 0, 32, 32, 4, 4, 0xff0000, 0xff00, 0xff, 8, 0, 0, 1, 1, 0, 0, 0}
	for i, v := range fields {
		binary.BigEndian.PutUint32(b[4*i:], v)
	}
	copy(b[100:], []byte{'x', 0, 0, 0})
	copy(b[104:], []byte{3, 2, 1, 0})
	return b
}

type fakeServer struct {
	events                       []string
	img                          []byte
	grabErr, ungrabErr, imageErr error
	closes                       atomic.Int32
	xid                          uint32
}

func (f *fakeServer) Grab() error   { f.events = append(f.events, "grab"); return f.grabErr }
func (f *fakeServer) Ungrab() error { f.events = append(f.events, "ungrab"); return f.ungrabErr }
func (f *fakeServer) Image(xid uint32) ([]byte, error) {
	f.events = append(f.events, "image")
	f.xid = xid
	return f.img, f.imageErr
}
func (f *fakeServer) Close() { f.closes.Add(1) }

func TestRB2FbreadArgvAndExitCodes(t *testing.T) {
	good := [][]string{{"read", fbPath}, {"getimage", ":100", "0"}, {"pair", ":100", "4294967295"}}
	for _, args := range good {
		if _, err := parseArgs(args); err != nil {
			t.Fatalf("%v: %v", args, err)
		}
	}
	bad := [][]string{nil, {"read", "/tmp/frame"}, {"read", fbPath, "extra"}, {"getimage", ":0", "1"}, {"getimage", ":100", "01"}, {"pair", ":100", "4294967296"}, {"pair", ":100", "-1"}, {"pair", ":100", "+1"}, {"pair", ":100", " 1"}, {"shell", ":100", "1"}, {"pair", ":100", "1", "extra"}}
	for _, args := range bad {
		var out, errout bytes.Buffer
		if rc := run(args, &out, &errout, dependencies{}); rc != 2 || out.Len() != 0 {
			t.Fatalf("%v rc %d stdout %d", args, rc, out.Len())
		}
	}
	for _, tc := range []struct {
		err error
		rc  int
	}{{errors.New("E_TORN"), 5}, {io.ErrUnexpectedEOF, 5}} {
		var out, errout bytes.Buffer
		deps := dependencies{read: func() ([]byte, error) { return nil, tc.err }}
		if rc := run([]string{"read", fbPath}, &out, &errout, deps); rc != tc.rc || out.Len() != 0 {
			t.Fatalf("rc=%d want=%d stdout=%d", rc, tc.rc, out.Len())
		}
	}
}

func TestRB2PairGrabFramingAndUngrab(t *testing.T) {
	a := fixture()
	b := append([]byte(nil), a...)
	b[len(b)-1] = 4
	f := &fakeServer{img: b}
	read := func() ([]byte, error) { f.events = append(f.events, "read"); return a, nil }
	out, err := capturePair(f, read, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if got := f.events; len(got) != 4 || got[0] != "grab" || got[1] != "read" || got[2] != "image" || got[3] != "ungrab" {
		t.Fatalf("events %v", got)
	}
	r := bytes.NewReader(out)
	for _, want := range [][]byte{a, b} {
		var n uint64
		if err := binary.Read(r, binary.BigEndian, &n); err != nil || n != uint64(len(want)) {
			t.Fatal("length framing", n, err)
		}
		v := make([]byte, n)
		if _, err := io.ReadFull(r, v); err != nil || !bytes.Equal(v, want) {
			t.Fatal("frame framing", err)
		}
	}
	if r.Len() != 0 {
		t.Fatal("trailing bytes")
	}
	for _, failRead := range []bool{false, true} {
		f := &fakeServer{img: b, ungrabErr: errors.New("ungrab failed")}
		read := func() ([]byte, error) {
			if failRead {
				return nil, errors.New("E_TORN")
			}
			return a, nil
		}
		got, err := capturePair(f, read, time.Second)
		if err == nil || len(got) != 0 || f.events[len(f.events)-1] != "ungrab" {
			t.Fatalf("failed ungrab/read admitted %v %v", f.events, err)
		}
	}
}

func TestRB2PairTimeoutAndPanicRelease(t *testing.T) {
	f := &fakeServer{img: fixture()}
	_, err := capturePair(f, func() ([]byte, error) { time.Sleep(20 * time.Millisecond); return fixture(), nil }, time.Millisecond)
	if err == nil || f.closes.Load() == 0 {
		t.Fatal("grab deadline not closed", err)
	}
	f = &fakeServer{img: fixture()}
	var out, errout bytes.Buffer
	deps := dependencies{open: func() (imageServer, error) { return f, nil }, read: func() ([]byte, error) { panic("test") }}
	if rc := run([]string{"pair", ":100", "0"}, &out, &errout, deps); rc != 5 || out.Len() != 0 {
		t.Fatalf("panic rc %d, stdout %d", rc, out.Len())
	}
	if len(f.events) == 0 || f.events[len(f.events)-1] != "ungrab" {
		t.Fatal("panic leaked server grab", f.events)
	}
}

func TestRB2BuildXWDGeometryAndMetadata(t *testing.T) {
	for _, order := range []uint32{0, 1} {
		b, err := buildXWD(imageData{XID: 42, W: 2, H: 1, RootW: 8, RootH: 4, X: 3, Y: 2, ByteOrder: order, VisualClass: 4, RedMask: 0xff0000, GreenMask: 0xff00, BlueMask: 0xff, Pixels: make([]byte, 8)})
		if err != nil {
			t.Fatal(err)
		}
		if string(b[100:len(b)-8]) != "srf-fbread xid=42\x00" {
			t.Fatal("window identity")
		}
		if binary.BigEndian.Uint32(b[88:]) != 3 || binary.BigEndian.Uint32(b[92:]) != 2 {
			t.Fatal("root coordinates")
		}
	}
	for _, im := range []imageData{{XID: 1, W: 1, H: 1, RootW: 1, RootH: 1, X: -1}, {XID: 1, W: 2, H: 1, RootW: 1, RootH: 1}, {W: 1, H: 1, RootW: 1, RootH: 1}} {
		if _, err := buildXWD(im); err == nil {
			t.Fatal("invalid geometry accepted")
		}
	}
}

func TestRB2ReadOnlyTmpfsSnapshot(t *testing.T) {
	for _, tc := range []struct {
		fs, size int64
		want     string
	}{{0, 108, "E_FB_NOT_TMPFS"}, {unix.TMPFS_MAGIC, 99, "E_XWD_SIZE"}, {unix.TMPFS_MAGIC, maxFrame + 1, "E_XWD_SIZE"}} {
		mapped := false
		_, err := readSnapshot(tc.fs, tc.size, func() ([]byte, func(), error) { mapped = true; return fixture(), func() {}, nil })
		if err == nil || err.Error() != tc.want || mapped {
			t.Fatalf("unsafe mapping attempted: %v mapped %v", err, mapped)
		}
	}
	for _, invalid := range []bool{false, true} {
		frame := fixture()
		if invalid {
			frame = append(frame, 0)
		}
		unmapped := false
		got, err := readSnapshot(unix.TMPFS_MAGIC, int64(len(frame)), func() ([]byte, func(), error) { return frame, func() { unmapped = true }, nil })
		if !unmapped {
			t.Fatal("mapping not released")
		}
		if invalid {
			if err == nil || err.Error() != "E_XWD_SIZE" {
				t.Fatal("extra bytes admitted", err)
			}
		} else if err != nil || !bytes.Equal(got, frame) {
			t.Fatal("snapshot changed", err)
		}
	}
}

func TestRB2RunPairPassesXIDAndRejectsBadImages(t *testing.T) {
	f := &fakeServer{img: fixture()}
	deps := dependencies{read: func() ([]byte, error) { return fixture(), nil }, open: func() (imageServer, error) { return f, nil }}
	var out, errout bytes.Buffer
	if rc := run([]string{"pair", ":100", "42"}, &out, &errout, deps); rc != 0 || f.xid != 42 || out.Len() != 232 {
		t.Fatalf("pair rc=%d xid=%d bytes=%d stderr=%s", rc, f.xid, out.Len(), errout.String())
	}
	for _, sub := range []string{"read", "getimage", "pair"} {
		out.Reset()
		errout.Reset()
		f.img = []byte("not XWD")
		deps.read = func() ([]byte, error) { return []byte("not XWD"), nil }
		args := []string{sub, ":100", "42"}
		if sub == "read" {
			args = []string{sub, fbPath}
		}
		if rc := run(args, &out, &errout, deps); rc != 5 || out.Len() != 0 {
			t.Fatal("invalid frame admitted", sub, rc)
		}
	}
	out.Reset()
	errout.Reset()
	deps.read = func() ([]byte, error) { return nil, errors.New(strings.Repeat("x", 8192)) }
	if rc := run([]string{"read", fbPath}, &out, &errout, deps); rc != 5 || errout.Len() > 4096 || out.Len() != 0 {
		t.Fatal("unbounded diagnostics", rc, errout.Len())
	}
}

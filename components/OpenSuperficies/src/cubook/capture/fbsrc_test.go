package capture

import (
	"bytes"
	"io"
	"math"
	"os"
	"path/filepath"
	"testing"
)

func writeXWD(t *testing.T, w, h int, trim int) (string, []byte) {
	t.Helper()
	x := SynthXWD(w, h, 0, nil)
	p := filepath.Join(t.TempDir(), "Xvfb_screen0")
	if err := os.WriteFile(p, x[:len(x)-trim], 0o600); err != nil {
		t.Fatal(err)
	}
	return p, x
}

func TestMmapSourceMatchesReadAt(t *testing.T) {
	p, x := writeXWD(t, tw, th, 0)
	f, _ := os.Open(p)
	defer f.Close()
	m, err := NewMmapSource(f)
	if err != nil {
		t.Fatal(err)
	}
	defer m.Close()
	a, b := make([]byte, tw*th*3), make([]byte, tw*th*3)
	if err := m.Frame(a); err != nil {
		t.Fatal(err)
	}
	if err := (&XWDSource{Src: bytes.NewReader(x), Size: int64(len(x))}).Frame(b); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(a, b) || bytes.Equal(a, make([]byte, len(a))) {
		t.Fatal("mmap frame differs from readat frame (or is empty)")
	}
	if err := m.Frame(make([]byte, 5)); err == nil {
		t.Fatal("wrong dst length accepted")
	}
}

func TestMmapSourceBoundsNoAddOverflow(t *testing.T) {
	p, _ := writeXWD(t, tw, th, 1) // one pixel byte missing
	f, _ := os.Open(p)
	defer f.Close()
	if _, err := NewMmapSource(f); err == nil {
		t.Fatal("truncated-by-one framebuffer accepted")
	} else if c, rs := reason(err); c != ClassIO || rs != "pixels_out_of_bounds" {
		t.Fatalf("got %v", err)
	}
	p2, _ := writeXWD(t, tw, th, 0)
	f2, _ := os.Open(p2)
	defer f2.Close()
	m, err := NewMmapSource(f2)
	if err != nil {
		t.Fatalf("exact-fit refused: %v", err)
	}
	m.Close()
	if mmapInBounds(math.MaxUint64-5, 10, math.MaxUint64) || !mmapInBounds(4, 6, 10) || mmapInBounds(4, 7, 10) || mmapInBounds(11, 0, 10) {
		t.Fatal("range check wrong (o+l must never be formed)")
	}
}

func TestMmapSourceNoSyscallPerFrame(t *testing.T) {
	p, x := writeXWD(t, tw, th, 0)
	f, _ := os.Open(p)
	defer f.Close()
	m, err := NewMmapSource(f)
	if err != nil {
		t.Fatal(err)
	}
	defer m.Close()
	dst := make([]byte, tw*th*3)
	for i := 0; i < 100; i++ {
		if err := m.Frame(dst); err != nil {
			t.Fatal(err)
		}
	}
	if m.Syscalls != 1 {
		t.Fatalf("syscalls=%d want 1 (the single mmap)", m.Syscalls)
	}
	first := append([]byte(nil), dst...)
	w, _ := os.OpenFile(p, os.O_WRONLY, 0) // the mapping is live: a later write shows in the next frame, no cached copy
	w.WriteAt([]byte{0xFF, 0xFF, 0xFF, 0}, int64(len(x)-tw*th*4))
	w.Close()
	m.Frame(dst)
	if bytes.Equal(first, dst) {
		t.Fatal("mmap source returned a stale copy")
	}
}

type constShm byte

func (c constShm) GetImage(dst []byte) error {
	for i := range dst {
		dst[i] = byte(c)
	}
	return nil
}

func TestXshmUnavailableRc5(t *testing.T) {
	_, err := OpenSource("xshm", nil, nil)
	if c, rs := reason(err); c != ClassGate || rs != "xshm_unavailable" {
		t.Fatalf("xshm without getter: %v", err)
	}
	s, err := OpenSource("xshm", nil, constShm(0xAB)) // positive: an injected getter works
	if err != nil {
		t.Fatal(err)
	}
	dst := make([]byte, 12)
	if err := s.Frame(dst); err != nil || dst[0] != 0xAB || dst[11] != 0xAB {
		t.Fatalf("err=%v dst=%v", err, dst)
	}
	if _, err := OpenSource("bogus", nil, nil); err == nil {
		t.Fatal("unknown source accepted")
	} else if c, _ := reason(err); c != ClassUsage {
		t.Fatalf("bogus: %v", err)
	}
	p, _ := writeXWD(t, tw, th, 0)
	f, _ := os.Open(p)
	defer f.Close()
	for _, n := range []string{"readat", "mmap"} {
		s, err := OpenSource(n, f, nil)
		if err != nil {
			t.Fatalf("%s: %v", n, err)
		}
		if err := s.Frame(make([]byte, tw*th*3)); err != nil {
			t.Fatalf("%s frame: %v", n, err)
		}
	}
}

type countRA struct {
	io.ReaderAt
	n int
}

func (c *countRA) ReadAt(b []byte, o int64) (int, error) { c.n++; return c.ReaderAt.ReadAt(b, o) }

// ADC-W3-09: readat parses the header once (frame 0); later frames cost one ReadAt (pixels) each.
func TestXWDSourceCachesHeaderAfterFrame0(t *testing.T) {
	img := SynthXWD(tw, th, 0, nil)
	h, _ := ParseXWDHeader(bytes.NewReader(img))
	ra := &countRA{ReaderAt: bytes.NewReader(img)}
	x := &XWDSource{Src: ra, Size: int64(len(img))}
	dst := make([]byte, h.Width*h.Height*3)
	if err := x.Frame(dst); err != nil {
		t.Fatal(err)
	}
	first := ra.n
	for i := 0; i < 5; i++ {
		if err := x.Frame(dst); err != nil {
			t.Fatal(err)
		}
	}
	if per := (ra.n - first) / 5; per != 1 {
		t.Fatalf("ReadAt per cached frame = %d, want 1 (header re-parsed)", per)
	}
}

// ADC-W3-09: every source can be closed; Frame after Close fails instead of reading a stale handle.
func TestSourcesCloseAndRefuseFrames(t *testing.T) {
	img := SynthXWD(tw, th, 0, nil)
	h, _ := ParseXWDHeader(bytes.NewReader(img))
	dst := make([]byte, h.Width*h.Height*3)
	var srcs = []FrameSource{&XWDSource{Src: bytes.NewReader(img), Size: int64(len(img))}, NewXshmSource(nil)}
	for i, s := range srcs {
		c, ok := s.(io.Closer)
		if !ok {
			t.Fatalf("source %d has no Close", i)
		}
		if err := c.Close(); err != nil {
			t.Fatal(err)
		}
		if _, rs := reason(s.Frame(dst)); rs != "source_closed" && i == 0 {
			t.Fatalf("XWDSource Frame after Close: %q", rs)
		}
	}
}

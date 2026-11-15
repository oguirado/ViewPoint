package capture

import (
	"bytes"
	"os"
	"syscall"
)

// Frame sources of the 4K pacing path (os-cu-0 W3 lane P): readat (XWDSource), mmap (map Xvfb_screen0 once, one
// forward copy-out per frame, SW-0198 §2.4) and xshm (injected MIT-SHM getter; the real xgb shm path is [H]).

// ShmGetter is the injected MIT-SHM image getter: it fills dst with W*H*3 packed RGB24.
type ShmGetter interface{ GetImage(dst []byte) error }

// mmapInBounds is the addition-free range check of SW-0009 I3 (o+l is never formed).
func mmapInBounds(o, l, m uint64) bool { return InBounds(o, l, m) }

// MmapSource maps the framebuffer file read-only once; Frame is a single forward pass over the mapping with no
// syscall and no intermediate scratch copy (the payload may tear while read: the caller owns that residual, SW-0198).
type MmapSource struct {
	// Syscalls counts the syscalls this source issued: exactly 1 (the mmap) for its whole life.
	Syscalls int
	data     []byte
	hdr      Header
	pix      []byte // data[PixelOffset : PixelOffset+BytesPerLine*Height], bounds proven once at open
}

// NewMmapSource maps f, parses the header once and proves the pixel range inside the mapping (hoisted bounds).
func NewMmapSource(f *os.File) (*MmapSource, error) {
	st, err := f.Stat()
	if err != nil {
		return nil, ioErr("fb_stat", err)
	}
	if st.Size() <= 0 || st.Size() > 1<<40 {
		return nil, ioErr("pixels_out_of_bounds", nil)
	}
	data, err := syscall.Mmap(int(f.Fd()), 0, int(st.Size()), syscall.PROT_READ, syscall.MAP_SHARED)
	if err != nil {
		return nil, ioErr("fb_mmap", err)
	}
	m := &MmapSource{data: data, Syscalls: 1}
	if m.hdr, err = ParseXWDHeader(bytes.NewReader(data)); err != nil {
		m.Close()
		return nil, err
	}
	need := uint64(m.hdr.BytesPerLine) * uint64(m.hdr.Height)
	if !mmapInBounds(uint64(m.hdr.PixelOffset), need, uint64(len(data))) {
		m.Close()
		return nil, ioErr("pixels_out_of_bounds", nil)
	}
	m.pix = data[m.hdr.PixelOffset : m.hdr.PixelOffset+int(need)]
	return m, nil
}

// Header is the header cached at open (geometry of the map).
func (m *MmapSource) Header() Header { return m.hdr }

// Frame converts the mapped BGRX pixels straight into dst (RGB24); the row slices are hoisted per row.
func (m *MmapSource) Frame(dst []byte) error {
	if m.pix == nil {
		return ioErr("source_closed", nil)
	}
	w, h, bpl := m.hdr.Width, m.hdr.Height, m.hdr.BytesPerLine
	if w*h*3 != len(dst) {
		return &Error{Class: ClassStale, Reason: "geometry_changed"}
	}
	o := 0
	for y := 0; y < h; y++ {
		row := m.pix[y*bpl : y*bpl+w*4]
		out := dst[o : o+w*3]
		for x := 0; x < w; x++ {
			p := row[x*4 : x*4+3 : x*4+3]
			q := out[x*3 : x*3+3 : x*3+3]
			q[0], q[1], q[2] = p[2], p[1], p[0]
		}
		o += w * 3
	}
	return nil
}

// Close unmaps; Frame after Close fails instead of faulting.
func (m *MmapSource) Close() error {
	d := m.data
	m.data, m.pix = nil, nil
	if d == nil {
		return nil
	}
	return syscall.Munmap(d)
}

// XshmSource reads through an injected ShmGetter; without one every Frame is rc 5 xshm_unavailable.
type XshmSource struct{ g ShmGetter }

func NewXshmSource(g ShmGetter) *XshmSource { return &XshmSource{g: g} }

// Close drops the getter (the injected getter's lifetime stays with its owner); later frames are xshm_unavailable.
func (x *XshmSource) Close() error { x.g = nil; return nil }

func (x *XshmSource) Frame(dst []byte) error {
	if x == nil || x.g == nil {
		return gate("xshm_unavailable")
	}
	return x.g.GetImage(dst)
}

// OpenSource builds the FrameSource named by --src (closed enum readat|mmap|xshm). f is the Xvfb_screen0 file for
// readat/mmap; g is only used by xshm. Every source implements io.Closer.
func OpenSource(name string, f *os.File, g ShmGetter) (FrameSource, error) {
	switch name {
	case "readat", "mmap":
		if f == nil {
			return nil, usage(name + "_needs_fb")
		}
		if name == "mmap" {
			return NewMmapSource(f)
		}
		st, err := f.Stat()
		if err != nil {
			return nil, ioErr("fb_stat", err)
		}
		return &XWDSource{Src: f, Size: st.Size()}, nil
	case "xshm":
		if g == nil {
			return nil, gate("xshm_unavailable")
		}
		return NewXshmSource(g), nil
	}
	return nil, usage("src")
}

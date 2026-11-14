// Package ramcap captures exact framebuffer pixels into a managed RAM workspace.
package ramcap

import (
	"crypto/sha256"
	"encoding/binary"
	"errors"
	"regexp"
	"strconv"
)

type Header struct {
	HeaderSize, Width, Height, Depth, BPP, BytesPerLine, ByteOrder, NColors, VisualClass int
	RedMask, GreenMask, BlueMask                                                         uint32
	PixelsOffset, Size                                                                   int64
	RootX, RootY                                                                         int32
}

func fail(code string) error { return errors.New(code) }
func ParseHeader(b []byte) (Header, error) {
	var h Header
	if len(b) < 100 {
		return h, fail("E_XWD_SHORT")
	}
	var f [25]uint32
	for i := range f {
		f[i] = binary.BigEndian.Uint32(b[i*4:])
	}
	if f[1] != 7 {
		return h, fail("E_XWD_VERSION")
	}
	if f[0] < 100 {
		return h, fail("E_XWD_SHORT")
	}
	if f[2] != 2 || f[3] != 24 || f[11] != 32 {
		return h, fail("E_XWD_FORMAT")
	}
	if (f[13] != 4 && f[13] != 5) || f[14] != 0xff0000 || f[15] != 0xff00 || f[16] != 0xff {
		return h, fail("E_XWD_VISUAL")
	}
	if f[4] == 0 || f[5] == 0 || f[4] > 16384 || f[5] > 16384 || int64(f[12]) < 4*int64(f[4]) || f[12]%4 != 0 {
		return h, fail("E_XWD_GEOMETRY")
	}
	if f[7] > 1 {
		return h, fail("E_XWD_FORMAT")
	}
	if f[0] > 65536 || f[19] > 65536 || f[12] > 65536 {
		return h, fail("E_XWD_GEOMETRY")
	}
	off := int64(f[0]) + 12*int64(f[19])
	size := off + int64(f[5])*int64(f[12])
	h = Header{HeaderSize: int(f[0]), Width: int(f[4]), Height: int(f[5]), Depth: int(f[3]), BPP: int(f[11]), BytesPerLine: int(f[12]), ByteOrder: int(f[7]), NColors: int(f[19]), VisualClass: int(f[13]), RedMask: f[14], GreenMask: f[15], BlueMask: f[16], PixelsOffset: off, Size: size, RootX: int32(f[22]), RootY: int32(f[23])}
	return h, nil
}

// Validate uses zero expected dimensions to mean unspecified.
func Validate(b []byte, w, h int) (Header, error) {
	hd, e := ParseHeader(b)
	if e != nil {
		return hd, e
	}
	if (w != 0 && w != hd.Width) || (h != 0 && h != hd.Height) {
		return hd, fail("E_XWD_GEOMETRY")
	}
	if int64(len(b)) != hd.Size {
		return hd, fail("E_XWD_SIZE")
	}
	return hd, nil
}

// Crop returns canonical RGB24 pixels without clipping out-of-bounds rectangles.
func Crop(b []byte, h Header, x, y, w, height int) ([]byte, error) {
	checked, e := Validate(b, 0, 0)
	if e != nil {
		return nil, e
	}
	if checked != h || x < 0 || y < 0 || w <= 0 || height <= 0 || w > h.Width || height > h.Height || x > h.Width-w || y > h.Height-height {
		return nil, fail("E_XWD_GEOMETRY")
	}
	out := make([]byte, w*height*3)
	r, g, bl := 2, 1, 0
	if h.ByteOrder == 1 {
		r, g, bl = 1, 2, 3
	}
	for yy := range height {
		for xx := range w {
			i := int(h.PixelsOffset) + (y+yy)*h.BytesPerLine + (x+xx)*4
			o := (yy*w + xx) * 3
			out[o], out[o+1], out[o+2] = b[i+r], b[i+g], b[i+bl]
		}
	}
	return out, nil
}
func RGB24(b []byte, h Header) ([]byte, error) { return Crop(b, h, 0, 0, h.Width, h.Height) }
func FrameSHA256(b []byte, h Header) ([32]byte, error) {
	rgb, e := RGB24(b, h)
	if e != nil {
		return [32]byte{}, e
	}
	return sha256.Sum256(rgb), nil
}

type Window struct {
	XID  uint32
	X, Y int
	W, H int
}

var windowName = regexp.MustCompile(`^srf-fbread xid=([1-9][0-9]{0,9})\x00$`)

func WindowInfo(b []byte, h Header) (Window, error) {
	if checked, e := Validate(b, 0, 0); e != nil {
		return Window{}, e
	} else if checked != h {
		return Window{}, fail("E_XWD_GEOMETRY")
	}
	if h.HeaderSize < 100 || h.HeaderSize > len(b) {
		return Window{}, fail("E_XWD_GEOMETRY")
	}
	m := windowName.FindSubmatch(b[100:h.HeaderSize])
	if m == nil {
		return Window{}, fail("E_XWD_GEOMETRY")
	}
	id, e := strconv.ParseUint(string(m[1]), 10, 32)
	if e != nil || id == 0 {
		return Window{}, fail("E_XWD_GEOMETRY")
	}
	return Window{uint32(id), int(h.RootX), int(h.RootY), h.Width, h.Height}, nil
}

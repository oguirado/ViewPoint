package capture

import (
	"encoding/binary"
	"io"
)

const (
	xwdFixedFields = 25
	xwdFixedBytes  = xwdFixedFields * 4
	// MaxDim mirrors visioncore layout.DIM_MAX.
	MaxDim = 8192
)

// Header is the parsed XWD header of Xvfb_screen0 (big-endian CARD32 fields).
type Header struct {
	HeaderSize   int
	Width        int
	Height       int
	Depth        int
	BitsPerPixel int
	BytesPerLine int
	NColors      int
	PixelOffset  int // header_size + ncolors*12
}

// ParseXWDHeader reads and validates the header; only BGRX 32 bpp little-endian is accepted.
func ParseXWDHeader(r io.ReaderAt) (Header, error) {
	var raw [xwdFixedBytes]byte
	if n, err := r.ReadAt(raw[:], 0); n < len(raw) {
		return Header{}, schemaErr("xwd_header_short", err)
	}
	f := func(i int) uint32 { return binary.BigEndian.Uint32(raw[i*4:]) }
	h := Header{
		HeaderSize: int(f(0)), Width: int(f(4)), Height: int(f(5)), Depth: int(f(3)),
		BitsPerPixel: int(f(11)), BytesPerLine: int(f(12)), NColors: int(f(19)),
	}
	if f(1) != 7 || f(7) != 0 || f(11) != 32 || (f(3) != 24 && f(3) != 32) ||
		f(14) != 0xff0000 || f(15) != 0xff00 || f(16) != 0xff {
		return Header{}, schemaErr("xwd_format_unsupported", nil)
	}
	if h.HeaderSize < xwdFixedBytes || h.HeaderSize > 4096 || h.NColors < 0 || h.NColors > 256 ||
		h.Width < 1 || h.Width > MaxDim || h.Height < 1 || h.Height > MaxDim || h.BytesPerLine < h.Width*4 {
		return Header{}, schemaErr("xwd_geometry", nil)
	}
	h.PixelOffset = h.HeaderSize + h.NColors*12
	return h, nil
}

// BGRXToRGB24 converts rows of BGRX (stride bpl) into packed RGB24; dst must hold w*h*3 bytes.
func BGRXToRGB24(dst, src []byte, w, h, bpl int) {
	o := 0
	for y := 0; y < h; y++ {
		row := src[y*bpl : y*bpl+w*4]
		for x := 0; x < w; x++ {
			p := row[x*4 : x*4+3]
			dst[o], dst[o+1], dst[o+2] = p[2], p[1], p[0]
			o += 3
		}
	}
}

// SynthXWD builds a synthetic XWD image in memory (tests and --fake-fb); fn gives the RGB of a pixel.
func SynthXWD(w, h, ncolors int, fn func(x, y int) (r, g, b byte)) []byte {
	if fn == nil {
		fn = func(x, y int) (byte, byte, byte) { return byte(x * 7), byte(y * 13), byte(x ^ y) }
	}
	name := []byte("Xvfb screen 0\x00")
	hs := xwdFixedBytes + len(name)
	bpl := w * 4
	buf := make([]byte, hs+ncolors*12+bpl*h)
	put := func(i int, v uint32) { binary.BigEndian.PutUint32(buf[i*4:], v) }
	put(0, uint32(hs))
	put(1, 7)
	put(2, 2) // ZPixmap
	put(3, 24)
	put(4, uint32(w))
	put(5, uint32(h))
	put(8, 32)
	put(10, 32)
	put(11, 32)
	put(12, uint32(bpl))
	put(13, 4) // TrueColor
	put(14, 0xff0000)
	put(15, 0xff00)
	put(16, 0xff)
	put(17, 8)
	put(18, 256)
	put(19, uint32(ncolors))
	put(20, uint32(w))
	put(21, uint32(h))
	copy(buf[xwdFixedBytes:], name)
	off := hs + ncolors*12
	for y := 0; y < h; y++ {
		for x := 0; x < w; x++ {
			r, g, b := fn(x, y)
			i := off + y*bpl + x*4
			buf[i], buf[i+1], buf[i+2], buf[i+3] = b, g, r, 0xff
		}
	}
	return buf
}

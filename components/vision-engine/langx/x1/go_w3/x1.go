package x1

import (
	"encoding/binary"
	"errors"
	"math/bits"
)

var (
	ErrDim    = errors.New("dimension out of range")
	ErrStride = errors.New("stride below minimum required")
	ErrShort  = errors.New("buffer too short")
	ErrRect   = errors.New("invalid rectangle dimension")
	ErrGrid   = errors.New("invalid grid tile size")
)

type Rect struct {
	X int32
	Y int32
	W int32
	H int32
}

func RepackBGRXToRGB(src []byte, w, h, srcStride int, dst []byte, dstStride int) error {
	if w < 0 || w > 16384 || h < 0 || h > 16384 {
		return ErrDim
	}
	if srcStride < 4*w || dstStride < 3*w {
		return ErrStride
	}

	var minSrcLen, minDstLen int
	if w > 0 && h > 0 {
		minSrcLen = (h-1)*srcStride + 4*w
		minDstLen = (h-1)*dstStride + 3*w
	} else {
		minSrcLen = 0
		minDstLen = 0
	}

	if len(src) < minSrcLen || len(dst) < minDstLen {
		return ErrShort
	}

	if w == 0 || h == 0 {
		return nil
	}

	for y := 0; y < h; y++ {
		srcRow := src[y*srcStride : y*srcStride+4*w]
		dstRow := dst[y*dstStride : y*dstStride+3*w]

		// Process 4 pixels at a time if possible
		x := 0
		for ; x <= w-4; x += 4 {
			s0 := srcRow[4*x : 4*x+16]
			d0 := dstRow[3*x : 3*x+12]

			// Pixel 0: B, G, R, X -> R, G, B
			d0[0] = s0[2]
			d0[1] = s0[1]
			d0[2] = s0[0]

			// Pixel 1
			d0[3] = s0[6]
			d0[4] = s0[5]
			d0[5] = s0[4]

			// Pixel 2
			d0[6] = s0[10]
			d0[7] = s0[9]
			d0[8] = s0[8]

			// Pixel 3
			d0[9] = s0[14]
			d0[10] = s0[13]
			d0[11] = s0[12]
		}

		for ; x < w; x++ {
			s := srcRow[4*x : 4*x+4]
			d := dstRow[3*x : 3*x+3]
			d[0] = s[2] // R
			d[1] = s[1] // G
			d[2] = s[0] // B
		}
	}

	return nil
}

func DamageTiles(w, h, s int, rects []Rect) ([]uint64, int, error) {
	if w < 1 || w > 16384 || h < 1 || h > 16384 {
		return nil, 0, ErrDim
	}
	if s < 1 || s > 256 || (s&(s-1)) != 0 {
		return nil, 0, ErrGrid
	}

	for _, r := range rects {
		if r.W < 0 || r.H < 0 {
			return nil, 0, ErrRect
		}
	}

	gw := (w + s - 1) / s
	gh := (h + s - 1) / s
	totalTiles := gw * gh
	wordsLen := (totalTiles + 63) / 64

	words := make([]uint64, wordsLen)

	for _, r := range rects {
		x0 := int64(r.X)
		if x0 < 0 {
			x0 = 0
		}
		y0 := int64(r.Y)
		if y0 < 0 {
			y0 = 0
		}

		x1 := int64(r.X) + int64(r.W)
		if x1 > int64(w) {
			x1 = int64(w)
		}
		y1 := int64(r.Y) + int64(r.H)
		if y1 > int64(h) {
			y1 = int64(h)
		}

		if x1 <= x0 || y1 <= y0 {
			continue
		}

		tx0 := x0 / int64(s)
		tx1 := (x1 + int64(s) - 1) / int64(s)
		ty0 := y0 / int64(s)
		ty1 := (y1 + int64(s) - 1) / int64(s)

		for ty := ty0; ty < ty1; ty++ {
			for tx := tx0; tx < tx1; tx++ {
				t := ty*int64(gw) + tx
				wordIdx := t / 64
				bitIdx := t % 64
				words[wordIdx] |= (uint64(1) << bitIdx)
			}
		}
	}

	popcount := 0
	for _, word := range words {
		popcount += bits.OnesCount64(word)
	}

	return words, popcount, nil
}

type SplitMix64 struct {
	state uint64
}

func NewSplitMix64(seed uint64) *SplitMix64 {
	return &SplitMix64{state: seed}
}

func (sm *SplitMix64) Next() uint64 {
	sm.state += 0x9E3779B97F4A7C15
	z := sm.state
	z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9
	z = (z ^ (z >> 27)) * 0x94D049BB133111EB
	return z ^ (z >> 31)
}

func FillBufferSplitMix64(seed uint64, buf []byte) {
	sm := NewSplitMix64(seed)
	n := len(buf)
	off := 0
	for off < n {
		val := sm.Next()
		var tmp [8]byte
		binary.LittleEndian.PutUint64(tmp[:], val)
		copied := copy(buf[off:], tmp[:])
		off += copied
	}
}

func FNV1a64(data []byte) uint64 {
	const offsetBasis uint64 = 0xcbf29ce484222325
	const prime uint64 = 0x100000001b3
	hash := offsetBasis
	for _, b := range data {
		hash ^= uint64(b)
		hash *= prime
	}
	return hash
}

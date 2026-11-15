package x1

import (
	"errors"
	"math/bits"
)

// Exported sentinel error values as required by specification.
var (
	ErrDim    = errors.New("E_DIM")
	ErrStride = errors.New("E_STRIDE")
	ErrShort  = errors.New("E_SHORT")
	ErrRect   = errors.New("E_RECT")
	ErrGrid   = errors.New("E_GRID")
)

// Rect defines a 2D rectangle with 32-bit signed integer coordinates and dimensions.
type Rect struct {
	X, Y, W, H int32
}

// RepackBGRXToRGB performs Kernel K1: BGRX32 to RGB24 repack with stride.
func RepackBGRXToRGB(src []byte, w, h, srcStride int, dst []byte, dstStride int) error {
	if w < 0 || w > 16384 || h < 0 || h > 16384 {
		return ErrDim
	}
	if srcStride < 4*w || dstStride < 3*w {
		return ErrStride
	}

	var reqSrcLen, reqDstLen int
	if w > 0 && h > 0 {
		reqSrcLen = (h-1)*srcStride + 4*w
		reqDstLen = (h-1)*dstStride + 3*w
	}

	if len(src) < reqSrcLen || len(dst) < reqDstLen {
		return ErrShort
	}

	if w == 0 || h == 0 {
		return nil
	}

	for y := 0; y < h; y++ {
		srcRow := src[y*srcStride : y*srcStride+4*w]
		dstRow := dst[y*dstStride : y*dstStride+3*w]

		x := 0
		// Process 4 pixels at a time for efficiency
		for ; x <= w-4; x += 4 {
			s := srcRow[4*x : 4*x+16]
			d := dstRow[3*x : 3*x+12]

			// Pixel 0
			d[0] = s[2] // R
			d[1] = s[1] // G
			d[2] = s[0] // B

			// Pixel 1
			d[3] = s[6] // R
			d[4] = s[5] // G
			d[5] = s[4] // B

			// Pixel 2
			d[6] = s[10] // R
			d[7] = s[9]  // G
			d[8] = s[8]  // B

			// Pixel 3
			d[9] = s[14]  // R
			d[10] = s[13] // G
			d[11] = s[12] // B
		}

		// Handle remaining pixels
		for ; x < w; x++ {
			sOff := 4 * x
			dOff := 3 * x
			dstRow[dOff+0] = srcRow[sOff+2] // R
			dstRow[dOff+1] = srcRow[sOff+1] // G
			dstRow[dOff+2] = srcRow[sOff+0] // B
		}
	}

	return nil
}

// DamageTiles performs Kernel K2: damage rectangles to a tile bitset for grid s.
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
	numTiles := gw * gh
	numWords := (numTiles + 63) / 64

	words := make([]uint64, numWords)

	s64 := int64(s)
	w64 := int64(w)
	h64 := int64(h)

	for _, r := range rects {
		rx := int64(r.X)
		ry := int64(r.Y)
		rw := int64(r.W)
		rh := int64(r.H)

		x0 := rx
		if x0 < 0 {
			x0 = 0
		}
		y0 := ry
		if y0 < 0 {
			y0 = 0
		}

		x1 := rx + rw
		if x1 > w64 {
			x1 = w64
		}

		y1 := ry + rh
		if y1 > h64 {
			y1 = h64
		}

		if x1 <= x0 || y1 <= y0 {
			continue
		}

		txMin := int(x0 / s64)
		txMax := int((x1 + s64 - 1) / s64)
		tyMin := int(y0 / s64)
		tyMax := int((y1 + s64 - 1) / s64)

		for ty := tyMin; ty < tyMax; ty++ {
			rowBase := ty * gw
			for tx := txMin; tx < txMax; tx++ {
				t := rowBase + tx
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

// SplitMix64 implementation for pseudorandom fixture data generation.
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

func (sm *SplitMix64) FillBytes(buf []byte) {
	off := 0
	for off < len(buf) {
		val := sm.Next()
		for i := 0; i < 8 && off < len(buf); i++ {
			buf[off] = byte(val >> (i * 8))
			off++
		}
	}
}

// FNV1a64 computes the FNV-1a 64-bit hash over byte slice.
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

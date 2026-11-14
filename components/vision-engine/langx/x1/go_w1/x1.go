package x1

import (
	"errors"
	"math/bits"
)

var (
	ErrDim    = errors.New("E_DIM")
	ErrStride = errors.New("E_STRIDE")
	ErrShort  = errors.New("E_SHORT")
	ErrRect   = errors.New("E_RECT")
	ErrGrid   = errors.New("E_GRID")
)

type Rect struct {
	X, Y, W, H int32
}

// RepackBGRXToRGB converts BGRX32 buffer to RGB24 buffer with strides.
func RepackBGRXToRGB(src []byte, w, h, srcStride int, dst []byte, dstStride int) error {
	if w < 0 || w > 16384 || h < 0 || h > 16384 {
		return ErrDim
	}
	if srcStride < 4*w || dstStride < 3*w {
		return ErrStride
	}
	if w == 0 || h == 0 {
		return nil
	}

	minSrcLen := (h-1)*srcStride + 4*w
	minDstLen := (h-1)*dstStride + 3*w
	if len(src) < minSrcLen || len(dst) < minDstLen {
		return ErrShort
	}

	for y := 0; y < h; y++ {
		srcRow := src[y*srcStride : y*srcStride+4*w]
		dstRow := dst[y*dstStride : y*dstStride+3*w]

		for x := 0; x < w; x++ {
			sOff := 4 * x
			dOff := 3 * x
			b := srcRow[sOff]
			g := srcRow[sOff+1]
			r := srcRow[sOff+2]

			dstRow[dOff] = r
			dstRow[dOff+1] = g
			dstRow[dOff+2] = b
		}
	}

	return nil
}

// DamageTiles maps damage rectangles to tile bitset words and returns words, popcount, error.
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
	numWords := (totalTiles + 63) / 64

	words := make([]uint64, numWords)

	W64 := int64(w)
	H64 := int64(h)
	S64 := int64(s)

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
		if x1 > W64 {
			x1 = W64
		}

		y1 := int64(r.Y) + int64(r.H)
		if y1 > H64 {
			y1 = H64
		}

		if x1 <= x0 || y1 <= y0 {
			continue
		}

		txMin := x0 / S64
		txMax := (x1 + S64 - 1) / S64
		tyMin := y0 / S64
		tyMax := (y1 + S64 - 1) / S64

		for ty := tyMin; ty < tyMax; ty++ {
			rowBase := ty * int64(gw)
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

// SplitMix64 PRNG generator.
type SplitMix64 struct {
	State uint64
}

func (sm *SplitMix64) Next() uint64 {
	sm.State += 0x9E3779B97F4A7C15
	z := sm.State
	z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9
	z = (z ^ (z >> 27)) * 0x94D049BB133111EB
	return z ^ (z >> 31)
}

func FillSplitMix64(seed uint64, buf []byte) {
	sm := SplitMix64{State: seed}
	written := 0
	for written < len(buf) {
		v := sm.Next()
		for i := 0; i < 8 && written < len(buf); i++ {
			buf[written] = byte(v >> (i * 8))
			written++
		}
	}
}

// FNV1a64 hash function.
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

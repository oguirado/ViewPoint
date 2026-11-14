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

	reqSrcLen := (h-1)*srcStride + 4*w
	reqDstLen := (h-1)*dstStride + 3*w
	if len(src) < reqSrcLen || len(dst) < reqDstLen {
		return ErrShort
	}

	for y := 0; y < h; y++ {
		srcRow := src[y*srcStride : y*srcStride+4*w]
		dstRow := dst[y*dstStride : y*dstStride+3*w]
		for x := 0; x < w; x++ {
			b := srcRow[4*x+0]
			g := srcRow[4*x+1]
			r := srcRow[4*x+2]
			dstRow[3*x+0] = r
			dstRow[3*x+1] = g
			dstRow[3*x+2] = b
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
	numTiles := gw * gh
	numWords := (numTiles + 63) / 64
	words := make([]uint64, numWords)

	for _, r := range rects {
		rx0 := int64(r.X)
		ry0 := int64(r.Y)
		rx1 := int64(r.X) + int64(r.W)
		ry1 := int64(r.Y) + int64(r.H)

		x0 := rx0
		if x0 < 0 {
			x0 = 0
		}
		y0 := ry0
		if y0 < 0 {
			y0 = 0
		}

		x1 := rx1
		if x1 > int64(w) {
			x1 = int64(w)
		}
		y1 := ry1
		if y1 > int64(h) {
			y1 = int64(h)
		}

		if x1 <= x0 || y1 <= y0 {
			continue
		}

		txMin := int(x0 / int64(s))
		txMax := int((x1 + int64(s) - 1) / int64(s))
		tyMin := int(y0 / int64(s))
		tyMax := int((y1 + int64(s) - 1) / int64(s))

		for ty := tyMin; ty < tyMax; ty++ {
			for tx := txMin; tx < txMax; tx++ {
				t := ty*gw + tx
				words[t/64] |= (uint64(1) << (t % 64))
			}
		}
	}

	popcount := 0
	for _, wVal := range words {
		popcount += bits.OnesCount64(wVal)
	}

	return words, popcount, nil
}

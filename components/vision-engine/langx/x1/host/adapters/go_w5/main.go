// Thin LANG-X X1 host adapter. Imports the seed package read-only (go.mod
// replace directive) and speaks the binary stdin/stdout protocol documented in
// langx/x1/host/README.md. The only seed-specific line is the import path.
package main

import (
	"bufio"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"os"
	"time"

	x1 "langx/x1/go_w5"
)

const (
	stOK     = 0
	stDim    = 1
	stStride = 2
	stShort  = 3
	stRect   = 4
	stGrid   = 5
	stPanic  = 97
	stOther  = 98
)

var (
	in  = bufio.NewReaderSize(os.Stdin, 1<<20)
	out = bufio.NewWriterSize(os.Stdout, 1<<20)
	le  = binary.LittleEndian
)

func rd(n int) []byte {
	b := make([]byte, n)
	if _, err := io.ReadFull(in, b); err != nil {
		os.Exit(0)
	}
	return b
}
func ri64() int64  { return int64(le.Uint64(rd(8))) }
func ru64() uint64 { return le.Uint64(rd(8)) }
func ru32() uint32 { return le.Uint32(rd(4)) }
func wi32(v int32) { var b [4]byte; le.PutUint32(b[:], uint32(v)); out.Write(b[:]) }
func wu32(v uint32) { var b [4]byte; le.PutUint32(b[:], v); out.Write(b[:]) }
func wu64(v uint64) { var b [8]byte; le.PutUint64(b[:], v); out.Write(b[:]) }

func code(err error) (int32, string) {
	switch {
	case err == nil:
		return stOK, ""
	case errors.Is(err, x1.ErrDim):
		return stDim, ""
	case errors.Is(err, x1.ErrStride):
		return stStride, ""
	case errors.Is(err, x1.ErrShort):
		return stShort, ""
	case errors.Is(err, x1.ErrRect):
		return stRect, ""
	case errors.Is(err, x1.ErrGrid):
		return stGrid, ""
	}
	return stOther, err.Error()
}

func status(st int32, msg string) {
	wi32(st)
	if st == stOther || st == stPanic {
		wu32(uint32(len(msg)))
		out.WriteString(msg)
	}
}

func k1(w, h, ss, ds int, src, dst []byte) (st int32, msg string) {
	defer func() {
		if r := recover(); r != nil {
			st, msg = stPanic, fmt.Sprint(r)
		}
	}()
	return code(x1.RepackBGRXToRGB(src, w, h, ss, dst, ds))
}

func k2(w, h, s int, rects []x1.Rect) (words []uint64, pc int, st int32, msg string) {
	defer func() {
		if r := recover(); r != nil {
			st, msg = stPanic, fmt.Sprint(r)
		}
	}()
	words, pc, err := x1.DamageTiles(w, h, s, rects)
	st, msg = code(err)
	return
}

func readK1() (int, int, int, int, []byte, []byte) {
	w, h, ss, ds := int(ri64()), int(ri64()), int(ri64()), int(ri64())
	sl, dl := ru64(), ru64()
	return w, h, ss, ds, rd(int(sl)), rd(int(dl))
}

func readK2() (int, int, int, []x1.Rect) {
	w, h, s := int(ri64()), int(ri64()), int(ri64())
	n := int(ru64())
	rects := make([]x1.Rect, n)
	for i := range rects {
		b := rd(16)
		rects[i] = x1.Rect{X: int32(le.Uint32(b[0:])), Y: int32(le.Uint32(b[4:])), W: int32(le.Uint32(b[8:])), H: int32(le.Uint32(b[12:]))}
	}
	return w, h, s, rects
}

func main() {
	defer out.Flush()
	for {
		op := rd(1)[0]
		switch op {
		case 0:
			return
		case 1:
			w, h, ss, ds, src, dst := readK1()
			st, msg := k1(w, h, ss, ds, src, dst)
			status(st, msg)
			wu64(uint64(len(dst)))
			out.Write(dst)
		case 2:
			w, h, s, rects := readK2()
			words, pc, st, msg := k2(w, h, s, rects)
			status(st, msg)
			wu64(uint64(pc))
			wu64(uint64(len(words)))
			for _, v := range words {
				wu64(v)
			}
		case 3:
			w, h, ss, ds, src, dst := readK1()
			warm, iters := int(ru32()), int(ru32())
			for i := 0; i < warm; i++ {
				x1.RepackBGRXToRGB(src, w, h, ss, dst, ds)
			}
			wu32(uint32(iters))
			for i := 0; i < iters; i++ {
				t0 := time.Now()
				x1.RepackBGRXToRGB(src, w, h, ss, dst, ds)
				wu64(uint64(time.Since(t0).Nanoseconds()))
			}
		case 4:
			w, h, s, rects := readK2()
			warm, iters := int(ru32()), int(ru32())
			for i := 0; i < warm; i++ {
				x1.DamageTiles(w, h, s, rects)
			}
			wu32(uint32(iters))
			for i := 0; i < iters; i++ {
				t0 := time.Now()
				x1.DamageTiles(w, h, s, rects)
				wu64(uint64(time.Since(t0).Nanoseconds()))
			}
		default:
			fmt.Fprintf(os.Stderr, "bad op %d\n", op)
			os.Exit(2)
		}
		out.Flush()
	}
}

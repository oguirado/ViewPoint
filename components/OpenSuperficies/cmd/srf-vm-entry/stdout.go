package main

import (
	"encoding/binary"
	"path/filepath"

	"opensuperficies.local/srf/src/vmsurface/proto"
)

const pairStdoutLimit = 16 << 20

// stdoutBound retains only the partial length header, never a frame body.
type stdoutBound struct {
	pair, streaming bool
	total           uint64
	header          [8]byte
	nheader, legs   int
	remaining       uint64
}

func newStdoutBound(o proto.Open) stdoutBound {
	b := stdoutBound{streaming: o.StdinCap == -1}
	if len(o.Argv) > 0 {
		b.pair = filepath.Base(o.Argv[0]) == "srf-fbread"
	}
	return b
}

func (b *stdoutBound) accept(p []byte) bool {
	if !b.pair {
		if !b.streaming && b.total+uint64(len(p)) > proto.MaxPayload {
			return false
		}
		if !b.streaming {
			b.total += uint64(len(p))
		}
		return true
	}
	for len(p) > 0 {
		if b.remaining > 0 {
			n := uint64(len(p))
			if n > b.remaining {
				n = b.remaining
			}
			b.remaining -= n
			b.total += n
			p = p[n:]
			continue
		}
		if b.legs == 2 {
			return false
		}
		n := copy(b.header[b.nheader:], p)
		b.nheader += n
		b.total += uint64(n)
		p = p[n:]
		if b.nheader == 8 {
			b.remaining = binary.BigEndian.Uint64(b.header[:])
			b.legs++
			b.nheader = 0
			reserve := uint64((2 - b.legs) * 8)
			if b.remaining > pairStdoutLimit-b.total-reserve {
				return false
			}
		}
	}
	return true
}

func (b *stdoutBound) complete() bool {
	return !b.pair || (b.legs == 2 && b.remaining == 0 && b.nheader == 0)
}

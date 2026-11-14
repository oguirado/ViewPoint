package main

import (
	"encoding/binary"
	"io"

	"opensuperficies.local/srf/src/vmsurface/proto"
)

const maxPairStdout = 16 << 20

// pairBudget recognizes two BE64-length-prefixed legs without retaining bodies.
// Both headers count against the total budget, including the not-yet-seen one.
type pairBudget struct {
	header    [8]byte
	have      int
	legs      int
	remaining uint64
	declared  uint64
}

func stdoutMalformed() error {
	return &proto.Error{Code: 5, Detail: "AUSENTE — malformed or oversized stdout"}
}

func (p *pairBudget) accept(b []byte) error {
	for len(b) > 0 {
		if p.remaining > 0 {
			n := uint64(len(b))
			if n > p.remaining {
				n = p.remaining
			}
			p.remaining -= n
			b = b[n:]
			continue
		}
		if p.legs == 2 {
			return stdoutMalformed()
		}
		n := copy(p.header[p.have:], b)
		p.have += n
		b = b[n:]
		if p.have == len(p.header) {
			size := binary.BigEndian.Uint64(p.header[:])
			// Subtract before adding to reject uint64 overflow as well.
			if size > maxPairStdout-16-p.declared {
				return stdoutMalformed()
			}
			p.declared += size
			p.remaining = size
			p.legs++
			p.have = 0
		}
	}
	return nil
}

func (p *pairBudget) finish() error {
	if p.legs != 2 || p.remaining != 0 || p.have != 0 {
		return stdoutMalformed()
	}
	return nil
}

// relayStdout owns just one wire-payload-sized buffer. Pair reads are parsed as
// they arrive (so a malicious header alone is refused immediately), then coalesced
// into full wire frames. Other output remains interactive. Record has no total
// budget, but uses the same bounded buffer and cancellation-aware output writer.
func relayStdout(r io.Reader, out io.Writer, pair, record bool) error {
	buf := make([]byte, proto.MaxPayload)
	used, total := 0, 0
	var budget pairBudget
	for {
		n, err := r.Read(buf[used:])
		if n > 0 {
			chunk := buf[used : used+n]
			if pair {
				if e := budget.accept(chunk); e != nil {
					return e
				}
			} else if !record {
				if n > proto.MaxPayload-total {
					return stdoutMalformed()
				}
				total += n
			}
			used += n
			if !pair || used == len(buf) {
				if _, e := out.Write(buf[:used]); e != nil {
					return e
				}
				used = 0
			}
		}
		if err != nil {
			if err != io.EOF {
				return err
			}
			if pair {
				if e := budget.finish(); e != nil {
					return e
				}
			}
			if used > 0 {
				_, err = out.Write(buf[:used])
				return err
			}
			return nil
		}
	}
}

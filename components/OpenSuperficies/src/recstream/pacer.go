package recstream

import (
	"io"
	"sync"
)

const Width = 1280
const Height = 900
const FPS = 30

type Stats struct{ Frames, Duplicated, Dropped uint64 }

// A single pending frame bounds memory and provides latest-wins resampling.
// The decoder owns each offered slice and never changes it after offering it.
type pacer struct {
	mu            sync.Mutex
	pending, last []byte
	counters      Stats
}

func (p *pacer) offer(b []byte) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.pending != nil {
		p.counters.Dropped++
	}
	p.pending = b
}
func (p *pacer) tick(w io.Writer) error {
	p.mu.Lock()
	next := p.pending
	p.pending = nil
	duplicate := next == nil
	if duplicate {
		next = p.last
	}
	p.mu.Unlock()
	if next == nil {
		return nil
	}
	n, err := w.Write(next)
	if err != nil {
		return err
	}
	if n != len(next) {
		return io.ErrShortWrite
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	p.last = next
	p.counters.Frames++
	if duplicate {
		p.counters.Duplicated++
	}
	return nil
}
func (p *pacer) stats() Stats { p.mu.Lock(); defer p.mu.Unlock(); return p.counters }

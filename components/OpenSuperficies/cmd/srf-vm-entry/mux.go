package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"opensuperficies.local/srf/src/vmsurface/helpers"
	"opensuperficies.local/srf/src/vmsurface/proto"
	"sync"
	"sync/atomic"
	"time"
)

const streamQueueBytes = 16 << 20

type stream struct {
	id       uint32
	frames   chan proto.Frame
	done     chan struct{}
	once     sync.Once
	terminal atomic.Int32
	queued   atomic.Int64
	space    chan struct{}
	deadline time.Time
}

func (s *stream) close() { s.once.Do(func() { close(s.done) }) }

func (s *stream) consumed(n int) {
	s.queued.Add(-int64(n))
	select {
	case s.space <- struct{}{}:
	default:
	}
}

// enqueue retains at most streamQueueBytes of payload, including the frame
// being written downstream. ReadFrame owns at most one additional frame.
// A blocked stream delays other streams on this port, preserving frame order.
func (s *stream) enqueue(f proto.Frame) {
	select {
	case <-s.done:
		return
	default:
	}
	n := int64(len(f.Payload))
	// The common path needs neither a timer nor a wake-up round trip.
	if s.queued.Load()+n <= streamQueueBytes {
		s.queued.Add(n)
		select {
		case s.frames <- f:
			return
		default:
			s.queued.Add(-n)
		}
	}
	until := time.Now().Add(2 * time.Second)
	if !s.deadline.IsZero() {
		until = s.deadline
	}
	timer := time.NewTimer(time.Until(until))
	defer timer.Stop()
	for s.queued.Load()+n > streamQueueBytes {
		select {
		case <-s.space:
		case <-s.done:
			return
		case <-timer.C:
			s.terminal.Store(6)
			s.close()
			return
		}
	}
	s.queued.Add(n)
	// Bound frame descriptors too, so tiny/empty frames cannot grow metadata
	// without limit. Slot pressure uses the same deadline, never drops frames.
	select {
	case <-s.done:
		s.queued.Add(-n)
	case s.frames <- f:
	case <-timer.C:
		s.queued.Add(-n)
		s.terminal.Store(6)
		s.close()
	}
}

type hub struct {
	conn    net.Conn
	writeMu sync.Mutex
	mu      sync.Mutex
	streams map[uint32]*stream
	next    uint32
	limit   int
	done    chan struct{}
	once    sync.Once
}

func newHub(c net.Conn, limit int) *hub {
	h := &hub{conn: c, streams: map[uint32]*stream{}, limit: limit, done: make(chan struct{})}
	go h.read()
	return h
}
func (h *hub) close() {
	h.once.Do(func() {
		close(h.done)
		h.conn.Close()
		h.mu.Lock()
		defer h.mu.Unlock()
		for _, s := range h.streams {
			s.close()
		}
	})
}
func (h *hub) open() (*stream, error) { return h.openContext(context.Background()) }

func (h *hub) openContext(ctx context.Context) (*stream, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	select {
	case <-h.done:
		return nil, refused(6, "dead peer")
	default:
	}
	if len(h.streams) >= h.limit {
		return nil, refused(7, "stream limit")
	}
	for i := 0; i <= h.limit; i++ {
		h.next++
		if h.next == 0 {
			h.next++
		}
		if h.streams[h.next] == nil {
			deadline, _ := ctx.Deadline()
			s := &stream{id: h.next, frames: make(chan proto.Frame, 1024), done: make(chan struct{}), space: make(chan struct{}, 1), deadline: deadline}
			h.streams[s.id] = s
			return s, nil
		}
	}
	return nil, refused(7, "stream ids exhausted")
}
func (h *hub) remove(s *stream) { h.mu.Lock(); delete(h.streams, s.id); h.mu.Unlock(); s.close() }
func (h *hub) send(f proto.Frame) error {
	h.writeMu.Lock()
	defer h.writeMu.Unlock()
	_ = h.conn.SetWriteDeadline(time.Now().Add(5 * time.Second))
	select {
	case <-h.done:
		return io.ErrClosedPipe
	default:
	}
	e := proto.WriteFrame(h.conn, f)
	if e != nil {
		h.close()
	}
	return e
}
func (h *hub) read() {
	defer h.close()
	for {
		f, e := proto.ReadFrame(h.conn)
		if e != nil {
			var p *proto.Error
			if errors.As(e, &p) && p.Code == 5 {
				h.mu.Lock()
				for _, s := range h.streams {
					s.terminal.Store(5)
				}
				h.mu.Unlock()
			}
			return
		}
		h.mu.Lock()
		s := h.streams[f.Stream]
		h.mu.Unlock()
		if s != nil {
			s.enqueue(f)
		}
	}
}

// relayOne preserves the client's local stream id while allocating a unique guest id.
func relayOne(ctx context.Context, c net.Conn, h *hub) {
	defer c.Close()
	_ = c.SetWriteDeadline(time.Now().Add(5 * time.Second))
	_ = c.SetReadDeadline(time.Now().Add(5 * time.Second))
	f, e := proto.ReadFrame(c)
	if e != nil || f.Type != proto.TOpen || f.Stream == 0 {
		_ = proto.WriteFrame(c, proto.Frame{Type: proto.TExit, Stream: f.Stream, Payload: []byte(`{"rc":5}`)})
		return
	}
	var o proto.Open
	if json.Unmarshal(f.Payload, &o) != nil {
		_ = proto.WriteFrame(c, proto.Frame{Type: proto.TExit, Stream: f.Stream, Payload: []byte(`{"rc":5}`)})
		return
	}
	deadline, err := proto.Allowed(o, helpers.Hashes())
	if err != nil {
		writeExit(c, f.Stream, 2)
		return
	}
	if deadline > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, time.Duration(deadline)*time.Millisecond)
		defer cancel()
	}
	bound := newStdoutBound(o)
	_ = c.SetReadDeadline(time.Time{})
	s, e := h.openContext(ctx)
	if e != nil {
		_ = proto.WriteFrame(c, proto.Frame{Type: proto.TExit, Stream: f.Stream, Payload: []byte(`{"rc":7}`)})
		return
	}
	defer h.remove(s)
	defer h.send(proto.Frame{Type: proto.TCancel, Stream: s.id})
	local := f.Stream
	f.Stream = s.id
	if h.send(f) != nil {
		return
	}
	upstream := make(chan int, 1)
	go func() {
		rc := 6
		defer func() { upstream <- rc }()
		for {
			f, e := proto.ReadFrame(c)
			if e != nil {
				var p *proto.Error
				if errors.As(e, &p) && p.Code == 5 {
					rc = 5
				}
				return
			}
			if f.Stream != local || (f.Type != proto.TStdin && f.Type != proto.TStdinEOF && f.Type != proto.TCancel) {
				rc = 5
				return
			}
			f.Stream = s.id
			if h.send(f) != nil {
				return
			}
			if f.Type == proto.TCancel {
				return
			}
		}
	}()
	for {
		select {
		case f := <-s.frames:
			if f.Type != proto.TStdout && f.Type != proto.TStderr && f.Type != proto.TExit {
				writeExit(c, local, 5)
				return
			}
			if f.Type == proto.TStdout && !bound.accept(f.Payload) {
				writeExit(c, local, 5)
				return
			}
			if f.Type == proto.TExit {
				var x proto.Exit
				if json.Unmarshal(f.Payload, &x) != nil || x.RC < 0 || x.RC > 255 || x.RC == 1 || (x.RC == 0 && !bound.complete()) {
					writeExit(c, local, 5)
					return
				}
			}
			f.Stream = local
			_ = c.SetWriteDeadline(time.Now().Add(5 * time.Second))
			if proto.WriteFrame(c, f) != nil {
				return
			}
			s.consumed(len(f.Payload))
			if f.Type == proto.TExit {
				return
			}
		case rc := <-upstream:
			writeExit(c, local, rc)
			return
		case <-s.done:
			rc := int(s.terminal.Load())
			if rc == 0 {
				rc = 6
			}
			writeExit(c, local, rc)
			return
		case <-ctx.Done():
			rc := int(s.terminal.Load())
			if rc == 0 {
				rc = 6
			}
			writeExit(c, local, rc)
			return
		}
	}
}
func relayView(ctx context.Context, c net.Conn, h *hub) {
	defer c.Close()
	s, e := h.openContext(ctx)
	if e != nil {
		return
	}
	defer h.remove(s)
	defer h.send(proto.Frame{Type: proto.TViewClose, Stream: s.id})
	if h.send(proto.Frame{Type: proto.TViewOpen, Stream: s.id}) != nil {
		return
	}
	upstream := make(chan struct{})
	go func() {
		defer close(upstream)
		buf := make([]byte, 32*1024)
		for {
			n, e := c.Read(buf)
			if n > 0 {
				if h.send(proto.Frame{Type: proto.TViewData, Stream: s.id, Payload: buf[:n]}) != nil {
					return
				}
			}
			if e != nil {
				return
			}
		}
	}()
	for {
		select {
		case f := <-s.frames:
			if f.Type == proto.TViewClose {
				return
			}
			if f.Type != proto.TViewData {
				return
			}
			_ = c.SetWriteDeadline(time.Now().Add(5 * time.Second))
			if _, e = c.Write(f.Payload); e != nil {
				return
			}
			s.consumed(len(f.Payload))
		case <-upstream:
			return
		case <-s.done:
			return
		case <-ctx.Done():
			return
		}
	}
}

func client(ctx context.Context, c net.Conn, o proto.Open, in io.ReadCloser, out, errout io.Writer) int {
	if deadline, err := proto.Allowed(o, helpers.Hashes()); err == nil && deadline > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, time.Duration(deadline)*time.Millisecond)
		defer cancel()
	}
	defer c.Close()
	defer in.Close()
	payload, e := json.Marshal(o)
	if e != nil {
		return 5
	}
	var mu sync.Mutex
	send := func(f proto.Frame) error {
		mu.Lock()
		defer mu.Unlock()
		_ = c.SetWriteDeadline(time.Now().Add(5 * time.Second))
		return proto.WriteFrame(c, f)
	}
	if send(proto.Frame{Type: proto.TOpen, Stream: 1, Payload: payload}) != nil {
		return 6
	}
	done := make(chan struct{})
	defer close(done)
	go func() {
		select {
		case <-ctx.Done():
			_ = send(proto.Frame{Type: proto.TCancel, Stream: 1})
			c.Close()
		case <-done:
		}
	}()
	go func() {
		hadInput := false
		buf := make([]byte, 32*1024)
		for {
			n, e := in.Read(buf)
			if n > 0 {
				hadInput = true
				if send(proto.Frame{Type: proto.TStdin, Stream: 1, Payload: buf[:n]}) != nil {
					return
				}
			}
			if e != nil {
				if e == io.EOF {
					// docker exec starts the recorder without -i; its initial
					// stdin EOF is not a request to terminate the event stream.
					// Signals and loss of the shim connection still CANCEL it.
					if o.StdinCap != -1 || hadInput {
						_ = send(proto.Frame{Type: proto.TStdinEOF, Stream: 1})
					}
				} else {
					_ = send(proto.Frame{Type: proto.TCancel, Stream: 1})
					c.Close()
				}
				return
			}
		}
	}()
	bound := newStdoutBound(o)
	var tail []byte
	defer func() { _, _ = errout.Write(tail) }()
	for {
		f, e := proto.ReadFrame(c)
		if e != nil {
			if ctx.Err() != nil {
				return 6
			}
			var p *proto.Error
			if errors.As(e, &p) && p.Code == 5 {
				_ = send(proto.Frame{Type: proto.TCancel, Stream: 1})
				return 5
			}
			return 6
		}
		if f.Stream != 1 {
			return 5
		}
		switch f.Type {
		case proto.TStdout:
			if !bound.accept(f.Payload) {
				_ = send(proto.Frame{Type: proto.TCancel, Stream: 1})
				return 5
			}
			if _, e = out.Write(f.Payload); e != nil {
				_ = send(proto.Frame{Type: proto.TCancel, Stream: 1})
				return 6
			}
		case proto.TStderr:
			tail = append(tail, f.Payload...)
			if len(tail) > 4096 {
				tail = tail[len(tail)-4096:]
			}
		case proto.TExit:
			var x proto.Exit
			if json.Unmarshal(f.Payload, &x) != nil || x.RC < 0 || x.RC > 255 || x.RC == 1 {
				return 5
			}
			if x.RC == 0 && !bound.complete() {
				_ = send(proto.Frame{Type: proto.TCancel, Stream: 1})
				return 5
			}
			return x.RC
		default:
			return 5
		}
	}
}

func writeExit(c net.Conn, id uint32, rc int) {
	_ = c.SetWriteDeadline(time.Now().Add(5 * time.Second))
	p, _ := json.Marshal(proto.Exit{RC: rc})
	_ = proto.WriteFrame(c, proto.Frame{Type: proto.TExit, Stream: id, Payload: p})
}

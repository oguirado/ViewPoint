package main

import (
	"context"
	"errors"
	"io"
	"net"
	"opensuperficies.local/srf/src/vmsurface/proto"
	"sync"
	"time"
)

// serveView carries independently bounded xpra connections on the dedicated view port.
func serveView(parent context.Context, port io.ReadWriteCloser, path string) error {
	ctx, cancel := context.WithCancel(parent)
	defer cancel()
	defer port.Close()
	w := &sender{conn: port}
	var mu sync.Mutex
	var wg sync.WaitGroup
	type view struct {
		conn   net.Conn
		input  chan []byte
		cancel context.CancelFunc
		done   chan struct{}
	}
	views := map[uint32]*view{}
	done := make(chan struct{})
	defer close(done)
	go func() {
		select {
		case <-ctx.Done():
			port.Close()
		case <-done:
		}
	}()
	defer func() {
		cancel()
		mu.Lock()
		for _, v := range views {
			v.cancel()
			v.conn.Close()
		}
		mu.Unlock()
		wg.Wait()
	}()
	for {
		f, e := proto.ReadFrame(port)
		if e != nil {
			var pe *proto.Error
			if errors.As(e, &pe) {
				_ = w.exit(f.Stream, 5)
			}
			return e
		}
		mu.Lock()
		v := views[f.Stream]
		full := len(views) >= 32
		mu.Unlock()
		if f.Type == proto.TViewOpen {

			if v != nil {
				v.cancel()
				v.conn.Close()
				// Wait for its close frame and cleanup before reusing the wire id.
				<-v.done
				full = false
			}
			if full || len(f.Payload) != 0 {
				_ = w.send(proto.Frame{Type: proto.TViewClose, Stream: f.Stream})
				continue
			}
			conn, e := net.DialTimeout("unix", path, time.Second)
			if e != nil {
				_ = w.send(proto.Frame{Type: proto.TViewClose, Stream: f.Stream})
				continue
			}
			vc, stop := context.WithCancel(ctx)
			v = &view{conn, make(chan []byte, 4), stop, make(chan struct{})}
			mu.Lock()
			views[f.Stream] = v
			mu.Unlock()
			wg.Add(1)
			go func(id uint32, v *view) {
				defer wg.Done()
				defer close(v.done)
				defer v.cancel()
				defer v.conn.Close()
				ended := make(chan struct{})
				go func() {
					defer close(ended)
					for {
						select {
						case <-vc.Done():
							return
						case b := <-v.input:
							v.conn.SetWriteDeadline(time.Now().Add(2 * time.Second))
							if e := writeBytes(v.conn, b); e != nil {
								v.cancel()
								v.conn.Close()
								return
							}
						}
					}
				}()
				buf := make([]byte, 65536)
				for {
					n, e := v.conn.Read(buf)
					if n > 0 {
						if e := w.send(proto.Frame{Type: proto.TViewData, Stream: id, Payload: buf[:n]}); e != nil {
							cancel()
							port.Close()
							break
						}
					}
					if e != nil {
						break
					}
				}
				v.cancel()
				v.conn.Close()
				<-ended
				if w.send(proto.Frame{Type: proto.TViewClose, Stream: id}) != nil {
					cancel()
					port.Close()
				}
				mu.Lock()
				delete(views, id)
				mu.Unlock()
			}(f.Stream, v)
			continue
		}
		if v == nil {
			_ = w.send(proto.Frame{Type: proto.TViewClose, Stream: f.Stream})
			continue
		}
		switch f.Type {
		case proto.TViewClose:
			v.cancel()
			v.conn.Close()
		case proto.TViewData:
			select {
			case v.input <- f.Payload:
			default:
				v.cancel()
				v.conn.Close()
			}
		default:
			v.cancel()
			v.conn.Close()
		}
	}
}
func writeBytes(w io.Writer, b []byte) error {
	for len(b) > 0 {
		n, e := w.Write(b)
		if e != nil {
			return e
		}
		if n <= 0 {
			return io.ErrShortWrite
		}
		b = b[n:]
	}
	return nil
}

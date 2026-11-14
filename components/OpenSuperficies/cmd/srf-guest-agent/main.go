// srf-guest-agent executes only the frozen VM relay grammar, as its current uid.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"opensuperficies.local/srf/src/vmsurface/helpers"
	"opensuperficies.local/srf/src/vmsurface/proto"
	"os"
	"os/exec"
	"os/signal"
	"sync"
	"syscall"
	"time"
)

type commandFunc func(context.Context, []string) *exec.Cmd

func realCommand(ctx context.Context, a []string) *exec.Cmd {
	path := a[0]
	switch path {
	case "xdotool":
		path = "/usr/bin/xdotool"
	case "python3":
		path = "/usr/bin/python3"
	case "srf-fbread":
		path = "/usr/local/libexec/srf-fbread"
	case "identity-entrypoint.sh":
		path = "/usr/local/bin/identity-entrypoint.sh"
	}
	cmd := exec.CommandContext(ctx, path, a[1:]...)
	cmd.Args[0] = a[0]
	return cmd
}
func main() { os.Exit(run()) }
func run() int {
	flags := flag.NewFlagSet("srf-guest-agent", flag.ContinueOnError)
	port := flags.String("port", "", "agent port device")
	view := flags.String("view-port", "", "optional view port device")
	if flags.Parse(os.Args[1:]) != nil || *port == "" || flags.NArg() != 0 {
		fmt.Fprintln(os.Stderr, "AUSENTE — --port device required")
		return 2
	}
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer cancel()
	f, e := os.OpenFile(*port, os.O_RDWR, 0)
	if e != nil {
		fmt.Fprintln(os.Stderr, "AUSENTE — agent port unavailable")
		return 3
	}
	if *view != "" {
		v, e := os.OpenFile(*view, os.O_RDWR, 0)
		if e != nil {
			f.Close()
			fmt.Fprintln(os.Stderr, "AUSENTE — view port unavailable")
			return 3
		}
		go func() { _ = serveView(ctx, v, "/run/srfview/xpra.sock"); cancel(); f.Close() }()
	}
	if e := serve(ctx, f, realCommand, nil); e != nil {
		return 6
	}
	return 0
}

type stream struct {
	cancel     context.CancelFunc
	input      chan []byte
	eof        bool
	cap, total int64
	forced     int
	mu         sync.Mutex
}

func (s *stream) stop(rc int) {
	s.mu.Lock()
	if s.forced == 0 {
		s.forced = rc
	}
	s.mu.Unlock()
	s.cancel()
}

type sender struct {
	conn io.ReadWriteCloser
	mu   sync.Mutex
	// Reused only for devices that cannot set write deadlines, never per frame.
	stall *time.Timer
}

func (w *sender) send(f proto.Frame) (err error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	defer func() {
		if err != nil {
			w.conn.Close()
		}
	}()
	if c, ok := w.conn.(interface{ SetWriteDeadline(time.Time) error }); ok {
		if err := c.SetWriteDeadline(time.Now().Add(2 * time.Second)); err == nil {
			return proto.WriteFrame(w.conn, f)
		}
	}
	// A serial device may not support deadlines. Keep one reusable watchdog
	// for its output streams; closing the device interrupts a stalled write.
	if w.stall == nil {
		w.stall = time.AfterFunc(2*time.Second, func() { w.conn.Close() })
	} else {
		w.stall.Reset(2 * time.Second)
	}
	defer w.stall.Stop()
	return proto.WriteFrame(w.conn, f)
}
func (w *sender) exit(id uint32, rc int) error {
	b, _ := json.Marshal(proto.Exit{RC: rc})
	return w.send(proto.Frame{Type: proto.TExit, Stream: id, Payload: b})
}

func serve(parent context.Context, c io.ReadWriteCloser, command commandFunc, testEnv []string) error {
	ctx, cancel := context.WithCancel(parent)
	defer cancel()
	defer c.Close()
	w := &sender{conn: c}
	streams := map[uint32]*stream{}
	var mu sync.Mutex
	var workers sync.WaitGroup
	// A context or dead peer cancels every process group and interrupts port I/O.
	watcherDone := make(chan struct{})
	defer close(watcherDone)
	go func() {
		select {
		case <-ctx.Done():
			c.Close()
		case <-watcherDone:
		}
	}()
	defer func() {
		cancel()
		mu.Lock()
		for _, s := range streams {
			s.stop(6)
		}
		mu.Unlock()
		workers.Wait()
	}()
	for {
		f, e := proto.ReadFrame(c)
		if e != nil {
			var pe *proto.Error
			if errors.As(e, &pe) {
				_ = w.exit(f.Stream, 5)
			}
			if e == io.EOF {
				return nil
			}
			return e
		}
		mu.Lock()
		s := streams[f.Stream]
		mu.Unlock()
		if f.Type == proto.TOpen {
			if s != nil {
				s.stop(5)
				continue
			}
			var o proto.Open
			if e := json.Unmarshal(f.Payload, &o); e != nil {
				if e = w.exit(f.Stream, 5); e != nil {
					return e
				}
				continue
			}
			timeout, e := proto.Allowed(o, helpers.Hashes())
			if e != nil {
				if e = w.exit(f.Stream, 2); e != nil {
					return e
				}
				continue
			}
			mu.Lock()
			full := len(streams) >= 64
			mu.Unlock()
			if full {
				if e = w.exit(f.Stream, 5); e != nil {
					return e
				}
				continue
			}
			var streamCtx context.Context
			var stop context.CancelFunc
			if timeout > 0 {
				streamCtx, stop = context.WithTimeout(ctx, time.Duration(timeout)*time.Millisecond)
			} else {
				streamCtx, stop = context.WithCancel(ctx)
			}
			s = &stream{cancel: stop, input: make(chan []byte, 4), cap: stdinLimit(o)}
			mu.Lock()
			streams[f.Stream] = s
			mu.Unlock()
			workers.Add(1)
			go func(id uint32, s *stream, o proto.Open) {
				defer workers.Done()
				defer stop()
				rc := execute(streamCtx, s, o, w, id, command, testEnv)
				mu.Lock()
				delete(streams, id)
				mu.Unlock()
				// The previous command is fully stopped before the stream can be reused.
				if w.exit(id, rc) != nil {
					cancel()
					c.Close()
				}
			}(f.Stream, s, o)
			continue
		}
		if s == nil {
			if e = w.exit(f.Stream, 5); e != nil {
				return e
			}
			continue
		}
		switch f.Type {
		case proto.TCancel:
			if len(f.Payload) != 0 {
				s.stop(5)
			} else {
				s.stop(6)
			}
		case proto.TStdinEOF:
			if s.eof || len(f.Payload) != 0 {
				s.stop(5)
				continue
			}
			s.eof = true
			if s.cap == -1 {
				s.stop(6)
			} else {
				close(s.input)
			}
		case proto.TStdin:
			if s.eof {
				s.stop(5)
				continue
			}
			s.total += int64(len(f.Payload))
			if s.cap >= 0 && s.total > s.cap {
				s.stop(5)
				continue
			}
			select {
			case s.input <- f.Payload:
			default:
				s.stop(5)
			}
		default:
			s.stop(5)
		}
	}
}
func stdinLimit(o proto.Open) int64 {
	cap, err := proto.StdinLimit(o, helpers.Hashes())
	if err != nil {
		return 0
	}
	return cap
}

func execute(ctx context.Context, s *stream, o proto.Open, w *sender, id uint32, command commandFunc, testEnv []string) int {
	cmd := command(ctx, o.Argv)
	cmd.Env = []string{"PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME=/profile"}
	var env []string
	for k, v := range o.Env {
		env = append(env, k+"="+v)
	}
	for k, v := range proto.ForwardEnv(env) {
		cmd.Env = append(cmd.Env, k+"="+v)
	}
	// Only tests inject this environment; the production caller passes nil.
	cmd.Env = append(cmd.Env, testEnv...)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Cancel = func() error {
		if cmd.Process == nil {
			return os.ErrProcessDone
		}
		e := syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
		if e == syscall.ESRCH {
			return os.ErrProcessDone
		}
		return e
	}
	inR, inW, e := os.Pipe()
	if e != nil {
		return 3
	}
	defer inR.Close()
	defer inW.Close()
	cmd.Stdin = inR
	stdout, e := cmd.StdoutPipe()
	if e != nil {
		return 3
	}
	defer stdout.Close()
	stderr, e := cmd.StderrPipe()
	if e != nil {
		return 3
	}
	defer stderr.Close()
	if e = cmd.Start(); e != nil {
		s.mu.Lock()
		forced := s.forced
		s.mu.Unlock()
		if forced != 0 {
			return forced
		}
		if ctx.Err() != nil {
			return 6
		}
		return 3
	}
	inR.Close()
	// Own both drains: Wait must never close a pipe while it still has output.
	// The entry context also bounds inherited pipes held open by descendants.
	drains := make(chan error, 2)
	framePair := o.Argv[0] == "srf-fbread" || o.Argv[0] == "/usr/local/libexec/srf-fbread"
	for _, p := range []struct {
		r io.Reader
		t proto.Type
	}{{stdout, proto.TStdout}, {stderr, proto.TStderr}} {
		go func(r io.Reader, typ proto.Type) {
			out := &output{ctx, w, id, typ}
			if typ == proto.TStdout {
				drains <- relayStdout(r, out, framePair, stdinLimit(o) == -1)
				return
			}
			_, err := io.CopyBuffer(out, r, make([]byte, proto.MaxPayload))
			drains <- err
		}(p.r, p.t)
	}
	ioDone := make(chan struct{})
	go func() {
		select {
		case <-ctx.Done():
			stdout.Close()
			stderr.Close()
			inW.Close()
		case <-ioDone:
		}
	}()
	inputDone := make(chan struct{})
	go func() {
		defer close(inputDone)
		defer inW.Close()
		for {
			select {
			case <-ctx.Done():
				inW.Close()
				return
			case b, ok := <-s.input:
				if !ok {
					return
				}
				if _, e := inW.Write(b); e != nil {
					return
				}
			}
		}
	}()
	var drainErr error
	for i := 0; i < 2; i++ {
		if err := <-drains; err != nil {
			drainErr = err
			var pe *proto.Error
			if errors.As(err, &pe) && ctx.Err() == nil {
				s.stop(pe.Code)
			} else {
				s.cancel()
			}
		}
	}
	e = cmd.Wait()
	ctxErr := ctx.Err()
	s.cancel()
	inR.Close()
	inW.Close()
	<-inputDone
	close(ioDone)
	// Descendants that outlive the direct child must also be reaped by PID 1, never escape the stream.
	if cmd.Process != nil {
		_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
	}
	s.mu.Lock()
	forced := s.forced
	s.mu.Unlock()
	if forced != 0 {
		return forced
	}
	// Inspect the entry context before cleanup cancellation.
	if ctxErr != nil || drainErr != nil {
		return 6
	}
	if e == nil {
		return 0
	}
	var ee *exec.ExitError
	if errors.As(e, &ee) {
		rc := ee.ExitCode()
		if rc == 0 {
			return 0
		}
		if rc >= 2 && rc <= 7 {
			return rc
		}
		return 6
	}
	return 3
}

type output struct {
	ctx context.Context
	w   *sender
	id  uint32
	typ proto.Type
}

func (o *output) Write(b []byte) (int, error) {
	n := 0
	for len(b) > 0 {
		select {
		case <-o.ctx.Done():
			return n, o.ctx.Err()
		default:
		}
		size := len(b)
		if size > proto.MaxPayload {
			size = proto.MaxPayload
		}
		if e := o.w.send(proto.Frame{Type: o.typ, Stream: o.id, Payload: b[:size]}); e != nil {
			return n, e
		}
		n += size
		b = b[size:]
	}
	return n, nil
}

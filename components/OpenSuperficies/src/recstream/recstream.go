package recstream

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"opensuperficies.local/srf/src/cubook/capture"
	"opensuperficies.local/srf/src/ramcap"
)

const DefaultMaxBytes int64 = 1 << 30
const DefaultMaxDuration = 10 * time.Minute
const startupTimeout = 5 * time.Second
const shutdownTimeout = 3 * time.Second

type Config struct {
	Context          context.Context
	RunDir           string
	ResolveContainer func(context.Context, int) (string, error)
	Launch           Launcher
	MaxDuration      time.Duration
	MaxBytes         int64
}
type Engine struct {
	mu         sync.Mutex
	cfg        Config
	sessions   map[string]*session
	closed     bool
	parentStop func() bool
	ticker     func(time.Duration) (<-chan time.Time, func())
}
type session struct {
	mu            sync.Mutex // protects export/reap against each other
	id, dir       string
	stop          chan struct{}
	once          sync.Once
	done, started chan struct{}
	err           error // published by closing done
	reason        string
	pace          pacer
}

func New(cfg Config) (*Engine, error) {
	if cfg.Context != nil && cfg.Context.Err() != nil {
		return nil, cfg.Context.Err()
	}
	if cfg.ResolveContainer == nil {
		return nil, errors.New("E_NO_CAPSULE: resolver absent")
	}
	if cfg.MaxDuration == 0 {
		cfg.MaxDuration = DefaultMaxDuration
	}
	if cfg.MaxBytes == 0 {
		cfg.MaxBytes = DefaultMaxBytes
	}
	if cfg.MaxDuration < 0 || cfg.MaxDuration > DefaultMaxDuration || cfg.MaxBytes < 32 || cfg.MaxBytes > DefaultMaxBytes {
		return nil, errors.New("E_LIMIT: invalid recording caps")
	}
	if cfg.Launch == nil {
		cfg.Launch = launch
	}
	if err := checkRAM(cfg.RunDir); err != nil {
		return nil, err
	}
	e := &Engine{cfg: cfg, sessions: map[string]*session{}, ticker: func(d time.Duration) (<-chan time.Time, func()) { t := time.NewTicker(d); return t.C, t.Stop }}
	if cfg.Context != nil {
		e.mu.Lock()
		e.parentStop = context.AfterFunc(cfg.Context, func() { _ = e.Close() })
		e.mu.Unlock()
	}
	return e, nil
}
func checkRAM(dir string) error {
	const root = "/dev/shm/vqa-ramws"
	if !filepath.IsAbs(dir) {
		return errors.New("E_RAM_NOT_TMPFS")
	}
	resolved, err := filepath.EvalSymlinks(dir)
	if err != nil || resolved != filepath.Clean(dir) || !strings.HasPrefix(resolved, root+"/") {
		return errors.New("E_RAM_NOT_TMPFS")
	}
	g := capture.PathGuard{Root: root, FSType: capture.MountFSType}
	if err = g.Check(resolved); err != nil {
		return err
	}
	st, err := os.Lstat(resolved)
	if err != nil {
		return err
	}
	if !st.IsDir() || st.Mode().Perm() != 0700 {
		return errors.New("E_RAM_NOT_TMPFS: private directory required")
	}
	return nil
}
func (e *Engine) Start(ctx context.Context, slot int, xid uint32, ttl int64) (string, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if err := ctx.Err(); err != nil {
		return "", err
	}
	if e.cfg.Context != nil && e.cfg.Context.Err() != nil {
		return "", e.cfg.Context.Err()
	}
	if e.closed || len(e.sessions) != 0 {
		return "", errors.New("REFUSED: recording session retained")
	}
	if ttl <= 0 {
		return "", errors.New("E_LIMIT: invalid TTL")
	}
	container, err := e.cfg.ResolveContainer(ctx, slot)
	if err != nil {
		return "", err
	}
	if container == "" || strings.HasPrefix(container, "-") {
		return "", errors.New("E_NO_CAPSULE")
	}
	if err = checkRAM(e.cfg.RunDir); err != nil {
		return "", err
	}
	dir, err := os.MkdirTemp(e.cfg.RunDir, "rec-")
	if err != nil {
		return "", err
	}
	s := &session{id: filepath.Base(dir), dir: dir, stop: make(chan struct{}), done: make(chan struct{}), started: make(chan struct{})}
	duration := e.cfg.MaxDuration
	if ttl < int64(duration/time.Millisecond) {
		duration = time.Duration(ttl) * time.Millisecond
	}
	go e.record(s, container, xid, duration)
	timer := time.NewTimer(startupTimeout)
	defer timer.Stop()
	select {
	case <-s.started:
		// The caller's short operation context must not own a long recording.
		if err = ctx.Err(); err == nil {
			e.sessions[s.id] = s
			return s.id, nil
		}
	case <-s.done:
		err = s.err
		if err == nil {
			err = errors.New("E_STREAM: ended during startup")
		}
	case <-ctx.Done():
		err = ctx.Err()
	case <-timer.C:
		err = errors.New("E_TIMEOUT: first frame")
	}
	s.requestStop()
	<-s.done // record has its own bounded process teardown
	_ = os.RemoveAll(dir)
	return "", err
}
func (s *session) requestStop() { s.once.Do(func() { close(s.stop) }) }
func (e *Engine) get(id string) (*session, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	s := e.sessions[id]
	if s == nil {
		return nil, errors.New("E_SESSION: unknown recording")
	}
	return s, nil
}
func (e *Engine) Stop(ctx context.Context, id string) error {
	s, err := e.get(id)
	if err != nil {
		return err
	}
	s.requestStop()
	select {
	case <-s.done:
		return s.err
	case <-ctx.Done():
		return ctx.Err()
	}
}
func (e *Engine) SizeBytes(ctx context.Context, id string) (int64, error) {
	if err := ctx.Err(); err != nil {
		return 0, err
	}
	s, err := e.get(id)
	if err != nil {
		return 0, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	st, err := os.Stat(filepath.Join(s.dir, "video-000.mp4"))
	if err != nil {
		return 0, err
	}
	return st.Size(), nil
}
func (e *Engine) Stats(id string) (Stats, error) {
	s, err := e.get(id)
	if err != nil {
		return Stats{}, err
	}
	return s.pace.stats(), nil
}
func (e *Engine) Reap(ctx context.Context, id string) error {
	if id == "" {
		return nil
	}
	s, err := e.get(id)
	if err != nil {
		return err
	}
	_ = e.Stop(ctx, id)
	select {
	case <-s.done:
	case <-ctx.Done():
		return ctx.Err()
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if err = os.RemoveAll(s.dir); err != nil {
		return err
	}
	e.mu.Lock()
	delete(e.sessions, id)
	e.mu.Unlock()
	return nil
}
func (e *Engine) Close() error {
	e.mu.Lock()
	e.closed = true
	ids := make([]string, 0, len(e.sessions))
	for id := range e.sessions {
		ids = append(ids, id)
	}
	parentStop := e.parentStop
	e.mu.Unlock()
	if parentStop != nil {
		parentStop()
	}
	var result error
	for _, id := range ids {
		ctx, cancel := context.WithTimeout(context.Background(), 4*shutdownTimeout)
		err := e.Reap(ctx, id)
		cancel()
		result = errors.Join(result, err)
	}
	return result
}
func waitProcess(done <-chan error, cancel context.CancelFunc) error {
	timer := time.NewTimer(shutdownTimeout)
	defer timer.Stop()
	select {
	case err := <-done:
		return err
	case <-timer.C:
		cancel()
	}
	// exec.Cmd.WaitDelay and CommandContext bound this second wait.
	timer.Reset(shutdownTimeout)
	select {
	case err := <-done:
		return err
	case <-timer.C:
		return errors.New("E_TIMEOUT: process teardown")
	}
}
func (e *Engine) record(s *session, container string, xid uint32, duration time.Duration) {
	defer close(s.done)
	f, err := os.OpenFile(filepath.Join(s.dir, "video-000.mp4"), os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		s.err = err
		return
	}
	defer f.Close()
	binding, err := nativeBinding(s.id, container, xid)
	if err != nil {
		s.err = err
		return
	}
	s.reason = "recording"
	if err = s.writeMetadata(); err != nil {
		s.err = err
		return
	}
	streamCtx, stopStream := context.WithTimeout(context.Background(), duration+startupTimeout+2*shutdownTimeout)
	encCtx, stopEncoder := context.WithTimeout(context.Background(), duration+startupTimeout+2*shutdownTimeout)
	defer stopStream()
	defer stopEncoder()
	controlR, controlW := io.Pipe()
	wireR, wireW := io.Pipe()
	pixelsR, pixelsW := io.Pipe()
	defer controlR.Close()
	defer controlW.Close()
	defer wireR.Close()
	defer wireW.Close()
	defer pixelsR.Close()
	defer pixelsW.Close()
	// Closing every pipe also unblocks the io.Copy goroutines owned by os/exec.
	closeStream := context.AfterFunc(streamCtx, func() { controlR.Close(); controlW.Close(); wireR.Close(); wireW.Close() })
	defer closeStream()
	closeEncoder := context.AfterFunc(encCtx, func() { pixelsR.Close(); pixelsW.Close() })
	defer closeEncoder()
	sink := newMP4Sink(f, e.cfg.MaxBytes)
	encoder, err := e.cfg.Launch(encCtx, encoderArgs(), pixelsR, sink)
	if err != nil {
		s.err = err
		return
	}
	encDone := make(chan error, 1)
	go func() { err := encoder.Wait(); pixelsR.CloseWithError(err); encDone <- err }()
	stream, err := e.cfg.Launch(streamCtx, []string{"docker", "exec", "-i", container, "/usr/local/bin/srf-fbread", "stream", ":100", strconv.FormatUint(uint64(xid), 10)}, controlR, wireW)
	if err != nil {
		pixelsW.Close()
		_ = waitProcess(encDone, stopEncoder)
		s.err = err
		return
	}
	streamDone := make(chan error, 1)
	go func() { err := stream.Wait(); wireW.CloseWithError(err); streamDone <- err }()
	sourceDone := make(chan error, 1)
	first := make(chan struct{})
	upstream := newStreamControl(controlW, binding)
	go func() { defer close(sourceDone); sourceDone <- decodeStream(wireR, upstream, binding, &s.pace, first) }()
	limitCtx, stopLimit := context.WithTimeout(context.Background(), duration)
	defer stopLimit()
	// Let a raw frame already being written drain in full. Only a wedged
	// encoder needs a forced pipe close; normal Stop/caps reach the loop below
	// and send clean EOF between frames.
	closeAfterGrace := func() {
		grace := time.NewTimer(shutdownTimeout)
		defer grace.Stop()
		select {
		case <-s.done:
		case <-grace.C:
			pixelsW.Close()
		}
	}
	closeLimit := context.AfterFunc(limitCtx, closeAfterGrace)
	defer closeLimit()
	go func() {
		select {
		case <-s.stop:
			closeAfterGrace()
		case <-s.done:
		}
	}()
	timer := time.NewTimer(duration)
	defer timer.Stop()
	var sourceErr error
	select {
	case <-first:
		if err = s.pace.tick(pixelsW); err == nil {
			close(s.started)
		}
	case sourceErr = <-sourceDone:
		err = sourceErr
	case <-s.stop:
		err = errors.New("E_CANCELLED: startup")
	case <-timer.C:
		err = errors.New("E_TIMEOUT: first frame")
	}
	ticks, stopTicks := e.ticker(time.Second / FPS)
	defer stopTicks()
	if err == nil {
	running:
		for {
			select {
			case <-ticks:
				select {
				case <-s.stop:
					s.reason = "stopped"
					break running
				default:
				}
				if limitCtx.Err() != nil {
					s.reason = "duration-cap"
					break running
				}
				if err = s.pace.tick(pixelsW); err != nil {
					break running
				}
			case sourceErr = <-sourceDone:
				err = sourceErr
				s.reason = "source-ended"
				break running
			case <-s.stop:
				s.reason = "stopped"
				break running
			case <-timer.C:
				s.reason = "duration-cap"
				break running
			}
		}
	}
	// A stop/deadline can interrupt a blocked pipe write as well as an idle tick.
	select {
	case <-s.stop:
		if sourceErr == nil && (err == nil || errors.Is(err, io.ErrClosedPipe)) {
			err = nil
		}
		s.reason = "stopped"
	default:
	}
	if limitCtx.Err() != nil {
		if sourceErr == nil && (err == nil || errors.Is(err, io.ErrClosedPipe)) {
			err = nil
		}
		s.reason = "duration-cap"
	}
	// EOF on encoder stdin drains/finalizes H.264 before its lifetime is cancelled.
	pixelsW.Close()
	// Request the remote reader's terminal record before dropping docker's
	// attachment. Closing stdin after STOP also lets os/exec's copier finish.
	controlDone := make(chan struct{})
	go func() { _ = upstream.stop(); controlW.Close(); close(controlDone) }()
	_ = waitProcess(streamDone, stopStream)
	stopStream()
	wireR.Close()
	select {
	case <-controlDone:
	case <-time.After(shutdownTimeout):
	}
	select {
	case <-sourceDone:
	case <-time.After(shutdownTimeout):
	}
	encoderErr := waitProcess(encDone, stopEncoder)
	if sink.limited && sourceErr == nil {
		err = nil
		encoderErr = nil
		s.reason = "byte-cap"
	}
	if err == nil {
		err = encoderErr
	}
	if finishErr := sink.finish(); err == nil {
		err = finishErr
	}
	if err == nil {
		err = f.Sync()
	}
	if err == nil {
		err = s.writeMetadata()
	}
	s.err = err
}
func decodeStream(r io.Reader, control *streamControl, b ramcap.SeatBinding, p *pacer, first chan<- struct{}) error {
	if err := control.hello(); err != nil {
		return err
	}
	d := ramcap.NewStreamDecoder(r, b, control.host.Ledger())
	initial := true
	for {
		rec, err := d.Next()
		if err != nil {
			return err
		}
		if rec.Frame != nil {
			p.offer(rec.Frame.RGB24)
			if initial {
				close(first)
				initial = false
			}
			if err = control.ack(rec.Frame.Header.Seq); err != nil {
				return err
			}
		}
		if rec.End != nil {
			return nil
		}
	}
}
func (s *session) writeMetadata() error {
	stats := s.pace.stats()
	event := "sealed"
	if s.reason == "recording" {
		event = "starting"
	}
	contents := map[string]string{
		"eventos.jsonl":   fmt.Sprintf("{\"event\":%q,\"frames\":%d,\"duplicated\":%d,\"dropped\":%d}\n", event, stats.Frames, stats.Duplicated, stats.Dropped),
		"FLUJO.md":        "Native cockpit recording; RGB24 stream -> RAM fragmented H.264 MP4.\n",
		"manifiesto.json": fmt.Sprintf("{\"schema\":\"os.cu.recstream/v1\",\"fps\":30,\"width\":1280,\"height\":900,\"store\":\"ram\",\"reason\":%q,\"frames\":%d}\n", s.reason, stats.Frames),
	}
	for name, body := range contents {
		if err := os.WriteFile(filepath.Join(s.dir, name), []byte(body), 0600); err != nil {
			return err
		}
	}
	return nil
}

// Wait observes automatic sealing without sending Stop.
func (e *Engine) Wait(ctx context.Context, id string) error {
	s, err := e.get(id)
	if err != nil {
		return err
	}
	select {
	case <-s.done:
		return s.err
	case <-ctx.Done():
		return ctx.Err()
	}
}

func encoderArgs() []string {
	return []string{"ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "1280x900", "-r", "30", "-i", "pipe:0", "-an", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-pix_fmt", "yuv420p", "-movflags", "+frag_keyframe+empty_moov", "-g", "30", "-f", "mp4", "pipe:1"}
}

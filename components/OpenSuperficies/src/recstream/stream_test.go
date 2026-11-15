package recstream

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"opensuperficies.local/srf/src/ramcap"
)

func golden(t *testing.T) (ramcap.SeatBinding, []byte, []byte) {
	t.Helper()
	var vectors struct {
		Binding struct {
			Canonical string `json:"canonical_utf8"`
		}
		Good map[string]struct {
			Hex string `json:"hex"`
		}
	}
	raw, err := os.ReadFile("../../testdata/w8u/seatstream_vectors.json")
	if err != nil {
		t.Fatal(err)
	}
	if err = json.Unmarshal(raw, &vectors); err != nil {
		t.Fatal(err)
	}
	b, err := ramcap.ParseSeatBinding([]byte(vectors.Binding.Canonical))
	if err != nil {
		t.Fatal(err)
	}
	wire, err := hex.DecodeString(vectors.Good["stream_sender_host"].Hex)
	if err != nil {
		t.Fatal(err)
	}
	d := ramcap.NewStreamDecoder(bytes.NewReader(wire), b, nil)
	var rgb []byte
	for {
		r, err := d.Next()
		if err != nil {
			t.Fatal(err)
		}
		if r.Frame != nil {
			rgb = r.Frame.RGB24
		}
		if r.End != nil {
			break
		}
	}
	if len(rgb) != b.Width*b.Height*3 {
		t.Fatal("golden frame absent")
	}
	return b, wire, rgb
}
func TestGoldenWireAndMalformedRefusal(t *testing.T) {
	b, wire, _ := golden(t)
	for _, bad := range [][]byte{wire[:len(wire)-2], append([]byte("NOPE"), wire[4:]...)} {
		d := ramcap.NewStreamDecoder(bytes.NewReader(bad), b, nil)
		var err error
		for i := 0; i < 8; i++ {
			_, err = d.Next()
			if err != nil {
				break
			}
		}
		if err == nil {
			t.Fatal("bad wire accepted")
		}
	}
}

// Pixels from the pinned small golden are expanded only in memory. Production
// geometry is fixed; the XWD pair below exercises the real decoder at that size.
func expandedLeg(rgb []byte) []byte {
	name := []byte("recstream-test\x00")
	fields := []uint32{uint32(100 + len(name)), 7, 2, 24, Width, Height, 0, 0, 32, 0, 32, 32, Width * 4, 4, 0xff0000, 0xff00, 0xff, 8, 0, 0, Width, Height, 0, 0, 0}
	out := make([]byte, 100+len(name)+Width*Height*4)
	for i, v := range fields {
		binary.BigEndian.PutUint32(out[4*i:], v)
	}
	copy(out[100:], name)
	pixels := out[100+len(name):]
	for i := 0; i < Width*Height; i++ {
		j := (i * 3) % len(rgb)
		pixels[i*4], pixels[i*4+1], pixels[i*4+2] = rgb[j+2], rgb[j+1], rgb[j]
	}
	return out
}
func resident(leg []byte, frames int) func(context.Context, io.Reader, io.Writer) error {
	return func(ctx context.Context, in io.Reader, out io.Writer) error {
		h, err := ramcap.ReadControl(in, ramcap.MagicControl)
		if err != nil {
			return err
		}
		b, err := ramcap.ParseSeatBinding(h.Body)
		if err != nil {
			return err
		}
		if _, err = ramcap.ReadControl(in, ramcap.MagicControl); err != nil {
			return err
		}
		if _, err = out.Write(ramcap.EncodeControl(ramcap.MagicHelloConfirm, ramcap.KindHello, b.SHA256, nil)); err != nil {
			return err
		}
		_, sha, err := ramcap.PairLegs(leg, leg, Width, Height)
		if err != nil {
			return err
		}
		for i := 1; i <= frames; i++ {
			frame := ramcap.EncodeFrame(uint64(i), 0, uint64(i), ramcap.FlagPairUnderGrab, sha, b.SHA256, Width, Height, leg, leg)
			if _, err = out.Write(frame); err != nil {
				return err
			}
			control, readErr := ramcap.ReadControl(in, ramcap.MagicControl)
			if readErr != nil {
				return readErr
			}
			if control.Kind == ramcap.KindStop {
				_, err = out.Write(ramcap.EncodeControl(ramcap.MagicEnd, ramcap.KindEnd, b.SHA256, ramcap.EndBody{LastSeq: uint64(i), Sent: uint64(i)}.Encode()))
				return err
			}
		}
		// Stay resident until an explicit STOP (or EOF/cancellation on failure).
		control, err := ramcap.ReadControl(in, ramcap.MagicControl)
		if err != nil {
			return err
		}
		if control.Kind != ramcap.KindStop {
			return errors.New("expected STOP")
		}
		_, err = out.Write(ramcap.EncodeControl(ramcap.MagicEnd, ramcap.KindEnd, b.SHA256, ramcap.EndBody{LastSeq: uint64(frames), Sent: uint64(frames)}.Encode()))
		return err
	}
}
func streamFixture(t *testing.T, duration time.Duration, maxBytes int64) (*Engine, *fakeExec) {
	t.Helper()
	_, _, rgb := golden(t)
	f := &fakeExec{stream: resident(expandedLeg(rgb), 3)}
	e, err := New(Config{RunDir: ramDir(t), Launch: f.launch, MaxDuration: duration, MaxBytes: maxBytes, ResolveContainer: func(context.Context, int) (string, error) { return "srf-ng-2", nil }})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := e.Close(); err != nil {
			t.Error(err)
		}
	})
	return e, f
}
func await(t *testing.T, e *Engine, id string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
	defer cancel()
	if err := e.Wait(ctx, id); err != nil {
		t.Fatal(err)
	}
}
func TestDurationCapCadenceAndRetention(t *testing.T) {
	e, f := streamFixture(t, 2*time.Second, 0)
	ctx, cancel := context.WithCancel(context.Background())
	id, err := e.Start(ctx, 2, 42, 3000)
	cancel()
	if err != nil {
		t.Fatal(err)
	}
	await(t, e, id)
	stats, _ := e.Stats(id)
	f.mu.Lock()
	frames := f.frames
	argv := f.calls
	f.mu.Unlock()
	if stats.Frames < 2 || stats.Frames > 61 || uint64(frames) != stats.Frames || stats.Duplicated == 0 {
		t.Fatalf("cadence %+v actual=%d", stats, frames)
	}
	if len(argv) != 2 || strings.Join(argv[1], " ") != "docker exec -i srf-ng-2 /usr/local/bin/srf-fbread stream :100 42" {
		t.Fatalf("argv %v", argv)
	}
	if !strings.Contains(strings.Join(argv[0], " "), "-r 30") {
		t.Fatal("encoder fps")
	}
	s, _ := e.get(id)
	st, _ := os.Stat(s.dir)
	if st.Mode().Perm() != 0700 {
		t.Fatal("directory permissions")
	}
	st, _ = os.Stat(filepath.Join(s.dir, "video-000.mp4"))
	if st.Mode().Perm() != 0600 {
		t.Fatal("file permissions")
	}
	if n, err := e.SizeBytes(context.Background(), id); err != nil || n <= 16 {
		t.Fatalf("size %d %v", n, err)
	}
	if _, err = e.Start(context.Background(), 2, 0, 1000); err == nil {
		t.Fatal("retained session overwritten")
	}
	if err = e.Stop(context.Background(), id); err != nil {
		t.Fatal(err)
	}
	if err = e.Reap(context.Background(), id); err != nil {
		t.Fatal(err)
	}
	if _, err = os.Stat(s.dir); !os.IsNotExist(err) {
		t.Fatal("reap retained RAM files")
	}
	t.Logf("[VM] fake encoder: frames=%d duplicates=%d drops=%d over configured 2s; no live-fps claim", frames, stats.Duplicated, stats.Dropped)
}
func TestByteCapAutoStopAndNativeExportCopy(t *testing.T) {
	e, _ := streamFixture(t, time.Second, 70)
	id, err := e.Start(context.Background(), 2, 42, 2000)
	if err != nil {
		t.Fatal(err)
	}
	await(t, e, id)
	s, _ := e.get(id)
	n, err := e.SizeBytes(context.Background(), id)
	if err != nil || n > 70 || n <= 16 {
		t.Fatalf("cap size %d %v", n, err)
	}
	if s.reason != "byte-cap" {
		t.Fatalf("reason %s", s.reason)
	}
	// An operator-chosen RAM directory exercises the copy without persisting video.
	target := ramDir(t)
	saved, err := e.Export(context.Background(), id, target)
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"video-000.mp4", "eventos.jsonl", "FLUJO.md", "manifiesto.json"} {
		a, err := os.ReadFile(filepath.Join(s.dir, name))
		if err != nil {
			t.Fatal(err)
		}
		b, err := os.ReadFile(filepath.Join(saved, name))
		if err != nil {
			t.Fatal(err)
		}
		if sha256.Sum256(a) != sha256.Sum256(b) {
			t.Fatalf("copy changed %s", name)
		}
	}
	if _, err = e.Export(context.Background(), id, "relative"); err == nil {
		t.Fatal("relative target accepted")
	}
}
func TestExplicitStopAndUnsealedExportRefused(t *testing.T) {
	e, _ := streamFixture(t, time.Second, 0)
	id, err := e.Start(context.Background(), 2, 42, 2000)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = e.Export(context.Background(), id, ramDir(t)); err == nil {
		t.Fatal("active session exported")
	}
	if err = e.Stop(context.Background(), id); err != nil {
		t.Fatal(err)
	}
	await(t, e, id)
}
func TestEncoderFailureAndStartupCancellation(t *testing.T) {
	e, _ := streamFixture(t, time.Second, 0)
	e.cfg.Launch = func(context.Context, []string, io.Reader, io.Writer) (Process, error) {
		return nil, errors.New("E_ENCODER")
	}
	if id, err := e.Start(context.Background(), 2, 42, 2000); err == nil || id != "" {
		t.Fatal("encoder failure admitted")
	}
	entries, err := os.ReadDir(e.cfg.RunDir)
	if err != nil || len(entries) != 0 {
		t.Fatal("failed startup retained files")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err = e.Start(ctx, 2, 42, 2000); !errors.Is(err, context.Canceled) {
		t.Fatal("cancel ignored")
	}
}

func TestThirtyFPSResamplingUsesClockNotArrival(t *testing.T) {
	e, f := streamFixture(t, 2*time.Second, 0)
	ticks := make(chan time.Time)
	periods := make(chan time.Duration, 1)
	e.ticker = func(period time.Duration) (<-chan time.Time, func()) { periods <- period; return ticks, func() {} }
	id, err := e.Start(context.Background(), 2, 42, 3000)
	if err != nil {
		t.Fatal(err)
	}
	select {
	case period := <-periods:
		if period != time.Second/30 {
			t.Fatalf("cadence %v", period)
		}
	case <-time.After(time.Second):
		t.Fatal("clock absent")
	}
	// First frame is emitted at t0. Only clock ticks may emit the rest, even
	// while the source offers its burst and then stalls.
	for i := 0; i < 29; i++ {
		select {
		case ticks <- time.Time{}:
		case <-time.After(time.Second):
			t.Fatal("pacer stalled")
		}
	}
	var stats Stats
	for i := 0; i < 100; i++ {
		stats, _ = e.Stats(id)
		if stats.Frames == 30 {
			break
		}
		time.Sleep(time.Millisecond)
	}
	if stats.Frames != 30 {
		t.Fatalf("virtual second %+v", stats)
	}
	if err = e.Stop(context.Background(), id); err != nil {
		t.Fatal(err)
	}
	f.mu.Lock()
	frames := f.frames
	f.mu.Unlock()
	if frames != 30 {
		t.Fatalf("encoder got %d complete frames", frames)
	}
	t.Logf("[VM] fake clock: encoder received %d full RGB24 frames for one virtual second", frames)
}

func TestStopFinishesInFlightFrame(t *testing.T) {
	e, f := streamFixture(t, 2*time.Second, 0)
	partial := make(chan struct{})
	release := make(chan struct{})
	e.cfg.Launch = func(ctx context.Context, args []string, in io.Reader, out io.Writer) (Process, error) {
		if args[0] == "docker" {
			return f.launch(ctx, args, in, out)
		}
		proc := fakeProcess{make(chan error, 1)}
		go func() {
			_, err := out.Write(append(box("ftyp", nil), box("moov", nil)...))
			if err != nil {
				proc.done <- err
				return
			}
			frame := make([]byte, Width*Height*3)
			if _, err = io.ReadFull(in, frame); err != nil {
				proc.done <- err
				return
			}
			if _, err = out.Write(append(box("moof", nil), box("mdat", []byte{1})...)); err != nil {
				proc.done <- err
				return
			}
			if _, err = io.ReadFull(in, frame[:64]); err != nil {
				proc.done <- err
				return
			}
			close(partial)
			select {
			case <-release:
			case <-ctx.Done():
				proc.done <- ctx.Err()
				return
			}
			if _, err = io.ReadFull(in, frame[64:]); err != nil {
				proc.done <- err
				return
			}
			if _, err = out.Write(append(box("moof", nil), box("mdat", []byte{2})...)); err != nil {
				proc.done <- err
				return
			}
			_, err = io.Copy(io.Discard, in)
			proc.done <- err
		}()
		return proc, nil
	}
	id, err := e.Start(context.Background(), 2, 42, 3000)
	if err != nil {
		t.Fatal(err)
	}
	select {
	case <-partial:
	case <-time.After(time.Second):
		t.Fatal("no in-flight frame")
	}
	result := make(chan error, 1)
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		result <- e.Stop(ctx, id)
	}()
	s, _ := e.get(id)
	<-s.stop
	// The raw writer is blocked while only part of a complete frame was read.
	// Stop must drain it, rather than turn a clean recording into truncated input.
	time.Sleep(10 * time.Millisecond)
	close(release)
	if err = <-result; err != nil {
		t.Fatalf("Stop truncated in-flight frame: %v", err)
	}
	stats, _ := e.Stats(id)
	if stats.Frames != 2 {
		t.Fatalf("in-flight frames %+v", stats)
	}
}

func TestStopSendsW8UCBeforeDisconnect(t *testing.T) {
	e, f := streamFixture(t, 2*time.Second, 0)
	original := f.stream
	var upstream bytes.Buffer
	f.stream = func(ctx context.Context, in io.Reader, out io.Writer) error {
		return original(ctx, io.TeeReader(in, &upstream), out)
	}
	id, err := e.Start(context.Background(), 2, 42, 3000)
	if err != nil {
		t.Fatal(err)
	}
	if err = e.Stop(context.Background(), id); err != nil {
		t.Fatal(err)
	}
	received := false
	for upstream.Len() > 0 {
		control, err := ramcap.ReadControl(&upstream, ramcap.MagicControl)
		if err != nil {
			t.Fatal(err)
		}
		if control.Kind == ramcap.KindStop {
			received = true
			if upstream.Len() != 0 {
				t.Fatal("control record after STOP")
			}
		}
	}
	if !received {
		t.Fatal("source disconnected without explicit W8UC STOP")
	}
}

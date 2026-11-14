package recstream

import (
	"bytes"
	"context"
	"encoding/binary"
	"errors"
	"io"
	"os"
	"path/filepath"
	"sync"
	"testing"
)

func ramDir(t *testing.T) string {
	t.Helper()
	const root = "/dev/shm/vqa-ramws"
	if err := os.MkdirAll(root, 0700); err != nil {
		t.Fatal(err)
	}
	d, err := os.MkdirTemp(root, "recstream-test-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(d) })
	return d
}
func TestRAMOnly(t *testing.T) {
	for _, p := range []string{t.TempDir(), "/tmp/rec", "relative", "/dev/shm/elsewhere"} {
		if _, err := New(Config{RunDir: p, ResolveContainer: func(context.Context, int) (string, error) { return "srf-ng-0", nil }}); err == nil {
			t.Fatalf("accepted %s", p)
		}
	}
	root := ramDir(t)
	link := filepath.Join(root, "escape")
	if err := os.Symlink(t.TempDir(), link); err != nil {
		t.Fatal(err)
	}
	if _, err := New(Config{RunDir: link, ResolveContainer: func(context.Context, int) (string, error) { return "srf-ng-0", nil }}); err == nil {
		t.Fatal("symlink escape accepted")
	}
}
func TestPacerDupDrop(t *testing.T) {
	p := pacer{}
	p.offer([]byte{1})
	p.offer([]byte{2})
	p.offer([]byte{3})
	var out bytes.Buffer
	for range 30 {
		if err := p.tick(&out); err != nil {
			t.Fatal(err)
		}
	}
	s := p.stats()
	if s.Frames != 30 || s.Duplicated != 29 || s.Dropped != 2 || !bytes.Equal(out.Bytes(), bytes.Repeat([]byte{3}, 30)) {
		t.Fatalf("pacing %+v %v", s, out.Bytes())
	}
	t.Logf("[VM] virtual cadence: frames=%d duplicates=%d drops=%d", s.Frames, s.Duplicated, s.Dropped)
}
func box(kind string, body []byte) []byte {
	b := binary.BigEndian.AppendUint32(nil, uint32(8+len(body)))
	b = append(b, kind...)
	return append(b, body...)
}
func TestMP4HardCap(t *testing.T) {
	f, err := os.CreateTemp(ramDir(t), "mp4-")
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	sink := newMP4Sink(f, 40)
	good := append(box("ftyp", nil), box("moov", nil)...)
	good = append(good, box("moof", nil)...)
	good = append(good, box("mdat", []byte{1, 2})...)
	for _, b := range good {
		if _, err := sink.Write([]byte{b}); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := sink.Write(box("moof", nil)); !errors.Is(err, ErrByteLimit) {
		t.Fatalf("cap: %v", err)
	}
	if err := sink.finish(); err != nil {
		t.Fatal(err)
	}
	st, _ := f.Stat()
	if st.Size() != 34 || st.Size() > 40 {
		t.Fatalf("size %d", st.Size())
	}
}

type fakeProcess struct{ done chan error }

func (p fakeProcess) Wait() error { return <-p.done }

type fakeExec struct {
	mu     sync.Mutex
	calls  [][]string
	frames int
	stream func(context.Context, io.Reader, io.Writer) error
}

func (f *fakeExec) launch(ctx context.Context, args []string, in io.Reader, out io.Writer) (Process, error) {
	f.mu.Lock()
	f.calls = append(f.calls, append([]string(nil), args...))
	f.mu.Unlock()
	p := fakeProcess{make(chan error, 1)}
	go func() {
		if args[0] == "docker" {
			p.done <- f.stream(ctx, in, out)
			return
		}
		_, err := out.Write(append(box("ftyp", nil), box("moov", nil)...))
		if err != nil {
			p.done <- err
			return
		}
		frame := make([]byte, Width*Height*3)
		for {
			_, err = io.ReadFull(in, frame)
			if err != nil {
				if err == io.EOF {
					err = nil
				}
				p.done <- err
				return
			}
			f.mu.Lock()
			f.frames++
			f.mu.Unlock()
			if _, err = out.Write(append(box("moof", nil), box("mdat", []byte{42})...)); err != nil {
				p.done <- err
				return
			}
		}
	}()
	return p, nil
}
func TestUnresolvedContainerDoesNotExec(t *testing.T) {
	f := &fakeExec{}
	e, err := New(Config{RunDir: ramDir(t), Launch: f.launch, ResolveContainer: func(context.Context, int) (string, error) { return "", errors.New("E_NO_CAPSULE") }})
	if err != nil {
		t.Fatal(err)
	}
	defer e.Close()
	if id, err := e.Start(context.Background(), 0, 0, 1000); err == nil || id != "" {
		t.Fatal("unresolved accepted")
	}
	if len(f.calls) != 0 {
		t.Fatal("executed before resolution")
	}
}
func TestStopBeforeStartAndInvalidConfig(t *testing.T) {
	if _, err := New(Config{}); err == nil {
		t.Fatal("missing config accepted")
	}
	e, err := New(Config{RunDir: ramDir(t), ResolveContainer: func(context.Context, int) (string, error) { return "srf-ng-0", nil }})
	if err != nil {
		t.Fatal(err)
	}
	defer e.Close()
	if e.Stop(context.Background(), "missing") == nil {
		t.Fatal("unknown session accepted")
	}
	if err = e.Reap(context.Background(), ""); err != nil {
		t.Fatal(err)
	}
}

func TestMP4CapRollsBackUnpairedMoof(t *testing.T) {
	f, err := os.CreateTemp(ramDir(t), "boxes-")
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	sink := newMP4Sink(f, 50)
	prefix := append(box("ftyp", nil), box("moov", nil)...)
	prefix = append(prefix, box("moof", nil)...)
	prefix = append(prefix, box("mdat", []byte{1, 2})...)
	if _, err = sink.Write(prefix); err != nil {
		t.Fatal(err)
	}
	if _, err = sink.Write(box("moof", nil)); err != nil {
		t.Fatal(err)
	}
	if _, err = sink.Write(box("mdat", []byte{3})); !errors.Is(err, ErrByteLimit) {
		t.Fatalf("cap %v", err)
	}
	if err = sink.finish(); err != nil {
		t.Fatal(err)
	}
	b, err := os.ReadFile(f.Name())
	if err != nil || !bytes.Equal(b, prefix) {
		t.Fatal("unpaired fragment retained")
	}
}
func TestMP4RefusesInitializationOnlyAndTruncation(t *testing.T) {
	for _, truncated := range []bool{false, true} {
		f, err := os.CreateTemp(ramDir(t), "bad-mp4-")
		if err != nil {
			t.Fatal(err)
		}
		sink := newMP4Sink(f, 256)
		_, err = sink.Write(append(box("ftyp", nil), box("moov", nil)...))
		if err != nil {
			t.Fatal(err)
		}
		if truncated {
			_, err = sink.Write(box("mdat", []byte{1, 2})[:9])
			if err != nil {
				t.Fatal(err)
			}
		}
		if sink.finish() == nil {
			t.Fatal("non-video encoder output accepted")
		}
		f.Close()
	}
}

func TestParentCancellationRefusesNewProcesses(t *testing.T) {
	parent, cancel := context.WithCancel(context.Background())
	f := &fakeExec{}
	cfg := Config{Context: parent, RunDir: ramDir(t), Launch: f.launch, ResolveContainer: func(context.Context, int) (string, error) { return "srf-ng-0", nil }}
	e, err := New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer e.Close()
	cancel()
	if _, err = e.Start(context.Background(), 0, 0, 1000); !errors.Is(err, context.Canceled) {
		t.Fatalf("cancelled parent: %v", err)
	}
	if len(f.calls) != 0 {
		t.Fatal("cancelled app started a process")
	}
	if _, err = New(cfg); !errors.Is(err, context.Canceled) {
		t.Fatal("cancelled parent admitted engine")
	}
}

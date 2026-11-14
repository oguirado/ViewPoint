//go:build cockpit

package main

import (
	"bytes"
	"context"
	"encoding/binary"
	"io"
	"opensuperficies.local/srf/src/ramcap"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"opensuperficies.local/srf/src/recstream"
)

func TestRECStartupUsesStreamEngine(t *testing.T) {
	b, err := os.ReadFile("ramcap_wire.go")
	if err != nil {
		t.Fatal(err)
	}
	s := string(b)
	start := strings.Index(s, "func startRamCapture(")
	if start < 0 {
		t.Fatal("startup missing")
	}
	s = s[start : strings.Index(s[start:], "func stopRamCapture(")+start]
	if !strings.Contains(s, "wireRecstreamEngine(a, ctx, dir)") || strings.Contains(s, "Cadence: 500") {
		t.Fatal("REC still uses low-cadence raw-frame engine")
	}
}
func TestRECAdapterImplementsCaptureAndHostExporter(t *testing.T) {
	var _ Exporter = (*recstream.Engine)(nil)
	// The adapter is not the Wails-bound App. No disk export method is added there.
	b, err := os.ReadFile("recstream.go")
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(b), "func (a *App)") {
		t.Fatal("REC added agent-callable App method")
	}
}

// Both child processes are in-memory doubles. No docker/ffmpeg/display starts.
type recTestProcess struct{ done chan error }

func (p recTestProcess) Wait() error { return <-p.done }
func recTestBox(kind string, body []byte) []byte {
	b := binary.BigEndian.AppendUint32(nil, uint32(8+len(body)))
	b = append(b, kind...)
	return append(b, body...)
}
func recTestLaunch(ctx context.Context, args []string, in io.Reader, out io.Writer) (recstream.Process, error) {
	p := recTestProcess{make(chan error, 1)}
	go func() {
		if args[0] == "ffmpeg" {
			_, err := out.Write(append(recTestBox("ftyp", nil), recTestBox("moov", nil)...))
			if err != nil {
				p.done <- err
				return
			}
			frame := make([]byte, recstream.Width*recstream.Height*3)
			for {
				_, err = io.ReadFull(in, frame)
				if err != nil {
					if err == io.EOF {
						err = nil
					}
					p.done <- err
					return
				}
				if _, err = out.Write(append(recTestBox("moof", nil), recTestBox("mdat", []byte{7})...)); err != nil {
					p.done <- err
					return
				}
			}
		}
		hello, err := ramcap.ReadControl(in, ramcap.MagicControl)
		if err != nil {
			p.done <- err
			return
		}
		b, err := ramcap.ParseSeatBinding(hello.Body)
		if err != nil {
			p.done <- err
			return
		}
		if _, err = ramcap.ReadControl(in, ramcap.MagicControl); err != nil {
			p.done <- err
			return
		}
		if _, err = out.Write(ramcap.EncodeControl(ramcap.MagicHelloConfirm, ramcap.KindHello, b.SHA256, nil)); err != nil {
			p.done <- err
			return
		}
		w, h := recstream.Width, recstream.Height
		leg := make([]byte, 104+4*w*h)
		fields := []uint32{104, 7, 2, 24, uint32(w), uint32(h), 0, 0, 32, 0, 32, 32, uint32(w * 4), 4, 0xff0000, 0xff00, 0xff, 8, 0, 0, uint32(w), uint32(h), 0, 0, 0}
		for i, v := range fields {
			binary.BigEndian.PutUint32(leg[4*i:], v)
		}
		copy(leg[100:], []byte("rec\x00"))
		_, sha, err := ramcap.PairLegs(leg, leg, w, h)
		if err != nil {
			p.done <- err
			return
		}
		_, err = out.Write(ramcap.EncodeFrame(1, 0, 1, ramcap.FlagPairUnderGrab, sha, b.SHA256, w, h, leg, leg))
		if err != nil {
			p.done <- err
			return
		}
		// One consumed ACK, then wait for host EOF on Stop/cap.
		if _, err = ramcap.ReadControl(in, ramcap.MagicControl); err == nil {
			_, err = ramcap.ReadControl(in, ramcap.MagicControl)
		}
		p.done <- err
	}()
	return p, nil
}
func TestRECAdapterAutoSealAndNativeSave(t *testing.T) {
	root := "/dev/shm/vqa-ramws"
	if err := os.MkdirAll(root, 0700); err != nil {
		t.Fatal(err)
	}
	dir, err := os.MkdirTemp(root, "rec-adapter-test-")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(dir)
	a, _, _ := recordingSetup(t, 3000)
	e, err := recstream.New(recstream.Config{RunDir: dir, Launch: recTestLaunch, MaxDuration: 500 * time.Millisecond, ResolveContainer: func(ctx context.Context, slot int) (string, error) { return resolveRamcapContainer(a, ctx, slot) }})
	if err != nil {
		t.Fatal(err)
	}
	defer e.Close()
	a.grab.raiz = dir
	a.grab.policy.config.RamCapture = &recstreamCapture{engine: e, app: a}
	a.grab.policy.config.Dialog = func(r SaveDialogRequest) (SaveDialogChoice, error) {
		return SaveDialogChoice{Action: "Guardar", TargetDir: dir}, nil
	}
	id := recordingStart(t, a)
	ctx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
	defer cancel()
	if err = e.Wait(ctx, id); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 100 && a.EstadoGrabacion().Fase != "sealed"; i++ {
		time.Sleep(10 * time.Millisecond)
	}
	if state := a.EstadoGrabacion(); state.Fase != "sealed" || state.Store == nil || *state.Store != "ram" {
		t.Fatalf("auto-seal %+v", state)
	}
	if a.grab.policy.config.Exporter != e {
		t.Fatal("native exporter not installed")
	}
	before, err := os.ReadFile(filepath.Join(dir, id, "video-000.mp4"))
	if err != nil {
		t.Fatal(err)
	}
	if err = triggerOperatorSave(a); err != nil {
		t.Fatal(err)
	}
	a.grab.mu.Lock()
	saved := a.grab.dir
	a.grab.mu.Unlock()
	after, err := os.ReadFile(filepath.Join(saved, "video-000.mp4"))
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(before, after) {
		t.Fatal("native save changed bytes")
	}
	if _, err = os.Stat(filepath.Join(dir, id)); !os.IsNotExist(err) {
		t.Fatal("native save retained RAM session")
	}
	if st := a.EstadoGrabacion(); st.Store == nil || *st.Store != "disk" {
		t.Fatal("save projection")
	}
}

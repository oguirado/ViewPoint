//go:build cockpit

package main

import (
	"bufio"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"time"

	"opensuperficies.local/srf/src/cubook/capture"
	"opensuperficies.local/srf/src/ramcap"
)

// These adapters keep the frozen cockpit seam independent of ramcap's types.
type ramcapCapture struct{ engine *ramcap.Engine }

var _ D11Capture = ramcapCapture{}
var _ FrozenFramebuffer = ramcapFramebuffer{}

func (c ramcapCapture) Start(ctx context.Context, slot int, xid uint32, ttl int64) (string, error) {
	return c.engine.Capture().Start(ctx, slot, xid, ttl)
}
func (c ramcapCapture) Stop(ctx context.Context, id string) error {
	return c.engine.Capture().Stop(ctx, id)
}
func (c ramcapCapture) Reap(ctx context.Context, id string) error {
	return c.engine.Capture().Reap(ctx, id)
}
func (c ramcapCapture) SizeBytes(ctx context.Context, id string) (int64, error) {
	return c.engine.Capture().SizeBytes(ctx, id)
}
func (c ramcapCapture) Provenance(ctx context.Context, id string) (CaptureProvenance, error) {
	p, err := c.engine.Capture().Provenance(ctx, id)
	return CaptureProvenance{Source: p.Source, Codec: p.Codec, Store: p.Store, XID: p.XID, Swap: p.Swap, Disk: p.Disk}, err
}
func ramcapDigest(f ramcap.FrameDigest) FrameDigest {
	return FrameDigest{Seq: f.Seq, W: f.W, H: f.H, Stride: f.Stride, Format: f.Format, SHA256: f.SHA256}
}
func (c ramcapCapture) Probe(ctx context.Context, id, display string, xid uint32) (FrameDigest, error) {
	f, err := c.engine.Capture().Probe(ctx, id, display, xid)
	return ramcapDigest(f), err
}

type ramcapFramebuffer struct{ engine *ramcap.Engine }

func (d ramcapFramebuffer) Read(ctx context.Context, display string, xid uint32) (FrameDigest, error) {
	f, err := d.engine.Direct().Read(ctx, display, xid)
	return ramcapDigest(f), err
}
func (d ramcapFramebuffer) Freeze(ctx context.Context, display string, xid uint32) (func() error, error) {
	return d.engine.Direct().Freeze(ctx, display, xid)
}
func wireRamcapEngine(cfg ramcap.Config) (RamCapture, *ramcap.Engine, error) {
	e, err := ramcap.NewEngine(cfg)
	if err != nil {
		return nil, nil, err
	}
	return admitRamCapture(ramcapCapture{e}, ramcapFramebuffer{e}, ":100"), e, nil
}

var ramcapLifetimes = struct {
	sync.Mutex
	items map[*App]func()
}{items: map[*App]func(){}}

// resolveRamcapContainer uses the same read-only state document exposed by /state;
// a numeric slot alone never authorizes an arbitrary docker exec target.
func resolveRamcapContainer(a *App, ctx context.Context, slot int) (string, error) {
	if err := ctx.Err(); err != nil {
		return "", err
	}
	var doc StateDocument
	if a == nil || json.Unmarshal([]byte(a.EstadoCanonico()), &doc) != nil {
		return "", errors.New("E_NO_CAPSULE")
	}
	for _, c := range doc.Capsules {
		if c.Slot == slot && c.SlotID == fmt.Sprintf("srf-ng-%d", slot) && c.Lifecycle != "stopped" && c.Lifecycle != "failed" {
			return c.SlotID, nil
		}
	}
	return "", errors.New("E_NO_CAPSULE")
}

// openRamcapShim starts only the repository's pinned RAM owner, without a shell.
// Its hello is bounded, validated by the existing live-lock/tmpfs guard, and never
// treated as a filesystem path until those checks pass.
func openRamcapShim(parent context.Context, root string) (string, func(), error) {
	if root == "" {
		return "", nil, errors.New("AUSENTE: ramws shim")
	}
	shim := filepath.Join(root, "vision", "ramws_shim.py")
	st, err := os.Stat(shim)
	if err != nil || !st.Mode().IsRegular() {
		return "", nil, errors.New("AUSENTE: ramws shim")
	}
	var nonce [16]byte
	if _, err = rand.Read(nonce[:]); err != nil {
		return "", nil, err
	}
	// A hard lifetime also bounds a forgotten shutdown callback.
	ctx, cancel := context.WithTimeout(parent, 24*time.Hour)
	cmd := exec.CommandContext(ctx, "python3", "-B", shim, "session", "--run-id", "cockpit-"+hex.EncodeToString(nonce[:]))
	cmd.Dir = root
	cmd.Env = append(os.Environ(), "PYTHONDONTWRITEBYTECODE=1")
	cmd.Stderr = io.Discard
	stdin, err := cmd.StdinPipe()
	if err != nil {
		cancel()
		return "", nil, err
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		_ = stdin.Close()
		cancel()
		return "", nil, err
	}
	if err = cmd.Start(); err != nil {
		_ = stdin.Close()
		cancel()
		return "", nil, err
	}
	done := make(chan struct{})
	go func() { _ = cmd.Wait(); close(done) }()
	var once sync.Once
	stop := func() {
		once.Do(func() {
			_ = stdin.Close()
			select {
			case <-done:
			case <-time.After(2 * time.Second):
				cancel()
				<-done
			}
			cancel()
		})
	}
	type hello struct {
		Code  string `json:"code"`
		Event string `json:"event"`
		Root  string `json:"root"`
	}
	type result struct {
		line []byte
		err  error
	}
	read := make(chan result, 1)
	go func() {
		b, _, e := bufio.NewReaderSize(io.LimitReader(stdout, 4097), 4097).ReadLine()
		read <- result{b, e}
	}()
	timer := time.NewTimer(2 * time.Second)
	defer timer.Stop()
	var h hello
	select {
	case r := <-read:
		if r.err != nil || len(r.line) > 4096 || json.Unmarshal(r.line, &h) != nil || h.Code != "OK" || h.Event != "open" {
			stop()
			return "", nil, errors.New("AUSENTE: ramws shim hello")
		}
	case <-timer.C:
		stop()
		return "", nil, errors.New("AUSENTE: ramws shim timeout")
	case <-ctx.Done():
		stop()
		return "", nil, ctx.Err()
	}
	if err = capture.DefaultGuard().CheckRunDir(h.Root); err != nil {
		stop()
		return "", nil, err
	}
	return h.Root, stop, nil
}

func startRamCapture(a *App, ctx context.Context) RamCapture {
	if a == nil {
		return nil
	}
	stopRamCapture(a)
	dir, stop, err := openRamcapShim(ctx, raizRepo())
	if err != nil {
		return nil
	}
	cap, eng, err := wireRecstreamEngine(a, ctx, dir)
	if err != nil {
		stop()
		return nil
	}
	var once sync.Once
	cleanup := func() {
		once.Do(func() {
			// App.shutdown invokes this after grab.Abortar; cancellation follows the same order.
			_ = eng.Close()
			stop()
		})
	}
	ramcapLifetimes.Lock()
	ramcapLifetimes.items[a] = cleanup
	ramcapLifetimes.Unlock()
	context.AfterFunc(ctx, func() {
		if a.grab != nil {
			a.grab.Abortar()
		}
		stopRamCapture(a)
	})
	return cap
}
func stopRamCapture(a *App) {
	ramcapLifetimes.Lock()
	stop := ramcapLifetimes.items[a]
	delete(ramcapLifetimes.items, a)
	ramcapLifetimes.Unlock()
	if stop != nil {
		stop()
	}
}

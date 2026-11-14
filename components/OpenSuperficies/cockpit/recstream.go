//go:build cockpit

package main

import (
	"context"
	"time"

	"opensuperficies.local/srf/src/recstream"
)

// recstreamCapture is deliberately separate from D11's raw, bit-exact engine.
// It is not a Wails binding: only App is bound to the webview.
type recstreamCapture struct {
	engine *recstream.Engine
	app    *App
}

var _ RamCapture = (*recstreamCapture)(nil)

func wireRecstreamEngine(a *App, ctx context.Context, dir string) (RamCapture, *recstream.Engine, error) {
	engine, err := recstream.New(recstream.Config{Context: ctx, RunDir: dir,
		ResolveContainer: func(ctx context.Context, slot int) (string, error) { return resolveRamcapContainer(a, ctx, slot) },
	})
	if err != nil {
		return nil, nil, err
	}
	return &recstreamCapture{engine: engine, app: a}, engine, nil
}
func (c *recstreamCapture) Start(ctx context.Context, slot int, xid uint32, ttl int64) (string, error) {
	// StartRecording has installed the policy before calling the adapter. Its
	// native save flow is the sole consumer of this exporter; the capture seam
	// itself contains no disk-save verb. Native menu save is unavailable during
	// starting/recording, so this assignment precedes any possible export read.
	if c.app != nil && c.app.grab != nil {
		g := c.app.grab
		g.mu.Lock()
		if g.policy != nil {
			g.policy.config.Exporter = c.engine
		}
		g.mu.Unlock()
	}
	id, err := c.engine.Start(ctx, slot, xid, ttl)
	if err != nil {
		return "", err
	}
	if c.app != nil && c.app.grab != nil {
		go c.observeSeal(id)
	}
	return id, nil
}
func (c *recstreamCapture) observeSeal(id string) {
	ctx, cancel := context.WithTimeout(context.Background(), recstream.DefaultMaxDuration+20*time.Second)
	defer cancel()
	err := c.engine.Wait(ctx, id)
	g := c.app.grab
	// startRAM owns operation while publishing its identity. Waiting for it also
	// serializes this projection with explicit Stop, expiry/reap and native save.
	g.operation.Lock()
	g.mu.Lock()
	if g.sessionID == id && (g.fase == "recording" || g.fase == "starting") {
		g.duracion = time.Since(g.inicio).Milliseconds()
		if err == nil {
			g.fase = "sealed"
		} else {
			g.fase, g.err = "error", recordingError(err)
		}
	}
	g.mu.Unlock()
	g.operation.Unlock()
	updateRecordingMenu(c.app)
}
func (c *recstreamCapture) Stop(ctx context.Context, id string) error { return c.engine.Stop(ctx, id) }
func (c *recstreamCapture) Reap(ctx context.Context, id string) error { return c.engine.Reap(ctx, id) }
func (c *recstreamCapture) SizeBytes(ctx context.Context, id string) (int64, error) {
	return c.engine.SizeBytes(ctx, id)
}

package daemon

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"image"
	"image/color"
	"image/png"
	"net"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"testing"
	"time"

	"metaviewer/daemon-adapter/internal/capsule"
	"metaviewer/daemon-adapter/internal/frames"
	"metaviewer/daemon-adapter/internal/ipc"
)

type settlementRunner struct {
	started, finish chan struct{}
	initialRunner
	seq  int
	mode string
}

func (r *settlementRunner) Run(ctx context.Context, args []string, in []byte) ([]byte, error) {
	switch args[1] {
	case "input":
		if r.mode == "takeover" || r.mode == "lost" {
			close(r.started)
			<-ctx.Done()
			<-r.finish
			return nil, ctx.Err()
		}
		if r.mode == "native" {
			return nil, errors.New("native execution failed")
		}
		return []byte(`{"surface_id":"w1","kind":"key","focus_verified":true,"result":"ok","input_mono_ns":200}`), nil
	case "frame":
		r.seq++
		if r.seq > 1 && r.mode == "capture" {
			return nil, errors.New("capture failed")
		}
		path := filepath.Join(r.root, "demo-run/frames/w1/f.png")
		b, _ := json.Marshal(map[string]any{"sha256_rgb24": fmt.Sprintf("%x", sha256.Sum256(make([]byte, 6))), "path": path, "seq": r.seq, "width": 2, "height": 1, "capture_mono_ns": 200, "generation": "srf-id-demo", "window": map[string]any{"id": "w1", "w": 2, "h": 1}})
		return b, nil
	}
	return r.initialRunner.Run(ctx, args, in)
}

func TestInputFailuresSettleWithProposal(t *testing.T) {
	for _, mode := range []string{"native", "unsupported", "capture", "publish", "takeover", "lost"} {
		t.Run(mode, func(t *testing.T) {
			root, err := os.MkdirTemp("/dev/shm", "mv-input-settle-")
			if err != nil {
				t.Fatal(err)
			}
			defer os.RemoveAll(root)
			source := filepath.Join(root, "demo-run/frames/w1")
			if err = os.MkdirAll(source, 0700); err != nil {
				t.Fatal(err)
			}
			path := filepath.Join(source, "f.png")
			f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY, 0600)
			if err != nil {
				t.Fatal(err)
			}
			img := image.NewNRGBA(image.Rect(0, 0, 2, 1))
			img.Set(0, 0, color.NRGBA{A: 255})
			img.Set(1, 0, color.NRGBA{A: 255})
			if err = png.Encode(f, img); err != nil {
				t.Fatal(err)
			}
			f.Close()
			profiles := map[string]capsule.Profile{"demo": {Kind: "browser", NativeKind: "browser", RAMRoot: root, Display: ":100", Width: 2, Height: 1}}
			runner := &settlementRunner{initialRunner: initialRunner{root: root}, mode: mode, started: make(chan struct{}), finish: make(chan struct{})}
			m, err := capsule.NewManager(capsule.Config{BinDir: t.TempDir(), Profiles: profiles, MaxSurfaces: 1, MaxOutputBytes: 8192, Timeout: time.Second, OriginNS: 100}, runner, func() uint64 { return 200 })
			if err != nil {
				t.Fatal(err)
			}
			pub, err := frames.NewPublisher(frames.PublishConfig{RAMRoot: root, MaxFrameBytes: 8192, MaxPixels: 2, MaxSurfaces: 1})
			if err != nil {
				t.Fatal(err)
			}
			defer pub.Close()
			h := isolatedWireHandler(t, m, profiles, map[string]*frames.Publisher{root: pub}, 100)
			call := func(op string, params any) []json.RawMessage {
				t.Helper()
				body, _ := json.Marshal(map[string]any{"schema": "surface_request/v0.2", "op": op, "params": params})
				events, err := h(context.Background(), ipc.Envelope{Body: body})
				if err != nil {
					t.Fatal(err)
				}
				return events
			}
			created := call("create", map[string]any{"kind": "browser", "profile_id": "demo", "geometry": map[string]int{"w": 2, "h": 1}})
			if len(created) != 2 {
				t.Fatalf("create: %s", created)
			}
			var event struct {
				Data struct{ Surface capsule.Surface }
			}
			if err = json.Unmarshal(created[0], &event); err != nil {
				t.Fatal(err)
			}
			action := map[string]any{"type": "key", "keys": []string{"a"}}
			if mode == "unsupported" {
				action = map[string]any{"type": "unknown"}
			}
			if mode == "publish" {
				if err = os.Chmod(path, 0644); err != nil {
					t.Fatal(err)
				}
			}
			params := map[string]any{"surface_id": event.Data.Surface.SurfaceID, "proposal_id": "failed-proposal", "action": action}
			var events []json.RawMessage
			if mode == "takeover" || mode == "lost" {
				done := make(chan []json.RawMessage, 1)
				go func() { done <- call("input", params) }()
				select {
				case <-runner.started:
				case <-time.After(time.Second):
					t.Fatal("input never started")
				}
				if mode == "lost" {
					if _, err := m.MarkLost(event.Data.Surface.SurfaceID, true); err != nil {
						close(runner.finish)
						t.Fatal(err)
					}
				} else {
					call("take_control", map[string]any{"surface_id": event.Data.Surface.SurfaceID, "owner": "human"})
				}
				close(runner.finish)
				select {
				case events = <-done:
				case <-time.After(time.Second):
					t.Fatal("cancelled input never settled")
				}
				if mode == "takeover" {
					if ready, err := m.ControlReady(event.Data.Surface.SurfaceID); err != nil || !ready {
						t.Fatalf("failure did not drain: %v", err)
					}
					released := call("release_control", map[string]any{"surface_id": event.Data.Surface.SurfaceID})
					var result struct{ Event string }
					if len(released) != 2 || json.Unmarshal(released[0], &result) != nil || result.Event != "state_changed" {
						t.Fatalf("release refused: %s", released)
					}
					if json.Unmarshal(released[1], &result) != nil || result.Event != "frame_ready" {
						t.Fatalf("release missing fresh frame: %s", released)
					}
				}
			} else {
				events = call("input", params)
			}
			var receipt map[string]any
			if len(events) != 1 || json.Unmarshal(events[0], &receipt) != nil {
				t.Fatalf("missing receipt: %s", events)
			}
			if receipt["schema"] != "action_receipt/v0.2" || receipt["proposal_id"] != "failed-proposal" || receipt["verified"] != false || receipt["error"] == "" || receipt["error"] == nil || receipt["after_frame"] == nil {
				t.Fatalf("uncorrelated or verified failure: %s", events)
			}
			if mode == "lost" {
				if again, err := m.MarkLost(event.Data.Surface.SurfaceID, true); err != nil || len(again) != 0 {
					t.Fatalf("repeated loss emitted a duplicate settlement: %s %v", again, err)
				}
			}
		})
	}
}

func TestHumanOriginWireReceipts(t *testing.T) {
	root, err := os.MkdirTemp("/dev/shm", "mv-human-origin-")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(root)
	source := filepath.Join(root, "demo-run/frames/w1")
	if err = os.MkdirAll(source, 0700); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(source, "f.png")
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY, 0600)
	if err != nil {
		t.Fatal(err)
	}
	img := image.NewNRGBA(image.Rect(0, 0, 2, 1))
	img.Set(0, 0, color.NRGBA{A: 255})
	img.Set(1, 0, color.NRGBA{A: 255})
	if err = png.Encode(f, img); err != nil {
		t.Fatal(err)
	}
	f.Close()
	profiles := map[string]capsule.Profile{"demo": {Kind: "browser", NativeKind: "browser", RAMRoot: root, Display: ":100", Width: 2, Height: 1}}
	runner := &settlementRunner{initialRunner: initialRunner{root: root}, mode: "ok", started: make(chan struct{}), finish: make(chan struct{})}
	m, err := capsule.NewManager(capsule.Config{BinDir: t.TempDir(), Profiles: profiles, MaxSurfaces: 1, MaxOutputBytes: 8192, Timeout: time.Second, OriginNS: 100}, runner, func() uint64 { return 200 })
	if err != nil {
		t.Fatal(err)
	}
	pub, err := frames.NewPublisher(frames.PublishConfig{RAMRoot: root, MaxFrameBytes: 8192, MaxPixels: 2, MaxSurfaces: 1})
	if err != nil {
		t.Fatal(err)
	}
	defer pub.Close()
	h := isolatedWireHandler(t, m, profiles, map[string]*frames.Publisher{root: pub}, 100)
	call := func(op string, params any) []json.RawMessage {
		t.Helper()
		body, _ := json.Marshal(map[string]any{"schema": "surface_request/v0.2", "op": op, "params": params})
		events, err := h(context.Background(), ipc.Envelope{Body: body})
		if err != nil {
			t.Fatal(err)
		}
		return events
	}
	created := call("create", map[string]any{"kind": "browser", "profile_id": "demo", "geometry": map[string]int{"w": 2, "h": 1}})
	var event struct {
		Data struct{ Surface capsule.Surface }
	}
	if err = json.Unmarshal(created[0], &event); err != nil {
		t.Fatal(err)
	}
	sid := event.Data.Surface.SurfaceID
	action := map[string]any{"type": "key", "keys": []string{"a"}}

	// 1. Human origin under agent control -> refused with SURFACE_NOT_HUMAN_CONTROLLED receipt.
	refusedHuman := call("input", map[string]any{"surface_id": sid, "proposal_id": "hi-under-agent", "origin": "human", "action": action})
	var receipt map[string]any
	if len(refusedHuman) != 1 || json.Unmarshal(refusedHuman[0], &receipt) != nil || receipt["proposal_id"] != "hi-under-agent" || receipt["error"] != "input failed: SURFACE_NOT_HUMAN_CONTROLLED: agent control blocks human input" {
		t.Fatalf("unexpected receipt for human origin under agent control: %s", refusedHuman)
	}

	// 2. Take control for human -> human origin applied without post-input pixel capture (input_applied + action_receipt error=="").
	call("take_control", map[string]any{"surface_id": sid, "owner": "human"})
	appliedHuman := call("input", map[string]any{"surface_id": sid, "proposal_id": "hi-under-human", "origin": "human", "action": action})
	if len(appliedHuman) != 2 {
		t.Fatalf("expected 2 events for applied human input (no post-input capture), got %s", appliedHuman)
	}
	receipt = nil
	if json.Unmarshal(appliedHuman[1], &receipt) != nil || receipt["proposal_id"] != "hi-under-human" || receipt["error"] != "" {
		t.Fatalf("unexpected receipt for applied human input: %s", appliedHuman[1])
	}

	// 3. Agent origin under human control -> refused with SURFACE_NOT_AGENT_CONTROLLED receipt.
	refusedAgent := call("input", map[string]any{"surface_id": sid, "proposal_id": "ag-under-human", "origin": "agent", "action": action})
	receipt = nil
	if len(refusedAgent) != 1 || json.Unmarshal(refusedAgent[0], &receipt) != nil || receipt["proposal_id"] != "ag-under-human" || receipt["error"] != "input failed: SURFACE_NOT_AGENT_CONTROLLED: human control blocks agent input" {
		t.Fatalf("unexpected receipt for agent origin under human control: %s", refusedAgent)
	}

	// 4. Release control back to agent (with refresh capture) -> agent path keeps post-input pixel verification (3 events: input_applied + frame_ready + action_receipt).
	call("release_control", map[string]any{"surface_id": sid})
	if _, _, err := m.Capture(context.Background(), sid); err != nil {
		t.Fatal(err)
	}
	appliedAgent := call("input", map[string]any{"surface_id": sid, "proposal_id": "ag-under-agent", "origin": "agent", "action": action})
	if len(appliedAgent) != 3 {
		t.Fatalf("expected 3 events for applied agent input, got %s", appliedAgent)
	}
}

type holdingsSettlementRunner struct {
	root string
}

func (r *holdingsSettlementRunner) Run(_ context.Context, args []string, _ []byte) ([]byte, error) {
	switch filepath.Base(args[0]) + ":" + args[1] {
	case "srf-holdings-capsule:start":
		framesDir := filepath.Join(r.root, "hc-0123456789ab/frames")
		return json.Marshal([]map[string]any{{
			"slot":        1,
			"container":   "srf-ng-hcap-hc-0123456789ab-1",
			"run_id":      "hc-0123456789ab",
			"display":     ":100",
			"width":       2,
			"height":      1,
			"lane_socket": "/tmp/lane.sock",
			"ready_line":  "READY",
			"ready_ms":    0,
			"frames_dir":  framesDir,
		}})
	case "srf-holdings-capsule:frame":
		path := filepath.Join(r.root, "hc-0123456789ab/frames/f.png")
		return json.Marshal(map[string]any{
			"sha256_rgb24":    fmt.Sprintf("%x", sha256.Sum256(make([]byte, 6))),
			"path":            path,
			"seq":             1,
			"width":           2,
			"height":          1,
			"capture_mono_ns": 200,
			"generation":      "srf-ng-hcap-hc-0123456789ab-1",
		})
	case "srf-holdings-capsule:input":
		return []byte(`{"input_mono_ns":200}`), nil
	}
	return nil, errors.New("unexpected native command")
}

func TestHumanEmptyBaselineSettles(t *testing.T) {
	root, err := os.MkdirTemp("/dev/shm", "mv-human-empty-baseline-")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(root)
	source := filepath.Join(root, "demo-run/frames/w1")
	if err = os.MkdirAll(source, 0700); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(source, "f.png")
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY, 0600)
	if err != nil {
		t.Fatal(err)
	}
	img := image.NewNRGBA(image.Rect(0, 0, 2, 1))
	img.Set(0, 0, color.NRGBA{A: 255})
	img.Set(1, 0, color.NRGBA{A: 255})
	if err = png.Encode(f, img); err != nil {
		t.Fatal(err)
	}
	f.Close()

	profiles := map[string]capsule.Profile{"demo": {Kind: "browser", NativeKind: "browser", RAMRoot: root, Display: ":100", Width: 2, Height: 1}}
	runner := &settlementRunner{initialRunner: initialRunner{root: root}, mode: "ok", started: make(chan struct{}), finish: make(chan struct{})}
	m, err := capsule.NewManager(capsule.Config{BinDir: t.TempDir(), Profiles: profiles, MaxSurfaces: 1, MaxOutputBytes: 8192, Timeout: time.Second, OriginNS: 100}, runner, func() uint64 { return 200 })
	if err != nil {
		t.Fatal(err)
	}
	pub, err := frames.NewPublisher(frames.PublishConfig{RAMRoot: root, MaxFrameBytes: 8192, MaxPixels: 2, MaxSurfaces: 1})
	if err != nil {
		t.Fatal(err)
	}
	defer pub.Close()

	// Create directly on Manager so wireHandler's lastFrames baseline is empty.
	created, err := m.Handle(context.Background(), capsule.Request{
		Schema: "surface_request/v0.2",
		Op:     "create",
		Params: json.RawMessage(`{"kind":"browser","profile_id":"demo","geometry":{"w":2,"h":1}}`),
	})
	if err != nil || len(created) != 1 {
		t.Fatalf("create failed: %s %v", created, err)
	}
	var event struct {
		Data struct{ Surface capsule.Surface }
	}
	if err = json.Unmarshal(created[0], &event); err != nil {
		t.Fatal(err)
	}
	sid := event.Data.Surface.SurfaceID

	h := isolatedWireHandler(t, m, profiles, map[string]*frames.Publisher{root: pub}, 100)
	call := func(op string, params any) []json.RawMessage {
		t.Helper()
		body, _ := json.Marshal(map[string]any{"schema": "surface_request/v0.2", "op": op, "params": params})
		events, err := h(context.Background(), ipc.Envelope{Body: body})
		if err != nil {
			t.Fatal(err)
		}
		return events
	}

	call("take_control", map[string]any{"surface_id": sid, "owner": "human"})
	action := map[string]any{"type": "key", "keys": []string{"a"}}
	settled := call("input", map[string]any{"surface_id": sid, "proposal_id": "hi-empty-baseline", "origin": "human", "action": action})
	if len(settled) != 2 {
		t.Fatalf("expected input_applied + error action_receipt when lastFrames is empty, got %s", settled)
	}
	var applied struct {
		Schema string `json:"schema"`
		Event  string `json:"event"`
		Data   struct {
			SurfaceID  string `json:"surface_id"`
			ProposalID string `json:"proposal_id"`
		} `json:"data"`
	}
	if err := json.Unmarshal(settled[0], &applied); err != nil || applied.Schema != "surface_event/v0.2" || applied.Event != "input_applied" || applied.Data.ProposalID != "hi-empty-baseline" {
		t.Fatalf("unexpected input_applied event: %s (%v)", settled[0], err)
	}
	var receipt map[string]any
	if err := json.Unmarshal(settled[1], &receipt); err != nil {
		t.Fatalf("unmarshal receipt: %s (%v)", settled[1], err)
	}
	if receipt["schema"] != "action_receipt/v0.2" || receipt["proposal_id"] != "hi-empty-baseline" || receipt["verified"] != false || receipt["error"] == "" || receipt["error"] == nil || receipt["after_frame"] == nil {
		t.Fatalf("unexpected error receipt for empty baseline: %s", settled[1])
	}

	policy, err := ipc.NewPolicy(ipc.Config{SocketPath: filepath.Join(t.TempDir(), "core.sock"), MaxReplayEntries: 100, MaxConnections: 1, HelloTimeout: time.Second, ReadTimeout: time.Second, WriteTimeout: time.Second, ProbeTimeout: time.Second, ShutdownTimeout: time.Second, MaxFrameBytes: 1 << 20, MaxDepth: 32, MaxStringBytes: 65536, MaxKeys: 256, MaxArrayItems: 4096}, os.DirFS(filepath.Join("..", "..", "..", "contracts", "v0.2")), &lifecyclePolicy{})
	if err != nil {
		t.Fatal(err)
	}
	envelope, _ := json.Marshal(ipc.Envelope{Schema: "envelope/v0.2", MsgID: "proof", From: ipc.Endpoint{Component: "daemon", Instance: "daemon"}, To: ipc.Endpoint{Component: "core", Instance: "core"}, SessionID: "proof", Seq: 1, Nonce: "proof", Body: settled[1]})
	if _, err = policy.ValidateEnvelope(envelope); err != nil {
		t.Fatalf("empty-baseline error receipt failed schema validation: %v (%s)", err, settled[1])
	}

	// Holdings human-origin input requires and invokes VerifyHoldingsInput.
	hSource := filepath.Join(root, "hc-0123456789ab/frames")
	if err = os.MkdirAll(hSource, 0700); err != nil {
		t.Fatal(err)
	}
	hPath := filepath.Join(hSource, "f.png")
	hf, err := os.OpenFile(hPath, os.O_CREATE|os.O_WRONLY, 0600)
	if err != nil {
		t.Fatal(err)
	}
	if err = png.Encode(hf, img); err != nil {
		t.Fatal(err)
	}
	hf.Close()

	hProfiles := map[string]capsule.Profile{"game": {Kind: "game_capsule", NativeKind: "holdings", AppDir: "/opt/game", RAMRoot: root, Display: ":100", Width: 2, Height: 1}}
	hRunner := &holdingsSettlementRunner{root: root}
	var verifiedCalls int
	var verifyErr error
	hm, err := capsule.NewManager(capsule.Config{
		BinDir:         t.TempDir(),
		Profiles:       hProfiles,
		MaxSurfaces:    1,
		MaxOutputBytes: 8192,
		Timeout:        time.Second,
		OriginNS:       100,
		VerifyHoldingsInput: func(_ context.Context, _ capsule.Binding, proposal string, _ json.RawMessage, stamp uint64) error {
			verifiedCalls++
			if proposal == "" || stamp != 200 {
				return errors.New("invalid holdings verification args")
			}
			return verifyErr
		},
	}, hRunner, func() uint64 { return 200 })
	if err != nil {
		t.Fatal(err)
	}
	hPub, err := frames.NewPublisher(frames.PublishConfig{RAMRoot: root, MaxFrameBytes: 8192, MaxPixels: 2, MaxSurfaces: 1})
	if err != nil {
		t.Fatal(err)
	}
	defer hPub.Close()
	hh := isolatedWireHandler(t, hm, hProfiles, map[string]*frames.Publisher{root: hPub}, 100)
	hCall := func(op string, params any) []json.RawMessage {
		t.Helper()
		body, _ := json.Marshal(map[string]any{"schema": "surface_request/v0.2", "op": op, "params": params})
		events, err := hh(context.Background(), ipc.Envelope{Body: body})
		if err != nil {
			t.Fatal(err)
		}
		return events
	}
	hCreated := hCall("create", map[string]any{"kind": "game_capsule", "profile_id": "game", "geometry": map[string]int{"w": 2, "h": 1}})
	var hEvent struct {
		Data struct{ Surface capsule.Surface }
	}
	if err = json.Unmarshal(hCreated[0], &hEvent); err != nil {
		t.Fatal(err)
	}
	hSID := hEvent.Data.Surface.SurfaceID
	hCall("take_control", map[string]any{"surface_id": hSID, "owner": "human"})

	verifyErr = errors.New("holdings proof rejected")
	hFailed := hCall("input", map[string]any{"surface_id": hSID, "proposal_id": "hi-holdings-fail", "origin": "human", "action": action})
	if len(hFailed) != 1 || verifiedCalls != 1 {
		t.Fatalf("expected VerifyHoldingsInput refusal to settle with 1 error receipt, calls=%d events=%s", verifiedCalls, hFailed)
	}
	verifyErr = nil
	hOK := hCall("input", map[string]any{"surface_id": hSID, "proposal_id": "hi-holdings-ok", "origin": "human", "action": action})
	if len(hOK) != 2 || verifiedCalls != 2 {
		t.Fatalf("expected verified holdings human input to emit 2 events, calls=%d events=%s", verifiedCalls, hOK)
	}
}

func TestHumanInputReceiptAttachesLatestPublishedFrameRef(t *testing.T) {
	root, err := os.MkdirTemp("/dev/shm", "mv-human-latest-frame-")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(root)
	source := filepath.Join(root, "demo-run/frames/w1")
	if err = os.MkdirAll(source, 0700); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(source, "f.png")
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY, 0600)
	if err != nil {
		t.Fatal(err)
	}
	img := image.NewNRGBA(image.Rect(0, 0, 2, 1))
	img.Set(0, 0, color.NRGBA{A: 255})
	img.Set(1, 0, color.NRGBA{A: 255})
	if err = png.Encode(f, img); err != nil {
		t.Fatal(err)
	}
	f.Close()

	profiles := map[string]capsule.Profile{"demo": {Kind: "browser", NativeKind: "browser", RAMRoot: root, Display: ":100", Width: 2, Height: 1}}
	runner := &settlementRunner{initialRunner: initialRunner{root: root}, mode: "ok", started: make(chan struct{}), finish: make(chan struct{})}
	m, err := capsule.NewManager(capsule.Config{BinDir: t.TempDir(), Profiles: profiles, MaxSurfaces: 1, MaxOutputBytes: 8192, Timeout: time.Second, OriginNS: 100}, runner, func() uint64 { return 200 })
	if err != nil {
		t.Fatal(err)
	}
	pub, err := frames.NewPublisher(frames.PublishConfig{RAMRoot: root, MaxFrameBytes: 8192, MaxPixels: 2, MaxSurfaces: 1})
	if err != nil {
		t.Fatal(err)
	}
	defer pub.Close()

	h := isolatedWireHandler(t, m, profiles, map[string]*frames.Publisher{root: pub}, 100)
	call := func(op string, params any) []json.RawMessage {
		t.Helper()
		body, _ := json.Marshal(map[string]any{"schema": "surface_request/v0.2", "op": op, "params": params})
		events, err := h(context.Background(), ipc.Envelope{Body: body})
		if err != nil {
			t.Fatal(err)
		}
		return events
	}

	// 1. Create surface and record initial create-time frame ref A.
	created := call("create", map[string]any{"kind": "browser", "profile_id": "demo", "geometry": map[string]int{"w": 2, "h": 1}})
	if len(created) != 2 {
		t.Fatalf("expected 2 events on create, got %s", created)
	}
	var createEv struct {
		Data struct{ Surface capsule.Surface }
	}
	if err := json.Unmarshal(created[0], &createEv); err != nil {
		t.Fatal(err)
	}
	sid := createEv.Data.Surface.SurfaceID

	var readyA struct {
		Data struct {
			FrameRef frames.Ref `json:"frame_ref"`
		} `json:"data"`
	}
	if err := json.Unmarshal(created[1], &readyA); err != nil {
		t.Fatal(err)
	}
	frameA := readyA.Data.FrameRef

	// 2. Perform an agent input that publishes a new post-input frame ref B.
	action := map[string]any{"type": "key", "keys": []string{"a"}}
	agentEvents := call("input", map[string]any{"surface_id": sid, "proposal_id": "agent-1", "origin": "agent", "action": action})
	if len(agentEvents) != 3 {
		t.Fatalf("expected 3 events for agent input, got %s", agentEvents)
	}
	var readyB struct {
		Data struct {
			FrameRef frames.Ref `json:"frame_ref"`
		} `json:"data"`
	}
	if err := json.Unmarshal(agentEvents[1], &readyB); err != nil {
		t.Fatal(err)
	}
	frameB := readyB.Data.FrameRef
	if frameB.Generation == frameA.Generation || frameB.RAMWSPath == frameA.RAMWSPath {
		t.Fatalf("post-input frame B (%+v) did not advance from create-time frame A (%+v)", frameB, frameA)
	}

	// 3. Take human control, perform a human input, and assert after_frame equals B (and NOT stale frame A).
	call("take_control", map[string]any{"surface_id": sid, "owner": "human"})
	humanEvents := call("input", map[string]any{"surface_id": sid, "proposal_id": "human-after-agent", "origin": "human", "action": action})
	if len(humanEvents) != 2 {
		t.Fatalf("expected 2 events for human input, got %s", humanEvents)
	}
	var humanReceipt struct {
		ProposalID string     `json:"proposal_id"`
		AfterFrame frames.Ref `json:"after_frame"`
		Error      string     `json:"error"`
	}
	if err := json.Unmarshal(humanEvents[1], &humanReceipt); err != nil {
		t.Fatal(err)
	}
	if humanReceipt.ProposalID != "human-after-agent" || humanReceipt.Error != "" {
		t.Fatalf("unexpected human receipt: %+v", humanReceipt)
	}
	if humanReceipt.AfterFrame == frameA || humanReceipt.AfterFrame.Generation == frameA.Generation || humanReceipt.AfterFrame.RAMWSPath == frameA.RAMWSPath {
		t.Fatalf("human receipt attached stale create-time frame A (%+v) instead of latest frame B (%+v)", humanReceipt.AfterFrame, frameB)
	}
	if humanReceipt.AfterFrame != frameB {
		t.Fatalf("human receipt after_frame = %+v, want latest frame B %+v", humanReceipt.AfterFrame, frameB)
	}
}

func TestHostXpraFixtureHelper(t *testing.T) {
	runHostXpraTestHelper()
}

func TestH1NativeWireUsesPublishedFrame(t *testing.T) {
	ctx := context.Background()
	fixture := startHostXpraTestFixture(t)
	id, root := fixture.Binding.Surface.SurfaceID, fixture.RAMRoot
	pub, err := frames.NewPublisher(frames.PublishConfig{RAMRoot: root, MaxFrameBytes: 8192, MaxPixels: 6, MaxSurfaces: 1})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = pub.Close() })
	wire := wireHandlerWithoutAdmission(fixture.Manager, nil, map[string]*frames.Publisher{root: pub}, 100)
	send := func(proposal string, want string) {
		t.Helper()
		params := map[string]any{
			"surface_id": id, "proposal_id": proposal,
			"action": map[string]any{"type": "click", "button": "left", "x": 0, "y": 0},
		}
		body, _ := json.Marshal(map[string]any{"schema": "surface_request/v0.2", "op": "input", "params": params})
		events, err := wire(ctx, ipc.Envelope{Body: body})
		if err != nil || len(events) == 0 {
			t.Fatalf("wire input: %s %v", events, err)
		}
		var event struct{ Event string }
		if err := json.Unmarshal(events[0], &event); err != nil || event.Event != want {
			t.Fatalf("want %s, got %s (%v)", want, events, err)
		}
	}
	publish := func() (frames.Ref, string) {
		t.Helper()
		// VERIFIED's resident-ring mode selects native capture for host Xpra.
		source := &frames.RingSource{Manager: fixture.Manager}
		binding, frame, err := source.Capture(ctx, id)
		if err != nil {
			t.Fatal(err)
		}
		ref, err := pub.Publish(id, binding, frame)
		if err != nil {
			t.Fatal(err)
		}
		return ref, frame.Path
	}
	send("missing-frame", "error")
	ref, _ := publish()
	original, err := os.ReadFile(ref.RAMWSPath)
	if err != nil {
		t.Fatal(err)
	}
	altered := append([]byte(nil), original...)
	altered[0] ^= 1
	for _, mutation := range []struct {
		proposal string
		data     []byte
	}{
		{"wrong-digest", altered},
		{"wrong-extent", original[:len(original)-1]},
	} {
		if err := os.WriteFile(ref.RAMWSPath, mutation.data, 0600); err != nil {
			t.Fatal(err)
		}
		send(mutation.proposal, "error")
	}
	if err := os.WriteFile(ref.RAMWSPath, original, 0600); err != nil {
		t.Fatal(err)
	}
	fixture.Manager.UpdateHostWindowGeometry(id, 3, 2)
	send("wrong-geometry", "error")
	fixture.Manager.UpdateHostWindowGeometry(id, 2, 1)
	send("stale-generation", "error")
	if fixture.Calls("input") != 0 {
		t.Fatal("missing, altered or stale publications dispatched input")
	}
	ref, sourcePath := publish()
	if err := os.Remove(sourcePath); err != nil {
		t.Fatal(err)
	}
	send("current-publication", "input_applied")
	if fixture.Calls("input") != 1 {
		t.Fatal("current publication did not dispatch exactly once")
	}
	after, err := pub.Current(id)
	if err != nil || after.Generation <= ref.Generation || after.SHA256 == ref.SHA256 {
		t.Fatalf("input effect missing from subsequent publication: %+v %v", after, err)
	}
}

type hostXpraTestFixture struct {
	Manager *capsule.Manager
	Binding capsule.Binding
	RAMRoot string
	dir     string
}

func startHostXpraTestFixture(t *testing.T) hostXpraTestFixture {
	t.Helper()
	dir, err := os.MkdirTemp("/tmp", "vp-hx-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	ram, err := os.MkdirTemp("/dev/shm", "vp-hx-frames-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(ram) })
	for _, subdir := range []string{"opening-modes", "human-bindings"} {
		if err := os.Mkdir(filepath.Join(dir, subdir), 0700); err != nil {
			t.Fatal(err)
		}
	}
	testBinary, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"xpra", "Xvfb", "xauth", "xdotool", "srf-host-window"} {
		script := "#!/bin/sh\nexit 0\n"
		if name == "Xvfb" || name == "xpra" || name == "srf-host-window" {
			quote := func(s string) string { return "'" + strings.ReplaceAll(s, "'", "'\\''") + "'" }
			script = fmt.Sprintf("#!/bin/sh\nexec %s -test.run '^TestHostXpraFixtureHelper$' -- %s %s \"$@\"\n",
				quote(testBinary), quote(name), quote(dir))
		}
		if err := os.WriteFile(filepath.Join(dir, name), []byte(script), 0700); err != nil {
			t.Fatal(err)
		}
	}
	const profileID = "native-fixture"
	opening := `{"schema":"metaviewer.opening_mode/v0.3","profile_id":"native-fixture","mode":"agent","host_binary":"xdotool","host_xpra":true}`
	if err := os.WriteFile(filepath.Join(dir, "opening-modes", profileID+".json"), []byte(opening), 0600); err != nil {
		t.Fatal(err)
	}
	runner, err := capsule.NewProcessRunner(dir, []string{"PATH=" + dir, "MV_SRF_STATE_DIR=" + dir}, 1<<20, 3*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	profiles := map[string]capsule.Profile{
		"demo": {Kind: "browser", NativeKind: "browser", Display: ":100", RAMRoot: ram, Width: 1280, Height: 900},
	}
	const origin = 100
	start := time.Now()
	m, err := capsule.NewManager(capsule.Config{BinDir: dir, OriginNS: origin, Profiles: profiles, MaxSurfaces: 2, MaxOutputBytes: 1 << 20, Timeout: 3 * time.Second}, runner, func() uint64 {
		return origin + uint64(time.Since(start))
	})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	create := capsule.Request{Schema: "surface_request/v0.2", Op: "create", Params: json.RawMessage(
		`{"kind":"desktop_capsule","profile_id":"native-fixture","geometry":{"w":1280,"h":900}}`)}
	events, err := m.Handle(ctx, create)
	if err != nil || len(events) != 1 {
		t.Fatalf("synthetic Xpra create: %s %v", events, err)
	}
	var created struct {
		Data struct{ Surface capsule.Surface }
	}
	if err := json.Unmarshal(events[0], &created); err != nil {
		t.Fatal(err)
	}
	id := created.Data.Surface.SurfaceID
	t.Cleanup(func() {
		params, _ := json.Marshal(map[string]string{"surface_id": id})
		_, err := m.Handle(context.Background(), capsule.Request{Schema: create.Schema, Op: "destroy", Params: params})
		if err != nil {
			t.Errorf("synthetic Xpra destroy: %v", err)
		}
	})
	m.UpdateHostWindowGeometry(id, 2, 1)
	b, err := m.SubscriptionBinding(id)
	if err != nil {
		t.Fatal(err)
	}
	return hostXpraTestFixture{Manager: m, Binding: b, RAMRoot: ram, dir: dir}
}

func (f hostXpraTestFixture) SetMode(t *testing.T, mode string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(f.dir, "mode"), []byte(mode), 0600); err != nil {
		t.Fatal(err)
	}
}

func (f hostXpraTestFixture) Calls(op string) int {
	trace, _ := os.ReadFile(filepath.Join(f.dir, "trace"))
	return strings.Count(string(trace), op+"\n")
}

func runHostXpraTestHelper() {
	idx := slices.Index(os.Args, "--")
	if idx < 0 {
		return
	}
	if err := runHostXpraHelper(os.Args[idx+1:]); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	os.Exit(0)
}

func runHostXpraHelper(args []string) error {
	tool, dir, argv := args[0], args[1], args[2:]
	value := func(flag string) string {
		i := slices.Index(argv, flag)
		if i < 0 || i+1 == len(argv) {
			return ""
		}
		return argv[i+1]
	}
	switch tool {
	case "Xvfb":
		path := "@" + filepath.Join("/tmp/.X11-unix", "X"+strings.TrimPrefix(argv[0], ":"))
		return serveFixtureSocket(path, func() error {
			return os.WriteFile(filepath.Join(dir, "pid"), []byte(strconv.Itoa(os.Getpid())), 0600)
		})
	case "xpra":
		for _, arg := range argv {
			if path, ok := strings.CutPrefix(arg, "--bind="); ok {
				return serveFixtureSocket(path, func() error { return nil })
			}
		}
		return fmt.Errorf("fixture bind argument absent")
	}

	op := argv[0]
	trace, err := os.OpenFile(filepath.Join(dir, "trace"), os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0600)
	if err != nil {
		return err
	}
	_, err = fmt.Fprintln(trace, op)
	trace.Close()
	if err != nil {
		return err
	}
	display, authority := value("--display"), value("--xauthority")
	if display == "" || display != os.Getenv("DISPLAY") || authority != os.Getenv("XAUTHORITY") ||
		filepath.Dir(authority) != os.Getenv("HOME") || os.Getenv("XDG_RUNTIME_DIR") != filepath.Join(os.Getenv("HOME"), "runtime") {
		return fmt.Errorf("native fixture command lacks launcher environment")
	}
	mode, _ := os.ReadFile(filepath.Join(dir, "mode"))
	switch op {
	case "list":
		pid, err := os.ReadFile(filepath.Join(dir, "pid"))
		if err != nil {
			return err
		}
		fmt.Printf(`{"windows":[{"xid":42,"witness":"fixture-window","pid":%s,"window_type":"normal","visible":true,"rect":{"width":2,"height":1}}]}`, pid)
	case "capture":
		switch string(mode) {
		case "refuse":
			return fmt.Errorf("synthetic host capture refusal")
		case "closed":
			return fmt.Errorf("host_window_unavailable: window closed")
		}
		const width, height = 2, 1
		xwd := make([]byte, 100+4*width*height)
		fields := map[int]uint32{0: 100, 1: 7, 2: 2, 3: 24, 4: width, 5: height, 11: 32, 12: 4 * width, 13: 4, 14: 0xff0000, 15: 0xff00, 16: 0xff}
		for index, value := range fields {
			binary.BigEndian.PutUint32(xwd[4*index:], value)
		}
		if len(mode) == 1 {
			xwd[100] = mode[0]
		}
		for {
			if _, err := os.Stdout.Write(xwd); err != nil || value("--max-frames") != "" {
				return err
			}
			time.Sleep(20 * time.Millisecond)
		}
	case "input":
		if err := os.WriteFile(filepath.Join(dir, "mode"), []byte("2"), 0600); err != nil {
			return err
		}
		fmt.Print(`{"ok":true,"xid":42,"witness":"fixture-window","kind":"click","focus_verified":true}`)
	default:
		return fmt.Errorf("unexpected fixture operation %q", op)
	}
	return nil
}

func serveFixtureSocket(path string, ready func() error) error {
	l, err := net.Listen("unix", path)
	if err != nil {
		return err
	}
	defer l.Close()
	if err := ready(); err != nil {
		return err
	}
	for {
		c, err := l.Accept()
		if err != nil {
			return err
		}
		c.Close()
	}
}

package capsule

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"metaviewer/daemon-adapter/internal/cli"
)

// Source: SW-0103 inv. 5 (Arithmetic safety & exact clock mapping)
func TestCaptureClockMapping(t *testing.T) {
	binDir := t.TempDir()
	stateDir := filepath.Join(t.TempDir(), "state")
	ramRoot, err := os.MkdirTemp("/dev/shm", "clock-test-shm-")
	if err != nil {
		t.Skip("skipping /dev/shm test if unavailable")
	}
	defer os.RemoveAll(ramRoot)

	framesDir := filepath.Join(ramRoot, "demo-run", "frames", "w1")
	if err := os.MkdirAll(framesDir, 0700); err != nil {
		t.Fatal(err)
	}

	framePath := filepath.Join(framesDir, "f.png")
	scriptPath := filepath.Join(binDir, "srf-identity-surface")

	_ = os.WriteFile(framePath, []byte("dummy"), 0600)

	// Script writer helper
	writeScriptWithNSAndSize := func(ns uint64, width, height int) {
		s := fmt.Sprintf(`#!/bin/sh
cmd="$1"
if [ "$cmd" = "start" ]; then
    echo '{"container": "srf-id-demo", "run_id": "demo-run", "windows": [{"id": "w1", "x": 0, "y": 0, "w": 2, "h": 1, "title": "", "url": ""}]}'
    exit 0
elif [ "$cmd" = "frame" ]; then
    echo '{"path": "%s", "seq": 1, "sha256_rgb24": "%s", "width": %d, "height": %d, "capture_mono_ns": %d, "generation": "srf-id-demo", "window": {"id": "w1", "x": 0, "y": 0, "w": %d, "h": %d, "title": "", "url": ""}}'
    exit 0
fi
`, framePath, hex.EncodeToString(make([]byte, 32)), width, height, ns, width, height)
		_ = os.WriteFile(scriptPath, []byte(s), 0755)
	}

	writeScriptWithNSAndSize(2000, 2, 1)

	runner, err := NewProcessRunner(binDir, []string{"PATH=/usr/bin:/bin", "MV_SRF_STATE_DIR=" + stateDir}, 4096, 2*time.Second)
	if err != nil {
		t.Fatal(err)
	}

	originNS := uint64(1000)
	var currentClock atomic.Uint64
	currentClock.Store(1500)

	clockFunc := func() uint64 {
		return currentClock.Load()
	}

	cfg := Config{
		BinDir:         binDir,
		MaxSurfaces:    10,
		MaxOutputBytes: 4096,
		Timeout:        2 * time.Second,
		OriginNS:       originNS,
		Profiles: map[string]Profile{
			"id-demo": {Kind: "browser", NativeKind: "browser", Display: ":0", Width: 2, Height: 1, RAMRoot: ramRoot},
		},
	}

	mgr, err := NewManager(cfg, runner, clockFunc)
	if err != nil {
		t.Fatal(err)
	}

	req := Request{
		Schema: "surface_request/v0.2",
		Op:     "create",
		Params: []byte(`{"kind":"browser","profile_id":"id-demo","geometry":{"w":2,"h":1}}`),
	}
	resp, err := mgr.Handle(context.Background(), req)
	if err != nil {
		t.Fatal(err)
	}
	var created struct {
		Data struct {
			Surface struct {
				SurfaceID string `json:"surface_id"`
			} `json:"surface"`
		} `json:"data"`
	}
	_ = cli.ParseLine(resp[0], 4096, &created)
	surfaceID := created.Data.Surface.SurfaceID

	// Test 1: CaptureMonoNS < OriginNS -> Refused
	writeScriptWithNSAndSize(500, 2, 1) // 500 < OriginNS (1000)
	currentClock.Store(1500)
	_, _, err = mgr.Capture(context.Background(), surfaceID)
	if err == nil {
		t.Fatal("expected refusal when CaptureMonoNS < OriginNS")
	}

	// Test 2: CaptureMonoNS < before -> Refused
	writeScriptWithNSAndSize(1200, 2, 1) // before clock set to 1500
	currentClock.Store(1500)
	_, _, err = mgr.Capture(context.Background(), surfaceID)
	if err == nil {
		t.Fatal("expected refusal when CaptureMonoNS < before clock")
	}

	// Test 3: CaptureMonoNS > after -> Refused
	writeScriptWithNSAndSize(3000, 2, 1) // frame claims ns=3000, but after clock will be 2500
	currentClock.Store(1500)
	// We advance clock inside execution by having clock return 2500 on second read
	var callCount atomic.Int32
	steppingClock := func() uint64 {
		if callCount.Add(1) == 1 {
			return 1500 // before
		}
		return 2500 // after
	}
	mgrWithStepClock, _ := NewManager(cfg, runner, steppingClock)
	// Need to register binding on mgrWithStepClock
	mgrWithStepClock.bindings[surfaceID] = mgr.bindings[surfaceID]
	_, _, err = mgrWithStepClock.Capture(context.Background(), surfaceID)
	if err == nil {
		t.Fatal("expected refusal when CaptureMonoNS > after clock")
	}

	// Test 4: Arithmetic / width*height geometry overflow product refused
	writeScriptWithNSAndSize(2000, 99999, 99999)
	currentClock.Store(1500)
	_, _, err = mgr.Capture(context.Background(), surfaceID)
	if err == nil {
		t.Fatal("expected refusal for frame geometry exceeding surface profile bounds")
	}

	// Test 5: Valid Capture -> TSNS = CaptureMonoNS - OriginNS, CaptureUS = (after - before) / 1000
	writeScriptWithNSAndSize(2000, 2, 1) // CaptureMonoNS = 2000
	callCount.Store(0)                   // reset
	// steppingClock returns 1500 before, 2500 after -> CaptureUS = (2500-1500)/1000 = 1 us
	// TSNS = 2000 - 1000 = 1000 ns
	_, frame, err := mgrWithStepClock.Capture(context.Background(), surfaceID)
	if err != nil {
		t.Fatalf("valid capture failed: %v", err)
	}
	if frame.TSNS != 1000 {
		t.Errorf("expected TSNS=1000, got %d", frame.TSNS)
	}
	if frame.CaptureUS != 1 {
		t.Errorf("expected CaptureUS=1, got %d", frame.CaptureUS)
	}
}

func TestNativePairStampUsesValidatedCaptureClock(t *testing.T) {
	const child = "MV_OBS_NATIVE_TEST_CHILD"
	if os.Getenv(child) == "1" {
		runner := &scriptRunner{output: []byte(`{"container":"srf-id-demo","run_id":"demo-run","windows":[{"id":"w1","x":0,"y":0,"w":4,"h":3,"title":"","url":""}]}`)}
		manager := testManager(t, runner)
		profile := manager.cfg.Profiles["demo"]
		profile.Width, profile.Height = 4, 3
		manager.cfg.Profiles["demo"] = profile
		if _, err := manager.Handle(context.Background(), req("create", `{"kind":"browser","profile_id":"demo","geometry":{"w":4,"h":3}}`)); err != nil {
			t.Fatal(err)
		}
		for id, binding := range manager.bindings {
			trace := NewObservationTrace("daemon", ObservationFields{SurfaceID: id, SessionID: "fixture"})
			ctx := WithObservationTrace(context.Background(), trace)
			frame := NativeFrame{
				Path: filepath.Join(binding.FramesRoot, "f.png"), Seq: 1, Width: 4, Height: 3,
				CaptureMonoNS: 200, Generation: binding.Container,
				SHA256RGB24: strings.Repeat("a", 64), Window: &Window{ID: "w1", W: 4, H: 3},
			}
			runner.output, _ = json.Marshal(frame)
			if _, _, err := manager.Capture(ctx, id); err != nil {
				t.Fatal(err)
			}
			frame.CaptureMonoNS++
			runner.output, _ = json.Marshal(frame)
			if _, _, err := manager.Capture(ctx, id); err == nil {
				t.Fatal("future capture timestamp was accepted")
			}
			trace.Flush()
		}
		return
	}

	dir := t.TempDir()
	path := filepath.Join(dir, "stages.jsonl")
	for _, export := range []string{"", path} {
		cmd := exec.Command(os.Args[0], "-test.run=^TestNativePairStampUsesValidatedCaptureClock$")
		cmd.Env = append(os.Environ(), child+"=1", ObservationStampsPathEnv+"="+export)
		cmd.Dir = dir
		if output, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("%v: %s", err, output)
		}
		if export == "" {
			if _, err := os.Stat(path); !os.IsNotExist(err) {
				t.Fatalf("disabled export: %v", err)
			}
			continue
		}
		data, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		var row struct {
			Stage         string `json:"stage"`
			MonoNS        int64  `json:"mono_ns"`
			CaptureBranch string `json:"capture_branch"`
			RunID         string `json:"run_id"`
		}
		if err := json.Unmarshal(data, &row); err != nil {
			t.Fatal(err)
		}
		if row.Stage != "native_pair_begin" || row.MonoNS != 200 || row.CaptureBranch != "native" || row.RunID != "demo-run" {
			t.Fatalf("unexpected native stamp: %+v", row)
		}
	}
}

const rowsPerWriter = 512

func TestAppendAcrossProcesses(t *testing.T) {
	if os.Getenv("MV_OBS_TEST_CHILD") == "1" {
		if ObservationStampsEnabled() {
			fields := ObservationFields{SurfaceID: "fixture"}
			if digest := os.Getenv("MV_OBS_TEST_DIGEST"); digest != "" {
				fields = ObservationFields{
					SurfaceID: "surface-demo", SessionID: "serve-session",
					Generation: 17, FrameDigest: digest, CaptureSeq: 1,
					RunID: "fixture-run", Route: "ring", CaptureBranch: "ring",
				}
			}
			barrier := os.Getenv("MV_OBS_TEST_BARRIER")
			if barrier != "" {
				if err := os.WriteFile(barrier+".go", nil, 0600); err != nil {
					t.Fatal(err)
				}
				deadline := time.Now().Add(5 * time.Second)
				for {
					if _, err := os.Stat(barrier); err == nil {
						break
					}
					if time.Now().After(deadline) {
						t.Fatal("shared append barrier timed out")
					}
					time.Sleep(time.Millisecond)
				}
			}
			trace := NewObservationTrace("daemon", fields)
			for offset := 0; offset < rowsPerWriter; offset += 64 {
				for range 64 {
					trace.Mark("publish_end", "ok", "")
				}
				trace.Flush()
				if barrier != "" {
					time.Sleep(time.Millisecond)
				}
			}
		}
		return
	}
	destination := filepath.Join(t.TempDir(), "stages.jsonl")
	var children []*exec.Cmd
	for range 4 {
		cmd := exec.Command(os.Args[0], "-test.run=^TestAppendAcrossProcesses$")
		cmd.Env = append(os.Environ(), ObservationStampsPathEnv+"="+destination, "MV_OBS_TEST_CHILD=1")
		if err := cmd.Start(); err != nil {
			t.Fatal(err)
		}
		children = append(children, cmd)
	}
	for _, cmd := range children {
		if err := cmd.Wait(); err != nil {
			t.Fatal(err)
		}
	}
	data, err := os.ReadFile(destination)
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(strings.TrimSuffix(string(data), "\n"), "\n")
	pids := make(map[int]bool)
	for _, line := range lines {
		var event observationRow
		if err := json.Unmarshal([]byte(line), &event); err != nil {
			t.Fatal(err)
		}
		if event.Stage != "publish_end" || event.MonoNS <= 0 || event.BootID == "" ||
			event.TimeNamespace == "" || event.PID <= 0 || event.Role != "daemon" ||
			event.FrameID == "" || event.CaptureID != event.FrameID || event.SurfaceID != "fixture" {
			t.Fatalf("incomplete stage stamp: %+v", event)
		}
		pids[event.PID] = true
	}
	if len(lines) != rowsPerWriter*4 || len(pids) != 4 {
		t.Fatalf("got %d lines from %d processes", len(lines), len(pids))
	}

	// An unset export path must not create a default file.
	cmd := exec.Command(os.Args[0], "-test.run=^TestAppendAcrossProcesses$")
	cmd.Env = append(os.Environ(), ObservationStampsPathEnv+"=", "MV_OBS_TEST_CHILD=1")
	cmd.Dir = t.TempDir()
	if err := cmd.Run(); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(cmd.Dir, "stages.jsonl")); !os.IsNotExist(err) {
		t.Fatalf("disabled run created an export: %v", err)
	}
}

func TestAppendRefusesUnsafePaths(t *testing.T) {
	dir := t.TempDir()
	sentinel := filepath.Join(dir, "sentinel")
	if err := os.WriteFile(sentinel, []byte("unchanged"), 0600); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(dir, "link")
	if err := os.Symlink(sentinel, link); err != nil {
		t.Fatal(err)
	}
	missing := filepath.Join(dir, "missing")
	for _, target := range []string{"stages.jsonl", link, filepath.Join(missing, "stages.jsonl")} {
		cmd := exec.Command(os.Args[0], "-test.run=^TestAppendAcrossProcesses$")
		cmd.Env = append(os.Environ(), ObservationStampsPathEnv+"="+target, "MV_OBS_TEST_CHILD=1")
		cmd.Dir = dir
		if output, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("writer with %q: %v: %s", target, err, output)
		}
	}
	if data, err := os.ReadFile(sentinel); err != nil || string(data) != "unchanged" {
		t.Fatalf("writer modified symlink target: %q, %v", data, err)
	}
	for _, path := range []string{filepath.Join(dir, "stages.jsonl"), missing} {
		if _, err := os.Lstat(path); !os.IsNotExist(err) {
			t.Fatalf("writer created %q: %v", path, err)
		}
	}
}

var benchmarkSink int

func BenchmarkOffBaseline(b *testing.B) {
	for b.Loop() {
		benchmarkSink++
	}
}

func BenchmarkOffGuard(b *testing.B) {
	if ObservationStampsEnabled() {
		b.Fatal("trace must be disabled")
	}
	for b.Loop() {
		if ObservationStampsEnabled() {
			trace := NewObservationTrace("daemon", ObservationFields{SurfaceID: "fixture"})
			trace.Mark("capture_due", "", "")
			trace.Flush()
		}
		benchmarkSink++
	}
}

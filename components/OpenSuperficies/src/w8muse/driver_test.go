package w8muse

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"opensuperficies.local/srf/src/workerbridge"
)

type fakeSeat struct {
	calls   []string
	fail    map[string]int
	receipt json.RawMessage
	ready   bool
}

func (s *fakeSeat) Exchange(_ context.Context, req Request, bundle io.Reader, patch []byte) (Response, error) {
	s.calls = append(s.calls, req.Verb)
	if req.Verb == "submit" {
		if bundle == nil {
			return Response{}, errors.New("missing bundle")
		}
		_, _ = io.Copy(io.Discard, bundle)
	}
	if s.fail[req.Verb] > 0 {
		s.fail[req.Verb]--
		return Response{}, io.ErrUnexpectedEOF
	}
	switch req.Verb {
	case "submit":
		w, _ := workerbridge.Parse(req.WorkerTask)
		return Response{Handle: hash(canonical([]any{w["task_id"], w["attempt_id"], w["lease_id"]}))}, nil
	case "wait":
		return Response{Ready: s.ready}, nil
	case "receipt":
		return Response{Ready: true, Receipt: s.receipt, Artifacts: map[string]string{"report.md": "Test gate report\n"}}, nil
	default:
		return Response{}, nil
	}
}

type fakeRepo struct{ calls int }

func (r *fakeRepo) Prepare(_ context.Context, _ string, _ string, _ string) (Bundle, error) {
	r.calls++
	return Bundle{Tip: strings.Repeat("a", 40), Length: 6, Reader: io.NopCloser(strings.NewReader("bundle"))}, nil
}

type fakePublisher struct{ calls int }

func (p *fakePublisher) Publish(context.Context, Publication) error { p.calls++; return nil }
func fixture(t *testing.T) (Config, Item, *fakeSeat, *fakeRepo, *fakePublisher) {
	t.Helper()
	b, e := os.ReadFile("../../testdata/workerbridge/codec.json")
	if e != nil {
		t.Fatal(e)
	}
	var f map[string]json.RawMessage
	if e = json.Unmarshal(b, &f); e != nil {
		t.Fatal(e)
	}
	task, e := workerbridge.Parse(f["bridge"])
	if e != nil {
		t.Fatal(e)
	}
	task["expires_at_utc"] = "2099-01-01T00:00:00Z"
	taskBytes, _ := workerbridge.BridgeCanonical(task)
	item := Item{TaskID: task["task_id"].(string), Attempt: task["attempt_id"].(string), Objective: "registered objective"}
	root := scratch(t)
	bridge := filepath.Join(root, "bridge")
	dir := filepath.Join(bridge, "tasks", item.TaskID, item.Attempt)
	if e = os.MkdirAll(dir, 0700); e != nil {
		t.Fatal(e)
	}
	if e = os.WriteFile(filepath.Join(dir, "task.json"), taskBytes, 0644); e != nil {
		t.Fatal(e)
	}
	dir = filepath.Join(bridge, "expected", item.TaskID)
	_ = os.MkdirAll(dir, 0700)
	_ = os.WriteFile(filepath.Join(dir, item.Attempt+".json"), []byte(`{"answers":[]}`), 0644)
	h := sha256.Sum256(taskBytes)
	event := map[string]any{"event": "lease_granted", "seq": 1, "task_sha256": hex.EncodeToString(h[:])}
	for _, k := range append(workerbridge.SharedIDs(), "surface", "role", "allowed_write_paths", "expires_at_utc") {
		event[k] = task[k]
	}
	line, _ := json.Marshal(event)
	_ = os.WriteFile(filepath.Join(bridge, "ledger.jsonl"), append(line, '\n'), 0644)
	worker, e := workerbridge.BridgeToWorker(taskBytes, item.Objective, 10000)
	if e != nil {
		t.Fatal(e)
	}
	receipt, e := workerbridge.Parse(f["receipt"])
	if e != nil {
		t.Fatal(e)
	}
	h = sha256.Sum256(worker)
	receipt["task_sha256"] = hex.EncodeToString(h[:])
	rb, _ := workerbridge.Canonical(receipt)
	seat := &fakeSeat{receipt: rb, ready: true, fail: map[string]int{}}
	repo := &fakeRepo{}
	pub := &fakePublisher{}
	cfg := Config{Dry: true, CampaignDir: filepath.Join(root, "results"), BridgeDir: bridge, RepoDir: root, Timeout: 17 * time.Minute, GateTimeoutMS: 10000, Seat: seat, Repository: repo, Publisher: pub, PollInterval: time.Millisecond}
	return cfg, item, seat, repo, pub
}
func TestDryModeNeverPublishes(t *testing.T) {
	cfg, item, seat, _, pub := fixture(t)
	result, err := Run(context.Background(), cfg, item)
	if err != nil || result.ExitCode != 4 || result.Reason != "unavailable" || result.BridgeResult != nil {
		t.Fatalf("dry: %+v %v", result, err)
	}
	if pub.calls != 0 || strings.Join(seat.calls, ",") != "submit,wait,receipt,ack" {
		t.Fatalf("side effects: %v publish=%d", seat.calls, pub.calls)
	}
	_ = filepath.Walk(cfg.CampaignDir, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			t.Fatal(err)
		}
		if info.Name() == "outbox" || info.Name() == "result.json" {
			t.Fatalf("dry outbox: %s", path)
		}
		return nil
	})
}
func TestMusePublishRequiresDispatchedTaskAndLiveResult(t *testing.T) {
	t.Run("unregistered", func(t *testing.T) {
		cfg, item, s, r, p := fixture(t)
		item.Attempt = "a2"
		result, e := Run(context.Background(), cfg, item)
		if ExitCode(e) != 2 || result.ExitCode != 2 || len(s.calls) != 0 || r.calls != 0 || p.calls != 0 {
			t.Fatalf("%+v %v", result, e)
		}
	})
	t.Run("non-os-live", func(t *testing.T) {
		cfg, item, s, _, p := fixture(t)
		cfg.Dry = false
		_, e := Run(context.Background(), cfg, item)
		if ExitCode(e) != 4 || len(s.calls) != 0 || p.calls != 0 {
			t.Fatalf("%v %v", e, s.calls)
		}
	})
	t.Run("missing-lease", func(t *testing.T) {
		cfg, item, s, _, _ := fixture(t)
		_ = os.WriteFile(filepath.Join(cfg.BridgeDir, "ledger.jsonl"), nil, 0644)
		_, e := Run(context.Background(), cfg, item)
		if ExitCode(e) != 2 || len(s.calls) != 0 {
			t.Fatalf("%v", e)
		}
	})
}
func TestRetriesOnlyWaitAndReceipt(t *testing.T) {
	cfg, item, s, _, _ := fixture(t)
	s.fail["wait"] = 2
	s.fail["receipt"] = 2
	r, e := Run(context.Background(), cfg, item)
	if e != nil || r.ExitCode != 4 {
		t.Fatalf("%+v %v", r, e)
	}
	if strings.Join(s.calls, ",") != "submit,wait,wait,wait,receipt,receipt,receipt,ack" {
		t.Fatal(s.calls)
	}
}
func TestNeverRetrySubmitOrRunAttemptTwice(t *testing.T) {
	cfg, item, s, r, _ := fixture(t)
	s.fail["submit"] = 1
	result, e := Run(context.Background(), cfg, item)
	if ExitCode(e) != 6 || result.Reason != "seat_lost" || strings.Join(s.calls, ",") != "submit" {
		t.Fatalf("%+v %v %v", result, e, s.calls)
	}
	_, e = Run(context.Background(), cfg, item)
	if ExitCode(e) != 2 || r.calls != 1 || len(s.calls) != 1 {
		t.Fatalf("repeat: %v %v", e, s.calls)
	}
}
func TestReceiptBindingRejectedBeforeAck(t *testing.T) {
	cfg, item, s, _, p := fixture(t)
	receipt, _ := workerbridge.Parse(s.receipt)
	receipt["lease_id"] = "lease-other"
	s.receipt, _ = workerbridge.Canonical(receipt)
	_, e := Run(context.Background(), cfg, item)
	if ExitCode(e) != 5 || strings.Contains(strings.Join(s.calls, ","), "ack") || p.calls != 0 {
		t.Fatalf("%v %v", e, s.calls)
	}
}
func TestSeatLostReconcilesWithoutSubmit(t *testing.T) {
	cfg, item, s, _, _ := fixture(t)
	s.fail["wait"] = 3
	_, e := Run(context.Background(), cfg, item)
	if ExitCode(e) != 6 {
		t.Fatal(e)
	}
	s.calls = nil
	result, e := Reconcile(context.Background(), cfg, item)
	if e != nil || result.ExitCode != 4 || strings.Join(s.calls, ",") != "receipt,ack" {
		t.Fatalf("%+v %v %v", result, e, s.calls)
	}
}
func TestCollectionRejectsEscapesAndLeavesNoStaging(t *testing.T) {
	cfg, item, s, _, _ := fixture(t)
	// A seat transport remains bounded and cannot use an artifact name as a host path.
	cfg.Seat = seatFunc(func(ctx context.Context, req Request, b io.Reader, p []byte) (Response, error) {
		v, e := s.Exchange(ctx, req, b, p)
		if req.Verb == "receipt" {
			v.Artifacts = map[string]string{"../escape.txt": "bad"}
		}
		return v, e
	})
	_, e := Run(context.Background(), cfg, item)
	if ExitCode(e) != 5 {
		t.Fatal(e)
	}
	_ = filepath.Walk(cfg.CampaignDir, func(path string, info os.FileInfo, err error) error {
		if err == nil && strings.HasPrefix(info.Name(), ".collect-") {
			t.Fatal(path)
		}
		return nil
	})
}

type seatFunc func(context.Context, Request, io.Reader, []byte) (Response, error)

func (f seatFunc) Exchange(c context.Context, r Request, b io.Reader, p []byte) (Response, error) {
	return f(c, r, b, p)
}
func TestFrameBounds(t *testing.T) {
	var b bytes.Buffer
	if e := WriteFrame(&b, Request{Verb: "health"}); e != nil {
		t.Fatal(e)
	}
	var r Request
	if e := ReadFrame(&b, &r); e != nil || r.Verb != "health" {
		t.Fatalf("%+v %v", r, e)
	}
	if e := ReadFrame(bytes.NewReader([]byte{0, 32, 0, 0}), &r); ExitCode(e) != 2 {
		t.Fatalf("oversize frame: %v", e)
	}
}
func TestNoRc1(t *testing.T) {
	for _, e := range []error{nil, io.EOF, &workerbridge.Reject{Code: 1}, &workerbridge.Reject{Code: 7}} {
		if ExitCode(e) == 1 {
			t.Fatal(e)
		}
	}
}

func TestLostSubmitReplyCanFetchByDeterministicHandle(t *testing.T) {
	cfg, item, s, _, _ := fixture(t)
	s.fail["submit"] = 1
	r, e := Run(context.Background(), cfg, item)
	if ExitCode(e) != 6 || len(r.Handle) != 64 {
		t.Fatalf("%+v %v", r, e)
	}
	s.calls = nil
	r, e = Reconcile(context.Background(), cfg, item)
	if e != nil || r.Source != "seat_receipt" || strings.Join(s.calls, ",") != "receipt,ack" {
		t.Fatalf("%+v %v %v", r, e, s.calls)
	}
}

func TestMissingResponseExitCodeDoesNotPass(t *testing.T) {
	var b bytes.Buffer
	if e := WriteFrame(&b, map[string]any{"ready": true}); e != nil {
		t.Fatal(e)
	}
	var response Response
	if e := ReadFrame(&b, &response); ExitCode(e) != 5 {
		t.Fatalf("missing evidence accepted: %v", e)
	}
}
func TestExpiredWaitCancelsExactlyOnce(t *testing.T) {
	cfg, item, s, _, _ := fixture(t)
	cfg.Timeout = 5 * time.Millisecond
	cfg.PollInterval = time.Millisecond
	s.ready = false
	r, e := Run(context.Background(), cfg, item)
	if ExitCode(e) != 5 || r.ExitCode != 5 {
		t.Fatalf("%+v %v", r, e)
	}
	calls := strings.Join(s.calls, ",")
	if strings.Count(calls, "cancel") != 1 || strings.Count(calls, "submit") != 1 || strings.Contains(calls, "ack") {
		t.Fatal(calls)
	}
}

func scratch(t *testing.T) string {
	t.Helper()
	root := "../../build/room_FB3/tmp"
	if e := os.MkdirAll(root, 0700); e != nil {
		t.Fatal(e)
	}
	dir, e := os.MkdirTemp(root, "test-")
	if e != nil {
		t.Fatal(e)
	}
	dir, e = filepath.Abs(dir)
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	return dir
}

func TestAuthoritativeRc6CollectedAndAcknowledged(t *testing.T) {
	cfg, item, s, _, _ := fixture(t)
	receipt, _ := workerbridge.Parse(s.receipt)
	receipt["exit_code"] = json.Number("6")
	receipt["failure_reason"] = "indeterminate"
	cleanup := receipt["cleanup"].(map[string]any)
	cleanup["status"] = "not_observed"
	cleanup["resources_released"] = false
	receipt["output"] = nil
	s.receipt, _ = workerbridge.Canonical(receipt)
	cfg.Seat = seatFunc(func(ctx context.Context, req Request, b io.Reader, p []byte) (Response, error) {
		r, e := s.Exchange(ctx, req, b, p)
		if req.Verb != "submit" {
			r.ExitCode = 6
			r.Ready = true
			r.Receipt = s.receipt
			r.Reason = "unavailable"
		}
		return r, e
	})
	r, e := Run(context.Background(), cfg, item)
	if e != nil || r.ExitCode != 6 || r.Source != "seat_receipt" || !strings.HasSuffix(strings.Join(s.calls, ","), "receipt,ack") {
		t.Fatalf("%+v %v %v", r, e, s.calls)
	}
}

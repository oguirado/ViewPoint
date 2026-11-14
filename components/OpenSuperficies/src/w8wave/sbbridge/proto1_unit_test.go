package sbbridge

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"sync"
	"testing"

	"opensuperficies.local/srf/src/w8wave/switchboard"
)

// All fixture records and expected SHA-256 values are derived from independent
// standard-library encoders/hashers, never from coordinator internals.
type protoWire struct {
	Schema      string   `json:"schema"`
	Act         string   `json:"act"`
	WaveID      string   `json:"wave_id"`
	TaskID      string   `json:"task_id"`
	AttemptID   string   `json:"attempt_id"`
	LeaseID     string   `json:"lease_id"`
	Seq         int      `json:"seq"`
	ReproSHA256 *string  `json:"repro_sha256"`
	Affected    []string `json:"affected"`
	Status      *string  `json:"status"`
	Body        string   `json:"body"`
}

func protoRecord(g switchboard.Grant, act string, seq int, body, state string) []byte {
	w := protoWire{Schema: "os.cu.speech-act/v1", Act: act, WaveID: g.WaveID,
		TaskID: g.TaskID, AttemptID: g.AttemptID, LeaseID: g.LeaseID,
		Seq: seq, Affected: []string{}, Body: body}
	if act == "status" {
		w.Status = &state
	}
	if act == "repro" || act == "finding" {
		repro := strings.Repeat("f", 64)
		w.ReproSHA256 = &repro
	}
	if act == "finding" || act == "affected" {
		w.Affected = []string{"src/a.go"}
	}
	raw, err := json.Marshal(w)
	if err != nil {
		panic(err)
	}
	return raw
}

func protoSHA(b []byte) string { return fmt.Sprintf("%x", sha256.Sum256(b)) }

type protoClock struct{}

func (*protoClock) Pulse() uint64 { return 10 }
func (*protoClock) NowMS() int64  { return 100 }

type protoAuth map[string]switchboard.Principal

func (a protoAuth) Authenticate(endpoint string) (switchboard.Principal, error) {
	p, ok := a[endpoint]
	if !ok {
		return switchboard.Principal{}, errors.New("endpoint absent")
	}
	return p, nil
}

type protoEpochs struct {
	mu      sync.Mutex
	bus     uint64
	grants  map[string]switchboard.Grant
	rooms   map[string]uint64
	missing bool
}

func (e *protoEpochs) Current(target, topic string) (EpochSnapshot, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.missing {
		return EpochSnapshot{}, errors.New("epoch authority unavailable")
	}
	g, ok := e.grants[target]
	if !ok {
		return EpochSnapshot{}, errors.New("no authenticated grant")
	}
	return EpochSnapshot{Grant: g, BusEpoch: e.bus, RoomEpoch: e.rooms[topic]}, nil
}

type protoPort struct {
	mu          sync.Mutex
	store       switchboard.BodyStore
	messages    []switchboard.Envelope
	writes      int
	frameFiles  int // sink spy: the only test port has no filesystem capability
	mode        string
	failPublish bool
}

func (p *protoPort) publish(e switchboard.Envelope) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.failPublish {
		return errors.New("native publish indeterminate")
	}
	p.writes++
	p.messages = append(p.messages, e)
	return nil
}
func (p *protoPort) PublishMailbox(_ string, env switchboard.Envelope) error {
	return p.publish(env)
}
func (p *protoPort) PublishTopic(_ string, env switchboard.Envelope) error {
	return p.publish(env)
}
func (p *protoPort) FetchCompleteBody(ref string) ([]byte, error) {
	p.mu.Lock()
	mode := p.mode
	p.mu.Unlock()
	if mode == "offline" {
		return nil, errors.New("native fetch absent")
	}
	body, err := p.store.Resolve(ref)
	if err != nil {
		return nil, err
	}
	switch mode {
	case "truncate":
		return bytes.Clone(body[:len(body)-1]), nil
	case "wrong":
		return []byte(`{"different":"complete body"}`), nil
	}
	return body, nil
}
func (p *protoPort) ReadAfterCursor(cursor uint64, limit int) ([]switchboard.Envelope, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if limit <= 0 {
		return nil, errors.New("unbounded read")
	}
	if cursor >= uint64(len(p.messages)) {
		return nil, nil
	}
	end := int(cursor) + limit
	if end > len(p.messages) {
		end = len(p.messages)
	}
	return append([]switchboard.Envelope(nil), p.messages[int(cursor):end]...), nil
}
func (p *protoPort) published() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.writes
}
func (p *protoPort) appendReplay() {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.messages = append(p.messages, p.messages[0])
}

// A real digest allowlist: the fixture's host approves only exact, independently
// hashed bytes for one grant. It does not stand in for an external scorer.
type protoPinnedVerifier struct {
	permitted map[string]switchboard.Grant
}

func (v protoPinnedVerifier) Verify(raw []byte, grant switchboard.Grant) (switchboard.Verification, error) {
	sha := protoSHA(raw)
	if allowed, ok := v.permitted[sha]; !ok || allowed != grant {
		return switchboard.Verification{}, errors.New("no pinned evidence receipt")
	}
	return switchboard.Verification{Authenticated: true, ScoreMilli: 900,
		AttestationRef: "host-pinned-digest-receipt", BodySHA256: sha,
		ReproSHA256: strings.Repeat("f", 64), TargetID: grant.TargetID,
		Epoch: grant.Epoch, SourceCommit: grant.SourceCommit}, nil
}

type protoCompletions struct {
	mu   sync.Mutex
	list []Completion
	fail bool
}

func (s *protoCompletions) CommitCompletions(events []Completion) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.fail {
		return errors.New("indeterminate completion receipt")
	}
	s.list = append(s.list, events...)
	return nil
}
func (s *protoCompletions) count() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.list)
}

type protoFixture struct {
	coordinator *Coordinator
	adapter     *switchboard.Adapter
	port        *protoPort
	epochs      *protoEpochs
	grants      *switchboard.MemGrantTable
	complete    *protoCompletions
	grantA      switchboard.Grant
	grantB      switchboard.Grant
}

func newProtoFixture(t *testing.T) *protoFixture {
	t.Helper()
	g := func(target, worker string) switchboard.Grant {
		return switchboard.Grant{WaveID: "wave-1", TaskID: "task-" + target,
			AttemptID: "a1", LeaseID: "lease-" + target, TargetID: target,
			WorkerID: worker, Epoch: 4, SourceCommit: strings.Repeat("a", 40), ExpiresMS: 10000}
	}
	a, b := g("target-a", "worker-a"), g("target-b", "worker-b")
	grants := switchboard.NewMemGrantTable()
	for _, x := range []switchboard.Grant{a, b} {
		if err := grants.Add(switchboard.GrantRecord{Grant: x, Paths: []string{"src/a.go"}}); err != nil {
			t.Fatal(err)
		}
	}
	store := switchboard.NewMemBodyStore()
	port := &protoPort{store: store}
	clock := &protoClock{}
	adapter := &switchboard.Adapter{
		Policy: switchboard.IngressPolicy{MaxRecordBytes: 8192, MaxQueueBytes: 100000,
			PerSenderMsgsPerPulse: 16, PerSenderBytesPerPulse: 64000,
			RoomMsgsPerPulse: 16, RoomBytesPerPulse: 64000,
			MaxMembers: 5, HeartbeatExpiry: 2, StagnationWindow: 3},
		Clock: clock, Auth: protoAuth{
			"ep-a": {HostID: "trusted-host", Extension: 42, TargetID: a.TargetID, WorkerID: a.WorkerID},
			"ep-b": {HostID: "trusted-host", Extension: 42, TargetID: b.TargetID, WorkerID: b.WorkerID}},
		Grants: grants, Store: store, Transport: port,
		Repo: "oguirado/OpenSuperficies", BusEpoch: 7,
	}
	epochs := &protoEpochs{bus: 7,
		grants: map[string]switchboard.Grant{a.TargetID: a, b.TargetID: b}, rooms: map[string]uint64{}}
	complete := &protoCompletions{}
	c := &Coordinator{Adapter: adapter, Epochs: epochs,
		SourceCommit: a.SourceCommit, OnComplete: complete}
	return &protoFixture{coordinator: c, adapter: adapter, port: port,
		epochs: epochs, grants: grants, complete: complete, grantA: a, grantB: b}
}

func (f *protoFixture) submit(g switchboard.Grant, act string, seq int, body, state string) Submission {
	endpoint := "ep-a"
	if g.TargetID == f.grantB.TargetID {
		endpoint = "ep-b"
	}
	raw := protoRecord(g, act, seq, body, state)
	return Submission{Endpoint: endpoint, Extension: 42, LeaseEpoch: g.Epoch,
		BusEpoch: 7, Raw: raw, PayloadSHA256: protoSHA(raw)}
}

func (f *protoFixture) permitRepro(g switchboard.Grant, raw []byte) {
	f.adapter.Verifier = protoPinnedVerifier{permitted: map[string]switchboard.Grant{protoSHA(raw): g}}
}

func protoError(t *testing.T, err error, code int, reason string) {
	t.Helper()
	if err == nil || RefusalCode(err) != code || err.Error() != fmt.Sprintf("switchboard rc=%d: %s", code, reason) {
		t.Fatalf("expected exact refusal rc=%d %q, got %v", code, reason, err)
	}
}

func protoNoCommit(t *testing.T, f *protoFixture) {
	t.Helper()
	if f.complete.count() != 0 || f.coordinator.Cursor() != 0 || f.port.frameFiles != 0 {
		t.Fatalf("invalid admission escaped: callbacks=%d cursor=%d persisted_frames=%d",
			f.complete.count(), f.coordinator.Cursor(), f.port.frameFiles)
	}
}

func TestProto1Switchboard(t *testing.T) {
	// Positive controls execute before intentionally mutated/adversarial fixtures.
	t.Run("positive_authenticated_status_repro_and_replays", func(t *testing.T) {
		f := newProtoFixture(t)
		status := f.submit(f.grantA, "status", 0, "complete authenticated status", "done")
		repro := f.submit(f.grantA, "repro", 1, "host verified repro record", "")
		f.permitRepro(f.grantA, repro.Raw)
		// Two messages exhaust the sender quota; a replay must not consume a third.
		f.adapter.Policy.PerSenderMsgsPerPulse = 2
		for i, req := range []Submission{status, repro} {
			ack, err := f.coordinator.Publish(req)
			if err != nil || ack.Held || ack.Replay || ack.LastContiguous != i {
				t.Fatalf("seq %d: %+v %v", i, ack, err)
			}
		}
		if f.port.published() != 2 {
			t.Fatalf("expected exactly 2 published full envelopes; got %d", f.port.published())
		}
		out, err := f.coordinator.Consume(5)
		if err != nil || len(out) != 2 || f.coordinator.Cursor() != 2 {
			t.Fatalf("verified native cursor failed: %d, %v", len(out), err)
		}
		for i, msg := range out {
			want := status.Raw
			if i == 1 {
				want = repro.Raw
			}
			if !bytes.Equal(msg.Payload, want) || msg.Sidecar.PayloadSHA256 != protoSHA(want) ||
				msg.Sidecar.SourceBodyBytes != len(want) || msg.Sidecar.BusEpoch != 7 ||
				msg.Sidecar.RoomEpoch != 0 {
				t.Fatalf("unbound complete source at index %d", i)
			}
		}
		cov := f.coordinator.SequenceCoverage(f.grantA)
		if cov.LastContiguous != 1 || cov.Held != 0 || !cov.Completed ||
			!cov.StatusDone || !cov.VerifiedRepro || f.complete.count() != 1 {
			t.Fatalf("completion coverage incorrect: %+v receipts=%d", cov, f.complete.count())
		}
		ack, err := f.coordinator.Publish(status)
		if err != nil || !ack.Replay || ack.LastContiguous != 1 || f.port.published() != 2 {
			t.Fatalf("replay republished or regressed: %+v %v", ack, err)
		}
		f.port.appendReplay()
		if _, err := f.coordinator.Consume(5); err != nil {
			t.Fatalf("native identical replay refused: %v", err)
		}
		cov2 := f.coordinator.SequenceCoverage(f.grantA)
		if cov2 != cov || f.complete.count() != 1 || f.coordinator.Cursor() != 3 {
			t.Fatalf("replay changed counters or callback: before=%+v after=%+v", cov, cov2)
		}
		if f.port.frameFiles != 0 {
			t.Fatal("frame persisted via native test sink")
		}
		t.Logf("positive VM-TARGET: 2 full envelopes; 1 completion; 2 replay types; no disk writes")
	})

	// Each must-FAIL explicitly proves the absence of acceptance, cursor
	// advancement, new publication and pixel persistence as applicable.
	t.Run("must_fail_truncated_complete_body", func(t *testing.T) {
		f := newProtoFixture(t)
		f.port.mode = "truncate"
		_, err := f.coordinator.Publish(f.submit(f.grantA, "status", 0, "ordinary status", "done"))
		protoError(t, err, 6, "body reference digest mismatch")
		if f.port.published() != 0 {
			t.Fatal("truncated body published")
		}
		protoNoCommit(t, f)
	})
	t.Run("must_fail_wrong_complete_SHA", func(t *testing.T) {
		f := newProtoFixture(t)
		f.port.mode = "wrong"
		_, err := f.coordinator.Publish(f.submit(f.grantA, "status", 0, "ordinary status", "done"))
		protoError(t, err, 6, "body reference digest mismatch")
		if f.port.published() != 0 {
			t.Fatal("wrong complete body published")
		}
		protoNoCommit(t, f)
	})
	t.Run("must_fail_caller_digest", func(t *testing.T) {
		f := newProtoFixture(t)
		req := f.submit(f.grantA, "status", 0, "ordinary status", "done")
		req.PayloadSHA256 = strings.Repeat("0", 64)
		_, err := f.coordinator.Publish(req)
		protoError(t, err, 5, "caller digest differs from complete source")
		if f.port.published() != 0 {
			t.Fatal("wrong claimed SHA published")
		}
		protoNoCommit(t, f)
	})
	t.Run("must_fail_forged_grant", func(t *testing.T) {
		f := newProtoFixture(t)
		forged := f.grantA
		forged.TaskID = "other-task"
		if err := f.grants.Add(switchboard.GrantRecord{Grant: forged, Paths: []string{"src/a.go"}}); err != nil {
			t.Fatal(err)
		}
		_, err := f.coordinator.Publish(f.submit(f.grantA, "status", 0, "ordinary status", "done"))
		protoError(t, err, 5, "unbound task_id")
		if f.port.published() != 0 {
			t.Fatal("forged grant published")
		}
		protoNoCommit(t, f)
	})
	t.Run("must_fail_stale_bus_epoch", func(t *testing.T) {
		f := newProtoFixture(t)
		req := f.submit(f.grantA, "status", 0, "ordinary status", "done")
		req.BusEpoch = 6
		_, err := f.coordinator.Publish(req)
		protoError(t, err, 5, "stale bus epoch or grant capability")
		if f.port.published() != 0 {
			t.Fatal("stale bus published")
		}
		protoNoCommit(t, f)
	})
	t.Run("must_fail_stale_room_epoch", func(t *testing.T) {
		f := newProtoFixture(t)
		found, err := switchboard.ParseSpeechAct(protoRecord(f.grantA, "finding", 0, "finding in scope", ""), f.grantA)
		if err != nil {
			t.Fatal(err)
		}
		r := &switchboard.Room{Repo: f.adapter.Repo, Commit: f.grantA.SourceCommit,
			InitiatorTarget: f.grantA.TargetID, MaxMembers: 5, Clock: f.adapter.Clock}
		if err := r.Open(found, f.grants); err != nil {
			t.Fatal(err)
		}
		if err := f.coordinator.AttachRoom(r); err != nil {
			t.Fatal(err)
		}
		f.epochs.mu.Lock()
		f.epochs.rooms[r.Topic] = r.Epoch
		f.epochs.mu.Unlock()
		req := f.submit(f.grantA, "status", 0, "ordinary status", "done")
		req.Topic, req.RoomEpoch = r.Topic, r.Epoch+1
		_, err = f.coordinator.Publish(req)
		protoError(t, err, 5, "stale or unauthenticated room epoch")
		if f.port.published() != 0 {
			t.Fatal("stale room published")
		}
		protoNoCommit(t, f)
	})
	t.Run("must_fail_gap_and_later_sequence", func(t *testing.T) {
		f := newProtoFixture(t)
		for _, seq := range []int{2, 3} {
			ack, err := f.coordinator.Publish(f.submit(f.grantA, "status", seq, "ordinary status", "working"))
			if err != nil || !ack.Held || ack.LastContiguous != -1 {
				t.Fatalf("gap seq=%d incorrectly accepted: %+v %v", seq, ack, err)
			}
		}
		if f.port.published() != 0 {
			t.Fatal("published noncontiguous messages")
		}
		protoNoCommit(t, f)
	})
	t.Run("must_fail_changed_byte_duplicate", func(t *testing.T) {
		f := newProtoFixture(t)
		if _, err := f.coordinator.Publish(f.submit(f.grantA, "status", 0, "original", "working")); err != nil {
			t.Fatal(err)
		}
		ack, err := f.coordinator.Publish(f.submit(f.grantA, "status", 0, "altered", "working"))
		protoError(t, err, 6, "changed-byte sequence equivocation")
		if !ack.Quarantined || ack.LastContiguous != 0 {
			t.Fatal("equivocation not quarantined", ack)
		}
		_, err = f.coordinator.Publish(f.submit(f.grantA, "status", 1, "next", "working"))
		protoError(t, err, 6, "equivocation quarantine is sticky")
		if f.port.published() != 1 {
			t.Fatal("equivocation published additional bytes")
		}
		protoNoCommit(t, f)
	})
	t.Run("must_fail_sender_pulse_budget", func(t *testing.T) {
		f := newProtoFixture(t)
		f.adapter.Policy.PerSenderMsgsPerPulse = 1
		if _, err := f.coordinator.Publish(f.submit(f.grantA, "status", 0, "first", "working")); err != nil {
			t.Fatal(err)
		}
		_, err := f.coordinator.Publish(f.submit(f.grantA, "status", 1, "second", "done"))
		protoError(t, err, 5, "global sender flood quota")
		if f.port.published() != 1 {
			t.Fatal("over-budget message published")
		}
		protoNoCommit(t, f)
	})
	t.Run("must_fail_pixel_leakage", func(t *testing.T) {
		f := newProtoFixture(t)
		_, err := f.coordinator.Publish(f.submit(f.grantA, "status", 0,
			"data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAE", "done"))
		protoError(t, err, 5, "frame, STATE or pixel encoding forbidden")
		if f.port.published() != 0 {
			t.Fatal("pixel data published")
		}
		protoNoCommit(t, f)
	})
	t.Run("must_fail_stale_consumer_bus_epoch", func(t *testing.T) {
		f := newProtoFixture(t)
		if _, err := f.coordinator.Publish(f.submit(f.grantA, "status", 0, "ordinary status", "done")); err != nil {
			t.Fatal(err)
		}
		f.epochs.mu.Lock()
		f.epochs.bus = 8
		f.epochs.mu.Unlock()
		_, err := f.coordinator.Consume(5)
		protoError(t, err, 5, "stale bus epoch or grant capability")
		protoNoCommit(t, f)
	})
	t.Run("must_fail_uncertain_complete_sink_preserves_hold", func(t *testing.T) {
		f := newProtoFixture(t)
		x, y := f.submit(f.grantA, "status", 0, "done", "done"), f.submit(f.grantA, "repro", 1, "repro", "")
		f.permitRepro(f.grantA, y.Raw)
		for _, req := range []Submission{x, y} {
			if _, err := f.coordinator.Publish(req); err != nil {
				t.Fatal(err)
			}
		}
		f.complete.fail = true
		_, err := f.coordinator.Consume(5)
		protoError(t, err, 6, "completion commit indeterminate")
		protoNoCommit(t, f)
		if c := f.coordinator.SequenceCoverage(f.grantA); c.Held == 0 || c.LastContiguous != -1 {
			t.Fatal("uncertainty not held without acceptance", c)
		}
		f.complete.fail = false
		if _, err := f.coordinator.Consume(5); err != nil {
			t.Fatal("hold not retriable", err)
		}
		if f.complete.count() != 1 || f.coordinator.Cursor() != 2 {
			t.Fatal("recovery not committed")
		}
	})
	t.Run("must_fail_missing_epoch_capability", func(t *testing.T) {
		f := newProtoFixture(t)
		f.epochs.missing = true
		_, err := f.coordinator.Publish(f.submit(f.grantA, "status", 0, "ordinary status", "working"))
		protoError(t, err, 4, "authenticated live epoch evidence unavailable")
		if f.port.published() != 0 {
			t.Fatal("missing capability published")
		}
		protoNoCommit(t, f)
	})
	t.Run("must_fail_indeterminate_native_publish", func(t *testing.T) {
		f := newProtoFixture(t)
		f.port.failPublish = true
		req := f.submit(f.grantA, "status", 0, "ordinary status", "working")
		ack, err := f.coordinator.Publish(req)
		protoError(t, err, 6, "native transport delivery indeterminate")
		if !ack.Held || !ack.Quarantined || f.port.published() != 0 {
			t.Fatal("uncertain send acknowledged", ack)
		}
		f.port.failPublish = false
		_, err = f.coordinator.Publish(req)
		protoError(t, err, 6, "equivocation quarantine is sticky")
		if f.port.published() != 0 {
			t.Fatal("unknown native delivery retried")
		}
		protoNoCommit(t, f)
	})
	t.Run("must_fail_post_STOP_send", func(t *testing.T) {
		f := newProtoFixture(t)
		if _, err := f.coordinator.Publish(f.submit(f.grantA, "status", 0, "first", "working")); err != nil {
			t.Fatal(err)
		}
		if err := f.coordinator.Stop(f.grantA.TargetID, f.grantA.Epoch); err != nil {
			t.Fatal(err)
		}
		_, err := f.coordinator.Publish(f.submit(f.grantA, "status", 1, "later", "done"))
		protoError(t, err, 5, "target stopped at lease epoch")
		if f.port.published() != 1 {
			t.Fatal("published after STOP")
		}
		d, ok := f.adapter.DrainNext()
		if !ok || d.Control == nil || d.Control.Kind != switchboard.ControlSTOP {
			t.Fatal("STOP lost to data lane")
		}
		d, ok = f.adapter.DrainNext()
		if !ok || d.Data == nil {
			t.Fatal("ordinary data lost after STOP")
		}
		protoNoCommit(t, f)
	})
	t.Run("unverified_repro_is_not_A_completion", func(t *testing.T) {
		f := newProtoFixture(t)
		for _, req := range []Submission{
			f.submit(f.grantA, "status", 0, "done", "done"),
			f.submit(f.grantA, "repro", 1, "unattested repro", "")} {
			if _, err := f.coordinator.Publish(req); err != nil {
				t.Fatal(err)
			}
		}
		if _, err := f.coordinator.Consume(5); err != nil {
			t.Fatal(err)
		}
		if cov := f.coordinator.SequenceCoverage(f.grantA); cov.Completed || cov.VerifiedRepro ||
			cov.LastContiguous != 1 || f.complete.count() != 0 {
			t.Fatal("unverified repro accepted", cov)
		}
	})
	t.Run("all_policy_fields_require_positive_limits", func(t *testing.T) {
		fields := []struct {
			name  string
			clear func(*switchboard.IngressPolicy)
		}{
			{"max_record", func(p *switchboard.IngressPolicy) { p.MaxRecordBytes = 0 }},
			{"max_queue", func(p *switchboard.IngressPolicy) { p.MaxQueueBytes = 0 }},
			{"sender_msgs", func(p *switchboard.IngressPolicy) { p.PerSenderMsgsPerPulse = 0 }},
			{"sender_bytes", func(p *switchboard.IngressPolicy) { p.PerSenderBytesPerPulse = 0 }},
			{"room_msgs", func(p *switchboard.IngressPolicy) { p.RoomMsgsPerPulse = 0 }},
			{"room_bytes", func(p *switchboard.IngressPolicy) { p.RoomBytesPerPulse = 0 }},
			{"members", func(p *switchboard.IngressPolicy) { p.MaxMembers = 0 }},
			{"heartbeat", func(p *switchboard.IngressPolicy) { p.HeartbeatExpiry = 0 }},
			{"stagnation", func(p *switchboard.IngressPolicy) { p.StagnationWindow = 0 }},
		}
		for _, tc := range fields {
			t.Run(tc.name, func(t *testing.T) {
				f := newProtoFixture(t)
				tc.clear(&f.adapter.Policy)
				_, err := f.coordinator.Publish(f.submit(f.grantA, "status", 0, "ordinary", "working"))
				protoError(t, err, 4, "trusted coordinator capability or finite policy absent")
				if f.port.published() != 0 {
					t.Fatal("unbudgeted publication")
				}
				protoNoCommit(t, f)
			})
		}
	})
	t.Run("barrier_interleaved_senders_fair_drain", func(t *testing.T) {
		f := newProtoFixture(t)
		ready, start := make(chan struct{}, 2), make(chan struct{})
		errCh := make(chan error, 3)
		var wg sync.WaitGroup
		for _, target := range []switchboard.Grant{f.grantA, f.grantB} {
			g := target
			wg.Add(1)
			go func() {
				defer wg.Done()
				ready <- struct{}{}
				<-start // real barrier: simultaneous origins, no sleeps or timing assumptions
				count := 1
				if g.TargetID == f.grantA.TargetID {
					count = 2
				}
				for n := 0; n < count; n++ {
					_, err := f.coordinator.Publish(f.submit(g, "status", n, "fair", "working"))
					errCh <- err
				}
			}()
		}
		<-ready
		<-ready
		close(start)
		wg.Wait()
		close(errCh)
		for err := range errCh {
			if err != nil {
				t.Fatal(err)
			}
		}
		if f.port.published() != 3 {
			t.Fatal("barrier lost messages")
		}
		first, ok1 := f.adapter.DrainNext()
		second, ok2 := f.adapter.DrainNext()
		third, ok3 := f.adapter.DrainNext()
		if !ok1 || !ok2 || !ok3 || first.Data == nil || second.Data == nil || third.Data == nil ||
			first.Data.Binding.WorkerID != "worker-a" || second.Data.Binding.WorkerID != "worker-b" ||
			third.Data.Binding.WorkerID != "worker-a" {
			t.Fatalf("fair drain under contention failed: %+v %+v %+v", first, second, third)
		}
		if f.complete.count() != 0 || f.port.frameFiles != 0 {
			t.Fatal("unverified callbacks/persistence")
		}
		t.Logf("fairness VM-TARGET: 2 barrier-synchronized origins, 3 finite publications, A/B/A drain")
	})
}

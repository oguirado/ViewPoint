package streamreplay

import (
	"bytes"
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"reflect"
	"sync"
	"testing"
	"time"
)

type memoryStore struct {
	values     map[string][]byte
	deleted    []string
	failDelete bool
}

func (s *memoryStore) Get(_ context.Context, h string) ([]byte, error) {
	b, ok := s.values[h]
	if !ok {
		return nil, errors.New("not found")
	}
	return bytes.Clone(b), nil
}
func (s *memoryStore) Delete(_ context.Context, h string) error {
	if s.failDelete {
		return errors.New("delete fault")
	}
	delete(s.values, h)
	s.deleted = append(s.deleted, h)
	return nil
}

type memJournal struct {
	mu      sync.Mutex
	records map[Stream]map[uint64]Record
	failAt  string
}

func (j *memJournal) Load(context.Context) ([]Record, error) {
	j.mu.Lock()
	defer j.mu.Unlock()
	out := []Record{}
	for _, m := range j.records {
		for _, r := range m {
			out = append(out, copyRecord(r))
		}
	}
	return out, nil
}
func (j *memJournal) change(id Stream, n uint64, op string) error {
	j.mu.Lock()
	defer j.mu.Unlock()
	if j.failAt == op {
		return fmt.Errorf("fault %s", op)
	}
	r, ok := j.records[id][n]
	if !ok {
		return errors.New("missing intent")
	}
	switch op {
	case "apply":
		r.Applied = true
	case "downstream":
		r.Downstream = true
	case "deleted":
		r.Deleted = true
	}
	j.records[id][n] = r
	return nil
}
func (j *memJournal) AppendIntent(_ context.Context, r Record) error {
	j.mu.Lock()
	defer j.mu.Unlock()
	if j.failAt == "append" {
		return errors.New("append fault")
	}
	if j.records == nil {
		j.records = make(map[Stream]map[uint64]Record)
	}
	if j.records[r.Stream] == nil {
		j.records[r.Stream] = make(map[uint64]Record)
	}
	if x, ok := j.records[r.Stream][r.Seq]; ok {
		if x.BodySHA256 != r.BodySHA256 || !bytes.Equal(x.Body, r.Body) {
			return errors.New("changed intent")
		}
		return nil
	}
	j.records[r.Stream][r.Seq] = copyRecord(r)
	return nil
}
func (j *memJournal) MarkApplied(_ context.Context, id Stream, n uint64) error {
	return j.change(id, n, "apply")
}
func (j *memJournal) MarkDownstream(_ context.Context, id Stream, n uint64) error {
	return j.change(id, n, "downstream")
}
func (j *memJournal) MarkDeleted(_ context.Context, id Stream, n uint64) error {
	return j.change(id, n, "deleted")
}

type fakeVerifier struct {
	active     map[string]Grant
	downstream bool
}

func (v *fakeVerifier) Current(_ context.Context, side Sidecar, _ []byte) (Grant, error) {
	g, ok := v.active[side.TaskID]
	if !ok {
		return Grant{}, errors.New("no current policy")
	}
	return g, nil
}
func (v *fakeVerifier) DownstreamDurable(_ context.Context, _ Stream, _ uint64) error {
	if !v.downstream {
		return errors.New("downstream pending")
	}
	return nil
}

type fakeSink struct {
	applied []uint64
	sender  []string
	stopped int
	fail    bool
}

func (s *fakeSink) Apply(_ context.Context, r Record) error {
	if s.fail {
		return errors.New("sink unavailable")
	}
	s.applied = append(s.applied, r.Seq)
	s.sender = append(s.sender, r.Stream.Sender)
	return nil
}
func (s *fakeSink) Stop(context.Context) error { s.stopped++; return nil }

type fakeClock struct{ now time.Time }

func (f *fakeClock) Now() time.Time { return f.now }
func bounds() Limits {
	return Limits{MaxRecordBytes: 2048, MaxQueueRecords: 4, MaxSenderBytes: 4096, DataSlots: 1, ControlSlots: 1, MaxStopDelay: 2 * time.Second}
}

type harness struct {
	a     *Adapter
	store *memoryStore
	j     *memJournal
	v     *fakeVerifier
	sink  *fakeSink
	clock *fakeClock
}

func makeHarness(t *testing.T) *harness {
	t.Helper()
	h := &harness{store: &memoryStore{values: map[string][]byte{}}, j: &memJournal{records: map[Stream]map[uint64]Record{}}, v: &fakeVerifier{active: map[string]Grant{}}, sink: &fakeSink{}, clock: &fakeClock{now: time.Unix(100, 0)}}
	a, err := New(context.Background(), h.store, h.j, h.v, h.sink, h.clock, bounds())
	if err != nil {
		t.Fatal(err)
	}
	h.a = a
	return h
}
func (h *harness) make(seq uint64, task, sender string) Sidecar {
	b := []byte(fmt.Sprintf(`{"schema":"os.cu.speech-act/v1","act":"finding","wave_id":"w","task_id":%q,"attempt_id":"a","lease_id":"l","seq":%d,"body":"passive text"}`, task, seq))
	sha := digest(b)
	h.store.values[sha] = b
	h.v.active[task] = Grant{Sender: sender, TaskID: task, AttemptID: "a", LeaseID: "l", Epoch: 2, SourceBodySHA256: sha}
	return Sidecar{Sender: sender, TaskID: task, AttemptID: "a", LeaseID: "l", Epoch: 2, Seq: seq, BodySHA256: sha, BodyLength: len(b)}
}
func expectCode(t *testing.T, err error, code int) {
	t.Helper()
	if err == nil || ExitCode(err) != code {
		t.Fatalf("wanted code %d, got %v (%d)", code, err, ExitCode(err))
	}
}

func TestMustFailGapAckAndPredecessorReleasesSuccessor(t *testing.T) {
	h := makeHarness(t)
	ctx := context.Background()
	second := h.make(2, "task", "s")
	ack, err := h.a.Receive(ctx, second)
	if err != nil || ack.Valid || len(h.sink.applied) != 0 {
		t.Fatalf("gap acknowledged: %+v %v", ack, err)
	}
	first := h.make(1, "task", "s")
	ack, err = h.a.Receive(ctx, first)
	if err != nil || !ack.Valid || ack.Through != 2 || !reflect.DeepEqual(h.sink.applied, []uint64{1, 2}) {
		t.Fatalf("predecessor did not drain held successor: %+v %v %v", ack, err, h.sink.applied)
	}
	// A full same-sequence replay has no additional side effect.
	h.v.active["task"] = Grant{Sender: "s", TaskID: "task", AttemptID: "a", LeaseID: "l", Epoch: 2, SourceBodySHA256: second.BodySHA256}
	ack, err = h.a.Receive(ctx, second)
	if err != nil || !ack.Valid || len(h.sink.applied) != 2 {
		t.Fatalf("duplicate applied twice: %+v %v", ack, err)
	}
}
func TestMustFailChangedReplay(t *testing.T) {
	h := makeHarness(t)
	ctx := context.Background()
	first := h.make(1, "task", "s")
	if _, err := h.a.Receive(ctx, first); err != nil {
		t.Fatal(err)
	}
	changed := h.make(1, "task", "s")
	changedBytes := []byte(fmt.Sprintf(`{"schema":"os.cu.speech-act/v1","act":"finding","wave_id":"w","task_id":"task","attempt_id":"a","lease_id":"l","seq":1,"body":"DIFFERENT"}`))
	changed.BodySHA256 = digest(changedBytes)
	changed.BodyLength = len(changedBytes)
	h.store.values[changed.BodySHA256] = changedBytes
	h.v.active["task"] = Grant{Sender: "s", TaskID: "task", AttemptID: "a", LeaseID: "l", Epoch: 2, SourceBodySHA256: changed.BodySHA256}
	_, err := h.a.Receive(ctx, changed)
	expectCode(t, err, 6)
	if len(h.sink.applied) != 1 {
		t.Fatal("changed replay merged")
	}
}
func TestMustFailForgedSenderAndStaleEpoch(t *testing.T) {
	h := makeHarness(t)
	msg := h.make(1, "task", "s")
	msg.Sender = "impostor"
	_, err := h.a.Receive(context.Background(), msg)
	expectCode(t, err, 5)
	msg.Sender = "s"
	msg.Epoch = 1
	_, err = h.a.Receive(context.Background(), msg)
	expectCode(t, err, 5)
	if len(h.sink.applied) != 0 {
		t.Fatal("forged/stale source applied")
	}
}
func TestMustFailTruncatedBodyAndUnavailable(t *testing.T) {
	h := makeHarness(t)
	msg := h.make(1, "task", "s")
	h.store.values[msg.BodySHA256] = h.store.values[msg.BodySHA256][:6]
	_, err := h.a.Receive(context.Background(), msg)
	expectCode(t, err, 6)
	delete(h.store.values, msg.BodySHA256)
	_, err = h.a.Receive(context.Background(), msg)
	expectCode(t, err, 4)
	_, err = New(context.Background(), h.store, h.j, nil, h.sink, h.clock, bounds())
	expectCode(t, err, 4)
}
func TestMustFailEarlyBodyDeletionAndDurableStorage(t *testing.T) {
	h := makeHarness(t)
	msg := h.make(1, "task", "s")
	h.j.failAt = "append"
	ack, err := h.a.Receive(context.Background(), msg)
	expectCode(t, err, 5)
	if ack.Valid || len(h.sink.applied) > 0 {
		t.Fatal("append failure acknowledged")
	}
	h.j.failAt = ""
	ack, err = h.a.Receive(context.Background(), msg)
	if err != nil || !ack.Valid {
		t.Fatalf("retry failed %+v %v", ack, err)
	}
	id := h.v.active["task"].stream()
	if len(h.store.deleted) > 0 {
		t.Fatal("early deletion")
	}
	err = h.a.Release(context.Background(), id, 1)
	expectCode(t, err, 5)
	if len(h.store.deleted) > 0 {
		t.Fatal("released without downstream ack")
	}
	h.v.downstream = true
	h.store.failDelete = true
	err = h.a.Release(context.Background(), id, 1)
	expectCode(t, err, 5)
	if len(h.store.deleted) > 0 {
		t.Fatal("failed deletion claimed success")
	}
	h.store.failDelete = false
	if err = h.a.Release(context.Background(), id, 1); err != nil {
		t.Fatal(err)
	}
	if len(h.store.deleted) != 1 {
		t.Fatal("verified downstream ack did not release body")
	}
}
func TestMustFailStorageMarkAndRecovery(t *testing.T) {
	h := makeHarness(t)
	msg := h.make(1, "task", "s")
	h.j.failAt = "apply"
	ack, err := h.a.Receive(context.Background(), msg)
	expectCode(t, err, 5)
	if ack.Valid {
		t.Fatal("mark failure acknowledged")
	}
	h.j.failAt = ""
	a, err := New(context.Background(), h.store, h.j, h.v, h.sink, h.clock, bounds())
	if err != nil {
		t.Fatal(err)
	}
	ack, err = a.Receive(context.Background(), msg)
	if err != nil || !ack.Valid || ack.Through != 1 {
		t.Fatalf("unresolved durable intent not retried: %+v %v", ack, err)
	}
}
func TestMustFailFloodStarvingStopAndOtherSenderProgress(t *testing.T) {
	h := makeHarness(t)
	q, err := NewQueue(bounds())
	if err != nil {
		t.Fatal(err)
	}
	flood := h.make(2, "flood", "noisy")
	other := h.make(1, "other", "quiet")
	if !q.Data(flood) || q.Data(flood) {
		t.Fatal("data lane not bounded")
	}
	if !q.Stop(h.clock.Now()) {
		t.Fatal("STOP could not enter independent lane")
	}
	if _, err := h.a.Step(context.Background(), q); err != nil || h.sink.stopped != 1 {
		t.Fatalf("STOP starved: %v", err)
	}
	if _, err := h.a.Step(context.Background(), q); err != nil {
		t.Fatal(err)
	}
	ack, err := h.a.Receive(context.Background(), other)
	if err != nil || !ack.Valid || ack.Through != 1 {
		t.Fatalf("other sender stalled: %+v %v", ack, err)
	}
	if len(h.sink.sender) != 1 || h.sink.sender[0] != "quiet" {
		t.Fatal("flood contaminated independent sender")
	}
	delayed := h.make(1, "late", "late")
	if !q.Stop(h.clock.Now()) {
		t.Fatal("STOP channel unexpectedly blocked")
	}
	h.clock.now = h.clock.now.Add(3 * time.Second)
	_, err = h.a.Step(context.Background(), q)
	expectCode(t, err, 5)
	if h.sink.stopped != 2 {
		t.Fatal("late STOP not delivered before latency refusal")
	}
	_ = delayed
}
func TestBoundsAndPassiveText(t *testing.T) {
	h := makeHarness(t)
	msg := h.make(1, "task", "s")
	msg.BodyLength = 99999
	_, err := h.a.Receive(context.Background(), msg)
	expectCode(t, err, 2)
	l := bounds()
	l.ControlSlots = 0
	_, err = NewQueue(l)
	expectCode(t, err, 2)
	h = makeHarness(t)
	msg = h.make(1, "task", "s")
	raw := []byte(`{"schema":"os.cu.speech-act/v1","act":"DELETE","wave_id":"w","task_id":"task","attempt_id":"a","lease_id":"l","seq":1}`)
	msg.BodySHA256 = digest(raw)
	msg.BodyLength = len(raw)
	h.store.values[msg.BodySHA256] = raw
	h.v.active["task"] = Grant{Sender: "s", TaskID: "task", AttemptID: "a", LeaseID: "l", Epoch: 2, SourceBodySHA256: msg.BodySHA256}
	_, err = h.a.Receive(context.Background(), msg)
	expectCode(t, err, 2)
}

type evidenceMem struct {
	entries []EvidenceEntry
	fail    bool
}

func (m *evidenceMem) LoadEvidence(context.Context) ([]EvidenceEntry, error) { return m.entries, nil }
func (m *evidenceMem) AppendWitness(_ context.Context, d string, c []byte, w Witness) error {
	if m.fail {
		return errors.New("no storage")
	}
	for i := range m.entries {
		if m.entries[i].Digest == d && bytes.Equal(m.entries[i].Canonical, c) {
			m.entries[i].Witnesses = append(m.entries[i].Witnesses, w)
			return nil
		}
	}
	m.entries = append(m.entries, EvidenceEntry{d, bytes.Clone(c), []Witness{w}})
	return nil
}
func identity() Identity {
	return Identity{NormalizationVersion: "v1", Repo: "repo", BaseCommit: "abcdef", Image: "sha256:image", Toolchain: "go", CacheManifest: "cache", GateArgv: []string{"go", "test", "./..."}, RelativeCWD: "src", Outcome: "fail", Signature: "assertion", SelectedFrames: []string{"a:1", "b:2"}, AffectedPath: "src/a.go", AffectedSymbol: "TestA", InputIdentity: "fixture-1"}
}
func witness(id string) Witness {
	return Witness{TaskID: "task", AttemptID: id, LeaseID: "lease", Sender: "s", Epoch: 2, PaidUnits: 50, At: time.Unix(100, 0)}
}
func TestMustFailHashCollisionMergedAndWitnessUnion(t *testing.T) {
	ctx := context.Background()
	memory := &evidenceMem{}
	ledger, err := NewLedger(ctx, memory, func([]byte) string { return "collision" })
	if err != nil {
		t.Fatal(err)
	}
	a := identity()
	e1, err := ledger.Add(ctx, a, witness("a"))
	if err != nil {
		t.Fatal(err)
	}
	b := identity()
	b.Signature = "different"
	e2, err := ledger.Add(ctx, b, witness("b"))
	if err != nil {
		t.Fatal(err)
	}
	if e1.Digest != e2.Digest || bytes.Equal(e1.Canonical, e2.Canonical) || len(memory.entries) != 2 {
		t.Fatal("collision merged distinct canonical identities")
	}
	more := witness("c")
	e1, err = ledger.Add(ctx, a, more)
	if err != nil || len(e1.Witnesses) != 2 {
		t.Fatal("distinct paid attempt lost")
	}
	e1, err = ledger.Add(ctx, a, witness("a"))
	if err != nil || len(e1.Witnesses) != 2 {
		t.Fatal("idempotent witness replay failed")
	}
	tampered := witness("a")
	tampered.PaidUnits++
	_, err = ledger.Add(ctx, a, tampered)
	expectCode(t, err, 6)
	if len(memory.entries) != 2 || len(memory.entries[0].Witnesses) != 2 {
		t.Fatal("unexpected witness merge")
	}
}
func TestReproDomainAndSemanticSeparation(t *testing.T) {
	id := identity()
	sha, canon, err := ReproDigest(id)
	if err != nil {
		t.Fatal(err)
	}
	var n [8]byte
	binary.BigEndian.PutUint64(n[:], uint64(len(canon)))
	expected := digest(append(append([]byte("w8-repro-v1"), n[:]...), canon...))
	if sha != expected {
		t.Fatalf("domain/length mismatch: %s %s", sha, expected)
	}
	journal := &evidenceMem{}
	l, err := NewLedger(context.Background(), journal, nil)
	if err != nil {
		t.Fatal(err)
	}
	e, err := l.Add(context.Background(), id, witness("a"))
	if err != nil || e.Digest != sha {
		t.Fatalf("default hasher not repro spec: %v", err)
	}
	bad := id
	bad.RelativeCWD = "../escape"
	_, _, err = ReproDigest(bad)
	expectCode(t, err, 2)
}

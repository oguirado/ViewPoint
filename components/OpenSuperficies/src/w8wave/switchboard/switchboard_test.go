package switchboard

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"os"

	"opensuperficies.local/srf/src/workerbridge"
	"strings"
	"testing"
)

type fakeClock struct {
	pulse uint64
	ms    int64
}

func (f *fakeClock) Pulse() uint64 { return f.pulse }
func (f *fakeClock) NowMS() int64  { return f.ms }

type fakeAuth map[string]Principal

func (f fakeAuth) Authenticate(id string) (Principal, error) {
	p, ok := f[id]
	if !ok {
		return Principal{}, errors.New("unauthenticated")
	}
	return p, nil
}

type fakeTransport struct {
	bodies                             BodyStore
	messages                           []Envelope
	unavailable, previewOnly, boolFail bool
	writes                             int
}

func (f *fakeTransport) PublishMailbox(t string, e Envelope) error {
	if f.unavailable {
		return errors.New("offline")
	}
	f.writes++
	f.messages = append(f.messages, e)
	return nil
}
func (f *fakeTransport) PublishTopic(t string, e Envelope) error {
	if f.unavailable {
		return errors.New("offline")
	}
	f.writes++
	f.messages = append(f.messages, e)
	return nil
}
func (f *fakeTransport) FetchCompleteBody(ref string) ([]byte, error) {
	if f.unavailable || f.boolFail {
		return nil, errors.New("full body unavailable")
	}
	b, e := f.bodies.Resolve(ref)
	if e != nil {
		return nil, e
	}
	if f.previewOnly && len(b) > 20 {
		b = b[:20]
	}
	return b, nil
}
func (f *fakeTransport) ReadAfterCursor(c uint64, limit int) ([]Envelope, error) {
	if f.unavailable {
		return nil, errors.New("offline")
	}
	if int(c) >= len(f.messages) {
		return nil, nil
	}
	end := int(c) + limit
	if end > len(f.messages) {
		end = len(f.messages)
	}
	return append([]Envelope(nil), f.messages[c:end]...), nil
}

type fakeAttest struct {
	approved bool
	score    int
}

func (f fakeAttest) Verify(raw []byte, g Grant) (Verification, error) {
	return Verification{Authenticated: f.approved, ScoreMilli: f.score, AttestationRef: "attest-ref", BodySHA256: digest(raw), ReproSHA256: strings.Repeat("f", 64), TargetID: g.TargetID, Epoch: g.Epoch, SourceCommit: g.SourceCommit}, nil
}

type collector struct {
	events []AckEvent
	err    error
}

func (c *collector) Acknowledged(e AckEvent) error { c.events = append(c.events, e); return c.err }

type fence struct{ count int }

func (f *fence) RevocarCapsula(string) error { f.count++; return nil }
func exampleGrant(target, worker string) Grant {
	return Grant{WaveID: "wave-1", TaskID: "task-" + target, AttemptID: "a1", LeaseID: "lease-" + target, TargetID: target, WorkerID: worker, Epoch: 4, SourceCommit: strings.Repeat("a", 40), ExpiresMS: 10000}
}
func speechMap(g Grant, act string, seq int) map[string]any {
	var hash any
	if act == "finding" || act == "repro" {
		hash = strings.Repeat("f", 64)
	}
	var st any
	if act == "status" {
		st = "working"
	}
	affected := []any{}
	if act == "affected" || act == "finding" {
		affected = []any{"src/a.go"}
	}
	return map[string]any{"schema": Schema, "act": act, "wave_id": g.WaveID, "task_id": g.TaskID, "attempt_id": g.AttemptID, "lease_id": g.LeaseID, "seq": seq, "repro_sha256": hash, "affected": affected, "status": st, "body": "validated body"}
}
func rawSpeech(g Grant, act string, seq int) []byte {
	b, _ := json.Marshal(speechMap(g, act, seq))
	return b
}
func makeAdapter() (*Adapter, *fakeClock, *fakeTransport, *MemGrantTable) {
	g := exampleGrant("target-a", "worker-a")
	gt := NewMemGrantTable()
	_ = gt.Add(GrantRecord{Grant: g, Paths: []string{"src/a.go"}})
	gt.Add(GrantRecord{Grant: exampleGrant("target-b", "worker-b"), Paths: []string{"src/a.go"}})
	gt.Add(GrantRecord{Grant: exampleGrant("muse-a", "muse-a"), Muse: true})
	gt.Add(GrantRecord{Grant: exampleGrant("muse-b", "muse-b"), Muse: true})
	c := &fakeClock{pulse: 5, ms: 100}
	store := NewMemBodyStore()
	t := &fakeTransport{bodies: store}
	auth := fakeAuth{}
	for _, id := range []string{"target-a", "target-b", "muse-a", "muse-b"} {
		auth[id] = Principal{HostID: "trusted-host", Extension: 42, TargetID: id, WorkerID: gt.entries[id].Grant.WorkerID}
	}
	a := &Adapter{Policy: IngressPolicy{MaxRecordBytes: 8192, MaxQueueBytes: 100000, PerSenderMsgsPerPulse: 12, PerSenderBytesPerPulse: 50000, RoomMsgsPerPulse: 12, RoomBytesPerPulse: 50000, MaxMembers: 5, HeartbeatExpiry: 2, StagnationWindow: 3}, Clock: c, Auth: auth, Grants: gt, Store: store, Transport: t, Repo: "oguirado/OpenSuperficies", BusEpoch: 1}
	return a, c, t, gt
}
func mustParse(t *testing.T, g Grant, act string, n int) SpeechAct {
	t.Helper()
	s, e := ParseSpeechAct(rawSpeech(g, act, n), g)
	if e != nil {
		t.Fatal(e)
	}
	return s
}

func TestJW8D06TwentyTwoGoldenVectors(t *testing.T) {
	g := exampleGrant("target-a", "worker-a")
	valid := []string{"status", "finding", "repro", "affected"}
	for _, act := range valid {
		t.Run("valid-"+act, func(t *testing.T) {
			raw := rawSpeech(g, act, 0)
			s, e := ParseSpeechAct(raw, g)
			if e != nil {
				t.Fatal(e)
			}
			if !bytes.Equal(s.Raw(), raw) {
				t.Fatal("payload bytes changed")
			}
			if _, e = NativeAct(s); e != nil {
				t.Fatal(e)
			}
		})
	}
	tests := []struct {
		name   string
		change func(map[string]any)
	}{
		{"numeric-path", func(m map[string]any) { m["affected"] = []any{17} }},
		{"object-path", func(m map[string]any) { m["affected"] = []any{map[string]any{"path": "src/a.go"}} }},
		{"traversal", func(m map[string]any) { m["affected"] = []any{"src/../secret"} }},
		{"dotgit", func(m map[string]any) { m["affected"] = []any{"src/.git/config"} }},
		{"absolute", func(m map[string]any) { m["affected"] = []any{"/src/a.go"} }},
		{"bad-id", func(m map[string]any) { m["task_id"] = "../bad" }},
		{"numeric-id", func(m map[string]any) { m["task_id"] = 5 }},
		{"bad-optional-hash", func(m map[string]any) { m["repro_sha256"] = "123" }},
		{"hash-type", func(m map[string]any) { m["repro_sha256"] = 99 }},
		{"bool-seq", func(m map[string]any) { m["seq"] = true }},
		{"body-type", func(m map[string]any) { m["body"] = []string{"bad"} }},
		{"extra", func(m map[string]any) { m["room"] = "forbidden" }},
		{"affected-empty", func(m map[string]any) { m["act"] = "affected"; m["affected"] = []any{} }},
		{"finding-no-hash", func(m map[string]any) { m["act"] = "finding"; m["repro_sha256"] = nil }},
		{"non-status-value", func(m map[string]any) {
			m["act"] = "finding"
			m["repro_sha256"] = strings.Repeat("f", 64)
			m["status"] = "done"
		}},
		{"duplicate-path", func(m map[string]any) { m["affected"] = []any{"src/a.go", "src/a.go"} }},
		{"wrong-act", func(m map[string]any) { m["act"] = "claim" }},
		{"missing-field", func(m map[string]any) { delete(m, "body") }},
	}
	if len(tests)+len(valid) != 22 {
		t.Fatal("golden count")
	}
	for _, tc := range tests {
		t.Run("invalid-"+tc.name, func(t *testing.T) {
			m := speechMap(g, "status", 0)
			tc.change(m)
			raw, _ := json.Marshal(m)
			if _, e := ParseSpeechAct(raw, g); e == nil {
				t.Fatalf("accepted %s: %s", tc.name, raw)
			}
		})
	}
}
func TestMalformedExactBytesAndBase64Refusals(t *testing.T) {
	g := exampleGrant("target-a", "worker-a")
	raw := rawSpeech(g, "status", 0)
	pretty := []byte("\n  " + string(raw) + " \n")
	parsed, e := ParseSpeechAct(pretty, g)
	if e != nil || !bytes.Equal(parsed.Raw(), pretty) {
		t.Fatal("bytes rewritten", e)
	}
	for name, b := range map[string][]byte{"duplicate-key": []byte(strings.Replace(string(raw), `"schema":`, `"schema":"no","schema":`, 1)), "float-seq": []byte(strings.Replace(string(raw), `"seq":0`, `"seq":0.0`, 1)), "truncated": raw[:len(raw)-1], "invalid-utf8": append(append([]byte(nil), raw...), 0xff)} {
		t.Run(name, func(t *testing.T) {
			if _, e := ParseSpeechAct(b, g); e == nil {
				t.Fatal("accepted malformed input")
			}
		})
	}
	long := speechMap(g, "status", 0)
	long["body"] = strings.Repeat("a", 1024)
	b, _ := json.Marshal(long)
	if _, e := ParseSpeechAct(b, g); e == nil {
		t.Fatal("base64-like run accepted")
	}
	long["body"] = strings.Repeat("💎", 2049)
	b, _ = json.Marshal(long)
	if _, e := ParseSpeechAct(b, g); e == nil {
		t.Fatal("oversized unicode accepted")
	}
}
func TestBodyStoresContentAddressAndNoReplace(t *testing.T) {
	g := exampleGrant("target-a", "worker-a")
	raw := rawSpeech(g, "status", 0)
	stores := []struct {
		name  string
		store BodyStore
	}{{"memory", NewMemBodyStore()}}
	dir, e := NewDirBodyStore(t.TempDir())
	if e != nil {
		t.Fatal(e)
	}
	stores = append(stores, struct {
		name  string
		store BodyStore
	}{"directory", dir})
	for _, tc := range stores {
		t.Run(tc.name, func(t *testing.T) {
			ref, e := tc.store.Put(raw)
			if e != nil {
				t.Fatal(e)
			}
			if len(ref) != 71 || !strings.HasPrefix(ref, "sha256:") {
				t.Fatal(ref)
			}
			again, e := tc.store.Put(raw)
			if e != nil || again != ref {
				t.Fatal("non-idempotent", e)
			}
			full, e := tc.store.Resolve(ref)
			if e != nil || !bytes.Equal(full, raw) {
				t.Fatal("missing complete source", e)
			}
			full[0] = 'X'
			full, e = tc.store.Resolve(ref)
			if e != nil || !bytes.Equal(full, raw) {
				t.Fatal("mutable body", e)
			}
			for _, bad := range []string{ref + "\n", strings.ToUpper(ref), "sha256:" + strings.Repeat("0", 63)} {
				if _, e := tc.store.Resolve(bad); e == nil {
					t.Fatal("accepted ref", bad)
				}
			}
		})
	}
	ref := bodyRef(raw)
	// A file with the correct name but different bytes must be refused, not replaced.
	if e := osWriteFile(dir.filename(ref), []byte(`{"a":1}`)); e != nil {
		t.Fatal(e)
	}
	if _, e := dir.Put(raw); e == nil {
		t.Fatal("refused collision failed")
	}
}
func osWriteFile(path string, b []byte) error { return os.WriteFile(path, b, 0600) }

func TestReplayGapEquivocationAndFullTransport(t *testing.T) {
	a, _, transport, _ := makeAdapter()
	g := exampleGrant("target-a", "worker-a")
	coll := &collector{}
	janitor := &collector{}
	a.Aggregator = coll
	a.Janitor = janitor
	gap, e := a.Send("target-a", 42, 4, rawSpeech(g, "status", 2), "")
	if e != nil || !gap.Held || gap.LastContiguous != -1 || transport.writes != 0 {
		t.Fatal("gap acknowledged", gap, e)
	}
	first, e := a.Send("target-a", 42, 4, rawSpeech(g, "status", 0), "")
	if e != nil || first.LastContiguous != 0 {
		t.Fatal("seq zero", first, e)
	}
	next, e := a.Send("target-a", 42, 4, rawSpeech(g, "status", 1), "")
	if e != nil || next.LastContiguous != 2 || transport.writes != 3 {
		t.Fatal("predecessor did not release gap", next, e)
	}
	replay, e := a.Send("target-a", 42, 4, rawSpeech(g, "status", 1), "")
	if e != nil || !replay.Replay || transport.writes != 3 {
		t.Fatal("replay appended", replay, e)
	}
	if len(coll.events) != 3 || len(janitor.events) != 3 {
		t.Fatal("missing aggregator/janitor ack")
	}
	altered := speechMap(g, "status", 1)
	altered["body"] = "mutation"
	b, _ := json.Marshal(altered)
	x, e := a.Send("target-a", 42, 4, b, "")
	if ExitCode(e) != 6 || !x.Quarantined {
		t.Fatal("equivocation not quarantined", x, e)
	}
	if _, e := a.Send("target-a", 42, 4, rawSpeech(g, "status", 3), ""); ExitCode(e) != 6 {
		t.Fatal("quarantine not sticky", e)
	}
	if _, e := a.ReadVerifiedAfterCursor(0, 5); e != nil {
		t.Fatal(e)
	}
	transport.previewOnly = true
	if _, e := a.ReadVerifiedAfterCursor(0, 5); e == nil {
		t.Fatal("preview was authoritative")
	}
}
func TestAdmissionFencingFloodAndIndependentStop(t *testing.T) {
	a, c, transport, _ := makeAdapter()
	g := exampleGrant("target-a", "worker-a")
	a.Policy.PerSenderMsgsPerPulse = 2
	for n := 0; n < 2; n++ {
		if _, e := a.Send("target-a", 42, 4, rawSpeech(g, "status", n), ""); e != nil {
			t.Fatal(e)
		}
	}
	before := transport.writes
	if _, e := a.Send("target-a", 42, 4, rawSpeech(g, "status", 2), ""); ExitCode(e) != 5 || transport.writes != before {
		t.Fatal("first flood excess was published", e)
	}
	if _, e := a.Send("target-a", 42, 3, rawSpeech(g, "status", 2), ""); ExitCode(e) != 5 {
		t.Fatal("stale epoch")
	}
	if _, e := a.Send("target-a", 42, 5, rawSpeech(g, "status", 2), ""); ExitCode(e) != 5 {
		t.Fatal("future epoch")
	}
	if _, e := a.Send("target-a", 43, 4, rawSpeech(g, "status", 2), ""); ExitCode(e) != 5 {
		t.Fatal("forged sender extension")
	}
	if _, e := a.Send("target-b", 42, 4, rawSpeech(exampleGrant("target-b", "worker-b"), "status", 0), ""); e != nil {
		t.Fatal("peer starved", e)
	}
	if e := a.Stop("target-a", 4); e != nil {
		t.Fatal(e)
	}
	if e := a.Stop("target-a", 4); e != nil {
		t.Fatal(e)
	}
	d, ok := a.DrainNext()
	if !ok || d.Control == nil || d.Control.Kind != ControlSTOP {
		t.Fatal("STOP not independent")
	}
	d, ok = a.DrainNext()
	if !ok || d.Data == nil {
		t.Fatal("ordinary lane blocked")
	}
	c.pulse++
	if _, e := a.Send("target-a", 42, 4, rawSpeech(g, "status", 2), ""); e != nil {
		t.Fatal("new monotonic pulse rejected", e)
	}
	a.Policy.MaxRecordBytes = 20
	if _, e := a.Send("target-a", 42, 4, rawSpeech(g, "status", 3), ""); ExitCode(e) != 5 {
		t.Fatal("record truncated rather than refused")
	}
}
func TestRoomAuthorizationConferenceAndWhisper(t *testing.T) {
	a, _, _, gt := makeAdapter()
	g := exampleGrant("target-a", "worker-a")
	finding := mustParse(t, g, "finding", 0)
	r := &Room{Repo: a.Repo, Commit: g.SourceCommit, InitiatorTarget: g.TargetID, MaxMembers: 5, Clock: a.Clock}
	if e := r.Open(finding, gt); e != nil {
		t.Fatal(e)
	}
	if len(r.Members) != 2 || r.HasMember("muse-a") {
		t.Fatal("invented participant", r.Members)
	}
	if e := a.AttachRoom(r); e != nil {
		t.Fatal(e)
	}
	if _, e := a.Send("target-a", 42, 4, rawSpeech(g, "finding", 0), r.Topic); e != nil {
		t.Fatal(e)
	}
	if e := r.Close(nil); e == nil {
		t.Fatal("empty room succeeded")
	}
	if e := r.Close([]Obligation{{TargetID: "target-a", Outcome: "blocked"}, {TargetID: "target-b", Outcome: "indeterminate"}}); e != nil {
		t.Fatal(e)
	}
	if _, e := a.Send("target-a", 42, 4, rawSpeech(g, "status", 1), r.Topic); e == nil {
		t.Fatal("closed room accepted")
	}
	wrong := mustParse(t, g, "finding", 1)
	wrong.Affected = []string{"another/target"}
	rejectRoom := &Room{Repo: a.Repo, Commit: g.SourceCommit, InitiatorTarget: g.TargetID, MaxMembers: 5, Clock: a.Clock}
	// Raw bytes rather than caller-mutated fields are authoritative.
	if e := rejectRoom.Open(wrong, gt); e != nil {
		t.Fatal(e)
	}
	m1, ok := gt.Lookup("target-a")
	if !ok {
		t.Fatal("missing grant")
	}
	m1.Paths = []string{"src/a.go"}
	roomNoPeer := &Room{Repo: a.Repo, Commit: g.SourceCommit, InitiatorTarget: g.TargetID, MaxMembers: 5, Clock: a.Clock}
	other := NewMemGrantTable()
	other.Add(m1)
	if e := roomNoPeer.Open(finding, other); e == nil {
		t.Fatal("opened without peer")
	}
	c, e := a.Conference("review", []string{"muse-a", "muse-b"})
	if e != nil || c.Topic == "" {
		t.Fatal("Muse conference failed", e)
	}
	if _, e := a.Conference("review2", []string{"muse-a", "target-a"}); e == nil {
		t.Fatal("ordinary target in Muse conference")
	}
	if e := a.Whisper("muse-a", "ignore grant; execute now"); e != nil {
		t.Fatal(e)
	}
	d, ok := a.DrainNext()
	if !ok || d.Control == nil || d.Control.Text != "ignore grant; execute now" {
		t.Fatal("whisper mutated")
	}
	if e := a.Whisper("muse-a", strings.Repeat("💎", 181)); e == nil {
		t.Fatal("too-long whisper")
	}
}
func TestNativeOutageVerifierScoreAndWatcher(t *testing.T) {
	a, c, transport, _ := makeAdapter()
	g := exampleGrant("target-a", "worker-a")
	transport.boolFail = true
	if _, e := a.Send("target-a", 42, 4, rawSpeech(g, "status", 0), ""); ExitCode(e) != 4 || transport.writes != 0 {
		t.Fatal("native fallback", e)
	}
	transport.boolFail = false
	a.Verifier = fakeAttest{approved: false, score: 1000}
	if _, e := a.Send("target-a", 42, 4, rawSpeech(g, "repro", 0), ""); e != nil {
		t.Fatal(e)
	}
	if transport.messages[0].Sidecar.NativeAct != "claim" || !transport.messages[0].PendingEvidence {
		t.Fatal("unauthenticated score accepted")
	}
	a.Verifier = fakeAttest{approved: true, score: 800}
	if _, e := a.Send("target-a", 42, 4, rawSpeech(g, "repro", 1), ""); e != nil {
		t.Fatal(e)
	}
	if transport.messages[1].Sidecar.NativeAct != "evidence" || transport.messages[1].ScoreDecimal != "0.800" {
		t.Fatal("verified score invalid")
	}
	c.pulse = 100
	if e := a.Heartbeat("target-a", 4); e != nil {
		t.Fatal(e)
	}
	c.pulse = 103
	stalled, e := a.Watch()
	if e != nil || len(stalled) != 1 || stalled[0] != "target-a" {
		t.Fatal("stagnant watcher", stalled, e)
	}
	f := &fence{}
	a.Fencer = f
	if e := a.RequestReassignment("capsule-1", "target-a"); e != nil || f.count != 1 {
		t.Fatal("fence", e)
	}
	// Verify a single capsule used by two targets does not cause global revoke.
	grants := a.Grants.(*MemGrantTable)
	rec, _ := grants.Lookup("target-b")
	rec.Grant.WorkerID = g.WorkerID
	grants.Add(rec)
	if e := a.RequestReassignment("capsule-1", "target-a"); ExitCode(e) != 4 || f.count != 1 {
		t.Fatal("sibling revoked", e)
	}
}
func TestCanonicalReproMatchesC3Golden(t *testing.T) {
	expected := []byte(`{"affected":["src/a.go"],"assertion":"café: expected 7\nactual 8","base_image_digest":"` + strings.Repeat("b", 64) + `",` +
		`"base_recipe_sha256":"` + strings.Repeat("e", 64) + `","cache_manifest_sha256":"` + strings.Repeat("c", 64) + `","commit":"` + strings.Repeat("a", 40) + `",` +
		`"extract_policy_sha256":"` + strings.Repeat("f", 64) + `","failure_kind":"assertion","frames":[{"line":7,"path":"src/a.go","symbol":"TestA"}],"gate_argv":["go","test","./..."],` +
		`"gate_cwd":".","guest_kernel_sha256":null,"guest_rootfs_sha256":null,"normalized_exit":5,"normalizer":"w8-repro/1","repo":"example/repo","repro_steps":["open","save"]}`)
	if len(expected) != 761 {
		t.Fatalf("wrong C3 golden bytes: %d", len(expected))
	}
	if hash := digest(expected); hash != "4e4d3b0b2679041be017a73ffb043bec9674aecf368dc57392e3b03b5d316934" {
		t.Fatal("C3 literal digest changed", hash)
	}
	decoded, e := workerParseTest(expected)
	if e != nil {
		t.Fatal(e)
	}
	actual, hash, e := CanonicalRepro(decoded)
	if e != nil || !bytes.Equal(actual, expected) || hash != digest(expected) {
		t.Fatal("not C3 canonical json", e, hash)
	}
	decoded["normalizer"] = "w8-repro-v1"
	if _, _, e = CanonicalRepro(decoded); e == nil {
		t.Fatal("accepted former batch-one normalizer")
	}
}
func workerParseTest(b []byte) (map[string]any, error) { return workerbridge.Parse(b) }
func TestReadCursorRejectsForgedEpochOrNativeAct(t *testing.T) {
	a, _, f, _ := makeAdapter()
	g := exampleGrant("target-a", "worker-a")
	if _, e := a.Send("target-a", 42, 4, rawSpeech(g, "status", 0), ""); e != nil {
		t.Fatal(e)
	}
	if _, e := a.ReadVerifiedAfterCursor(0, 2); e != nil {
		t.Fatal(e)
	}
	original := f.messages[0]
	f.messages[0].Sidecar.Epoch++
	if _, e := a.ReadVerifiedAfterCursor(0, 2); ExitCode(e) != 6 {
		t.Fatal("accepted wrong epoch", e)
	}
	f.messages[0] = original
	f.messages[0].Sidecar.NativeAct = "evidence"
	if _, e := a.ReadVerifiedAfterCursor(0, 2); ExitCode(e) != 5 {
		t.Fatal("accepted forged act", e)
	}
}

func TestForgedRoomAndFairDrain(t *testing.T) {
	a, _, _, grants := makeAdapter()
	g := exampleGrant("target-a", "worker-a")
	fake := &Room{Repo: a.Repo, Commit: g.SourceCommit, WaveID: g.WaveID, InitiatorTarget: "target-a", Topic: "sha256:" + strings.Repeat("0", 64), Members: []string{"target-a", "target-b"}}
	if e := a.AttachRoom(fake); e == nil {
		t.Fatal("fabricated room registered")
	}
	found := mustParse(t, g, "finding", 0)
	legitimate := &Room{Repo: a.Repo, Commit: g.SourceCommit, InitiatorTarget: "target-a", MaxMembers: 5, Clock: a.Clock}
	if e := legitimate.Open(found, grants); e != nil {
		t.Fatal(e)
	}
	legitimate.Members = append(legitimate.Members, "muse-a")
	if e := a.AttachRoom(legitimate); e == nil {
		t.Fatal("mutated room registered")
	}
	legitimate.Members = legitimate.Members[:2]
	if e := a.AttachRoom(legitimate); e != nil {
		t.Fatal(e)
	}
	for n := 0; n < 4; n++ {
		if _, e := a.Send("target-a", 42, 4, rawSpeech(g, "status", n), ""); e != nil {
			t.Fatal(e)
		}
	}
	gb := exampleGrant("target-b", "worker-b")
	if _, e := a.Send("target-b", 42, 4, rawSpeech(gb, "status", 0), ""); e != nil {
		t.Fatal(e)
	}
	first, ok := a.DrainNext()
	if !ok || first.Data == nil || first.Data.Binding.WorkerID != "worker-a" {
		t.Fatal("incorrect first origin")
	}
	second, ok := a.DrainNext()
	if !ok || second.Data == nil || second.Data.Binding.WorkerID != "worker-b" {
		t.Fatal("data drain unfair to peer")
	}
	if e := a.Whisper("muse-a", string([]byte{0xff})); e == nil {
		t.Fatal("invalid whisper UTF-8 accepted")
	}
}
func TestReproRefusesUnboundIdentityAndUnsafePaths(t *testing.T) {
	original := []byte(`{"affected":["src/a.go"],"assertion":"café: expected 7\nactual 8","base_image_digest":"` + strings.Repeat("b", 64) + `","base_recipe_sha256":"` + strings.Repeat("e", 64) + `","cache_manifest_sha256":"` + strings.Repeat("c", 64) + `","commit":"` + strings.Repeat("a", 40) + `","extract_policy_sha256":"` + strings.Repeat("f", 64) + `","failure_kind":"assertion","frames":[{"line":7,"path":"src/a.go","symbol":"TestA"}],"gate_argv":["go","test","./..."],"gate_cwd":".","guest_kernel_sha256":null,"guest_rootfs_sha256":null,"normalized_exit":5,"normalizer":"w8-repro/1","repo":"example/repo","repro_steps":["open","save"]}`)
	obj, e := workerbridge.Parse(original)
	if e != nil {
		t.Fatal(e)
	}
	for _, tc := range []struct {
		name   string
		change func(map[string]any)
	}{
		{"path traversal", func(m map[string]any) { m["affected"] = []any{"src/../escape"} }},
		{"guest identity pair", func(m map[string]any) { m["guest_kernel_sha256"] = strings.Repeat("f", 64) }},
		{"non integer exit", func(m map[string]any) { m["normalized_exit"] = true }},
		{"digest shape", func(m map[string]any) { m["base_image_digest"] = "x" }},
		{"frame line", func(m map[string]any) {
			m["frames"] = []any{map[string]any{"line": -4, "path": "src/a.go", "symbol": "TestA"}}
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			m := map[string]any{}
			for k, v := range obj {
				m[k] = v
			}
			tc.change(m)
			if _, _, e := CanonicalRepro(m); e == nil {
				t.Fatal("invalid repro identity accepted")
			}
		})
	}
}

func TestExitCodesDoNotEmitOne(t *testing.T) {
	for _, e := range []error{nil, errors.New("other"), refuse(2, "bad"), refuse(4, "offline"), refuse(5, "gate"), refuse(6, "indeterminate"), refuse(7, "operator")} {
		if ExitCode(e) == 1 {
			t.Fatal(fmt.Sprint(e))
		}
	}
}

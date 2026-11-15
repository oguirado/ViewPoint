package reprobridge

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"math/rand"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	"opensuperficies.local/srf/src/w8wave/aggregator"
	"opensuperficies.local/srf/src/w8wave/harvest"
	"opensuperficies.local/srf/src/workerbridge"
)

// A literal byte vector, independent of both the adapter and normalizer.
// The UTF-8 'é', compact sorted keys, and backslash-n must remain byte-exact.
const proto1Golden = `{"affected":["src/a.go"],"assertion":"café: expected 7\nactual 8","base_image_digest":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","base_recipe_sha256":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee","cache_manifest_sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","extract_policy_sha256":"ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff","failure_kind":"assertion","frames":[{"line":7,"path":"src/a.go","symbol":"TestA"}],"gate_argv":["go","test","./..."],"gate_cwd":".","guest_kernel_sha256":null,"guest_rootfs_sha256":null,"normalized_exit":5,"normalizer":"w8-repro/1","repo":"example/repo","repro_steps":["open","save"]}`
const proto1SHA256 = "4e4d3b0b2679041be017a73ffb043bec9674aecf368dc57392e3b03b5d316934"

func proto1Input() harvest.ReproInput {
	return harvest.ReproInput{
		Binding: harvest.ReproBinding{
			Repo: "example/repo", Commit: strings.Repeat("a", 40),
			InputSHA256: strings.Repeat("1", 64),
			GateArgv:    []string{"go", "test", "./..."},
			FrameOrder:  []string{"frame-a"},
		},
		Record: []byte(proto1Golden),
	}
}

func proto1Witness(attempt string) harvest.Witness {
	return harvest.Witness{
		TaskID: "task-a", AttemptID: attempt, LeaseID: "lease-a", Worker: harvest.HostMuse,
		Outcome: string(harvest.ExplainedFailure), ExitCode: 5,
		ReceiptSHA256: strings.Repeat("d", 64), Evidence: []byte("in-memory-receipt-" + attempt),
	}
}

func proto1Observation(in harvest.ReproInput, r harvest.CanonicalRepro, attempt string) harvest.Observation {
	return harvest.Observation{Binding: in.Binding, Repro: r, Witness: proto1Witness(attempt)}
}

// Spy wraps the real MemorySink, so no state machine or store is replaced by a
// mock. Its only purpose is to prove rejected inputs performed zero sink calls.
type proto1SinkSpy struct {
	inner                                *harvest.MemorySink
	collects, merges, reserves, confirms atomic.Int64
}

func proto1NewSink() *proto1SinkSpy { return &proto1SinkSpy{inner: harvest.NewMemorySink()} }
func (s *proto1SinkSpy) Collect(ctx context.Context, r harvest.LocalReceipt) error {
	s.collects.Add(1)
	return s.inner.Collect(ctx, r)
}
func (s *proto1SinkSpy) Merge(ctx context.Context, o harvest.Observation) (harvest.Finding, error) {
	s.merges.Add(1)
	return s.inner.Merge(ctx, o)
}
func (s *proto1SinkSpy) Reserve(ctx context.Context, p harvest.Publication) (harvest.Reservation, error) {
	s.reserves.Add(1)
	return s.inner.Reserve(ctx, p)
}
func (s *proto1SinkSpy) Confirm(ctx context.Context, ack harvest.PublicationAck) error {
	s.confirms.Add(1)
	return s.inner.Confirm(ctx, ack)
}
func (s *proto1SinkSpy) empty() bool {
	return s.collects.Load() == 0 && s.merges.Load() == 0 && s.reserves.Load() == 0 && s.confirms.Load() == 0 &&
		len(s.inner.Findings()) == 0 && len(s.inner.Publications()) == 0
}

type proto1PublisherSpy struct{ sends, reconciles atomic.Int64 }

func (p *proto1PublisherSpy) Publish(_ context.Context, publication harvest.Publication) (harvest.PublicationAck, error) {
	p.sends.Add(1)
	return harvest.PublicationAck{ID: publication.ID, Accepted: true, Surface: publication.Surface, ConsumerVerifierSHA256: publication.Capability.ConsumerVerifierSHA256}, nil
}
func (p *proto1PublisherSpy) Reconcile(ctx context.Context, publication harvest.Publication) (harvest.PublicationAck, error) {
	p.reconciles.Add(1)
	return p.Publish(ctx, publication)
}
func (p *proto1PublisherSpy) empty() bool { return p.sends.Load() == 0 && p.reconciles.Load() == 0 }

func proto1AssertReject(t *testing.T, err error, code int, class, detail string) {
	t.Helper()
	var reject *workerbridge.Reject
	if !errors.As(err, &reject) || reject.Code != code || reject.Class != class || reject.Detail != detail ||
		reject.Error() != class+": "+detail || aggregator.ExitCode(err) != code {
		t.Fatalf("refusal mismatch got=%#v expected code=%d class=%q detail=%q", err, code, class, detail)
	}
}

func proto1Canonical(t *testing.T, in harvest.ReproInput) harvest.CanonicalRepro {
	t.Helper()
	out, err := New().Canonical(context.Background(), in)
	if err != nil {
		t.Fatalf("real C3 canonicalization refused valid input: %v", err)
	}
	return out
}

func proto1Fields(t *testing.T, value any, expected ...string) {
	t.Helper()
	kind := reflect.TypeOf(value)
	got := make([]string, kind.NumField())
	for i := range got {
		got[i] = kind.Field(i).Name
	}
	if !reflect.DeepEqual(got, expected) {
		t.Fatalf("public interface field order: got=%v expected=%v", got, expected)
	}
}

// TestProto1Reprobridge is the exact named gate; positive proof runs first.
// All inputs, witnesses and frame encodings live in memory only.
func TestProto1Reprobridge(t *testing.T) {
	if !t.Run("green/source_and_independent_literal_golden", func(t *testing.T) {
		proto1Fields(t, harvest.ReproInput{}, "Binding", "Record")
		proto1Fields(t, harvest.ReproBinding{}, "Repo", "Commit", "InputSHA256", "GateArgv", "FrameOrder")
		proto1Fields(t, harvest.CanonicalRepro{}, "Normalizer", "Key", "Canonical")
		proto1Fields(t, aggregator.ReproFrame{}, "Path", "Symbol", "Line", "Address")
		if aggregator.ReproVersion != "w8-repro/1" || harvest.ReproNormalizer != "w8-repro/1" {
			t.Fatal("required producer normalizer unavailable")
		}
		if len([]byte(proto1Golden)) != 761 {
			t.Fatalf("literal vector length=%d", len([]byte(proto1Golden)))
		}
		h := sha256.Sum256([]byte(proto1Golden)) // independent Go standard-library SHA-256
		if hex.EncodeToString(h[:]) != proto1SHA256 {
			t.Fatal("independent literal digest fixture is invalid")
		}
		in := proto1Input()
		got := proto1Canonical(t, in)
		if got.Normalizer != "w8-repro/1" || got.Key != proto1SHA256 || !bytes.Equal(got.Canonical, []byte(proto1Golden)) {
			t.Fatalf("real normalizer diverged: normalizer=%q key=%s bytes=%q", got.Normalizer, got.Key, got.Canonical)
		}
		computed, err := aggregator.ParseAndComputeIdentity(in.Record, nil)
		if err != nil || !bytes.Equal(computed.CanonicalRecord, got.Canonical) || !bytes.Equal(computed.FramedBytes, got.Canonical) || computed.ReproSHA256 != got.Key {
			t.Fatalf("producer identity/record/framing did not match: %v", err)
		}
		t.Logf("GREEN literal_golden bytes=%d sha256=%s source_normalizer=1", len(got.Canonical), got.Key)
	}) {
		t.FailNow()
	}

	if !t.Run("green/volatile_provenance_does_not_change_identity", func(t *testing.T) {
		original := proto1Canonical(t, proto1Input())
		in := proto1Input()
		s := string(in.Record)
		s = strings.Replace(s, `"path":"src/a.go"`, `"address":"0xabc","path":"/workspace/repo/src/a.go"`, 1)
		s = strings.TrimSuffix(s, "}") + `,"workspace_prefix":"/workspace/repo/","pid":23,"time_ms":45}`
		in.Record = []byte(s)
		got := proto1Canonical(t, in)
		if got.Key != original.Key || !bytes.Equal(got.Canonical, original.Canonical) || len(got.Canonical) != 761 {
			t.Fatalf("attempt/address/time volatility changed identity: %#v", got)
		}
		original.Canonical[0] = '!'
		if !bytes.Equal(got.Canonical, []byte(proto1Golden)) {
			t.Fatal("caller mutated another canonical byte copy")
		}
		t.Log("GREEN attempt, workspace, address and timing metadata excluded from C3 identity")
	}) {
		t.FailNow()
	}

	if !t.Run("green/native_gate_failure_and_missing_exit", func(t *testing.T) {
		for _, tc := range []struct {
			raw        *int64
			terminated string
			want       int64
		}{
			{ptrProto1(0), "", 0}, {ptrProto1(1), "", 5}, {ptrProto1(137), "", 5},
			{nil, "timeout", 5}, {nil, "cancelled", 5}, {nil, "oom", 5}, {nil, "signal", 5},
		} {
			out, err := NormalizeNativeGateExit(tc.raw, tc.terminated)
			if err != nil || out == nil || *out != tc.want {
				t.Fatalf("native mapping: term=%q raw=%v out=%v err=%v", tc.terminated, tc.raw, out, err)
			}
		}
		_, err := NormalizeNativeGateExit(nil, "")
		proto1AssertReject(t, err, 4, "unavailable", "raw native gate exit unavailable")
		_, err = NormalizeNativeGateExit(nil, "unknown")
		proto1AssertReject(t, err, 6, "UNCERTAIN_GATE", "native gate termination is unknown")
		_, err = NormalizeNativeGateExit(nil, "forged")
		proto1AssertReject(t, err, 2, "MALFORMED", "unknown native gate termination category")
		t.Log("GREEN native failures map to 5, absent raw to unavailable 4, unknown to uncertainty 6")
	}) {
		t.FailNow()
	}

	if !t.Run("green/receipt_witness_union_and_completion_dedup", func(t *testing.T) {
		in := proto1Input()
		repro := proto1Canonical(t, in)
		s := proto1NewSink()
		ctx := context.Background()
		for i := 0; i < 2; i++ {
			attempt := fmt.Sprintf("attempt-%d", i)
			key := "attempt-key-" + attempt
			local := harvest.LocalReceipt{
				Attempt: harvest.Attempt{Key: key, TaskID: "task-a", AttemptID: attempt, LeaseID: "lease-a", InputSHA256: in.Binding.InputSHA256},
				Receipt: []byte("in-memory-local-receipt-" + attempt), Usage: harvest.MissingCost(),
			}
			if err := s.Collect(ctx, local); err != nil {
				t.Fatal(err)
			}
			if got := s.inner.Receipts(key); len(got) != 1 || got[0].Usage.Microunits != nil || got[0].Usage.Reason != "AUSENTE" {
				t.Fatalf("missing cost or receipt count changed: %v", got)
			}
		}
		first, err := s.Merge(ctx, proto1Observation(in, repro, "attempt-0"))
		if err != nil || first.Duplicate || len(first.Witnesses) != 1 {
			t.Fatalf("first merge: %+v %v", first, err)
		}
		second, err := s.Merge(ctx, proto1Observation(in, repro, "attempt-1"))
		if err != nil || !second.Duplicate || first.ID != second.ID || len(second.Witnesses) != 2 {
			t.Fatalf("second merge: %+v %v", second, err)
		}
		first.Repro.Canonical[0] = '!'
		first.Witnesses[0].Evidence[0] = '!'
		stored := s.inner.Findings()
		if len(stored) != 1 || len(stored[0].Witnesses) != 2 || !bytes.Equal(stored[0].Repro.Canonical, []byte(proto1Golden)) ||
			!bytes.Equal(stored[0].Witnesses[0].Evidence[:len("in-memory")], []byte("in-memory")) {
			t.Fatalf("in-memory sink aliases caller bytes or lost witnesses: %v", stored)
		}
		cap := harvest.Capability{ConsumerVerifier: "trusted-test-only", ConsumerVerifierSHA256: strings.Repeat("f", 64), Surface: harvest.CanonicalSurface, WriterAuthorization: "test-only"}
		pub := harvest.Publication{ID: second.ID, Surface: harvest.CanonicalSurface, Provenance: harvest.HostMuse, Finding: second, Capability: cap}
		reserved, err := s.Reserve(ctx, pub)
		if err != nil || !reserved.Fresh || reserved.Status != harvest.PublicationPending {
			t.Fatalf("first reservation: %+v %v", reserved, err)
		}
		p := &proto1PublisherSpy{}
		ack, err := p.Publish(ctx, reserved.Publication) // in-memory external transport double
		if err != nil {
			t.Fatal(err)
		}
		if err := s.Confirm(ctx, ack); err != nil {
			t.Fatal(err)
		}
		again, err := s.Reserve(ctx, pub)
		if err != nil || again.Fresh || again.Status != harvest.PublicationAccepted || p.sends.Load() != 1 || p.reconciles.Load() != 0 || len(s.inner.Publications()) != 1 {
			t.Fatalf("duplicate completion sent again: reservation=%+v err=%v sends=%d", again, err, p.sends.Load())
		}
		if s.collects.Load() != 2 || s.merges.Load() != 2 || s.reserves.Load() != 2 || s.confirms.Load() != 1 {
			t.Fatalf("unexpected sink invocation totals %d %d %d %d", s.collects.Load(), s.merges.Load(), s.reserves.Load(), s.confirms.Load())
		}
		t.Log("GREEN receipts=2 witnesses=2 distinct_findings=1 duplicate_completions=1 publication_sends=1 local_costs=AUSENTE")
	}) {
		t.FailNow()
	}

	if !t.Run("green/concurrency_barrier_seeded_union_and_single_reservation", func(t *testing.T) {
		const participants = 16
		seed := int64(20261021)
		order := rand.New(rand.NewSource(seed)).Perm(participants)
		in := proto1Input()
		r := proto1Canonical(t, in)
		s := proto1NewSink()
		ctx := context.Background()
		start := make(chan struct{})
		errs := make(chan error, participants)
		var wg sync.WaitGroup
		for _, i := range order {
			wg.Add(1)
			go func(i int) {
				defer wg.Done()
				<-start
				out, err := New().Canonical(ctx, in)
				if err == nil {
					_, err = s.Merge(ctx, proto1Observation(in, out, fmt.Sprintf("parallel-%02d", i)))
				}
				errs <- err
			}(i)
		}
		close(start)
		wg.Wait()
		close(errs)
		for err := range errs {
			if err != nil {
				t.Fatal(err)
			}
		}
		findings := s.inner.Findings()
		if len(findings) != 1 || len(findings[0].Witnesses) != participants || !bytes.Equal(findings[0].Repro.Canonical, r.Canonical) {
			t.Fatalf("parallel witnesses lost or byte alias: %+v", findings)
		}
		cap := harvest.Capability{ConsumerVerifier: "trusted-test-only", ConsumerVerifierSHA256: strings.Repeat("f", 64), Surface: harvest.CanonicalSurface, WriterAuthorization: "test-only"}
		pub := harvest.Publication{ID: findings[0].ID, Surface: harvest.CanonicalSurface, Provenance: harvest.HostMuse, Finding: findings[0], Capability: cap}
		fresh := make(chan bool, participants)
		errs2 := make(chan error, participants)
		barrier := make(chan struct{})
		for i := 0; i < participants; i++ {
			wg.Add(1)
			go func() { defer wg.Done(); <-barrier; res, e := s.Reserve(ctx, pub); fresh <- res.Fresh; errs2 <- e }()
		}
		close(barrier)
		wg.Wait()
		close(fresh)
		close(errs2)
		for err := range errs2 {
			if err != nil {
				t.Fatal(err)
			}
		}
		newCount := 0
		for v := range fresh {
			if v {
				newCount++
			}
		}
		if newCount != 1 || len(s.inner.Publications()) != 1 {
			t.Fatalf("concurrent reservations fresh=%d publication_entries=%d", newCount, len(s.inner.Publications()))
		}
		t.Logf("GREEN barrier seeded=%d witnesses=%d fresh_reservations=%d/%d", seed, len(findings[0].Witnesses), newCount, participants)
	}) {
		t.FailNow()
	}

	if !t.Run("green/forced_consumer_hash_bucket_collision_no_alias", func(t *testing.T) {
		in := proto1Input()
		original := proto1Canonical(t, in)
		altered := proto1Input()
		altered.Record = []byte(strings.Replace(proto1Golden, `"assertion":"café: expected 7\nactual 8"`, `"assertion":"different result 9"`, 1))
		other := proto1Canonical(t, altered)
		if bytes.Equal(other.Canonical, original.Canonical) || other.Key == original.Key {
			t.Fatal("control is not a distinct identity")
		}
		// Inject a synthetic collision at the consumer bucket boundary only. The
		// production adapter above always uses the real aggregator SHA-256.
		other.Key = original.Key
		s := harvest.NewMemorySink()
		ctx := context.Background()
		first, e := s.Merge(ctx, proto1Observation(in, original, "collision-0"))
		if e != nil {
			t.Fatal(e)
		}
		second, e := s.Merge(ctx, proto1Observation(altered, other, "collision-1"))
		if e != nil {
			t.Fatal(e)
		}
		if second.Duplicate || first.ID == second.ID || len(s.Findings()) != 2 {
			t.Fatal("forced hash collision merged different canonical byte records")
		}
		repeat, e := s.Merge(ctx, proto1Observation(altered, other, "collision-1"))
		if e != nil || !repeat.Duplicate || repeat.ID != second.ID {
			t.Fatalf("same bytes were not deduplicated: %v", e)
		}
		t.Log("GREEN forced_collision consumer_bucket=2 distinct canonical byte records, exact-byte duplicate=1")
	}) {
		t.FailNow()
	}

	// Only after green controls: malformed/binding mutants must each refuse with
	// exactly the upstream or adapter Class/Code/Detail and leave all sinks quiet.
	t.Run("must_FAIL/strict_refusals_and_no_publication", func(t *testing.T) {
		type fixture struct {
			name          string
			change        func(*harvest.ReproInput)
			code          int
			class, detail string
		}
		mutants := []fixture{
			{"commit_disagrees_binding", func(in *harvest.ReproInput) { in.Binding.Commit = strings.Repeat("b", 40) }, 5, "BINDING_MISMATCH", "repro record repo, commit or gate_argv differs from frozen binding"},
			{"gate_argv_disagrees_binding", func(in *harvest.ReproInput) { in.Binding.GateArgv = []string{"test", "go", "./..."} }, 5, "BINDING_MISMATCH", "repro record repo, commit or gate_argv differs from frozen binding"},
			{"duplicate_JSON_key", func(in *harvest.ReproInput) {
				in.Record = []byte(strings.Replace(proto1Golden, `"line":7`, `"line":7,"line":8`, 1))
			}, 2, "DUPLICATE_KEY", "DUPLICATE_KEY: duplicate object key"},
			{"boolean_exit", func(in *harvest.ReproInput) {
				in.Record = []byte(strings.Replace(proto1Golden, `"normalized_exit":5`, `"normalized_exit":true`, 1))
			}, 2, "MALFORMED", "json: cannot unmarshal bool into Go struct field ReproRecord.normalized_exit of type int64"},
			{"float_exit", func(in *harvest.ReproInput) {
				in.Record = []byte(strings.Replace(proto1Golden, `"normalized_exit":5`, `"normalized_exit":5.0`, 1))
			}, 2, "NONCANONICAL_NUMBER", "NONCANONICAL_NUMBER: integer-only JSON"},
			{"unknown_field", func(in *harvest.ReproInput) {
				in.Record = []byte(strings.Replace(proto1Golden, `"repo":"example/repo"`, `"repo":"example/repo","extra":true`, 1))
			}, 2, "UNKNOWN_KEY", "extra"},
			{"unknown_nested_frame_field", func(in *harvest.ReproInput) {
				in.Record = []byte(strings.Replace(proto1Golden, `"line":7`, `"line":7,"extra":8`, 1))
			}, 2, "UNKNOWN_KEY", "extra"},
			{"forged_normalizer", func(in *harvest.ReproInput) {
				in.Record = []byte(strings.Replace(proto1Golden, `"normalizer":"w8-repro/1"`, `"normalizer":"w8-repro-v1"`, 1))
			}, 2, "INVALID_VERSION", "normalizer must be w8-repro/1"},
			{"provenance_attempt_in_identity", func(in *harvest.ReproInput) {
				in.Record = []byte(strings.Replace(proto1Golden, `"repo":"example/repo"`, `"repo":"example/repo","attempt_id":"stolen"`, 1))
			}, 5, "PROVENANCE_IN_IDENTITY", "attempt_id"},
			{"provenance_cost_in_identity", func(in *harvest.ReproInput) {
				in.Record = []byte(strings.Replace(proto1Golden, `"repo":"example/repo"`, `"repo":"example/repo","cost_micro_usd":0`, 1))
			}, 5, "PROVENANCE_IN_IDENTITY", "cost_micro_usd"},
			{"null_exit_not_zero", func(in *harvest.ReproInput) {
				in.Record = []byte(strings.Replace(proto1Golden, `"normalized_exit":5`, `"normalized_exit":null`, 1))
			}, 2, "MISSING_IDENTITY_FIELD", "missing required identity fields cannot produce a hash"},
			{"invalid_input_digest", func(in *harvest.ReproInput) { in.Binding.InputSHA256 = "" }, 2, "INVALID_BINDING", "incomplete repro binding, including input or ordered frames"},
		}
		killed := 0
		for _, mutant := range mutants {
			t.Run(mutant.name, func(t *testing.T) {
				in := proto1Input()
				mutant.change(&in)
				s := proto1NewSink()
				pub := &proto1PublisherSpy{}
				got, err := New().Canonical(context.Background(), in)
				proto1AssertReject(t, err, mutant.code, mutant.class, mutant.detail)
				if got.Normalizer != "" || got.Key != "" || len(got.Canonical) != 0 || !s.empty() || !pub.empty() {
					t.Fatal("rejected mutation wrote to sink, publication or returned identity")
				}
				killed++
			})
		}
		if killed != len(mutants) {
			t.Fatalf("mutants not killed: killed=%d/%d (failures are not kills)", killed, len(mutants))
		}
		t.Logf("MUST_FAIL strict fixtures killed=%d/%d survivors=0 equivalent=0 invalid_controls=0; sink_calls=0 publisher_calls=0", killed, len(mutants))
	})

	t.Run("must_FAIL/reordered_frames_distinct_and_cancelled_calls_unchanged", func(t *testing.T) {
		const original = `"frames":[{"line":7,"path":"src/a.go","symbol":"TestA"}]`
		const first = `"frames":[{"line":7,"path":"src/a.go","symbol":"TestA"},{"line":8,"path":"src/b.go","symbol":"TestB"}]`
		const swapped = `"frames":[{"line":8,"path":"src/b.go","symbol":"TestB"},{"line":7,"path":"src/a.go","symbol":"TestA"}]`
		a := proto1Input()
		b := proto1Input()
		a.Binding.FrameOrder = []string{"frame-a", "frame-b"}
		b.Binding.FrameOrder = []string{"frame-a", "frame-b"}
		a.Record = []byte(strings.Replace(proto1Golden, original, first, 1))
		b.Record = []byte(strings.Replace(proto1Golden, original, swapped, 1))
		x := proto1Canonical(t, a)
		y := proto1Canonical(t, b)
		if x.Key == y.Key || bytes.Equal(x.Canonical, y.Canonical) {
			t.Fatal("frame reordering aliased w8-repro/1 identity")
		}
		s := proto1NewSink()
		p := &proto1PublisherSpy{}
		ctx, cancel := context.WithCancel(context.Background())
		cancel()
		for _, in := range []harvest.ReproInput{a, b} {
			out, err := New().Canonical(ctx, in)
			if !errors.Is(err, context.Canceled) || out.Key != "" || len(out.Canonical) != 0 {
				t.Fatalf("cancelled canonicalization: %v %#v", err, out)
			}
		}
		if !s.empty() || !p.empty() {
			t.Fatal("cancellation performed sink, resource or send calls")
		}
		t.Log("MUST_FAIL ordered_frame_alias=refused (distinct digest+bytes); cancelled_inputs=2; sink_calls=0 publisher_calls=0")
	})
}

func ptrProto1(v int64) *int64 { return &v }

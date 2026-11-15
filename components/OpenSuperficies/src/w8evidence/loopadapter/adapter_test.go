package loopadapter

import (
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"
)

type testWorld struct {
	p                                                                  Proposal
	partial, origin, copied, current, governor, collector, certificate bool
	released, rollback, reserved                                       int
}

func (w *testWorld) Propose(_ string, _ []Obligation) (Proposal, error)   { return w.p, nil }
func (w *testWorld) Allow(_ Snapshot, _ Policy, _ Proposal) (bool, error) { return w.governor, nil }
func (w *testWorld) ReserveGang(_ string, p Proposal) (GangGrant, error) {
	w.reserved++
	ids := []string{}
	for _, t := range p.Targets {
		ids = append(ids, t.ID)
	}
	g := GangGrant{LeaseID: "lease-1", Epoch: 1, HeldIDs: ids, EscrowHeld: p.Escrow, Complete: true}
	if w.partial {
		g.HeldIDs = ids[:1]
		g.Complete = false
	}
	return g, nil
}
func (w *testWorld) Current(_ GangGrant) (bool, error)              { return w.current, nil }
func (w *testWorld) RollbackVerified(_ GangGrant, _ []string) error { w.rollback++; return nil }
func (w *testWorld) Release(_ GangGrant) error                      { w.released++; return nil }
func (w *testWorld) Verify(_ AttemptReceipt) (bool, error)          { return w.collector, nil }
func (w *testWorld) VerifyOrigin(_, _ string) (bool, error)         { return w.origin, nil }
func (w *testWorld) VerifyReceipt(_ AttemptReceipt) (bool, error)   { return !w.copied, nil }
func (w *testWorld) VerifySettlement(_ Settlement) (bool, error)    { return w.certificate, nil }

type certFake struct{ w *testWorld }

func (c certFake) Verify(s Settlement) (bool, error) { return c.w.VerifySettlement(s) }

type clockFake struct{ now int64 }

func (c clockFake) MonotonicNS() int64 { return c.now }
func rvec(r uint64) ResourceVector {
	return ResourceVector{RAMBytes: r, PIDs: 1, FDs: 1, CPUNumerator: 1, CPUDenominator: 1}
}
func setup(t *testing.T) (*Adapter, *testWorld, Snapshot, []Obligation) {
	t.Helper()
	w := &testWorld{origin: true, current: true, governor: true, collector: true, certificate: true}
	w.p = Proposal{Targets: []BootTarget{{ID: "worker-1", Resource: rvec(100)}, {ID: "worker-2", Resource: rvec(100)}}, Resources: ResourceVector{RAMBytes: 200, PIDs: 2, FDs: 2, CPUNumerator: 1, CPUDenominator: 1}, Escrow: 50}
	pol := Policy{MaxAgeNS: 100, MaxTailLatencyNS: 1000, MinDwellNS: 10, MaxPressure: 100, MaxWidth: 4, MaxRAMBytes: 1000, MaxPIDs: 20, MaxFDs: 20, MaxCPUNumerator: 4, MaxCPUDenominator: 1}
	s := Snapshot{SampleID: "sample-1", WorkloadCommit: "commit-1", AcquiredMonotonicNS: 995, CPUNumerator: 4, CPUDenominator: 1, RAMBytes: 1000, PIDs: 20, FDs: 20, AncestorLimits: ResourceVector{RAMBytes: 1000, PIDs: 20, FDs: 20, CPUNumerator: 4, CPUDenominator: 1}, PhysicalHeadroomBytes: 900, StartupBytes: 20, CollectorBytes: 20, ReactionReserveBytes: 20, Pressure: 2, TailLatencyNS: 100, DwellNS: 20, LastQualifiedWidth: 2}
	a, err := New(Ports{Auction: w, Governor: w, LeaseJournal: w, Collector: w, Certificate: certFake{w}, OriginVerifier: w, Clock: clockFake{1000}}, pol)
	if err != nil {
		t.Fatal(err)
	}
	obligations := []Obligation{{ID: "a", Weight: 1, State: "unresolved"}, {ID: "b", Weight: 999999, State: "unresolved"}}
	return a, w, s, obligations
}
func beginAndBoot(t *testing.T, a *Adapter, s Snapshot, os []Obligation) {
	t.Helper()
	if err := a.Begin("wave", os, s); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 2; i++ {
		if _, err := a.NextBoot(); err != nil {
			t.Fatal(err)
		}
	}
}
func receipt(id, status string) AttemptReceipt {
	return AttemptReceipt{ObligationID: id, TaskID: id, AttemptID: "attempt-" + id, LeaseID: "lease-1", Epoch: 1, ReceiptSHA256: Digest([]byte(id + status)), Status: status, IOConfirmed: true, Fresh: true, Paid: true}
}
func review(status string) Review {
	return Review{SettlementID: "settlement-1", BodySHA256: Digest([]byte("settlement-body")), Disposition: status, GatesVerified: true, CleanupVerified: true}
}
func TestPositiveFiniteSyntheticLoop(t *testing.T) {
	a, w, s, os := setup(t)
	beginAndBoot(t, a, s, os)
	if err := a.Record(receipt("a", "accepted")); err != nil {
		t.Fatal(err)
	}
	if err := a.Record(receipt("b", "accepted")); err != nil {
		t.Fatal(err)
	}
	out, err := a.Finish(review("success"))
	if err != nil {
		t.Fatal(err)
	}
	if out.Disposition != "success" || len(out.Attempts) != 2 || w.released != 1 || a.State() != "settled" {
		t.Fatalf("incorrect settlement: %+v", out)
	}
	replay, err := a.Finish(review("success"))
	if err != nil || replay.SettlementID != out.SettlementID || w.released != 1 {
		t.Fatalf("idempotence failed: %v", err)
	}
	// The return value cannot mutate the stored inventory.
	replay.Obligations[0].State = "failed"
	again, _ := a.Finish(review("success"))
	if again.Obligations[0].State != "accepted" {
		t.Fatal("mutable settlement")
	}
}
func TestMustFailFixtures(t *testing.T) {
	t.Run("partial gang launch", func(t *testing.T) {
		a, w, s, os := setup(t)
		w.partial = true
		err := a.Begin("wave", os, s)
		if !errors.Is(err, ErrQuarantine) || w.rollback != 1 || a.State() != "quarantined" {
			t.Fatalf("%v state=%s rollback=%d", err, a.State(), w.rollback)
		}
		if _, err := a.NextBoot(); err == nil {
			t.Fatal("partial reservation emitted permit")
		}
	})
	t.Run("stale sample", func(t *testing.T) {
		a, w, s, os := setup(t)
		s.AcquiredMonotonicNS = 1
		if err := a.Begin("wave", os, s); !errors.Is(err, ErrHold) || w.reserved != 0 {
			t.Fatalf("%v", err)
		}
	})
	t.Run("missing origin", func(t *testing.T) {
		a, w, s, os := setup(t)
		w.origin = false
		if err := a.Begin("wave", os, s); !errors.Is(err, ErrHold) || w.reserved != 0 {
			t.Fatalf("%v", err)
		}
	})
	t.Run("copied evidence", func(t *testing.T) {
		a, w, s, os := setup(t)
		w.copied = true
		beginAndBoot(t, a, s, os)
		if err := a.Record(receipt("a", "accepted")); !errors.Is(err, ErrHold) {
			t.Fatalf("%v", err)
		}
	})
	t.Run("failure relabeled success", func(t *testing.T) {
		a, _, s, os := setup(t)
		beginAndBoot(t, a, s, os)
		if err := a.Record(receipt("a", "failed")); err != nil {
			t.Fatal(err)
		}
		if err := a.Record(receipt("b", "accepted")); err != nil {
			t.Fatal(err)
		}
		if _, err := a.Finish(review("success")); !errors.Is(err, ErrRefused) {
			t.Fatalf("%v", err)
		}
		out, err := a.Finish(review("failure"))
		if err != nil || out.Disposition != "failure" {
			t.Fatalf("%v", err)
		}
	})
	t.Run("foreign obligation", func(t *testing.T) {
		a, _, s, os := setup(t)
		beginAndBoot(t, a, s, os)
		if err := a.Record(receipt("foreign", "accepted")); !errors.Is(err, ErrRefused) {
			t.Fatalf("%v", err)
		}
	})
	t.Run("residual hidden by rounding", func(t *testing.T) {
		a, _, s, os := setup(t)
		beginAndBoot(t, a, s, os)
		if err := a.Record(receipt("b", "accepted")); err != nil {
			t.Fatal(err)
		}
		if _, err := a.Finish(review("success")); !errors.Is(err, ErrHold) {
			t.Fatalf("%v", err)
		}
	})
	t.Run("changed replay", func(t *testing.T) {
		a, _, s, os := setup(t)
		beginAndBoot(t, a, s, os)
		if err := a.Record(receipt("a", "accepted")); err != nil {
			t.Fatal(err)
		}
		if err := a.Record(receipt("b", "accepted")); err != nil {
			t.Fatal(err)
		}
		if _, err := a.Finish(review("success")); err != nil {
			t.Fatal(err)
		}
		altered := review("success")
		altered.BodySHA256 = Digest([]byte("altered"))
		if _, err := a.Finish(altered); !errors.Is(err, ErrRefused) {
			t.Fatalf("%v", err)
		}
	})
	t.Run("release before cleanup", func(t *testing.T) {
		a, w, s, os := setup(t)
		beginAndBoot(t, a, s, os)
		if err := a.Record(receipt("a", "accepted")); err != nil {
			t.Fatal(err)
		}
		if err := a.Record(receipt("b", "accepted")); err != nil {
			t.Fatal(err)
		}
		r := review("success")
		r.CleanupVerified = false
		if _, err := a.Finish(r); !errors.Is(err, ErrHold) || w.released != 0 {
			t.Fatalf("%v release=%d", err, w.released)
		}
	})
}
func TestOtherFailClosedGuards(t *testing.T) {
	t.Run("invalid inventory", func(t *testing.T) {
		a, _, s, os := setup(t)
		os[1].ID = "a"
		if err := a.Begin("wave", os, s); !errors.Is(err, ErrRefused) {
			t.Fatal(err)
		}
	})
	t.Run("unclean stopping liability", func(t *testing.T) {
		a, _, s, os := setup(t)
		s.Entries = []Entry{{EntryID: "old", LeaseID: "old-lease", Generation: 1, Epoch: 1, State: "stopping", ReservedBytes: 850}}
		if err := a.Begin("wave", os, s); !errors.Is(err, ErrHold) {
			t.Fatal(err)
		}
	})
	t.Run("unknown measurements", func(t *testing.T) {
		a, _, s, os := setup(t)
		s.Unknown = map[string]string{"fds": "unavailable"}
		if err := a.Begin("wave", os, s); !errors.Is(err, ErrHold) {
			t.Fatal(err)
		}
	})
	t.Run("revoked authority", func(t *testing.T) {
		a, w, s, os := setup(t)
		if err := a.Begin("wave", os, s); err != nil {
			t.Fatal(err)
		}
		w.current = false
		if _, err := a.NextBoot(); !errors.Is(err, ErrQuarantine) || a.State() != "quarantined" {
			t.Fatal(err)
		}
	})
	t.Run("paid receipt replay mismatch", func(t *testing.T) {
		a, _, s, os := setup(t)
		beginAndBoot(t, a, s, os)
		r := receipt("a", "accepted")
		if err := a.Record(r); err != nil {
			t.Fatal(err)
		}
		r.ReceiptSHA256 = Digest([]byte("changed"))
		if err := a.Record(r); !errors.Is(err, ErrQuarantine) {
			t.Fatal(err)
		}
	})
}

type verifySidecar struct{ allow bool }

func (v verifySidecar) Verify(_ SidecarHeader, _ []byte) (bool, error) { return v.allow, nil }

type journalSidecar struct {
	frames []Frame
	stops  []Frame
	fail   bool
}

func (j *journalSidecar) Apply(f Frame) error {
	if j.fail {
		return fmt.Errorf("not durable")
	}
	j.frames = append(j.frames, f)
	return nil
}
func (j *journalSidecar) Stop(f Frame) error { j.stops = append(j.stops, f); return nil }
func frame(seq uint64) Frame {
	body, _ := json.Marshal(Speech{Schema: "os.cu.speech-act/v1", Act: "finding", WaveID: "wave", TaskID: "task", AttemptID: "attempt", LeaseID: "lease", Seq: seq, Body: json.RawMessage(`{"finding":true}`)})
	return Frame{Header: SidecarHeader{Sender: "worker", TaskID: "task", AttemptID: "attempt", LeaseID: "lease", Epoch: 1, Seq: seq, BodySHA256: Digest(body), BodyLength: uint64(len(body))}, Body: body}
}
func TestSidecarDurableGapReplayStop(t *testing.T) {
	j := &journalSidecar{}
	r, err := NewReceiver(verifySidecar{true}, j, 1, 1)
	if err != nil {
		t.Fatal(err)
	}
	if got, err := r.Submit(frame(2)); !errors.Is(err, ErrHold) || got.Ack != 0 {
		t.Fatalf("gap %v %+v", err, got)
	}
	if err := r.SubmitStop(frame(3)); err != nil || len(j.stops) != 1 {
		t.Fatalf("STOP channel blocked: %v", err)
	}
	j.fail = true
	if got, err := r.Submit(frame(1)); !errors.Is(err, ErrHold) || got.Ack != 0 {
		t.Fatalf("premature ack %v %+v", err, got)
	}
	j.fail = false
	if got, err := r.Submit(frame(1)); err != nil || got.Ack != 2 || len(j.frames) != 2 {
		t.Fatalf("contiguous %v %+v", err, got)
	}
	if got, err := r.Submit(frame(1)); err != nil || got.Status != "replayed" {
		t.Fatalf("idempotent replay %v %+v", err, got)
	}
	forged := frame(1)
	forged.Body = []byte(strings.ReplaceAll(string(forged.Body), "finding", "status"))
	forged.Header.BodySHA256 = Digest(forged.Body)
	forged.Header.BodyLength = uint64(len(forged.Body))
	if _, err := r.Submit(forged); !errors.Is(err, ErrQuarantine) || !r.Quarantined() {
		t.Fatalf("modified replay: %v", err)
	}
}
func TestSidecarRejectsForgedAndUnknownFields(t *testing.T) {
	j := &journalSidecar{}
	r, _ := NewReceiver(verifySidecar{true}, j, 2, 1)
	f := frame(1)
	f.Header.BodyLength++
	if _, err := r.Submit(f); !errors.Is(err, ErrRefused) {
		t.Fatal(err)
	}
	f = frame(1)
	f.Body = append(f.Body, []byte(` {}`)...)
	f.Header.BodyLength = uint64(len(f.Body))
	f.Header.BodySHA256 = Digest(f.Body)
	if _, err := r.Submit(f); !errors.Is(err, ErrRefused) {
		t.Fatal(err)
	}
	r, _ = NewReceiver(verifySidecar{false}, j, 2, 1)
	if _, err := r.Submit(frame(1)); !errors.Is(err, ErrHold) {
		t.Fatal(err)
	}
}

package auction

import (
	"errors"
	"testing"
)

func TestQueueGatesAndSingleCompletionLatch(t *testing.T) {
	s := Submitted
	for _, step := range []struct {
		kind string
		next State
	}{{"validate", Validated}, {"enqueue", Waiting}, {"eligible", Eligible}, {"reserve", Reserved}, {"launch", Launching}, {"start", Running}, {"finish", Collecting}, {"collect", Cleaning}} {
		next, err := Transition(s, Event{Expected: s, Kind: step.kind})
		if err != nil || next != step.next {
			t.Fatalf("%s --%s--> %s err %v", s, step.kind, next, err)
		}
		s = next
	}
	if next, err := Transition(s, Event{Expected: s, Kind: "settle", TotalMembers: 2, ProvenClean: 1}); !errors.Is(err, ErrDenied) || next != Cleaning {
		t.Fatal("unproven cleanup settled")
	}
	if next, err := Transition(s, Event{Expected: s, Kind: "settle", TotalMembers: 2, ProvenClean: 2, SuiteFailed: true, ReportSuccess: true}); err == nil || next != Cleaning {
		t.Fatal("failed suite marked success")
	}
	if next, err := Transition(s, Event{Expected: s, Kind: "settle", TotalMembers: 2, ProvenClean: 2, SuiteFailed: true}); err != nil || next != Settled {
		t.Fatalf("failed suite must settle as failure: %v %s", err, next)
	}
	if _, err := Transition(Settled, Event{Expected: Settled, Kind: "settle", TotalMembers: 2, ProvenClean: 2}); !errors.Is(err, ErrConflict) {
		t.Fatal("duplicate completion accepted")
	}
}
func TestSTOPCASQuarantineAndRevoke(t *testing.T) {
	for _, s := range []State{Submitted, Validated, Waiting, Eligible, Reserved, Launching, Running, Collecting, Cleaning, StartupFailed, Preempting, Indeterminate} {
		next, err := Transition(s, Event{Expected: s, Kind: "STOP"})
		if err != nil {
			t.Fatalf("STOP cannot interrupt %s: %v", s, err)
		}
		if s == Reserved && next != Preempting {
			t.Fatal("revoke prematurely freed reserved pack")
		}
	}
	s, err := Transition(Reserved, Event{Expected: Reserved, Kind: "revoke"})
	if err != nil || s != Preempting {
		t.Fatal("revocation must lead to cleanup", s, err)
	}
	if _, err := Transition(s, Event{Expected: s, Kind: "cleanup", TotalMembers: 2, ProvenClean: 1}); err == nil {
		t.Fatal("revoke intent freed resources without proof")
	}
	s, err = Transition(Indeterminate, Event{Expected: Indeterminate, Kind: "quarantine"})
	if err != nil || s != Quarantined {
		t.Fatal(err)
	}
	if _, err := Transition(s, Event{Expected: s, Kind: "settle", TotalMembers: 1, ProvenClean: 1, ReportSuccess: true}); err == nil {
		t.Fatal("quarantine became success")
	}
	if _, err := Transition(Validated, Event{Expected: Submitted, Kind: "eligible"}); !errors.Is(err, ErrConflict) {
		t.Fatal("CAS mismatch accepted")
	}
	if ExitCode(ErrInvalid) != 2 || ExitCode(ErrHold) != 4 || ExitCode(ErrConflict) != 5 || ExitCode(nil) != 0 {
		t.Fatal("W8 forbidden exit code normalization")
	}
}

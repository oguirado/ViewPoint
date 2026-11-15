package auction

import (
	"errors"
	"fmt"
)

// State is the complete, closed VMA auction queue state set.
type State string

const (
	Submitted     State = "submitted"
	Validated     State = "validated"
	Waiting       State = "waiting"
	Eligible      State = "eligible"
	Reserved      State = "reserved"
	Launching     State = "launching"
	Running       State = "running"
	Collecting    State = "collecting"
	Cleaning      State = "cleaning"
	Settled       State = "settled"
	Rejected      State = "rejected"
	BidExpired    State = "bid-expired"
	Cancelled     State = "cancelled"
	StartupFailed State = "startup-failed"
	Preempting    State = "preempting"
	Quarantined   State = "quarantined"
	Indeterminate State = "indeterminate"
)

// Event describes a compare-and-swap transition. The caller must durably
// record lifecycle changes in the EXISTING host journal, not in this package.
// ProvenClean is a count backed by host cleanup evidence, never an intention.
type Event struct {
	Expected      State  `json:"expected"`
	Kind          string `json:"kind"`
	TotalMembers  uint64 `json:"total_members"`
	ProvenClean   uint64 `json:"proven_clean"`
	SuiteFailed   bool   `json:"suite_failed"`
	ReportSuccess bool   `json:"report_success"`
}

func knownState(s State) bool {
	switch s {
	case Submitted, Validated, Waiting, Eligible, Reserved, Launching, Running, Collecting, Cleaning, Settled, Rejected, BidExpired, Cancelled, StartupFailed, Preempting, Quarantined, Indeterminate:
		return true
	}
	return false
}
func terminal(s State) bool {
	return s == Settled || s == Rejected || s == BidExpired || s == Cancelled || s == Quarantined
}

var transitions = map[State]map[string]State{
	Submitted:     {"validate": Validated, "reject": Rejected, "expire": BidExpired},
	Validated:     {"enqueue": Waiting, "eligible": Eligible, "reject": Rejected, "expire": BidExpired},
	Waiting:       {"eligible": Eligible, "reject": Rejected, "expire": BidExpired},
	Eligible:      {"wait": Waiting, "reserve": Reserved, "reject": Rejected, "expire": BidExpired},
	Reserved:      {"launch": Launching, "revoke": Preempting, "fail": StartupFailed},
	Launching:     {"start": Running, "fail": StartupFailed, "revoke": Preempting},
	Running:       {"finish": Collecting, "revoke": Preempting, "uncertain": Indeterminate},
	Collecting:    {"collect": Cleaning, "uncertain": Indeterminate},
	Cleaning:      {"settle": Settled, "uncertain": Indeterminate},
	StartupFailed: {"cleanup": Cleaning, "uncertain": Indeterminate},
	Preempting:    {"cleanup": Cleaning, "uncertain": Indeterminate},
	Indeterminate: {"quarantine": Quarantined},
}

// Transition is a pure map + CAS gate. STOP has priority over all ordinary
// transitions. Once reserved, revoke/STOP do NOT release reservations: they
// pass through cleanup. Settled is a single terminal completion latch.
func Transition(cur State, ev Event) (State, error) {
	if !knownState(cur) || !knownState(ev.Expected) || ev.Expected != cur {
		return cur, fmt.Errorf("%w: CAS mismatch or unknown state", ErrConflict)
	}
	if terminal(cur) {
		return cur, fmt.Errorf("%w: completion latch already closed", ErrConflict)
	}
	if ev.ReportSuccess && ev.SuiteFailed {
		return cur, fmt.Errorf("%w: failed suite cannot report success", ErrInvalid)
	}
	if ev.Kind == "STOP" {
		switch cur {
		case Submitted, Validated, Waiting, Eligible:
			return Cancelled, nil
		case Indeterminate:
			return Quarantined, nil
		default:
			return Preempting, nil
		}
	}
	next, ok := transitions[cur][ev.Kind]
	if !ok {
		return cur, fmt.Errorf("%w: illegal state transition", ErrInvalid)
	}
	if next == Settled {
		if ev.TotalMembers == 0 || ev.ProvenClean != ev.TotalMembers {
			return cur, fmt.Errorf("%w: incomplete proven cleanup", ErrDenied)
		}
		if ev.SuiteFailed && ev.ReportSuccess {
			return cur, ErrInvalid
		}
	}
	// Cleanup cannot rely on a revoke intent for proof of release.
	if ev.Kind == "cleanup" && (cur == Preempting || cur == StartupFailed) && (ev.TotalMembers == 0 || ev.ProvenClean != ev.TotalMembers) {
		return cur, ErrDenied
	}
	return next, nil
}

// ExitCode normalizes all package refusals to W8's permitted status range.
func isError(err, target error) bool { return errors.Is(err, target) }
func ExitCode(err error) int {
	switch {
	case err == nil:
		return 0
	case isError(err, ErrInvalid):
		return 2
	case isError(err, ErrHold):
		return 4
	case isError(err, ErrAbstain), isError(err, ErrDenied), isError(err, ErrConflict), isError(err, ErrOverflow):
		return 5
	default:
		return 5
	}
}

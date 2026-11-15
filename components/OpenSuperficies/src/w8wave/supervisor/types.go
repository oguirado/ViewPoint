// Package supervisor coordinates W8 waves through an injected B3 seat adapter.
// It never launches a process, owns a capsule, or performs janitor duties.
package supervisor

import (
	"context"
	"errors"
	"fmt"
	"time"

	"opensuperficies.local/srf/src/orchestratorbook/wal"
)

// MaxAttempts is a TARGET cap for one durable chain, including the original.
const MaxAttempts = 3

var (
	ErrSeatDown        = errors.New("w8 supervisor: seat is unavailable")
	ErrInvalidIdentity = errors.New("w8 supervisor: invalid bound identity")
	ErrLedgerConflict  = errors.New("w8 supervisor: inconsistent WAL identity or history")
	ErrInvalidRequeue  = errors.New("w8 supervisor: operator requeue requires an auditable event")
)

// Identity binds attempts to their immutable workload, not their renewable lease.
// Digests must be lowercase SHA-256 and Commit a lowercase Git object ID.
type Identity struct {
	TaskDigest                string
	Commit                    string
	GateArgvDigest            string
	EnvironmentManifestDigest string
}

type Work struct {
	Identity Identity
	LeaseID  string
}

// Attempt is an immutable, per-dispatch identity. A fresh lease cannot recycle it.
type Attempt struct {
	Identity       Identity
	IdentityDigest string
	ChainID        string
	AttemptID      string
	Ordinal        int
	LeaseID        string
}

// Ticket is the seat's accepted, attempt-bound handle.
type Ticket struct {
	AttemptID string
	Handle    string
}

// FaultClass is established by the host, never inferred merely from an RC.
type FaultClass string

const (
	FaultInfrastructure FaultClass = "infrastructure"
	FaultGate           FaultClass = "deterministic_gate"
	FaultInput          FaultClass = "invalid_input"
	FaultContainment    FaultClass = "containment"
	FaultOperator       FaultClass = "operator_refusal"
	FaultUnknown        FaultClass = "unknown"
)

// SubmitReply reports admission by the B3 seat. Code 0 requires a bound ticket.
// Refusals have no ticket; Code 6 and unacknowledged socket errors are uncertain.
type SubmitReply struct {
	AttemptID        string
	Ticket           Ticket
	Code             int
	ExecutionStarted *bool
	Fault            FaultClass
	CleanupVerified  bool
}

type WaitReply struct {
	AttemptID        string
	Code             int
	ExecutionStarted *bool
	Fault            FaultClass
	CleanupVerified  bool
}

type CollectReply struct {
	AttemptID       string
	Evidence        *wal.EvidenciaExitoW8
	CleanupVerified bool
	Fault           FaultClass
}

// Submitter owns all seat interactions. The supervisor has no launch capability.
// Wait and Collect are read/reconciliation operations, never submissions.
type Submitter interface {
	Submit(context.Context, Attempt) (SubmitReply, error)
	Wait(context.Context, Ticket) (WaitReply, error)
	Collect(context.Context, Ticket) (CollectReply, error)
}

// ReadFailure allows a rest-for-one restart of an idempotent read child only.
// A general network error is NOT automatically a ReadFailure.
type ReadFailure struct {
	Err error
}

func (f *ReadFailure) Error() string {
	return fmt.Sprintf("w8 supervisor: read child interrupted: %v", f.Err)
}
func (f *ReadFailure) Unwrap() error { return f.Err }

// Config bounds restart intensity. MaxRestarts counts stage restarts and
// clean infrastructure retries in the rolling RestartPeriod, across WAL replay.
type Config struct {
	MaxRestarts   int
	RestartPeriod time.Duration
	Now           func() time.Time
}

// Result is the current durable chain state. Escalated stops its parent wave.
type Result struct {
	IdentityDigest string
	ChainID        string
	AttemptID      string
	Attempts       int
	Code           int
	Outcome        wal.SrfDesenlace
	Escalated      bool
	Reason         string
}

func (r Result) uncertain(reason string) Result {
	r.Code = 6
	r.Outcome = wal.DesenlaceEnDuda
	r.Reason = reason
	return r
}

// OperatorRequeue is an explicit, audit-only action creating a new chain.
// EventID must be unique and cannot be reused with different identity fields.
type OperatorRequeue struct {
	Identity Identity
	EventID  string
	Operator string
	Reason   string
}

func validW8Code(rc int) bool { return rc == 0 || (rc >= 2 && rc <= 7) }

func boolp(b bool) *bool { return &b }

package supervisor

import (
	"context"
	"errors"
	"sync"
	"time"

	"opensuperficies.local/srf/src/orchestratorbook/wal"
)

// Supervisor owns the wave/attempt supervision tree; it has no process launcher.
// The WAL's exclusive lock fences other processes; the path mutex fences
// independent Supervisor instances sharing one LibroConsumo within a process.
type Supervisor struct {
	book      *wal.LibroConsumo
	submitter Submitter
	config    Config
	mu        sync.Mutex
}

func New(book *wal.LibroConsumo, submitter Submitter, config Config) (*Supervisor, error) {
	if book == nil || submitter == nil || config.MaxRestarts < 0 || config.RestartPeriod < 0 {
		return nil, errors.New("w8 supervisor: invalid dependencies or policy")
	}
	if config.RestartPeriod == 0 {
		config.RestartPeriod = time.Minute
	}
	if config.Now == nil {
		config.Now = time.Now
	}
	return &Supervisor{book: book, submitter: submitter, config: config}, nil
}

// Run advances at most one bound chain. It never re-submits a persisted intent.
// An explicit Requeue is the only way to create a fresh chain after a terminal
// or ambiguous result; a new lease does not change chain identity or quota.
func (s *Supervisor) Run(ctx context.Context, w Work) (Result, error) {
	digest, err := identityHash(w.Identity)
	if err != nil || w.LeaseID == "" {
		return Result{Code: 2, Outcome: wal.DesenlaceNoEnviado, Reason: "invalid_bound_input"}, nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	lock := ledgerLock(s.book.Path())
	lock.Lock()
	defer lock.Unlock()

	h, err := s.readChain(digest)
	if err != nil {
		return Result{IdentityDigest: digest, Code: 6, Outcome: wal.DesenlaceEnDuda}, err
	}
	if h.halted {
		r := lastResult(h).uncertain("wave_halted_after_restart_escalation")
		r.Escalated = true
		return r, nil
	}
	if len(h.attempts) > 0 {
		last := h.attempts[len(h.attempts)-1]
		if last.outcome == nil || !last.ended {
			// Crash replay never repeats submission. An open dispatch is EN_DUDA.
			wasOpen := last.outcome == nil
			if err := s.finishOpen(last); err != nil {
				return lastResult(h).uncertain("replay_finalize_failed"), err
			}
			h, err = s.readChain(digest)
			if err != nil {
				return Result{Code: 6, Outcome: wal.DesenlaceEnDuda}, err
			}
			if wasOpen {
				return lastResult(h).uncertain("open_intention_replayed_without_submit"), nil
			}
		}
		if !retryableTail(h) {
			return lastResult(h), nil
		}
	}

	for len(h.attempts) < MaxAttempts {
		if err := ctx.Err(); err != nil {
			return lastResult(h).uncertain("wave_context_cancelled"), nil
		}
		if len(h.attempts) > 0 {
			ok, err := s.reserveRestart(h.chainID, h.attempts[len(h.attempts)-1].mark.AttemptID, "next_clean_attempt")
			if err != nil {
				return lastResult(h).uncertain("restart_audit_failed"), err
			}
			if !ok {
				r := lastResult(h).uncertain("restart_intensity_exceeded")
				r.Escalated = true
				return r, nil
			}
		}
		ordinal := len(h.attempts) + 1
		a := Attempt{Identity: w.Identity, IdentityDigest: digest, ChainID: h.chainID,
			AttemptID: attemptKey(h.chainID, ordinal), Ordinal: ordinal, LeaseID: w.LeaseID}
		if err := s.beginAttempt(a); err != nil {
			return lastResult(h).uncertain("durable_intent_failed"), err
		}
		result, started, class, cleanup, retry, escalated := s.executeAttempt(ctx, a)
		result.IdentityDigest = digest
		result.ChainID = h.chainID
		result.AttemptID = a.AttemptID
		result.Attempts = ordinal
		result.Escalated = escalated
		if err := s.endAttempt(a, result, started, class, cleanup, retry); err != nil {
			return result.uncertain("durable_outcome_failed"), err
		}
		if escalated {
			// Escalation itself must survive process restart, not just the outcome.
			if err := s.appendLocal(audit{Kind: markHalt, IdentityDigest: digest, ChainID: h.chainID,
				AttemptID: a.AttemptID, Reason: "restart_intensity_exceeded"}); err != nil {
				return result.uncertain("durable_halt_failed"), err
			}
			return result.uncertain("restart_intensity_exceeded"), nil
		}
		if !retry || ordinal == MaxAttempts {
			return result, nil
		}
		h, err = s.readChain(digest)
		if err != nil {
			return result.uncertain("history_replay_failed"), err
		}
	}
	return lastResult(h), nil
}

// RunWave is the parent: individual task chains never restart a sibling.
// If restart intensity escalates, the wave stops and leaves all unstarted
// siblings in EN_DUDA; no additional seat submission follows escalation.
func (s *Supervisor) RunWave(ctx context.Context, tasks []Work) ([]Result, error) {
	results := make([]Result, 0, len(tasks))
	for _, w := range tasks {
		r, err := s.Run(ctx, w)
		results = append(results, r)
		if err != nil {
			return results, err
		}
		if r.Escalated {
			for len(results) < len(tasks) {
				results = append(results, Result{Code: 6, Outcome: wal.DesenlaceEnDuda,
					Escalated: true, Reason: "parent_wave_stopped"})
			}
			break
		}
	}
	return results, nil
}

// executeAttempt is a rest-for-one pipeline: only the idempotent wait/collect
// children may restart inside the current attempt. A failed submit never
// repeats under the same attempt ID; a clean infra fault needs a fresh intent.
func (s *Supervisor) executeAttempt(ctx context.Context, a Attempt) (Result, *bool, FaultClass, bool, bool, bool) {
	unknown := func(why string) (Result, *bool, FaultClass, bool, bool, bool) {
		return Result{Code: 6, Outcome: wal.DesenlaceEnDuda, Reason: why}, nil, FaultUnknown, false, false, false
	}

	accepted, err := s.submitter.Submit(ctx, a)
	if err != nil {
		if errors.Is(err, ErrSeatDown) {
			return unknown("seat_daemon_off_no_submit_ack")
		}
		return unknown("unacknowledged_submit_boundary")
	}
	if accepted.AttemptID != a.AttemptID || !validW8Code(accepted.Code) {
		return unknown("unbound_or_invalid_submit_reply")
	}
	if accepted.Code != 0 {
		outcome := wal.ResolverDesenlaceW8(accepted.Code, accepted.ExecutionStarted, a.AttemptID, nil)
		r := Result{Code: accepted.Code, Outcome: outcome, Reason: "seat_refused_before_ticket"}
		retry := canRetry(accepted.Code, outcome, accepted.ExecutionStarted, accepted.Fault, accepted.CleanupVerified)
		return r, accepted.ExecutionStarted, accepted.Fault, accepted.CleanupVerified, retry, false
	}
	if accepted.Ticket.AttemptID != a.AttemptID || accepted.Ticket.Handle == "" {
		return unknown("seat_ticket_identity_mismatch")
	}

	var observed WaitReply
	for {
		observed, err = s.submitter.Wait(ctx, accepted.Ticket)
		if err == nil {
			break
		}
		if !isReadFailure(err) || ctx.Err() != nil {
			return unknown("wait_boundary_unknown")
		}
		allowed, auditErr := s.reserveRestart(a.ChainID, a.AttemptID, "wait_then_collect")
		if auditErr != nil {
			return unknown("restart_audit_failure")
		}
		if !allowed {
			r, started, cls, cleaned, retry, _ := unknown("restart_intensity_exceeded")
			return r, started, cls, cleaned, retry, true
		}
	}
	if observed.AttemptID != a.AttemptID || !validW8Code(observed.Code) || observed.ExecutionStarted == nil {
		return unknown("unbound_or_invalid_wait_reply")
	}
	if observed.Code == 6 {
		return Result{Code: 6, Outcome: wal.DesenlaceEnDuda, Reason: "quarantined_or_indeterminate"},
			observed.ExecutionStarted, observed.Fault, false, false, false
	}

	var collected CollectReply
	for {
		collected, err = s.submitter.Collect(ctx, accepted.Ticket)
		if err == nil {
			break
		}
		if !isReadFailure(err) || ctx.Err() != nil {
			return unknown("collect_boundary_unknown")
		}
		allowed, auditErr := s.reserveRestart(a.ChainID, a.AttemptID, "collect")
		if auditErr != nil {
			return unknown("restart_audit_failure")
		}
		if !allowed {
			r, started, cls, cleaned, retry, _ := unknown("restart_intensity_exceeded")
			return r, started, cls, cleaned, retry, true
		}
	}
	if collected.AttemptID != a.AttemptID ||
		(collected.Evidence != nil && collected.Evidence.AttemptID != a.AttemptID) {
		return unknown("collector_recycled_attempt_identity")
	}
	outcome := wal.ResolverDesenlaceW8(observed.Code, observed.ExecutionStarted, a.AttemptID, collected.Evidence)
	if observed.Code == 0 && !collected.CleanupVerified {
		outcome = wal.DesenlaceEnDuda
	}
	clean := observed.CleanupVerified && collected.CleanupVerified
	retry := canRetry(observed.Code, outcome, observed.ExecutionStarted, observed.Fault, clean)
	return Result{Code: observed.Code, Outcome: outcome, Reason: "host_collected"},
		observed.ExecutionStarted, observed.Fault, clean, retry, false
}

func canRetry(rc int, outcome wal.SrfDesenlace, started *bool, class FaultClass, cleanup bool) bool {
	return (rc == 3 || rc == 4) && outcome == wal.DesenlaceNoEnviado && started != nil && !*started &&
		class == FaultInfrastructure && cleanup
}

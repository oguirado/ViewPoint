package supervisor

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"sort"
	"strings"
	"sync"

	"opensuperficies.local/srf/src/orchestratorbook/wal"
)

const (
	markAttempt = "w8_supervisor_attempt_v1"
	markRequeue = "w8_supervisor_operator_requeue_v1"
	markRestart = "w8_supervisor_restart_v1"
	markHalt    = "w8_supervisor_halt_v1"
)

var (
	operatorEvent = regexp.MustCompile(`^[A-Za-z0-9_.:/-]{1,160}$`)
	ledgerLocks   sync.Map // WAL file path -> *sync.Mutex, shared by all supervisors in-process
)

func ledgerLock(path string) *sync.Mutex {
	v, _ := ledgerLocks.LoadOrStore(path, &sync.Mutex{})
	return v.(*sync.Mutex)
}

// One JSON object format across intent, outcome, restart, halt and requeue.
// IdentityDigest, ChainID and AttemptID are cryptographic join keys, not leases.
type audit struct {
	Kind             string     `json:"kind"`
	IdentityDigest   string     `json:"identity_digest,omitempty"`
	ChainID          string     `json:"chain_id,omitempty"`
	PreviousChainID  string     `json:"previous_chain_id,omitempty"`
	AttemptID        string     `json:"attempt_id,omitempty"`
	Ordinal          int        `json:"ordinal,omitempty"`
	LeaseID          string     `json:"lease_id,omitempty"`
	EventID          string     `json:"event_id,omitempty"`
	Operator         string     `json:"operator,omitempty"`
	Reason           string     `json:"reason,omitempty"`
	Fault            FaultClass `json:"fault,omitempty"`
	CleanupVerified  bool       `json:"cleanup_verified,omitempty"`
	ExecutionStarted *bool      `json:"execution_started,omitempty"`
	Retryable        bool       `json:"retryable,omitempty"`
	Code             int        `json:"code"`
}

type attemptHistory struct {
	mark     audit
	outcome  *wal.Registro
	decision audit
	ended    bool
}

type chainHistory struct {
	identityDigest string
	chainID        string
	attempts       []*attemptHistory
	halted         bool
}

func isLowerHex(s string, n int) bool {
	if len(s) != n {
		return false
	}
	for _, c := range s {
		if (c < '0' || c > '9') && (c < 'a' || c > 'f') {
			return false
		}
	}
	return true
}

func identityHash(id Identity) (string, error) {
	if !isLowerHex(id.TaskDigest, 64) || !(isLowerHex(id.Commit, 40) || isLowerHex(id.Commit, 64)) ||
		!isLowerHex(id.GateArgvDigest, 64) || !isLowerHex(id.EnvironmentManifestDigest, 64) {
		return "", ErrInvalidIdentity
	}
	return hashParts("w8-identity-v1", id.TaskDigest, id.Commit, id.GateArgvDigest, id.EnvironmentManifestDigest), nil
}

func hashParts(parts ...string) string {
	b, _ := json.Marshal(parts) // []string has no possibility of marshal failure
	h := sha256.Sum256(b)
	return hex.EncodeToString(h[:])
}

func rootChain(identityDigest string) string {
	return hashParts("w8-chain-v1", identityDigest, "initial")
}

func namedChain(identityDigest, previous, eventID, operator string) string {
	return hashParts("w8-chain-v1", identityDigest, previous, eventID, operator)
}

func attemptKey(chain string, ordinal int) string {
	return hashParts("w8-attempt-v1", chain, fmt.Sprint(ordinal))
}

func decodeAudit(raw json.RawMessage) (audit, error) {
	var a audit
	if len(raw) == 0 {
		return a, nil
	}
	if err := json.Unmarshal(raw, &a); err != nil {
		return audit{}, fmt.Errorf("decode supervisor WAL evidence: %w", err)
	}
	return a, nil
}

func readChain(regs []wal.Registro, identityDigest string) (chainHistory, error) {
	h := chainHistory{identityDigest: identityDigest, chainID: rootChain(identityDigest)}
	allEvents := make(map[string]audit)
	chainTips := make(map[string]string)
	chainOrigin := make(map[string]string)
	chainBirth := make(map[string]uint64)
	// A new chain exists only after an explicit, durable operator audit record.
	for _, rec := range regs {
		if rec.Tipo != wal.TipoReserva {
			continue
		}
		a, err := decodeAudit(rec.Detalles)
		if err != nil {
			return h, err
		}
		if a.Kind != markRequeue {
			continue
		}
		if !operatorEvent.MatchString(a.EventID) || strings.TrimSpace(a.Operator) == "" || strings.TrimSpace(a.Reason) == "" || !isLowerHex(a.IdentityDigest, 64) {
			return h, ErrLedgerConflict
		}
		if old, ok := allEvents[a.EventID]; ok {
			if old.IdentityDigest != a.IdentityDigest || old.ChainID != a.ChainID {
				return h, ErrLedgerConflict
			}
			return h, ErrLedgerConflict // a committed event cannot occur twice
		}
		allEvents[a.EventID] = a
		previous := chainTips[a.IdentityDigest]
		if previous == "" {
			previous = rootChain(a.IdentityDigest)
		}
		if a.PreviousChainID != previous || a.ChainID != namedChain(a.IdentityDigest, previous, a.EventID, a.Operator) {
			return h, ErrLedgerConflict
		}
		chainTips[a.IdentityDigest] = a.ChainID
		chainOrigin[a.ChainID] = a.IdentityDigest
		chainBirth[a.ChainID] = rec.LSN
		if a.IdentityDigest == identityDigest {
			h.chainID = a.ChainID
		}
	}

	byKey := make(map[string]*attemptHistory)
	byOrdinal := make(map[int]*attemptHistory)
	for _, rec := range regs {
		if rec.Tipo != wal.TipoIntencion {
			continue
		}
		a, err := decodeAudit(rec.Detalles)
		if err != nil {
			return h, err
		}
		if a.Kind != markAttempt {
			continue
		}
		if !isLowerHex(a.IdentityDigest, 64) || !isLowerHex(a.ChainID, 64) || a.Ordinal < 1 || a.Ordinal > MaxAttempts ||
			a.AttemptID != attemptKey(a.ChainID, a.Ordinal) || rec.Clave != a.AttemptID || rec.TxID != "w8:"+a.AttemptID || a.LeaseID == "" {
			return h, ErrLedgerConflict
		}
		if a.ChainID != rootChain(a.IdentityDigest) {
			if chainOrigin[a.ChainID] != a.IdentityDigest || chainBirth[a.ChainID] >= rec.LSN {
				return h, ErrLedgerConflict
			}
		}
		if a.ChainID == h.chainID && a.IdentityDigest != identityDigest {
			return h, ErrLedgerConflict
		}
		if _, exists := byKey[a.AttemptID]; exists {
			return h, ErrLedgerConflict
		}
		hist := &attemptHistory{mark: a}
		byKey[a.AttemptID] = hist
		if a.IdentityDigest == identityDigest && a.ChainID == h.chainID {
			if _, exists := byOrdinal[a.Ordinal]; exists {
				return h, ErrLedgerConflict
			}
			byOrdinal[a.Ordinal] = hist
		}
	}

	for _, rec := range regs {
		hist := byKey[rec.Clave]
		if hist != nil && rec.Tipo == wal.TipoDesenlace {
			if hist.outcome != nil || rec.TxID != "w8:"+rec.Clave || rec.RC == nil || !validW8Code(*rec.RC) {
				return h, ErrLedgerConflict
			}
			a, err := decodeAudit(rec.Detalles)
			if err != nil {
				return h, err
			}
			if a.Kind == "" && rec.Desenlace == wal.DesenlaceEnDuda {
				// Existing three-pass WAL recovery may close an open intention.
				a = audit{Kind: markAttempt, IdentityDigest: hist.mark.IdentityDigest,
					ChainID: hist.mark.ChainID, AttemptID: hist.mark.AttemptID,
					Ordinal: hist.mark.Ordinal, Code: 6, Reason: "recovered_uncertain"}
			} else if a.Kind != markAttempt || a.IdentityDigest != hist.mark.IdentityDigest ||
				a.ChainID != hist.mark.ChainID || a.AttemptID != hist.mark.AttemptID || a.Ordinal != hist.mark.Ordinal || a.Code != *rec.RC {
				return h, ErrLedgerConflict
			}
			outcome := wal.ResolverDesenlaceW8(a.Code, a.ExecutionStarted, a.AttemptID, nil)
			if a.Code == 0 {
				// Success evidence is not reconstructed from untrusted WAL details;
				// the checked outcome itself is authoritative after durable commit.
				outcome = wal.DesenlaceConfirmado
			}
			if rec.Desenlace != outcome && !(rec.Desenlace == wal.DesenlaceEnDuda && a.Code == 0) &&
				!(rec.Desenlace == wal.DesenlaceEnviadoParcial && a.Code == 3 && a.ExecutionStarted != nil && *a.ExecutionStarted) &&
				!(rec.Desenlace == wal.DesenlaceEnviadoParcial && a.Code == 5 && a.ExecutionStarted != nil && *a.ExecutionStarted) {
				return h, ErrLedgerConflict
			}
			if a.Retryable && (rec.Desenlace != wal.DesenlaceNoEnviado || a.Fault != FaultInfrastructure || !a.CleanupVerified ||
				a.ExecutionStarted == nil || *a.ExecutionStarted || (a.Code != 3 && a.Code != 4)) {
				return h, ErrLedgerConflict
			}
			copyRec := rec
			hist.outcome = &copyRec
			hist.decision = a
		}
		if rec.Tipo == wal.TipoEnd && strings.HasPrefix(rec.TxID, "w8:") {
			if hist := byKey[strings.TrimPrefix(rec.TxID, "w8:")]; hist != nil {
				hist.ended = true
			}
		}
	}
	for _, rec := range regs {
		if rec.Tipo != wal.TipoReserva {
			continue
		}
		a, err := decodeAudit(rec.Detalles)
		if err != nil {
			return h, err
		}
		if a.Kind == markHalt && a.ChainID == h.chainID {
			h.halted = true
		}
	}
	for ordinal, hist := range byOrdinal {
		if ordinal < 1 || ordinal > MaxAttempts {
			return h, ErrLedgerConflict
		}
		h.attempts = append(h.attempts, hist)
	}
	sort.Slice(h.attempts, func(i, j int) bool { return h.attempts[i].mark.Ordinal < h.attempts[j].mark.Ordinal })
	for i, hist := range h.attempts {
		if hist.mark.Ordinal != i+1 {
			return h, ErrLedgerConflict
		}
	}
	return h, nil
}

func (s *Supervisor) readChain(identityDigest string) (chainHistory, error) {
	recs, err := wal.LeerRegistros(s.book.Path())
	if err != nil {
		return chainHistory{}, err
	}
	return readChain(recs, identityDigest)
}

func (s *Supervisor) beginAttempt(a Attempt) error {
	mark := audit{Kind: markAttempt, IdentityDigest: a.IdentityDigest, ChainID: a.ChainID,
		AttemptID: a.AttemptID, Ordinal: a.Ordinal, LeaseID: a.LeaseID}
	if _, err := s.book.EscribirBegin("w8:" + a.AttemptID); err != nil {
		return err
	}
	// This EXTERNA intention is fsynced before the Submitter is invoked.
	_, err := s.book.EscribirIntencion("w8:"+a.AttemptID, a.AttemptID, mark)
	return err
}

func (s *Supervisor) endAttempt(a Attempt, r Result, started *bool, class FaultClass, cleanup, retryable bool) error {
	mark := audit{Kind: markAttempt, IdentityDigest: a.IdentityDigest, ChainID: a.ChainID,
		AttemptID: a.AttemptID, Ordinal: a.Ordinal, Code: r.Code, Reason: r.Reason,
		Fault: class, CleanupVerified: cleanup, ExecutionStarted: started, Retryable: retryable}
	data, err := json.Marshal(mark)
	if err != nil {
		return err
	}
	rc := r.Code
	_, err = s.book.Escribir(&wal.Registro{TxID: "w8:" + a.AttemptID, Clase: wal.ClaseExterna,
		Tipo: wal.TipoDesenlace, Clave: a.AttemptID, RC: &rc, Desenlace: r.Outcome, Detalles: data})
	if err != nil {
		return err
	}
	_, err = s.book.EscribirEnd("w8:" + a.AttemptID)
	return err
}

func (s *Supervisor) finishOpen(hist *attemptHistory) error {
	if hist.outcome == nil {
		a := hist.mark
		result := Result{Code: 6, Outcome: wal.DesenlaceEnDuda, Reason: "unreconciled_intention_on_replay"}
		return s.endAttempt(Attempt{IdentityDigest: a.IdentityDigest, ChainID: a.ChainID,
			AttemptID: a.AttemptID, Ordinal: a.Ordinal}, result, nil, FaultUnknown, false, false)
	}
	if !hist.ended {
		_, err := s.book.EscribirEnd("w8:" + hist.mark.AttemptID)
		return err
	}
	return nil
}

func (s *Supervisor) appendLocal(a audit) error {
	data, err := json.Marshal(a)
	if err != nil {
		return err
	}
	if _, err = s.book.Escribir(&wal.Registro{Clase: wal.ClaseLocal, Tipo: wal.TipoReserva, Detalles: data}); err != nil {
		return err
	}
	return s.book.Sync() // Local audit records must survive process restart too.
}

func (s *Supervisor) reserveRestart(chainID, attemptID, stage string) (bool, error) {
	recs, err := wal.LeerRegistros(s.book.Path())
	if err != nil {
		return false, err
	}
	limit := s.config.MaxRestarts
	cutoff := s.config.Now().Add(-s.config.RestartPeriod)
	count := 0
	for _, r := range recs {
		if r.Tipo != wal.TipoReserva || r.Timestamp.Before(cutoff) {
			continue
		}
		a, e := decodeAudit(r.Detalles)
		if e != nil {
			return false, e
		}
		if a.Kind == markRestart && a.ChainID == chainID {
			count++
		}
	}
	if count >= limit {
		if err := s.appendLocal(audit{Kind: markHalt, ChainID: chainID, AttemptID: attemptID, Reason: "restart_intensity_exceeded"}); err != nil {
			return false, err
		}
		return false, nil
	}
	if err := s.appendLocal(audit{Kind: markRestart, ChainID: chainID, AttemptID: attemptID, Reason: stage}); err != nil {
		return false, err
	}
	return true, nil
}

// Requeue creates one new auditable chain, without erasing any historical intent.
// A repeated operator event is idempotent; it never mints a second chain.
func (s *Supervisor) Requeue(req OperatorRequeue) (string, error) {
	identityDigest, err := identityHash(req.Identity)
	if err != nil {
		return "", err
	}
	if !operatorEvent.MatchString(req.EventID) || strings.TrimSpace(req.Operator) == "" || strings.TrimSpace(req.Reason) == "" {
		return "", ErrInvalidRequeue
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	lock := ledgerLock(s.book.Path())
	lock.Lock()
	defer lock.Unlock()
	recs, err := wal.LeerRegistros(s.book.Path())
	if err != nil {
		return "", err
	}
	for _, r := range recs {
		if r.Tipo != wal.TipoReserva {
			continue
		}
		a, e := decodeAudit(r.Detalles)
		if e != nil {
			return "", e
		}
		if a.Kind == markRequeue && a.EventID == req.EventID {
			if a.IdentityDigest != identityDigest || a.Operator != req.Operator || a.Reason != req.Reason {
				return "", ErrLedgerConflict
			}
			return a.ChainID, nil
		}
	}
	h, err := readChain(recs, identityDigest)
	if err != nil {
		return "", err
	}
	chain := namedChain(identityDigest, h.chainID, req.EventID, req.Operator)
	if err := s.appendLocal(audit{Kind: markRequeue, IdentityDigest: identityDigest, ChainID: chain,
		PreviousChainID: h.chainID, EventID: req.EventID, Operator: req.Operator, Reason: req.Reason}); err != nil {
		return "", err
	}
	return chain, nil
}

func lastResult(h chainHistory) Result {
	r := Result{IdentityDigest: h.identityDigest, ChainID: h.chainID, Code: 6, Outcome: wal.DesenlaceEnDuda}
	if len(h.attempts) == 0 {
		return r
	}
	last := h.attempts[len(h.attempts)-1]
	r.AttemptID = last.mark.AttemptID
	r.Attempts = len(h.attempts)
	if last.outcome == nil {
		return r.uncertain("pending_intention")
	}
	r.Code = *last.outcome.RC
	r.Outcome = last.outcome.Desenlace
	r.Reason = last.decision.Reason
	return r
}

func retryableTail(h chainHistory) bool {
	if len(h.attempts) == 0 || len(h.attempts) >= MaxAttempts || h.halted {
		return false
	}
	last := h.attempts[len(h.attempts)-1]
	return last.outcome != nil && last.decision.Retryable
}

func isReadFailure(err error) bool {
	var f *ReadFailure
	return errors.As(err, &f)
}

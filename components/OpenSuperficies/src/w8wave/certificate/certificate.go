// Package certificate implements the NS completion certificate gates (W8X_GATE_05_CERTIFICATE):
// a pure finite integer gate evaluator for VM-work completion.
package certificate

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"math/big"
	"math/bits"
	"strconv"
	"strings"

	"opensuperficies.local/srf/src/workerbridge"
)

// UnitState enumerates the four legal exclusive states of a work unit.
type UnitState string

const (
	Q UnitState = "Q" // queued
	X UnitState = "X" // running
	P UnitState = "P" // accepted-pass
	F UnitState = "F" // explained-terminal-failure

	StateQueued  UnitState = Q
	StateRunning UnitState = X
	StatePass    UnitState = P
	StateFail    UnitState = F
)

// Valid reports whether s is one of the four legal unit states (Q, X, P, F).
func (s UnitState) Valid() bool {
	return s == Q || s == X || s == P || s == F
}

// Terminal reports whether s is a terminal state (P or F).
func (s UnitState) Terminal() bool {
	return s == P || s == F
}

// Bit is a binary gate verdict in {0, 1}.
type Bit uint8

const (
	BitZero Bit = 0
	BitOne  Bit = 1
)

// Valid reports whether b is strictly 0 or 1.
func (b Bit) Valid() bool {
	return b == BitZero || b == BitOne
}

// Reason identifies the typed reason for a gate verdict.
type Reason string

const (
	ReasonOK                 Reason = "OK"
	ReasonPrerequisiteFailed Reason = "PREREQUISITE_FAILED"

	// SCHEMA reasons
	ReasonRequiredMissing    Reason = "REQUIRED_MISSING"
	ReasonTypeError          Reason = "TYPE_ERROR"
	ReasonRangeError         Reason = "RANGE_ERROR"
	ReasonDuplicateKey       Reason = "DUPLICATE_KEY"
	ReasonEmptyLedger        Reason = "EMPTY_LEDGER"
	ReasonEmptySuite         Reason = "EMPTY_SUITE"
	ReasonZeroWeight         Reason = "ZERO_WEIGHT"
	ReasonInvalidTerminalBit Reason = "INVALID_TERMINAL_BIT"
	ReasonOverflow           Reason = "OVERFLOW"

	// IDENTITY reasons
	ReasonStatePartitionInvalid Reason = "STATE_PARTITION_INVALID"
	ReasonForeignID             Reason = "FOREIGN_ID"
	ReasonStateMultiplicity     Reason = "STATE_MULTIPLICITY"
	ReasonInvalidTransition     Reason = "INVALID_TRANSITION"
	ReasonInventoryMismatch     Reason = "INVENTORY_MISMATCH"
	ReasonSilentPassRemoval     Reason = "SILENT_PASS_REMOVAL"

	// CONTINUITY reasons
	ReasonStageConservation    Reason = "STAGE_CONSERVATION_VIOLATION"
	ReasonUnitConservation     Reason = "UNIT_CONSERVATION_VIOLATION"
	ReasonWeightConservation   Reason = "WEIGHT_CONSERVATION_VIOLATION"
	ReasonResidualConservation Reason = "RESIDUAL_CONSERVATION_VIOLATION"

	// ORIGIN reasons
	ReasonBlockedEvidence    Reason = "BLOCKED_EVIDENCE"
	ReasonMissingEvidenceBit Reason = "MISSING_EVIDENCE_BIT"
	ReasonReplayDetected     Reason = "REPLAY_DETECTED"
	ReasonVerifierRejection  Reason = "VERIFIER_REJECTION"

	// MONOTONE / progress reasons
	ReasonEmptyLineage                Reason = "EMPTY_LINEAGE"
	ReasonSingletonLineage            Reason = "SINGLETON_LINEAGE"
	ReasonBaselineMismatch            Reason = "BASELINE_MISMATCH"
	ReasonNegativeResidual            Reason = "NEGATIVE_RESIDUAL"
	ReasonNonMonotoneStep             Reason = "NON_MONOTONE_STEP"
	ReasonUnrevokedObligationIncrease Reason = "UNREVOKED_OBLIGATION_INCREASE"

	// COVERAGE and LEASE CLOSURE reasons
	ReasonMissingMonitorSource Reason = "MISSING_MONITOR_SOURCE"
	ReasonSeqGap               Reason = "SEQ_GAP"
	ReasonDuplicateSeq         Reason = "DUPLICATE_SEQ"
	ReasonClockRegression      Reason = "CLOCK_REGRESSION"
	ReasonUncoveredBilledTicks Reason = "UNCOVERED_BILLED_TICKS"
	ReasonCounterReset         Reason = "COUNTER_RESET_WITHOUT_BOOT_CHANGE"
	ReasonOpenLeases           Reason = "OPEN_LEASES"
	ReasonUnfencedMembers      Reason = "UNFENCED_OR_UNCLEAN_MEMBERS"

	// SUCCESS and WARNINGS reasons
	ReasonPositiveResidual         Reason = "POSITIVE_RESIDUAL"
	ReasonAccountedFailuresPresent Reason = "ACCOUNTED_FAILURES_PRESENT"
	ReasonUnresolvedRevocations    Reason = "UNRESOLVED_REVOCATIONS"
	ReasonUnresolvedViolations     Reason = "UNRESOLVED_VIOLATIONS"
	ReasonDeadlineViolations       Reason = "DEADLINE_VIOLATIONS"
	ReasonStallZero                Reason = "STALL_ZERO"
	ReasonStallSpin                Reason = "STALL_SPIN"
	ReasonStallActivityUnknown     Reason = "STALL_ACTIVITY_UNKNOWN"
)

// StallKind classifies blow-up-style stall warnings when R > 0 and elapsed >= D.
type StallKind string

const (
	StallNone            StallKind = "STALL_NONE"
	StallZero            StallKind = "STALL_ZERO"
	StallSpin            StallKind = "STALL_SPIN"
	StallActivityUnknown StallKind = "STALL_ACTIVITY_UNKNOWN"
)

// Result is the terminal certificate outcome.
type Result string

const (
	DONE_SUCCESS           Result = "DONE_SUCCESS"
	DONE_ACCOUNTED_FAILURE Result = "DONE_ACCOUNTED_FAILURE"
	BLOCKED_EVIDENCE       Result = "BLOCKED_EVIDENCE"
	INDETERMINATE          Result = "INDETERMINATE"

	DoneSuccess          Result = DONE_SUCCESS
	DoneAccountedFailure Result = DONE_ACCOUNTED_FAILURE
	BlockedEvidence      Result = BLOCKED_EVIDENCE
	Indeterminate        Result = INDETERMINATE
)

// Error carries a W8 normalized exit code in {2..7} (never 1) and a typed Reason.
type Error struct {
	Code   int
	Reason Reason
	Detail string
}

func (e *Error) Error() string {
	if e == nil {
		return ""
	}
	return fmt.Sprintf("%s (rc=%d): %s", e.Reason, e.Code, e.Detail)
}

func errInput(r Reason, detail string) error {
	return &Error{Code: 2, Reason: r, Detail: detail}
}

func errGate(r Reason, detail string) error {
	return &Error{Code: 5, Reason: r, Detail: detail}
}

// ExitCode maps errors to W8 normalized exit codes in {0, 2..7}, never 1.
func ExitCode(err error) int {
	if err == nil {
		return 0
	}
	var ce *Error
	if errors.As(err, &ce) && ce != nil && ce.Code >= 2 && ce.Code <= 7 {
		return ce.Code
	}
	if rc := workerbridge.ExitCode(err); rc >= 2 && rc <= 7 {
		return rc
	}
	return 5
}

// ResultExitCode maps a certificate Result to a W8 normalized exit code in {0, 2..7}, never 1.
func ResultExitCode(r Result) int {
	switch r {
	case DONE_SUCCESS:
		return 0
	case DONE_ACCOUNTED_FAILURE:
		return 5
	case BLOCKED_EVIDENCE:
		return 4
	case INDETERMINATE:
		return 6
	default:
		return 6
	}
}

// Unit represents a single obligation unit with integer weight >= 1 and four evidence bits in {0, 1}.
type Unit struct {
	ID               string    `json:"id"`
	Weight           uint64    `json:"weight"`
	State            UnitState `json:"state"`
	VerifiedIO       uint8     `json:"verified_io"`
	OriginBound      uint8     `json:"origin_bound"`
	FreshRun         uint8     `json:"fresh_run"`
	AuthorityCurrent uint8     `json:"authority_current"`
}

// AllEvidenceBitsSet reports whether all four origin/verification evidence bits equal 1.
func (u Unit) AllEvidenceBitsSet() bool {
	return u.VerifiedIO == 1 && u.OriginBound == 1 && u.FreshRun == 1 && u.AuthorityCurrent == 1
}

// EvidenceBitsValid reports whether all four evidence bits are in {0, 1}.
func (u Unit) EvidenceBitsValid() bool {
	return u.VerifiedIO <= 1 && u.OriginBound <= 1 && u.FreshRun <= 1 && u.AuthorityCurrent <= 1
}

// Manifest defines the obligation epoch and requested work units.
type Manifest struct {
	ManifestSHA256 string   `json:"manifest_sha256"`
	Epoch          uint64   `json:"epoch"`
	Units          []Unit   `json:"units"`
	CarriedWeight  uint64   `json:"carried_weight"`
	CarriedUnitIDs []string `json:"carried_unit_ids,omitempty"`
	TrustRoot      string   `json:"trust_root,omitempty"`
	PreviousEpoch  uint64   `json:"previous_epoch,omitempty"`
	RevokedEpoch   uint64   `json:"revoked_epoch,omitempty"`
}

// Snapshot captures the unit states at a sequence point Seq.
// Callers may supply unit states via Units (carrying per-unit state and evidence bits)
// or via States (mapping unit ID to UnitState), or both if consistent.
type Snapshot struct {
	Seq       uint64               `json:"seq"`
	Units     []Unit               `json:"units,omitempty"`
	States    map[string]UnitState `json:"states,omitempty"`
	ReportedR *int64               `json:"reported_r,omitempty"`
}

// Quantities holds the exact integer quantities measured from a Manifest and Snapshot.
//
// Definitions:
//   - N: total requested units in the manifest (N >= 1)
//   - W: total obligation weight in the manifest (sum of unit weights, each >= 1)
//   - I: located inventory count in the snapshot (I == N by exact set equality)
//   - D: terminal / decided unit count (units in P or F; D + O = N)
//   - O: open / non-terminal unit count (units in Q or X; D + O = N)
//   - T: terminal unit weight (weight of units in P or F; T + A = W)
//   - A: active / open unit weight (weight of units in Q or X; T + A = W)
//   - S: accepted-pass weight newly certified in the current epoch snapshot
//   - C: total certified completed weight (CarriedWeight + S; C + R = W)
//   - R: residual obligation weight (W - C; C + R = W)
//   - FailureCount: number of units in explained-terminal-failure state F
//   - FailureWeight: total weight of units in explained-terminal-failure state F
type Quantities struct {
	N             uint64 `json:"n"`
	W             uint64 `json:"w"`
	I             uint64 `json:"i"`
	D             uint64 `json:"d"`
	T             uint64 `json:"t"`
	O             uint64 `json:"o"`
	A             uint64 `json:"a"`
	S             uint64 `json:"s"`
	C             uint64 `json:"c"`
	R             uint64 `json:"r"`
	FailureCount  uint64 `json:"failure_count"`
	FailureWeight uint64 `json:"failure_weight"`
	CarriedWeight uint64 `json:"carried_weight"`
	QueuedCount   uint64 `json:"queued_count"`
	RunningCount  uint64 `json:"running_count"`
	PassCount     uint64 `json:"pass_count"`
}

// CheckedAddUint64 adds a and b with overflow detection using math/bits.Add64.
func CheckedAddUint64(a, b uint64) (uint64, error) {
	sum, carry := bits.Add64(a, b, 0)
	if carry != 0 {
		return 0, errGate(ReasonOverflow, "uint64 addition overflow")
	}
	return sum, nil
}

// CheckedSubUint64 computes a - b, refusing underflow when b > a.
func CheckedSubUint64(a, b uint64) (uint64, error) {
	if b > a {
		return 0, errGate(ReasonNegativeResidual, "uint64 subtraction underflow")
	}
	return a - b, nil
}

// Compute evaluates the exact integer Quantities for m and s using checked uint64 arithmetic,
// refusing on empty suites, duplicate or foreign IDs, missing units, invalid states, invalid
// evidence bits, zero weights, or uint64 overflow.
func Compute(m Manifest, s Snapshot) (Quantities, error) {
	if len(m.Units) == 0 {
		return Quantities{}, errInput(ReasonEmptySuite, "manifest must contain at least 1 unit")
	}

	manifestByID := make(map[string]Unit, len(m.Units))
	var totalW uint64
	for _, mu := range m.Units {
		if strings.TrimSpace(mu.ID) == "" {
			return Quantities{}, errInput(ReasonRequiredMissing, "manifest unit has empty ID")
		}
		if _, dup := manifestByID[mu.ID]; dup {
			return Quantities{}, errInput(ReasonDuplicateKey, fmt.Sprintf("duplicate manifest unit ID %q", mu.ID))
		}
		if mu.Weight < 1 {
			return Quantities{}, errInput(ReasonZeroWeight, fmt.Sprintf("unit %q has weight 0 (must be >= 1)", mu.ID))
		}
		if !mu.EvidenceBitsValid() {
			return Quantities{}, errInput(ReasonRangeError, fmt.Sprintf("unit %q has evidence bit outside {0,1}", mu.ID))
		}
		var err error
		totalW, err = CheckedAddUint64(totalW, mu.Weight)
		if err != nil {
			return Quantities{}, err
		}
		manifestByID[mu.ID] = mu
	}

	if m.CarriedWeight > totalW {
		return Quantities{}, errGate(ReasonRangeError, "carried_weight exceeds total manifest weight W")
	}

	carriedSet := make(map[string]struct{}, len(m.CarriedUnitIDs))
	var carriedUnitsSum uint64
	for _, cid := range m.CarriedUnitIDs {
		mu, ok := manifestByID[cid]
		if !ok {
			return Quantities{}, errGate(ReasonForeignID, fmt.Sprintf("carried unit ID %q not in manifest", cid))
		}
		if _, dup := carriedSet[cid]; dup {
			return Quantities{}, errInput(ReasonDuplicateKey, fmt.Sprintf("duplicate carried unit ID %q", cid))
		}
		carriedSet[cid] = struct{}{}
		var err error
		carriedUnitsSum, err = CheckedAddUint64(carriedUnitsSum, mu.Weight)
		if err != nil {
			return Quantities{}, err
		}
	}
	if len(carriedSet) > 0 && carriedUnitsSum != m.CarriedWeight {
		return Quantities{}, errGate(ReasonRangeError, "carried_unit_ids weight sum does not match carried_weight")
	}

	resolved, err := resolveSnapshotUnits(manifestByID, s)
	if err != nil {
		return Quantities{}, err
	}

	n := uint64(len(m.Units))
	locatedI := uint64(len(resolved))
	if locatedI != n {
		return Quantities{}, errGate(ReasonInventoryMismatch, fmt.Sprintf("located inventory I=%d != N=%d", locatedI, n))
	}

	q := Quantities{
		N:             n,
		W:             totalW,
		I:             locatedI,
		CarriedWeight: m.CarriedWeight,
	}

	for _, mu := range m.Units {
		su, ok := resolved[mu.ID]
		if !ok {
			return Quantities{}, errGate(ReasonInventoryMismatch, fmt.Sprintf("missing unit %q in snapshot", mu.ID))
		}
		if !su.State.Valid() {
			return Quantities{}, errGate(ReasonStatePartitionInvalid, fmt.Sprintf("unit %q has invalid state %q", mu.ID, su.State))
		}
		if !su.EvidenceBitsValid() {
			return Quantities{}, errGate(ReasonRangeError, fmt.Sprintf("unit %q has evidence bit outside {0,1}", mu.ID))
		}

		switch su.State {
		case Q:
			q.QueuedCount++
			q.O++
			q.A, err = CheckedAddUint64(q.A, mu.Weight)
		case X:
			q.RunningCount++
			q.O++
			q.A, err = CheckedAddUint64(q.A, mu.Weight)
		case P:
			q.PassCount++
			q.D++
			q.T, err = CheckedAddUint64(q.T, mu.Weight)
			if err == nil {
				if _, isCarried := carriedSet[mu.ID]; !isCarried {
					q.S, err = CheckedAddUint64(q.S, mu.Weight)
				}
			}
		case F:
			q.FailureCount++
			q.D++
			q.T, err = CheckedAddUint64(q.T, mu.Weight)
			if err == nil {
				q.FailureWeight, err = CheckedAddUint64(q.FailureWeight, mu.Weight)
			}
		}
		if err != nil {
			return Quantities{}, err
		}
	}

	q.C, err = CheckedAddUint64(m.CarriedWeight, q.S)
	if err != nil {
		return Quantities{}, err
	}
	if q.C > q.W {
		return Quantities{}, errGate(ReasonRangeError, "certified weight C exceeds total obligation weight W")
	}
	q.R = q.W - q.C

	if s.ReportedR != nil {
		if *s.ReportedR < 0 {
			return Quantities{}, errGate(ReasonNegativeResidual, "snapshot reported negative R")
		}
		if uint64(*s.ReportedR) != q.R {
			return Quantities{}, errGate(ReasonResidualConservation, fmt.Sprintf("reported R=%d != computed R=%d", *s.ReportedR, q.R))
		}
	}

	return q, nil
}

func resolveSnapshotUnits(manifestByID map[string]Unit, s Snapshot) (map[string]Unit, error) {
	if len(s.Units) == 0 && len(s.States) == 0 {
		return nil, errGate(ReasonInventoryMismatch, "snapshot has no unit states")
	}
	resolved := make(map[string]Unit, len(manifestByID))

	for _, su := range s.Units {
		if strings.TrimSpace(su.ID) == "" {
			return nil, errInput(ReasonRequiredMissing, "snapshot unit has empty ID")
		}
		mu, exists := manifestByID[su.ID]
		if !exists {
			return nil, errGate(ReasonForeignID, fmt.Sprintf("snapshot contains foreign unit ID %q", su.ID))
		}
		if _, dup := resolved[su.ID]; dup {
			return nil, errGate(ReasonStateMultiplicity, fmt.Sprintf("snapshot unit %q appears multiple times", su.ID))
		}
		if !su.State.Valid() {
			return nil, errGate(ReasonStatePartitionInvalid, fmt.Sprintf("unit %q has invalid state %q", su.ID, su.State))
		}
		if !su.EvidenceBitsValid() {
			return nil, errGate(ReasonRangeError, fmt.Sprintf("unit %q has evidence bit outside {0,1}", su.ID))
		}
		if su.Weight != 0 && su.Weight != mu.Weight {
			return nil, errGate(ReasonRangeError, fmt.Sprintf("unit %q snapshot weight %d != manifest weight %d", su.ID, su.Weight, mu.Weight))
		}
		su.Weight = mu.Weight
		resolved[su.ID] = su
	}

	for id, st := range s.States {
		if strings.TrimSpace(id) == "" {
			return nil, errInput(ReasonRequiredMissing, "snapshot state map has empty ID")
		}
		mu, exists := manifestByID[id]
		if !exists {
			return nil, errGate(ReasonForeignID, fmt.Sprintf("snapshot state map contains foreign unit ID %q", id))
		}
		if !st.Valid() {
			return nil, errGate(ReasonStatePartitionInvalid, fmt.Sprintf("unit %q has invalid state %q", id, st))
		}
		if prev, already := resolved[id]; already {
			if prev.State != st {
				return nil, errGate(ReasonStateMultiplicity, fmt.Sprintf("unit %q has conflicting states %q and %q", id, prev.State, st))
			}
			continue
		}
		u := mu
		u.State = st
		resolved[id] = u
	}

	return resolved, nil
}

// Fraction returns the completion fraction (num, den) = (C, W).
// 100 percent TARGET holds iff R == 0 (i.e. C == W with W > 0), compared by integer cross-products.
func Fraction(q Quantities) (num, den uint64) {
	return q.C, q.W
}

// CompareFraction compares num1/den1 against num2/den2 using exact math/big integer cross-products
// (num1 * den2 vs num2 * den1), returning -1, 0, or +1, or an error if a denominator is 0.
func CompareFraction(num1, den1, num2, den2 uint64) (int, error) {
	if den1 == 0 || den2 == 0 {
		return 0, errInput(ReasonRangeError, "zero denominator in fraction comparison")
	}
	lhs := new(big.Int).Mul(new(big.Int).SetUint64(num1), new(big.Int).SetUint64(den2))
	rhs := new(big.Int).Mul(new(big.Int).SetUint64(num2), new(big.Int).SetUint64(den1))
	return lhs.Cmp(rhs), nil
}

// IsTargetComplete reports whether q reaches the 100 percent TARGET (R == 0 and C/W == 1/1 by integer cross-product).
func IsTargetComplete(q Quantities) bool {
	if q.W == 0 || q.R != 0 {
		return false
	}
	num, den := Fraction(q)
	cmp, err := CompareFraction(num, den, 1, 1)
	return err == nil && cmp == 0
}

// StageFlow records conservation counts for a single pipeline stage:
// InventoryBefore + Arrivals == InventoryAfter + Departures.
type StageFlow struct {
	StageID         string `json:"stage_id"`
	InventoryBefore uint64 `json:"inventory_before"`
	Arrivals        uint64 `json:"arrivals"`
	InventoryAfter  uint64 `json:"inventory_after"`
	Departures      uint64 `json:"departures"`
}

// MonitorTick represents a single telemetry sample from a monitor source.
type MonitorTick struct {
	Seq          uint64 `json:"seq"`
	ClockNs      uint64 `json:"clock_ns"`
	BootID       string `json:"boot_id"`
	Counter      uint64 `json:"counter"`
	BilledTicks  uint64 `json:"billed_ticks"`
	CoveredTicks uint64 `json:"covered_ticks"`
}

// MonitorSource groups sequential telemetry ticks for a single monitor source.
type MonitorSource struct {
	SourceID string        `json:"source_id"`
	Ticks    []MonitorTick `json:"ticks"`
}

// CoverageEvidence carries monitor source sequences and lease/fencing counters.
type CoverageEvidence struct {
	Sources                        []MonitorSource `json:"sources,omitempty"`
	Duplicates                     uint64          `json:"duplicates"`
	ClockRegressions               uint64          `json:"clock_regressions"`
	UncoveredBilledTicks           uint64          `json:"uncovered_billed_ticks"`
	CounterResetsWithoutBootChange uint64          `json:"counter_resets_without_boot_change"`
	OpenLeases                     uint64          `json:"open_leases"`
	UnfencedOrUncleanMembers       uint64          `json:"unfenced_or_unclean_members"`
}

// OriginEvidence carries trust root and replay/rejection counters for ORIGIN evaluation.
type OriginEvidence struct {
	TrustRoot                string `json:"trust_root"`
	ReplayCount              uint64 `json:"replay_count"`
	VerifierRejectionsAmongP uint64 `json:"verifier_rejections_among_p"`
}

// StallInput carries parameters for evaluating blow-up-style silent stalls.
type StallInput struct {
	ThresholdD           uint64 `json:"threshold_d"`
	Now                  uint64 `json:"now"`
	LastVerifiedDecrease uint64 `json:"last_verified_decrease"`
	ActivityKnown        bool   `json:"activity_known"`
	ObservedActivityOps  uint64 `json:"observed_activity_ops"`
}

// SchemaStats carries explicit parser/validator counts for the SCHEMA gate.
type SchemaStats struct {
	RequiredMissing uint64 `json:"required_missing"`
	TypeErrors      uint64 `json:"type_errors"`
	RangeErrors     uint64 `json:"range_errors"`
	DuplicateKeys   uint64 `json:"duplicate_keys"`
	EmptyLedger     bool   `json:"empty_ledger"`
}

// GateVerdict pairs a gate Bit (0 or 1) with a typed Reason and optional diagnostic detail.
type GateVerdict struct {
	Bit    Bit    `json:"bit"`
	Reason Reason `json:"reason"`
	Detail string `json:"detail,omitempty"`
}

func passVerdict() GateVerdict {
	return GateVerdict{Bit: BitOne, Reason: ReasonOK}
}

func failVerdict(r Reason, detail string) GateVerdict {
	return GateVerdict{Bit: BitZero, Reason: r, Detail: detail}
}

func prereqVerdict(prereq string) GateVerdict {
	return GateVerdict{Bit: BitZero, Reason: ReasonPrerequisiteFailed, Detail: "stopped by failed prerequisite: " + prereq}
}

// Gates holds the individual gate verdicts and summary counters for Outcome evaluation.
type Gates struct {
	Schema                GateVerdict `json:"schema"`
	Identity              GateVerdict `json:"identity"`
	Coverage              GateVerdict `json:"coverage"`
	Ranges                GateVerdict `json:"ranges"`
	Continuity            GateVerdict `json:"continuity"`
	Origin                GateVerdict `json:"origin"`
	Monotone              GateVerdict `json:"monotone"`
	Warnings              GateVerdict `json:"warnings"`
	LeaseClosure          GateVerdict `json:"lease_closure"`
	Success               GateVerdict `json:"success"`
	Stall                 StallKind   `json:"stall"`
	UnresolvedRevocations uint64      `json:"unresolved_revocations"`
	UnresolvedViolations  uint64      `json:"unresolved_violations"`
	DeadlineViolations    uint64      `json:"deadline_violations"`
}

// AllRequiredGatesPass reports whether all non-Success prerequisite gates have Bit == 1.
func (g Gates) AllRequiredGatesPass() bool {
	return g.Schema.Bit == BitOne &&
		g.Identity.Bit == BitOne &&
		g.Coverage.Bit == BitOne &&
		g.Ranges.Bit == BitOne &&
		g.Continuity.Bit == BitOne &&
		g.Origin.Bit == BitOne &&
		g.Monotone.Bit == BitOne &&
		g.Warnings.Bit == BitOne &&
		g.LeaseClosure.Bit == BitOne
}

// EvaluationInput bundles all inputs required to evaluate the full ordered gate chain.
type EvaluationInput struct {
	SchemaStats           SchemaStats      `json:"schema_stats"`
	Manifest              Manifest         `json:"manifest"`
	Series                []Snapshot       `json:"series"`
	Stages                []StageFlow      `json:"stages,omitempty"`
	Origin                OriginEvidence   `json:"origin"`
	Coverage              CoverageEvidence `json:"coverage"`
	Stall                 StallInput       `json:"stall"`
	UnresolvedRevocations uint64           `json:"unresolved_revocations"`
	UnresolvedViolations  uint64           `json:"unresolved_violations"`
	DeadlineViolations    uint64           `json:"deadline_violations"`
	ClaimedComplete       bool             `json:"claimed_complete,omitempty"`
	ClaimedRoundedPct     *uint64          `json:"claimed_rounded_pct,omitempty"`
}

// EvaluationReport contains the final Quantities, Gates, and Result from Evaluate.
type EvaluationReport struct {
	Quantities Quantities `json:"quantities"`
	Gates      Gates      `json:"gates"`
	Outcome    Result     `json:"outcome"`
}

// EvaluateSchema checks SCHEMA:
// required_missing, type_errors, range_errors and duplicate_keys all 0,
// non-empty ledger, N >= 1, every weight >= 1, and p and f in {0, 1}.
func EvaluateSchema(stats SchemaStats, m Manifest, s Snapshot) GateVerdict {
	if stats.EmptyLedger {
		return failVerdict(ReasonEmptyLedger, "ledger is empty")
	}
	if stats.RequiredMissing > 0 {
		return failVerdict(ReasonRequiredMissing, "required_missing > 0")
	}
	if stats.TypeErrors > 0 {
		return failVerdict(ReasonTypeError, "type_errors > 0")
	}
	if stats.DuplicateKeys > 0 {
		return failVerdict(ReasonDuplicateKey, "duplicate_keys > 0")
	}
	if stats.RangeErrors > 0 {
		return failVerdict(ReasonRangeError, "range_errors > 0")
	}
	if strings.TrimSpace(m.ManifestSHA256) == "" {
		return failVerdict(ReasonRequiredMissing, "manifest_sha256 is empty")
	}
	if len(m.Units) == 0 {
		return failVerdict(ReasonEmptySuite, "N must be at least 1")
	}
	seen := make(map[string]struct{}, len(m.Units))
	var wSum uint64
	for _, u := range m.Units {
		if strings.TrimSpace(u.ID) == "" {
			return failVerdict(ReasonRequiredMissing, "unit ID is empty")
		}
		if _, exists := seen[u.ID]; exists {
			return failVerdict(ReasonDuplicateKey, fmt.Sprintf("duplicate manifest unit ID %q", u.ID))
		}
		seen[u.ID] = struct{}{}
		if u.Weight < 1 {
			return failVerdict(ReasonZeroWeight, fmt.Sprintf("unit %q weight must be >= 1", u.ID))
		}
		if !u.EvidenceBitsValid() {
			return failVerdict(ReasonRangeError, fmt.Sprintf("unit %q evidence bit outside {0,1}", u.ID))
		}
		var err error
		wSum, err = CheckedAddUint64(wSum, u.Weight)
		if err != nil {
			return failVerdict(ReasonOverflow, err.Error())
		}
	}
	if m.CarriedWeight > wSum {
		return failVerdict(ReasonRangeError, "carried_weight > W")
	}
	for _, su := range s.Units {
		if !su.EvidenceBitsValid() {
			return failVerdict(ReasonRangeError, fmt.Sprintf("snapshot unit %q evidence bit outside {0,1}", su.ID))
		}
		if su.State != "" && !su.State.Valid() {
			return failVerdict(ReasonInvalidTerminalBit, fmt.Sprintf("snapshot unit %q invalid state %q", su.ID, su.State))
		}
	}
	for id, st := range s.States {
		if !st.Valid() {
			return failVerdict(ReasonInvalidTerminalBit, fmt.Sprintf("snapshot state %q invalid for unit %q", st, id))
		}
	}
	return passVerdict()
}

// EvaluateIdentity checks IDENTITY across the snapshot lineage:
// for every requested unit q+x+p+f == 1, foreign ids == 0, state multiplicity <= 1,
// invalid transitions == 0 (including silent pass removal), located inventory I == N by exact set equality.
func EvaluateIdentity(m Manifest, series []Snapshot) GateVerdict {
	if len(m.Units) == 0 {
		return failVerdict(ReasonEmptySuite, "manifest has no units")
	}
	if len(series) == 0 {
		return failVerdict(ReasonInventoryMismatch, "snapshot series is empty")
	}
	manifestByID := make(map[string]Unit, len(m.Units))
	for _, u := range m.Units {
		manifestByID[u.ID] = u
	}

	var prevResolved map[string]Unit
	for idx, snap := range series {
		resolved, err := resolveSnapshotUnits(manifestByID, snap)
		if err != nil {
			var ce *Error
			if errors.As(err, &ce) {
				return failVerdict(ce.Reason, ce.Detail)
			}
			return failVerdict(ReasonInventoryMismatch, err.Error())
		}
		if len(resolved) != len(m.Units) {
			return failVerdict(ReasonInventoryMismatch, fmt.Sprintf("snapshot[%d] located I=%d != N=%d", idx, len(resolved), len(m.Units)))
		}
		for _, mu := range m.Units {
			su, ok := resolved[mu.ID]
			if !ok {
				return failVerdict(ReasonInventoryMismatch, fmt.Sprintf("snapshot[%d] missing unit %q", idx, mu.ID))
			}
			var qBit, xBit, pBit, fBit uint64
			switch su.State {
			case Q:
				qBit = 1
			case X:
				xBit = 1
			case P:
				pBit = 1
			case F:
				fBit = 1
			default:
				return failVerdict(ReasonStatePartitionInvalid, fmt.Sprintf("unit %q has illegal state %q", mu.ID, su.State))
			}
			if qBit+xBit+pBit+fBit != 1 {
				return failVerdict(ReasonStatePartitionInvalid, fmt.Sprintf("unit %q state partition sum != 1", mu.ID))
			}
		}
		if prevResolved != nil {
			for _, mu := range m.Units {
				prevState := prevResolved[mu.ID].State
				currState := resolved[mu.ID].State
				if prevState == P && currState != P {
					return failVerdict(ReasonSilentPassRemoval, fmt.Sprintf("unit %q silently removed from pass state P -> %s", mu.ID, currState))
				}
				if prevState == F && currState != F {
					return failVerdict(ReasonInvalidTransition, fmt.Sprintf("unit %q transitioned out of terminal failure F -> %s", mu.ID, currState))
				}
				if prevState == X && currState == Q {
					return failVerdict(ReasonInvalidTransition, fmt.Sprintf("unit %q regressed from active X -> queued Q", mu.ID))
				}
			}
		}
		prevResolved = resolved
	}
	return passVerdict()
}

// EvaluateCoverage checks COVERAGE:
// per monitor source the next seq == seq + 1, duplicates == 0, clock regressions == 0,
// uncovered billed ticks == 0, counter reset without boot change == 0, open leases == 0,
// unfenced or unclean members == 0.
func EvaluateCoverage(ev CoverageEvidence) GateVerdict {
	if ev.Duplicates > 0 {
		return failVerdict(ReasonDuplicateSeq, "duplicates > 0")
	}
	if ev.ClockRegressions > 0 {
		return failVerdict(ReasonClockRegression, "clock_regressions > 0")
	}
	if ev.UncoveredBilledTicks > 0 {
		return failVerdict(ReasonUncoveredBilledTicks, "uncovered_billed_ticks > 0")
	}
	if ev.CounterResetsWithoutBootChange > 0 {
		return failVerdict(ReasonCounterReset, "counter_resets_without_boot_change > 0")
	}
	if len(ev.Sources) == 0 {
		return failVerdict(ReasonMissingMonitorSource, "at least one monitor source is required")
	}
	for _, src := range ev.Sources {
		if strings.TrimSpace(src.SourceID) == "" {
			return failVerdict(ReasonMissingMonitorSource, "monitor source_id is empty")
		}
		if len(src.Ticks) == 0 {
			return failVerdict(ReasonMissingMonitorSource, fmt.Sprintf("monitor source %q has no ticks", src.SourceID))
		}
		seenSeq := make(map[uint64]struct{}, len(src.Ticks))
		for i, tick := range src.Ticks {
			if _, dup := seenSeq[tick.Seq]; dup {
				return failVerdict(ReasonDuplicateSeq, fmt.Sprintf("source %q duplicate seq %d", src.SourceID, tick.Seq))
			}
			seenSeq[tick.Seq] = struct{}{}
			if tick.CoveredTicks < tick.BilledTicks {
				return failVerdict(ReasonUncoveredBilledTicks, fmt.Sprintf("source %q seq %d covered_ticks=%d < billed_ticks=%d", src.SourceID, tick.Seq, tick.CoveredTicks, tick.BilledTicks))
			}
			if i > 0 {
				prev := src.Ticks[i-1]
				nextExpected, err := CheckedAddUint64(prev.Seq, 1)
				if err != nil || tick.Seq != nextExpected {
					return failVerdict(ReasonSeqGap, fmt.Sprintf("source %q seq %d != prev.seq+1 (%d)", src.SourceID, tick.Seq, prev.Seq+1))
				}
				if tick.ClockNs < prev.ClockNs {
					return failVerdict(ReasonClockRegression, fmt.Sprintf("source %q clock regressed %d -> %d", src.SourceID, prev.ClockNs, tick.ClockNs))
				}
				if tick.Counter < prev.Counter && tick.BootID == prev.BootID {
					return failVerdict(ReasonCounterReset, fmt.Sprintf("source %q counter reset %d -> %d without boot change", src.SourceID, prev.Counter, tick.Counter))
				}
			}
		}
	}
	if ev.OpenLeases > 0 {
		return failVerdict(ReasonOpenLeases, "open_leases > 0")
	}
	if ev.UnfencedOrUncleanMembers > 0 {
		return failVerdict(ReasonUnfencedMembers, "unfenced_or_unclean_members > 0")
	}
	return passVerdict()
}

// EvaluateLeaseClosure checks that open leases == 0 and unfenced or unclean members == 0.
func EvaluateLeaseClosure(ev CoverageEvidence) GateVerdict {
	if ev.OpenLeases > 0 {
		return failVerdict(ReasonOpenLeases, "open_leases > 0")
	}
	if ev.UnfencedOrUncleanMembers > 0 {
		return failVerdict(ReasonUnfencedMembers, "unfenced_or_unclean_members > 0")
	}
	return passVerdict()
}

// EvaluateRanges verifies exact integer range bounds across Manifest, Snapshot, and Quantities.
func EvaluateRanges(m Manifest, s Snapshot, stallD uint64) (Quantities, GateVerdict) {
	q, err := Compute(m, s)
	if err != nil {
		var ce *Error
		if errors.As(err, &ce) {
			return Quantities{}, failVerdict(ce.Reason, ce.Detail)
		}
		return Quantities{}, failVerdict(ReasonRangeError, err.Error())
	}
	if q.N < 1 || q.W < 1 {
		return Quantities{}, failVerdict(ReasonRangeError, "N and W must be >= 1")
	}
	if q.R > 0 && stallD == 0 {
		return Quantities{}, failVerdict(ReasonRangeError, "stall threshold D must be positive when R > 0")
	}
	return q, passVerdict()
}

// EvaluateContinuity checks CONTINUITY:
// per stage inventory_before + arrivals == inventory_after + departures (checked individually
// per stage so opposite local deficits never cancel globally), plus D+O=N, T+A=W, C+R=W.
func EvaluateContinuity(q Quantities, stages []StageFlow) GateVerdict {
	for i, st := range stages {
		lhs, err := CheckedAddUint64(st.InventoryBefore, st.Arrivals)
		if err != nil {
			return failVerdict(ReasonOverflow, fmt.Sprintf("stage[%d] (%s) lhs overflow", i, st.StageID))
		}
		rhs, err := CheckedAddUint64(st.InventoryAfter, st.Departures)
		if err != nil {
			return failVerdict(ReasonOverflow, fmt.Sprintf("stage[%d] (%s) rhs overflow", i, st.StageID))
		}
		if lhs != rhs {
			return failVerdict(ReasonStageConservation, fmt.Sprintf("stage[%d] (%s) conservation failed: %d+%d=%d != %d+%d=%d", i, st.StageID, st.InventoryBefore, st.Arrivals, lhs, st.InventoryAfter, st.Departures, rhs))
		}
	}

	doSum, err := CheckedAddUint64(q.D, q.O)
	if err != nil || doSum != q.N {
		return failVerdict(ReasonUnitConservation, fmt.Sprintf("D(%d)+O(%d) != N(%d)", q.D, q.O, q.N))
	}
	taSum, err := CheckedAddUint64(q.T, q.A)
	if err != nil || taSum != q.W {
		return failVerdict(ReasonWeightConservation, fmt.Sprintf("T(%d)+A(%d) != W(%d)", q.T, q.A, q.W))
	}
	crSum, err := CheckedAddUint64(q.C, q.R)
	if err != nil || crSum != q.W {
		return failVerdict(ReasonResidualConservation, fmt.Sprintf("C(%d)+R(%d) != W(%d)", q.C, q.R, q.W))
	}
	return passVerdict()
}

// EvaluateOrigin checks ORIGIN:
// with no supplied trust root ORIGIN is BLOCKED_EVIDENCE (never a pass);
// every accepted unit (State == P) has all four evidence bits == 1,
// replay_count == 0, verifier rejections among P == 0.
func EvaluateOrigin(m Manifest, s Snapshot, ev OriginEvidence) GateVerdict {
	trustRoot := strings.TrimSpace(ev.TrustRoot)
	if trustRoot == "" {
		trustRoot = strings.TrimSpace(m.TrustRoot)
	}
	if trustRoot == "" {
		return failVerdict(ReasonBlockedEvidence, "missing supplied trust root")
	}
	if ev.ReplayCount > 0 {
		return failVerdict(ReasonReplayDetected, "replay_count > 0")
	}
	if ev.VerifierRejectionsAmongP > 0 {
		return failVerdict(ReasonVerifierRejection, "verifier_rejections_among_p > 0")
	}

	manifestByID := make(map[string]Unit, len(m.Units))
	for _, u := range m.Units {
		manifestByID[u.ID] = u
	}
	resolved, err := resolveSnapshotUnits(manifestByID, s)
	if err != nil {
		return failVerdict(ReasonBlockedEvidence, err.Error())
	}
	for _, mu := range m.Units {
		su := resolved[mu.ID]
		if su.State == P && !su.AllEvidenceBitsSet() {
			return failVerdict(ReasonMissingEvidenceBit, fmt.Sprintf("accepted unit %q missing required origin evidence bits", mu.ID))
		}
	}
	return passVerdict()
}

// EvaluateMonotone checks MONOTONE across the snapshot lineage:
//   - empty or singleton series is not lineage (must have len >= 2)
//   - R0 == W - carried_weight
//   - every R nonnegative
//   - next R == R - sum(weights newly accepted), strictly lower by at least 1 when newly accepted set is nonempty, and equal otherwise
//   - no silent pass removal or unrevoked obligation increase
func EvaluateMonotone(m Manifest, series []Snapshot) ([]uint64, GateVerdict) {
	if len(series) == 0 {
		return nil, failVerdict(ReasonEmptyLineage, "empty series is not lineage")
	}
	if len(series) == 1 {
		return nil, failVerdict(ReasonSingletonLineage, "singleton series is not lineage")
	}

	manifestByID := make(map[string]Unit, len(m.Units))
	var totalW uint64
	for _, u := range m.Units {
		manifestByID[u.ID] = u
		var err error
		totalW, err = CheckedAddUint64(totalW, u.Weight)
		if err != nil {
			return nil, failVerdict(ReasonOverflow, err.Error())
		}
	}
	if m.CarriedWeight > totalW {
		return nil, failVerdict(ReasonNegativeResidual, "carried_weight exceeds W")
	}
	expectedR0 := totalW - m.CarriedWeight

	rSeries := make([]uint64, len(series))
	var prevPass map[string]uint64
	var prevSeq uint64

	for i, snap := range series {
		if snap.ReportedR != nil && *snap.ReportedR < 0 {
			return nil, failVerdict(ReasonNegativeResidual, fmt.Sprintf("snapshot[%d] reported negative R=%d", i, *snap.ReportedR))
		}
		q, err := Compute(m, snap)
		if err != nil {
			var ce *Error
			if errors.As(err, &ce) {
				if ce.Reason == ReasonResidualConservation {
					return nil, failVerdict(ReasonNonMonotoneStep, ce.Detail)
				}
				return nil, failVerdict(ce.Reason, ce.Detail)
			}
			return nil, failVerdict(ReasonNonMonotoneStep, err.Error())
		}
		resolved, err := resolveSnapshotUnits(manifestByID, snap)
		if err != nil {
			return nil, failVerdict(ReasonNonMonotoneStep, err.Error())
		}

		currPass := make(map[string]uint64)
		for id, su := range resolved {
			if su.State == P {
				currPass[id] = su.Weight
			}
		}

		if i == 0 {
			if q.R != expectedR0 {
				return nil, failVerdict(ReasonBaselineMismatch, fmt.Sprintf("R0=%d != W-CarriedWeight=%d", q.R, expectedR0))
			}
		} else {
			if snap.Seq <= prevSeq {
				return nil, failVerdict(ReasonDuplicateSeq, fmt.Sprintf("snapshot[%d] seq %d <= prev seq %d", i, snap.Seq, prevSeq))
			}
			for id := range prevPass {
				if _, stillPass := currPass[id]; !stillPass {
					return nil, failVerdict(ReasonSilentPassRemoval, fmt.Sprintf("accepted pass %q removed at snapshot[%d]", id, i))
				}
			}
			var newlyAcceptedWeight uint64
			newlyAcceptedCount := 0
			for id, w := range currPass {
				if _, existed := prevPass[id]; !existed {
					newlyAcceptedCount++
					newlyAcceptedWeight, err = CheckedAddUint64(newlyAcceptedWeight, w)
					if err != nil {
						return nil, failVerdict(ReasonOverflow, err.Error())
					}
				}
			}
			prevR := rSeries[i-1]
			if q.R > prevR {
				return nil, failVerdict(ReasonUnrevokedObligationIncrease, fmt.Sprintf("R increased %d -> %d without epoch revocation", prevR, q.R))
			}
			if newlyAcceptedWeight > prevR {
				return nil, failVerdict(ReasonNegativeResidual, "newly accepted weight exceeds previous R")
			}
			expectedR := prevR - newlyAcceptedWeight
			if q.R != expectedR {
				return nil, failVerdict(ReasonNonMonotoneStep, fmt.Sprintf("R[%d]=%d != R[%d](%d)-newlyAccepted(%d)", i, q.R, i-1, prevR, newlyAcceptedWeight))
			}
			if newlyAcceptedCount > 0 {
				if prevR-q.R < 1 {
					return nil, failVerdict(ReasonNonMonotoneStep, "nonempty accepted set must decrease R by at least 1")
				}
			} else {
				if q.R != prevR {
					return nil, failVerdict(ReasonNonMonotoneStep, "empty accepted set must keep R equal")
				}
			}
		}

		rSeries[i] = q.R
		prevPass = currPass
		prevSeq = snap.Seq
	}

	return rSeries, passVerdict()
}

// EvaluateStall evaluates the STALL warning row:
// with a frozen positive D, R > 0 and (Now - LastVerifiedDecrease) >= D yields
// STALL_ZERO, STALL_SPIN, or STALL_ACTIVITY_UNKNOWN from observed activity.
// Idle or busy is never success.
func EvaluateStall(q Quantities, in StallInput) (StallKind, GateVerdict) {
	if q.R == 0 {
		return StallNone, passVerdict()
	}
	if in.ThresholdD == 0 {
		return StallActivityUnknown, failVerdict(ReasonRangeError, "frozen stall threshold D must be >= 1")
	}
	if in.Now < in.LastVerifiedDecrease {
		return StallActivityUnknown, failVerdict(ReasonClockRegression, "now < last_verified_decrease")
	}
	elapsed := in.Now - in.LastVerifiedDecrease
	if elapsed < in.ThresholdD {
		return StallNone, passVerdict()
	}
	if !in.ActivityKnown {
		return StallActivityUnknown, failVerdict(ReasonStallActivityUnknown, fmt.Sprintf("R=%d > 0 and elapsed %d >= D=%d with unknown activity", q.R, elapsed, in.ThresholdD))
	}
	if in.ObservedActivityOps == 0 {
		return StallZero, failVerdict(ReasonStallZero, fmt.Sprintf("R=%d > 0 and elapsed %d >= D=%d with zero activity", q.R, elapsed, in.ThresholdD))
	}
	return StallSpin, failVerdict(ReasonStallSpin, fmt.Sprintf("R=%d > 0 and elapsed %d >= D=%d with spinning activity=%d", q.R, elapsed, in.ThresholdD, in.ObservedActivityOps))
}

// Evaluate walks the gate order:
//  1. schema
//  2. identity and coverage
//  3. exact ranges
//  4. continuity
//  5. origin
//  6. progress (monotone)
//  7. warnings (stall)
//  8. lease closure
//  9. outcome
//
// A failed prerequisite stops dependent gates with ReasonPrerequisiteFailed.
func Evaluate(in EvaluationInput) EvaluationReport {
	var g Gates
	g.Stall = StallNone
	g.UnresolvedRevocations = in.UnresolvedRevocations
	g.UnresolvedViolations = in.UnresolvedViolations
	g.DeadlineViolations = in.DeadlineViolations

	var lastSnap Snapshot
	if len(in.Series) > 0 {
		lastSnap = in.Series[len(in.Series)-1]
	}

	// 1. SCHEMA
	g.Schema = EvaluateSchema(in.SchemaStats, in.Manifest, lastSnap)
	if g.Schema.Bit == BitZero {
		stopAfterSchema(&g)
		return EvaluationReport{Gates: g, Outcome: Outcome(Quantities{}, g)}
	}

	// 2. IDENTITY and COVERAGE
	g.Identity = EvaluateIdentity(in.Manifest, in.Series)
	g.Coverage = EvaluateCoverage(in.Coverage)
	if g.Identity.Bit == BitZero || g.Coverage.Bit == BitZero {
		reason := "identity"
		if g.Identity.Bit == BitOne {
			reason = "coverage"
		}
		stopAfterIdentityOrCoverage(&g, reason)
		return EvaluationReport{Gates: g, Outcome: Outcome(Quantities{}, g)}
	}

	// 3. EXACT RANGES
	stallD := in.Stall.ThresholdD
	if stallD == 0 {
		stallD = 1
	}
	q, rangesVerdict := EvaluateRanges(in.Manifest, lastSnap, stallD)
	if in.ClaimedRoundedPct != nil && *in.ClaimedRoundedPct == 100 && q.R > 0 {
		rangesVerdict = failVerdict(ReasonPositiveResidual, "rounded 100 percent claimed while residual R > 0")
	}
	if in.ClaimedComplete && q.R > 0 {
		rangesVerdict = failVerdict(ReasonPositiveResidual, "completion claimed while residual R > 0")
	}
	g.Ranges = rangesVerdict
	if g.Ranges.Bit == BitZero {
		stopAfterRanges(&g)
		return EvaluationReport{Quantities: q, Gates: g, Outcome: Outcome(q, g)}
	}

	// 4. CONTINUITY
	g.Continuity = EvaluateContinuity(q, in.Stages)
	if g.Continuity.Bit == BitZero {
		stopAfterContinuity(&g)
		return EvaluationReport{Quantities: q, Gates: g, Outcome: Outcome(q, g)}
	}

	// 5. ORIGIN
	g.Origin = EvaluateOrigin(in.Manifest, lastSnap, in.Origin)
	if g.Origin.Bit == BitZero {
		stopAfterOrigin(&g)
		return EvaluationReport{Quantities: q, Gates: g, Outcome: Outcome(q, g)}
	}

	// 6. PROGRESS (MONOTONE)
	_, g.Monotone = EvaluateMonotone(in.Manifest, in.Series)
	if g.Monotone.Bit == BitZero {
		stopAfterMonotone(&g)
		return EvaluationReport{Quantities: q, Gates: g, Outcome: Outcome(q, g)}
	}

	// 7. WARNINGS (STALL)
	if in.Stall.ThresholdD > 0 {
		g.Stall, g.Warnings = EvaluateStall(q, in.Stall)
	} else {
		g.Stall = StallNone
		g.Warnings = passVerdict()
	}
	if g.Warnings.Bit == BitZero {
		g.LeaseClosure = prereqVerdict("warnings")
		g.Success = prereqVerdict("warnings")
		return EvaluationReport{Quantities: q, Gates: g, Outcome: Outcome(q, g)}
	}

	// 8. LEASE CLOSURE
	g.LeaseClosure = EvaluateLeaseClosure(in.Coverage)
	if g.LeaseClosure.Bit == BitZero {
		g.Success = prereqVerdict("lease_closure")
		return EvaluationReport{Quantities: q, Gates: g, Outcome: Outcome(q, g)}
	}

	// 9. SUCCESS & OUTCOME
	g.Success = EvaluateSuccess(q, g)
	out := Outcome(q, g)
	return EvaluationReport{
		Quantities: q,
		Gates:      g,
		Outcome:    out,
	}
}

func stopAfterSchema(g *Gates) {
	p := prereqVerdict("schema")
	g.Identity = p
	g.Coverage = p
	g.Ranges = p
	g.Continuity = p
	g.Origin = p
	g.Monotone = p
	g.Warnings = p
	g.LeaseClosure = p
	g.Success = p
}

func stopAfterIdentityOrCoverage(g *Gates, which string) {
	p := prereqVerdict(which)
	g.Ranges = p
	g.Continuity = p
	g.Origin = p
	g.Monotone = p
	g.Warnings = p
	g.LeaseClosure = p
	g.Success = p
}

func stopAfterRanges(g *Gates) {
	p := prereqVerdict("ranges")
	g.Continuity = p
	g.Origin = p
	g.Monotone = p
	g.Warnings = p
	g.LeaseClosure = p
	g.Success = p
}

func stopAfterContinuity(g *Gates) {
	p := prereqVerdict("continuity")
	g.Origin = p
	g.Monotone = p
	g.Warnings = p
	g.LeaseClosure = p
	g.Success = p
}

func stopAfterOrigin(g *Gates) {
	p := prereqVerdict("origin")
	g.Monotone = p
	g.Warnings = p
	g.LeaseClosure = p
	g.Success = p
}

func stopAfterMonotone(g *Gates) {
	p := prereqVerdict("monotone")
	g.Warnings = p
	g.LeaseClosure = p
	g.Success = p
}

// EvaluateSuccess computes SUCCESS as the product of the required bits with R == 0,
// failure count == 0, unresolved revocations == 0, unresolved violations == 0, and deadline violations == 0.
func EvaluateSuccess(q Quantities, g Gates) GateVerdict {
	if !g.AllRequiredGatesPass() {
		return failVerdict(ReasonPrerequisiteFailed, "one or more required prerequisite gates failed")
	}
	if g.UnresolvedRevocations > 0 {
		return failVerdict(ReasonUnresolvedRevocations, "unresolved_revocations > 0")
	}
	if g.UnresolvedViolations > 0 {
		return failVerdict(ReasonUnresolvedViolations, "unresolved_violations > 0")
	}
	if g.DeadlineViolations > 0 {
		return failVerdict(ReasonDeadlineViolations, "deadline_violations > 0")
	}
	if q.FailureCount > 0 || q.FailureWeight > 0 {
		return failVerdict(ReasonAccountedFailuresPresent, "failure_count > 0")
	}
	if q.R != 0 || !IsTargetComplete(q) {
		return failVerdict(ReasonPositiveResidual, fmt.Sprintf("residual obligation R=%d > 0", q.R))
	}
	return passVerdict()
}

// Outcome determines the certificate Result:
//   - DONE_SUCCESS: every required gate is 1, SUCCESS is 1, R == 0, C/W == 1/1 by integer cross-product, A == 0, and FailureCount == 0
//   - DONE_ACCOUNTED_FAILURE: all prerequisite gates (schema, identity, coverage, ranges, continuity, origin, monotone, warnings, lease closure) pass,
//     A == 0, R > 0, and all remaining obligation is accounted for by valid terminal failures (FailureCount > 0, FailureWeight == R, T == W),
//     with no unresolved revocations, violations, or deadline violations
//   - BLOCKED_EVIDENCE: ORIGIN or another gate is blocked due to missing trust root or blocked evidence
//   - INDETERMINATE: any other invalid, incomplete, stalled, or in-progress state; invalid or incomplete evidence never becomes an accounted failure.
func Outcome(q Quantities, g Gates) Result {
	for _, v := range []GateVerdict{
		g.Schema, g.Identity, g.Coverage, g.Ranges, g.Continuity,
		g.Origin, g.Monotone, g.Warnings, g.LeaseClosure,
	} {
		if v.Reason == ReasonBlockedEvidence {
			return BLOCKED_EVIDENCE
		}
	}

	if !g.AllRequiredGatesPass() {
		return INDETERMINATE
	}
	if g.Stall != "" && g.Stall != StallNone {
		return INDETERMINATE
	}
	if g.UnresolvedRevocations > 0 || g.UnresolvedViolations > 0 || g.DeadlineViolations > 0 {
		return INDETERMINATE
	}

	if q.N < 1 || q.W < 1 || q.I != q.N {
		return INDETERMINATE
	}
	if do, err := CheckedAddUint64(q.D, q.O); err != nil || do != q.N {
		return INDETERMINATE
	}
	if ta, err := CheckedAddUint64(q.T, q.A); err != nil || ta != q.W {
		return INDETERMINATE
	}
	if cr, err := CheckedAddUint64(q.C, q.R); err != nil || cr != q.W {
		return INDETERMINATE
	}

	if g.Success.Bit == BitOne && q.R == 0 && q.A == 0 && q.FailureCount == 0 && q.FailureWeight == 0 && IsTargetComplete(q) {
		return DONE_SUCCESS
	}

	if q.A == 0 && q.R > 0 && q.T == q.W && q.FailureCount > 0 && q.FailureWeight == q.R {
		return DONE_ACCOUNTED_FAILURE
	}

	return INDETERMINATE
}

// RevocationRecord documents the revocation of an epoch due to a forged accepted receipt
// and the linked recovery epoch that restores the revoked obligation to its baseline R0.
type RevocationRecord struct {
	RevokedEpoch        uint64   `json:"revoked_epoch"`
	RecoveryEpoch       uint64   `json:"recovery_epoch"`
	ForgedUnitIDs       []string `json:"forged_unit_ids"`
	RestoredWeight      uint64   `json:"restored_weight"`
	PreviousResidualR   uint64   `json:"previous_residual_r"`
	RecoveryBaselineR0  uint64   `json:"recovery_baseline_r0"`
	RecoveryCarriedW    uint64   `json:"recovery_carried_weight"`
	RecoveryManifestSHA string   `json:"recovery_manifest_sha256"`
}

// RevokeAndOpenRecoveryEpoch revokes m.Epoch when one or more accepted receipts in s are found to be forged,
// and constructs a linked recovery epoch (recoveryEpoch > m.Epoch) whose baseline R0 restores the revoked obligation.
// The obligation increase is never disguised as monotone progress inside the revoked epoch.
func RevokeAndOpenRecoveryEpoch(m Manifest, s Snapshot, forgedUnitIDs []string, recoveryEpoch uint64) (RevocationRecord, Manifest, Snapshot, error) {
	if len(forgedUnitIDs) == 0 {
		return RevocationRecord{}, Manifest{}, Snapshot{}, errInput(ReasonRequiredMissing, "forgedUnitIDs must be non-empty")
	}
	if recoveryEpoch <= m.Epoch {
		return RevocationRecord{}, Manifest{}, Snapshot{}, errInput(ReasonRangeError, "recoveryEpoch must be strictly greater than revoked epoch")
	}

	qPrev, err := Compute(m, s)
	if err != nil {
		return RevocationRecord{}, Manifest{}, Snapshot{}, err
	}

	manifestByID := make(map[string]Unit, len(m.Units))
	for _, u := range m.Units {
		manifestByID[u.ID] = u
	}
	resolved, err := resolveSnapshotUnits(manifestByID, s)
	if err != nil {
		return RevocationRecord{}, Manifest{}, Snapshot{}, err
	}

	forgedSet := make(map[string]struct{}, len(forgedUnitIDs))
	var restoredWeight uint64
	for _, id := range forgedUnitIDs {
		if _, dup := forgedSet[id]; dup {
			return RevocationRecord{}, Manifest{}, Snapshot{}, errInput(ReasonDuplicateKey, fmt.Sprintf("duplicate forged unit ID %q", id))
		}
		su, ok := resolved[id]
		if !ok {
			return RevocationRecord{}, Manifest{}, Snapshot{}, errGate(ReasonForeignID, fmt.Sprintf("forged unit ID %q not in manifest", id))
		}
		if su.State != P {
			return RevocationRecord{}, Manifest{}, Snapshot{}, errGate(ReasonInvalidTransition, fmt.Sprintf("unit %q is in state %s, not accepted pass P", id, su.State))
		}
		forgedSet[id] = struct{}{}
		restoredWeight, err = CheckedAddUint64(restoredWeight, su.Weight)
		if err != nil {
			return RevocationRecord{}, Manifest{}, Snapshot{}, err
		}
	}

	if restoredWeight > qPrev.C {
		return RevocationRecord{}, Manifest{}, Snapshot{}, errGate(ReasonRangeError, "restored weight exceeds certified weight C")
	}
	newCarried := qPrev.C - restoredWeight
	newBaselineR0, err := CheckedAddUint64(qPrev.R, restoredWeight)
	if err != nil {
		return RevocationRecord{}, Manifest{}, Snapshot{}, err
	}

	recoveryUnits := make([]Unit, 0, len(m.Units))
	initialSnapUnits := make([]Unit, 0, len(m.Units))
	var carriedIDs []string

	for _, mu := range m.Units {
		su := resolved[mu.ID]
		if _, isForged := forgedSet[mu.ID]; isForged {
			resetUnit := Unit{
				ID:     mu.ID,
				Weight: mu.Weight,
				State:  Q,
			}
			recoveryUnits = append(recoveryUnits, resetUnit)
			initialSnapUnits = append(initialSnapUnits, resetUnit)
		} else {
			if su.State == P {
				carriedIDs = append(carriedIDs, mu.ID)
			}
			recoveryUnits = append(recoveryUnits, mu)
			initialSnapUnits = append(initialSnapUnits, su)
		}
	}

	recManifest := Manifest{
		Epoch:          recoveryEpoch,
		Units:          recoveryUnits,
		CarriedWeight:  newCarried,
		CarriedUnitIDs: carriedIDs,
		TrustRoot:      m.TrustRoot,
		PreviousEpoch:  m.Epoch,
		RevokedEpoch:   m.Epoch,
	}
	h := sha256.Sum256([]byte(fmt.Sprintf("%s:revoked:%d->%d:%d", m.ManifestSHA256, m.Epoch, recoveryEpoch, newCarried)))
	recManifest.ManifestSHA256 = hex.EncodeToString(h[:])

	recSnap := Snapshot{
		Seq:   s.Seq + 1,
		Units: initialSnapUnits,
	}

	rec := RevocationRecord{
		RevokedEpoch:        m.Epoch,
		RecoveryEpoch:       recoveryEpoch,
		ForgedUnitIDs:       append([]string(nil), forgedUnitIDs...),
		RestoredWeight:      restoredWeight,
		PreviousResidualR:   qPrev.R,
		RecoveryBaselineR0:  newBaselineR0,
		RecoveryCarriedW:    newCarried,
		RecoveryManifestSHA: recManifest.ManifestSHA256,
	}

	return rec, recManifest, recSnap, nil
}

// AggregatorPort is the narrow consumer-side port interface for the P2-07 aggregator,
// feeding Manifest and Snapshot series as plain structs without importing unmerged batch-1 packages.
type AggregatorPort interface {
	Manifest() (Manifest, error)
	Snapshots() ([]Snapshot, error)
}

// HarvestAccount represents the accounting payload supplied by the W8X-03 harvest account.
type HarvestAccount struct {
	Stages                []StageFlow      `json:"stages,omitempty"`
	Origin                OriginEvidence   `json:"origin"`
	Coverage              CoverageEvidence `json:"coverage"`
	Stall                 StallInput       `json:"stall"`
	UnresolvedRevocations uint64           `json:"unresolved_revocations"`
	UnresolvedViolations  uint64           `json:"unresolved_violations"`
	DeadlineViolations    uint64           `json:"deadline_violations"`
}

// HarvestAccountPort is the narrow consumer-side port interface for the W8X-03 harvest account.
type HarvestAccountPort interface {
	Account() (HarvestAccount, error)
}

// EvaluateFromPorts evaluates a certificate from the consumer-side AggregatorPort and HarvestAccountPort interfaces.
func EvaluateFromPorts(agg AggregatorPort, harv HarvestAccountPort) (EvaluationReport, error) {
	if agg == nil || harv == nil {
		return EvaluationReport{}, errInput(ReasonRequiredMissing, "aggregator and harvest account ports must be non-nil")
	}
	m, err := agg.Manifest()
	if err != nil {
		return EvaluationReport{}, err
	}
	snaps, err := agg.Snapshots()
	if err != nil {
		return EvaluationReport{}, err
	}
	acc, err := harv.Account()
	if err != nil {
		return EvaluationReport{}, err
	}
	in := EvaluationInput{
		Manifest:              m,
		Series:                snaps,
		Stages:                acc.Stages,
		Origin:                acc.Origin,
		Coverage:              acc.Coverage,
		Stall:                 acc.Stall,
		UnresolvedRevocations: acc.UnresolvedRevocations,
		UnresolvedViolations:  acc.UnresolvedViolations,
		DeadlineViolations:    acc.DeadlineViolations,
	}
	return Evaluate(in), nil
}

// CanonicalSidecar encodes any host sidecar payload using workerbridge.Canonical (src/workerbridge/json.go:198).
func CanonicalSidecar(v any) ([]byte, error) {
	b, err := workerbridge.Canonical(v)
	if err != nil {
		cls := workerbridge.Class(err)
		switch cls {
		case "DUPLICATE_KEY":
			return nil, errInput(ReasonDuplicateKey, err.Error())
		case "NONCANONICAL_NUMBER":
			return nil, errInput(ReasonTypeError, err.Error())
		default:
			return nil, errInput(ReasonTypeError, err.Error())
		}
	}
	return b, nil
}

// EvaluateJSON strictly parses a JSON sidecar/ledger payload using workerbridge.Parse
// (rejecting truncated JSON, floats, string-to-uint coercion, null-to-zero counters,
// duplicate keys, and missing required keys) and evaluates the certificate gate chain.
func EvaluateJSON(data []byte) EvaluationReport {
	in, verdict := parseStrictEvaluationJSON(data)
	if verdict.Bit == BitZero {
		var g Gates
		g.Stall = StallNone
		g.Schema = verdict
		stopAfterSchema(&g)
		return EvaluationReport{Gates: g, Outcome: Outcome(Quantities{}, g)}
	}
	return Evaluate(in)
}

func parseStrictEvaluationJSON(data []byte) (EvaluationInput, GateVerdict) {
	if len(strings.TrimSpace(string(data))) == 0 {
		return EvaluationInput{}, failVerdict(ReasonEmptyLedger, "empty JSON payload")
	}
	doc, err := workerbridge.Parse(data)
	if err != nil {
		cls := workerbridge.Class(err)
		switch cls {
		case "DUPLICATE_KEY":
			return EvaluationInput{}, failVerdict(ReasonDuplicateKey, err.Error())
		case "NONCANONICAL_NUMBER":
			return EvaluationInput{}, failVerdict(ReasonTypeError, err.Error())
		default:
			return EvaluationInput{}, failVerdict(ReasonTypeError, err.Error())
		}
	}
	if len(doc) == 0 {
		return EvaluationInput{}, failVerdict(ReasonEmptyLedger, "empty JSON object")
	}

	for _, reqKey := range []string{"manifest", "series", "origin", "coverage"} {
		v, ok := doc[reqKey]
		if !ok {
			return EvaluationInput{}, failVerdict(ReasonRequiredMissing, fmt.Sprintf("missing required key %q", reqKey))
		}
		if v == nil {
			return EvaluationInput{}, failVerdict(ReasonTypeError, fmt.Sprintf("null value for required key %q", reqKey))
		}
	}

	mObj, ok := doc["manifest"].(map[string]any)
	if !ok {
		return EvaluationInput{}, failVerdict(ReasonTypeError, "manifest must be an object")
	}
	for _, k := range []string{"manifest_sha256", "epoch", "units", "carried_weight"} {
		if _, exists := mObj[k]; !exists {
			return EvaluationInput{}, failVerdict(ReasonRequiredMissing, fmt.Sprintf("manifest missing key %q", k))
		}
		if mObj[k] == nil {
			return EvaluationInput{}, failVerdict(ReasonTypeError, fmt.Sprintf("manifest key %q is null", k))
		}
	}
	shaStr, ok := mObj["manifest_sha256"].(string)
	if !ok || strings.TrimSpace(shaStr) == "" {
		return EvaluationInput{}, failVerdict(ReasonTypeError, "manifest_sha256 must be a non-empty string")
	}
	epochVal, ok, r := parseStrictUint64(mObj["epoch"])
	if !ok {
		return EvaluationInput{}, failVerdict(r, "invalid manifest.epoch")
	}
	carriedVal, ok, r := parseStrictUint64(mObj["carried_weight"])
	if !ok {
		return EvaluationInput{}, failVerdict(r, "invalid manifest.carried_weight")
	}
	unitsArr, ok := mObj["units"].([]any)
	if !ok {
		return EvaluationInput{}, failVerdict(ReasonTypeError, "manifest.units must be an array")
	}
	if len(unitsArr) == 0 {
		return EvaluationInput{}, failVerdict(ReasonEmptySuite, "manifest.units is empty")
	}
	units := make([]Unit, 0, len(unitsArr))
	for i, rawU := range unitsArr {
		uObj, ok := rawU.(map[string]any)
		if !ok {
			return EvaluationInput{}, failVerdict(ReasonTypeError, fmt.Sprintf("manifest.units[%d] must be an object", i))
		}
		u, v := parseStrictUnit(uObj, false)
		if v.Bit == BitZero {
			return EvaluationInput{}, v
		}
		units = append(units, u)
	}

	seriesArr, ok := doc["series"].([]any)
	if !ok {
		return EvaluationInput{}, failVerdict(ReasonTypeError, "series must be an array")
	}
	if len(seriesArr) == 0 {
		return EvaluationInput{}, failVerdict(ReasonEmptyLedger, "series ledger is empty")
	}
	series := make([]Snapshot, 0, len(seriesArr))
	for i, rawS := range seriesArr {
		sObj, ok := rawS.(map[string]any)
		if !ok {
			return EvaluationInput{}, failVerdict(ReasonTypeError, fmt.Sprintf("series[%d] must be an object", i))
		}
		seqVal, ok, r := parseStrictRequiredUint64(sObj, "seq")
		if !ok {
			return EvaluationInput{}, failVerdict(r, fmt.Sprintf("series[%d].seq invalid", i))
		}
		rawUnits, hasUnits := sObj["units"]
		if !hasUnits || rawUnits == nil {
			return EvaluationInput{}, failVerdict(ReasonRequiredMissing, fmt.Sprintf("series[%d].units missing or null", i))
		}
		sUnitsArr, ok := rawUnits.([]any)
		if !ok {
			return EvaluationInput{}, failVerdict(ReasonTypeError, fmt.Sprintf("series[%d].units must be an array", i))
		}
		sUnits := make([]Unit, 0, len(sUnitsArr))
		for j, rawSU := range sUnitsArr {
			suObj, ok := rawSU.(map[string]any)
			if !ok {
				return EvaluationInput{}, failVerdict(ReasonTypeError, fmt.Sprintf("series[%d].units[%d] must be an object", i, j))
			}
			su, v := parseStrictUnit(suObj, true)
			if v.Bit == BitZero {
				return EvaluationInput{}, v
			}
			sUnits = append(sUnits, su)
		}
		series = append(series, Snapshot{Seq: seqVal, Units: sUnits})
	}

	origObj, ok := doc["origin"].(map[string]any)
	if !ok {
		return EvaluationInput{}, failVerdict(ReasonTypeError, "origin must be an object")
	}
	trustRoot, ok := origObj["trust_root"].(string)
	if !ok {
		return EvaluationInput{}, failVerdict(ReasonTypeError, "origin.trust_root must be a string")
	}
	replayCount, ok, r := parseStrictRequiredUint64(origObj, "replay_count")
	if !ok {
		return EvaluationInput{}, failVerdict(r, "origin.replay_count invalid")
	}
	verRej, ok, r := parseStrictRequiredUint64(origObj, "verifier_rejections_among_p")
	if !ok {
		return EvaluationInput{}, failVerdict(r, "origin.verifier_rejections_among_p invalid")
	}

	covObj, ok := doc["coverage"].(map[string]any)
	if !ok {
		return EvaluationInput{}, failVerdict(ReasonTypeError, "coverage must be an object")
	}
	dups, ok, r := parseStrictRequiredUint64(covObj, "duplicates")
	if !ok {
		return EvaluationInput{}, failVerdict(r, "coverage.duplicates invalid")
	}
	clockRegs, ok, r := parseStrictRequiredUint64(covObj, "clock_regressions")
	if !ok {
		return EvaluationInput{}, failVerdict(r, "coverage.clock_regressions invalid")
	}
	uncovered, ok, r := parseStrictRequiredUint64(covObj, "uncovered_billed_ticks")
	if !ok {
		return EvaluationInput{}, failVerdict(r, "coverage.uncovered_billed_ticks invalid")
	}
	resets, ok, r := parseStrictRequiredUint64(covObj, "counter_resets_without_boot_change")
	if !ok {
		return EvaluationInput{}, failVerdict(r, "coverage.counter_resets_without_boot_change invalid")
	}
	openLeases, ok, r := parseStrictRequiredUint64(covObj, "open_leases")
	if !ok {
		return EvaluationInput{}, failVerdict(r, "coverage.open_leases invalid")
	}
	unfenced, ok, r := parseStrictRequiredUint64(covObj, "unfenced_or_unclean_members")
	if !ok {
		return EvaluationInput{}, failVerdict(r, "coverage.unfenced_or_unclean_members invalid")
	}
	rawSources, hasSources := covObj["sources"]
	if !hasSources || rawSources == nil {
		return EvaluationInput{}, failVerdict(ReasonRequiredMissing, "coverage.sources missing or null")
	}
	srcArr, ok := rawSources.([]any)
	if !ok {
		return EvaluationInput{}, failVerdict(ReasonTypeError, "coverage.sources must be an array")
	}
	sources := make([]MonitorSource, 0, len(srcArr))
	for i, rawSrc := range srcArr {
		srcObj, ok := rawSrc.(map[string]any)
		if !ok {
			return EvaluationInput{}, failVerdict(ReasonTypeError, fmt.Sprintf("coverage.sources[%d] must be an object", i))
		}
		sid, ok := srcObj["source_id"].(string)
		if !ok || strings.TrimSpace(sid) == "" {
			return EvaluationInput{}, failVerdict(ReasonRequiredMissing, "source_id missing or non-string")
		}
		tArr, ok := srcObj["ticks"].([]any)
		if !ok {
			return EvaluationInput{}, failVerdict(ReasonTypeError, "ticks must be an array")
		}
		ticks := make([]MonitorTick, 0, len(tArr))
		for _, rawTick := range tArr {
			tObj, ok := rawTick.(map[string]any)
			if !ok {
				return EvaluationInput{}, failVerdict(ReasonTypeError, "tick must be an object")
			}
			tSeq, ok, r := parseStrictRequiredUint64(tObj, "seq")
			if !ok {
				return EvaluationInput{}, failVerdict(r, "tick.seq invalid")
			}
			tClk, ok, r := parseStrictRequiredUint64(tObj, "clock_ns")
			if !ok {
				return EvaluationInput{}, failVerdict(r, "tick.clock_ns invalid")
			}
			bootID, ok := tObj["boot_id"].(string)
			if !ok {
				return EvaluationInput{}, failVerdict(ReasonTypeError, "tick.boot_id must be a string")
			}
			tCnt, ok, r := parseStrictRequiredUint64(tObj, "counter")
			if !ok {
				return EvaluationInput{}, failVerdict(r, "tick.counter invalid")
			}
			tBilled, ok, r := parseStrictRequiredUint64(tObj, "billed_ticks")
			if !ok {
				return EvaluationInput{}, failVerdict(r, "tick.billed_ticks invalid")
			}
			tCov, ok, r := parseStrictRequiredUint64(tObj, "covered_ticks")
			if !ok {
				return EvaluationInput{}, failVerdict(r, "tick.covered_ticks invalid")
			}
			ticks = append(ticks, MonitorTick{
				Seq:          tSeq,
				ClockNs:      tClk,
				BootID:       bootID,
				Counter:      tCnt,
				BilledTicks:  tBilled,
				CoveredTicks: tCov,
			})
		}
		sources = append(sources, MonitorSource{SourceID: sid, Ticks: ticks})
	}

	in := EvaluationInput{
		Manifest: Manifest{
			ManifestSHA256: shaStr,
			Epoch:          epochVal,
			Units:          units,
			CarriedWeight:  carriedVal,
			TrustRoot:      trustRoot,
		},
		Series: series,
		Origin: OriginEvidence{
			TrustRoot:                trustRoot,
			ReplayCount:              replayCount,
			VerifierRejectionsAmongP: verRej,
		},
		Coverage: CoverageEvidence{
			Sources:                        sources,
			Duplicates:                     dups,
			ClockRegressions:               clockRegs,
			UncoveredBilledTicks:           uncovered,
			CounterResetsWithoutBootChange: resets,
			OpenLeases:                     openLeases,
			UnfencedOrUncleanMembers:       unfenced,
		},
	}
	return in, passVerdict()
}

func parseStrictUnit(uObj map[string]any, requireState bool) (Unit, GateVerdict) {
	rawID, exists := uObj["id"]
	if !exists {
		return Unit{}, failVerdict(ReasonRequiredMissing, "unit missing id")
	}
	idStr, ok := rawID.(string)
	if !ok || strings.TrimSpace(idStr) == "" {
		return Unit{}, failVerdict(ReasonTypeError, "unit.id must be a non-empty string")
	}
	wVal, ok, r := parseStrictRequiredUint64(uObj, "weight")
	if !ok {
		return Unit{}, failVerdict(r, "unit.weight invalid")
	}
	if wVal < 1 {
		return Unit{}, failVerdict(ReasonZeroWeight, "unit.weight must be >= 1")
	}
	var st UnitState
	if rawSt, hasSt := uObj["state"]; hasSt {
		stStr, ok := rawSt.(string)
		if !ok {
			return Unit{}, failVerdict(ReasonTypeError, "unit.state must be a string")
		}
		st = UnitState(stStr)
		if !st.Valid() {
			return Unit{}, failVerdict(ReasonInvalidTerminalBit, "unit.state invalid")
		}
	} else if requireState {
		return Unit{}, failVerdict(ReasonRequiredMissing, "snapshot unit missing state")
	}

	b1, ok, r := parseStrictBit(uObj, "verified_io")
	if !ok {
		return Unit{}, failVerdict(r, "verified_io invalid")
	}
	b2, ok, r := parseStrictBit(uObj, "origin_bound")
	if !ok {
		return Unit{}, failVerdict(r, "origin_bound invalid")
	}
	b3, ok, r := parseStrictBit(uObj, "fresh_run")
	if !ok {
		return Unit{}, failVerdict(r, "fresh_run invalid")
	}
	b4, ok, r := parseStrictBit(uObj, "authority_current")
	if !ok {
		return Unit{}, failVerdict(r, "authority_current invalid")
	}

	return Unit{
		ID:               idStr,
		Weight:           wVal,
		State:            st,
		VerifiedIO:       b1,
		OriginBound:      b2,
		FreshRun:         b3,
		AuthorityCurrent: b4,
	}, passVerdict()
}

func parseStrictBit(obj map[string]any, key string) (uint8, bool, Reason) {
	raw, exists := obj[key]
	if !exists {
		return 0, true, ReasonOK
	}
	v, ok, r := parseStrictUint64(raw)
	if !ok {
		return 0, false, r
	}
	if v > 1 {
		return 0, false, ReasonRangeError
	}
	return uint8(v), true, ReasonOK
}

func parseStrictRequiredUint64(obj map[string]any, key string) (uint64, bool, Reason) {
	raw, exists := obj[key]
	if !exists {
		return 0, false, ReasonRequiredMissing
	}
	return parseStrictUint64(raw)
}

func parseStrictUint64(raw any) (uint64, bool, Reason) {
	if raw == nil {
		// Never coerce null to zero.
		return 0, false, ReasonTypeError
	}
	num, ok := raw.(json.Number)
	if !ok {
		// Reject string, bool, float, object, array coercion.
		return 0, false, ReasonTypeError
	}
	s := string(num)
	if strings.HasPrefix(s, "-") {
		return 0, false, ReasonRangeError
	}
	val, err := strconv.ParseUint(s, 10, 64)
	if err != nil {
		if errors.Is(err, strconv.ErrRange) {
			return math.MaxUint64, false, ReasonOverflow
		}
		return 0, false, ReasonTypeError
	}
	return val, true, ReasonOK
}

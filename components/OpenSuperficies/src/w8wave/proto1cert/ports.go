// Package proto1cert projects independently supplied aggregator and harvest evidence
// into the existing certificate gate evaluator. It has no persistence, launcher,
// transport, clock or cleanup authority.
package proto1cert

import (
	"context"
	"errors"
	"fmt"
	"reflect"
	"strings"

	"opensuperficies.local/srf/src/w8wave/certificate"
)

// AggregatorEvidence is a frozen projection of one obligation manifest and its
// ordered lineage. Its getters return defensive copies, never retained storage.
// It does not implement an aggregator producer or synthesize accepted receipts.
type AggregatorEvidence struct {
	manifest  certificate.Manifest
	snapshots []certificate.Snapshot
}

// HarvestEvidence is a frozen projection of stage, origin, monitor, stall, lease
// and unresolved-counter evidence. It cannot publish a cleanup certificate.
type HarvestEvidence struct {
	account certificate.HarvestAccount
}

var (
	_ certificate.AggregatorPort     = (*AggregatorEvidence)(nil)
	_ certificate.HarvestAccountPort = (*HarvestEvidence)(nil)
)

// NewAggregatorEvidence validates the presence of required source fields and
// freezes every mutable field. It deliberately leaves gate outcomes to certificate.
func NewAggregatorEvidence(manifest certificate.Manifest, snapshots []certificate.Snapshot) (*AggregatorEvidence, error) {
	frozen := &AggregatorEvidence{manifest: copyManifest(manifest), snapshots: copySnapshots(snapshots)}
	if err := requiredManifest(frozen.manifest); err != nil {
		return nil, err
	}
	if err := requiredSnapshots(frozen.manifest, frozen.snapshots); err != nil {
		return nil, err
	}
	return frozen, nil
}

// Manifest implements certificate.AggregatorPort without aliasing caller storage.
func (a *AggregatorEvidence) Manifest() (certificate.Manifest, error) {
	if a == nil {
		return certificate.Manifest{}, missing("aggregator evidence port")
	}
	if err := requiredManifest(a.manifest); err != nil {
		return certificate.Manifest{}, err
	}
	return copyManifest(a.manifest), nil
}

// Snapshots implements certificate.AggregatorPort with a real ordered lineage.
func (a *AggregatorEvidence) Snapshots() ([]certificate.Snapshot, error) {
	if a == nil {
		return nil, missing("aggregator evidence port")
	}
	if err := requiredManifest(a.manifest); err != nil {
		return nil, err
	}
	if err := requiredSnapshots(a.manifest, a.snapshots); err != nil {
		return nil, err
	}
	return copySnapshots(a.snapshots), nil
}

// NewHarvestEvidence freezes independently supplied harvest account evidence.
func NewHarvestEvidence(account certificate.HarvestAccount) (*HarvestEvidence, error) {
	frozen := &HarvestEvidence{account: copyAccount(account)}
	if err := requiredAccount(frozen.account); err != nil {
		return nil, err
	}
	return frozen, nil
}

// Account implements certificate.HarvestAccountPort without narrowing the seven
// fields or turning missing evidence into a zero-valued passing account.
func (h *HarvestEvidence) Account() (certificate.HarvestAccount, error) {
	if h == nil {
		return certificate.HarvestAccount{}, missing("harvest account port")
	}
	if err := requiredAccount(h.account); err != nil {
		return certificate.HarvestAccount{}, err
	}
	return copyAccount(h.account), nil
}

// Evaluate is the Area A projection boundary. It reads producer ports once,
// checks required structural presence, freezes the source payloads, then uses
// certificate.EvaluateFromPorts for ALL gate and outcome decisions. Even a nil
// evaluation error does not imply DONE_SUCCESS: callers must inspect Outcome.
// No journal, publication or cleanup side effect exists in this package.
func Evaluate(agg certificate.AggregatorPort, harv certificate.HarvestAccountPort) (certificate.EvaluationReport, error) {
	if nilPort(agg) || nilPort(harv) {
		return certificate.EvaluationReport{}, missing("aggregator and harvest account ports must be non-nil")
	}
	manifest, err := agg.Manifest()
	if err != nil {
		return certificate.EvaluationReport{}, producerError("aggregator.Manifest", err)
	}
	// Freeze each producer value immediately; accessors need not themselves be
	// immutable, but the caller must not mutate a value during its copying.
	manifest = copyManifest(manifest)
	snapshots, err := agg.Snapshots()
	if err != nil {
		return certificate.EvaluationReport{}, producerError("aggregator.Snapshots", err)
	}
	snapshots = copySnapshots(snapshots)
	account, err := harv.Account()
	if err != nil {
		return certificate.EvaluationReport{}, producerError("harvest.Account", err)
	}
	account = copyAccount(account)

	frozenAgg, err := NewAggregatorEvidence(manifest, snapshots)
	if err != nil {
		return certificate.EvaluationReport{}, err
	}
	frozenHarv, err := NewHarvestEvidence(account)
	if err != nil {
		return certificate.EvaluationReport{}, err
	}
	return certificate.EvaluateFromPorts(frozenAgg, frozenHarv)
}

func missing(what string) error {
	return &certificate.Error{Code: 2, Reason: certificate.ReasonRequiredMissing, Detail: what + " is missing"}
}

func invalid(reason certificate.Reason, detail string) error {
	return &certificate.Error{Code: 2, Reason: reason, Detail: detail}
}

// producerError preserves normalized certificate refusal codes (including
// unavailable authority 4 and uncertain cleanup 6). Unknown errors are
// verification failures (5), not the unnormalized catch-all rc 1.
func producerError(port string, err error) error {
	var ce *certificate.Error
	if errors.As(err, &ce) && ce != nil && ce.Code >= 2 && ce.Code <= 7 {
		return err
	}
	if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
		return &certificate.Error{Code: 6, Reason: certificate.ReasonBlockedEvidence, Detail: fmt.Sprintf("%s: uncertain producer cleanup: %v", port, err)}
	}
	return &certificate.Error{Code: 5, Reason: certificate.ReasonVerifierRejection, Detail: fmt.Sprintf("%s: producer verification failed: %v", port, err)}
}

func nilPort(port any) bool {
	if port == nil {
		return true
	}
	v := reflect.ValueOf(port)
	switch v.Kind() {
	case reflect.Chan, reflect.Func, reflect.Interface, reflect.Map, reflect.Pointer, reflect.Slice:
		return v.IsNil()
	default:
		return false
	}
}

func requiredManifest(m certificate.Manifest) error {
	if strings.TrimSpace(m.ManifestSHA256) == "" {
		return missing("manifest_sha256")
	}
	if len(m.ManifestSHA256) != 64 {
		return invalid(certificate.ReasonTypeError, "manifest_sha256 must be 64 lowercase hexadecimal characters")
	}
	for _, ch := range m.ManifestSHA256 {
		if (ch < '0' || ch > '9') && (ch < 'a' || ch > 'f') {
			return invalid(certificate.ReasonTypeError, "manifest_sha256 must be 64 lowercase hexadecimal characters")
		}
	}
	if len(m.Units) == 0 {
		return missing("manifest obligation units")
	}
	for i, u := range m.Units {
		if strings.TrimSpace(u.ID) == "" {
			return missing(fmt.Sprintf("manifest unit[%d].id", i))
		}
		if u.Weight == 0 {
			return invalid(certificate.ReasonZeroWeight, fmt.Sprintf("manifest unit[%d].weight must be >= 1", i))
		}
	}
	return nil
}

func requiredSnapshots(m certificate.Manifest, snapshots []certificate.Snapshot) error {
	if len(snapshots) == 0 {
		return missing("snapshot lineage")
	}
	// One baseline and at most two state transitions (Q -> X -> P/F) for
	// each frozen obligation. This is a finite TARGET budget, never a retry.
	// The division form also avoids overflow on a very large inventory.
	if len(snapshots) > 1 && (len(snapshots)-2)/2 >= len(m.Units) {
		return invalid(certificate.ReasonRangeError, "snapshot lineage exceeds 2*N+1 frozen-inventory budget")
	}
	for i, s := range snapshots {
		if len(s.Units) == 0 && len(s.States) == 0 {
			return missing(fmt.Sprintf("snapshot[%d] unit states", i))
		}
		for j, u := range s.Units {
			if strings.TrimSpace(u.ID) == "" {
				return missing(fmt.Sprintf("snapshot[%d].units[%d].id", i, j))
			}
		}
		for id := range s.States {
			if strings.TrimSpace(id) == "" {
				return missing(fmt.Sprintf("snapshot[%d].states key", i))
			}
		}
	}
	return nil
}

func requiredAccount(a certificate.HarvestAccount) error {
	if len(a.Stages) == 0 {
		return missing("harvest.stages")
	}
	for i, s := range a.Stages {
		if strings.TrimSpace(s.StageID) == "" {
			return missing(fmt.Sprintf("harvest.stages[%d].stage_id", i))
		}
	}
	if strings.TrimSpace(a.Origin.TrustRoot) == "" {
		return missing("harvest.origin.trust_root")
	}
	if len(a.Coverage.Sources) == 0 {
		return missing("harvest.coverage.sources")
	}
	for i, src := range a.Coverage.Sources {
		if strings.TrimSpace(src.SourceID) == "" {
			return missing(fmt.Sprintf("harvest.coverage.sources[%d].source_id", i))
		}
		if len(src.Ticks) == 0 {
			return missing(fmt.Sprintf("harvest.coverage.sources[%d].ticks", i))
		}
		for j, tick := range src.Ticks {
			if strings.TrimSpace(tick.BootID) == "" {
				return missing(fmt.Sprintf("harvest.coverage.sources[%d].ticks[%d].boot_id", i, j))
			}
		}
	}
	if a.Stall.ThresholdD == 0 {
		return missing("harvest.stall.threshold_d")
	}
	return nil
}

func copyManifest(m certificate.Manifest) certificate.Manifest {
	m.Units = append([]certificate.Unit(nil), m.Units...)
	m.CarriedUnitIDs = append([]string(nil), m.CarriedUnitIDs...)
	return m
}

func copySnapshots(snapshots []certificate.Snapshot) []certificate.Snapshot {
	if snapshots == nil {
		return nil
	}
	out := make([]certificate.Snapshot, len(snapshots))
	for i, snap := range snapshots {
		out[i] = snap
		out[i].Units = append([]certificate.Unit(nil), snap.Units...)
		if snap.States != nil {
			out[i].States = make(map[string]certificate.UnitState, len(snap.States))
			for id, state := range snap.States {
				out[i].States[id] = state
			}
		}
		if snap.ReportedR != nil {
			v := *snap.ReportedR
			out[i].ReportedR = &v
		}
	}
	return out
}

func copyAccount(a certificate.HarvestAccount) certificate.HarvestAccount {
	a.Stages = append([]certificate.StageFlow(nil), a.Stages...)
	if a.Coverage.Sources != nil {
		sources := make([]certificate.MonitorSource, len(a.Coverage.Sources))
		for i, src := range a.Coverage.Sources {
			sources[i] = src
			sources[i].Ticks = append([]certificate.MonitorTick(nil), src.Ticks...)
		}
		a.Coverage.Sources = sources
	}
	return a
}

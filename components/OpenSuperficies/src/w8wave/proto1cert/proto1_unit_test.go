package proto1cert_test

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math/rand"
	"sync"
	"sync/atomic"
	"testing"

	"opensuperficies.local/srf/src/w8wave/certificate"
	"opensuperficies.local/srf/src/w8wave/proto1cert"
)

// The following are independently authored, immutable expected inputs. No
// Compute, Evaluate, fixture-success flag or production digest helper is used
// to obtain expected obligations, digests or gate outcomes.
const (
	inventoryBytes = "alpha:2\nbeta:3\ngamma:1\n"
	inventorySHA   = "f6ce7fa27b740f65daef3288c689bb4d040f2b13479632244f7433f2a6f5491a"
	originBytes    = "p1-os-certificate:trusted-monitor-origin:v1"
	originSHA      = "081ff6d98838381103763dda5505bfe306718e85221c645c2269c330e36c6319"
	trustRoot      = "sha256:" + originSHA
	obligations    = uint64(2 + 3 + 1)
)

// The extra methods intentionally make side effects observable if the consumer
// starts calling APIs outside the three authorized read-only port methods.
type sinkSpy struct {
	writes, allocations, releases, sends atomic.Int32
}

func (s *sinkSpy) Publish()  { s.writes.Add(1) }
func (s *sinkSpy) Allocate() { s.allocations.Add(1) }
func (s *sinkSpy) Release()  { s.releases.Add(1) }
func (s *sinkSpy) Send()     { s.sends.Add(1) }
func (s *sinkSpy) unchanged(t *testing.T) {
	t.Helper()
	if s.writes.Load() != 0 || s.allocations.Load() != 0 || s.releases.Load() != 0 || s.sends.Load() != 0 {
		t.Fatalf("pure projection acquired side effects: writes=%d alloc=%d release=%d sends=%d", s.writes.Load(), s.allocations.Load(), s.releases.Load(), s.sends.Load())
	}
}

type aggregatorSource struct {
	manifest  certificate.Manifest
	snapshots []certificate.Snapshot
	manErr    error
	snapErr   error
	readMan   atomic.Int32
	readSnaps atomic.Int32
	*sinkSpy
}

func (a *aggregatorSource) Manifest() (certificate.Manifest, error) {
	a.readMan.Add(1)
	return a.manifest, a.manErr
}
func (a *aggregatorSource) Snapshots() ([]certificate.Snapshot, error) {
	a.readSnaps.Add(1)
	return a.snapshots, a.snapErr
}

type harvestSource struct {
	account certificate.HarvestAccount
	err     error
	reads   atomic.Int32
	*sinkSpy
}

func (h *harvestSource) Account() (certificate.HarvestAccount, error) {
	h.reads.Add(1)
	return h.account, h.err
}

func fixture() (certificate.Manifest, []certificate.Snapshot, certificate.HarvestAccount) {
	m := certificate.Manifest{
		ManifestSHA256: inventorySHA,
		Epoch:          11,
		Units: []certificate.Unit{
			{ID: "alpha", Weight: 2, State: certificate.Q},
			{ID: "beta", Weight: 3, State: certificate.Q},
			{ID: "gamma", Weight: 1, State: certificate.Q},
		},
		CarriedWeight:  0,
		CarriedUnitIDs: []string{},
		TrustRoot:      trustRoot,
		PreviousEpoch:  10,
		RevokedEpoch:   0,
	}
	passed := func(id string, weight uint64) certificate.Unit {
		return certificate.Unit{ID: id, Weight: weight, State: certificate.P, VerifiedIO: 1, OriginBound: 1, FreshRun: 1, AuthorityCurrent: 1}
	}
	r0, r1, r2 := int64(6), int64(4), int64(0)
	series := []certificate.Snapshot{
		{Seq: 11, ReportedR: &r0, Units: []certificate.Unit{
			{ID: "alpha", Weight: 2, State: certificate.Q},
			{ID: "beta", Weight: 3, State: certificate.Q},
			{ID: "gamma", Weight: 1, State: certificate.Q},
		}, States: map[string]certificate.UnitState{"alpha": certificate.Q, "beta": certificate.Q, "gamma": certificate.Q}},
		{Seq: 12, ReportedR: &r1, Units: []certificate.Unit{
			passed("alpha", 2),
			{ID: "beta", Weight: 3, State: certificate.X},
			{ID: "gamma", Weight: 1, State: certificate.Q},
		}, States: map[string]certificate.UnitState{"alpha": certificate.P, "beta": certificate.X, "gamma": certificate.Q}},
		{Seq: 13, ReportedR: &r2, Units: []certificate.Unit{
			passed("alpha", 2), passed("beta", 3), passed("gamma", 1),
		}, States: map[string]certificate.UnitState{"alpha": certificate.P, "beta": certificate.P, "gamma": certificate.P}},
	}
	account := certificate.HarvestAccount{
		Stages: []certificate.StageFlow{
			{StageID: "Generate", InventoryBefore: 0, Arrivals: 3, InventoryAfter: 2, Departures: 1},
			{StageID: "ExtractVerify", InventoryBefore: 0, Arrivals: 3, InventoryAfter: 0, Departures: 3},
		},
		Origin: certificate.OriginEvidence{TrustRoot: trustRoot, ReplayCount: 0, VerifierRejectionsAmongP: 0},
		Coverage: certificate.CoverageEvidence{
			Sources: []certificate.MonitorSource{{SourceID: "capability:trusted-monitor-a", Ticks: []certificate.MonitorTick{
				{Seq: 1, ClockNs: 10, BootID: "boot-a", Counter: 10, BilledTicks: 2, CoveredTicks: 2},
				{Seq: 2, ClockNs: 20, BootID: "boot-a", Counter: 20, BilledTicks: 3, CoveredTicks: 3},
				{Seq: 3, ClockNs: 30, BootID: "boot-a", Counter: 30, BilledTicks: 1, CoveredTicks: 1},
			}}},
			Duplicates: 0, ClockRegressions: 0, UncoveredBilledTicks: 0,
			CounterResetsWithoutBootChange: 0, OpenLeases: 0, UnfencedOrUncleanMembers: 0,
		},
		Stall: certificate.StallInput{
			ThresholdD: 100, Now: 90, LastVerifiedDecrease: 80,
			ActivityKnown: true, ObservedActivityOps: 3,
		},
		UnresolvedRevocations: 0, UnresolvedViolations: 0, DeadlineViolations: 0,
	}
	return m, series, account
}

func sources(m certificate.Manifest, s []certificate.Snapshot, a certificate.HarvestAccount, spy *sinkSpy) (*aggregatorSource, *harvestSource) {
	return &aggregatorSource{manifest: m, snapshots: s, sinkSpy: spy}, &harvestSource{account: a, sinkSpy: spy}
}

func assertRefusal(t *testing.T, err error, code int, reason certificate.Reason) {
	t.Helper()
	var ce *certificate.Error
	if !errors.As(err, &ce) || ce == nil || ce.Code != code || ce.Reason != reason {
		t.Fatalf("refusal=%v, want certificate.Error{Code:%d, Reason:%s}", err, code, reason)
	}
	if certificate.ExitCode(err) != code || code == 1 {
		t.Fatalf("non-normalized refusal %v", err)
	}
}

func assertFailedGate(t *testing.T, report certificate.EvaluationReport, got certificate.GateVerdict, reason certificate.Reason, outcome certificate.Result) {
	t.Helper()
	if got.Bit != certificate.BitZero || got.Reason != reason {
		t.Fatalf("failed gate=%+v, want bit=0 reason=%s (report=%+v)", got, reason, report)
	}
	if report.Gates.Success.Bit != certificate.BitZero || report.Outcome != outcome || report.Outcome == certificate.DONE_SUCCESS {
		t.Fatalf("must-FAIL minted success: report=%+v, want outcome=%s", report, outcome)
	}
	if certificate.ResultExitCode(report.Outcome) == 0 {
		t.Fatalf("must-FAIL normalized to success rc=0: %+v", report)
	}
}

// TestProto1Certificate is the exact named entry point required by the frozen
// gate command. Run real passing controls first, then deliberate evidence mutants.
func TestProto1Certificate(t *testing.T) {
	t.Run("Green_IndependentSHA256AndCompleteAccountingSuccess", func(t *testing.T) {
		for _, tc := range []struct{ name, raw, want string }{
			{"inventory", inventoryBytes, inventorySHA},
			{"origin-capability", originBytes, originSHA},
		} {
			sum := sha256.Sum256([]byte(tc.raw))
			if hex.EncodeToString(sum[:]) != tc.want {
				t.Fatalf("independent %s SHA256 differs", tc.name)
			}
		}
		m, s, a := fixture()
		spy := new(sinkSpy)
		agg, harv := sources(m, s, a, spy)
		report, err := proto1cert.Evaluate(agg, harv)
		if err != nil {
			t.Fatalf("independent complete evidence refused: %v", err)
		}
		q := report.Quantities
		if q.N != 3 || q.I != 3 || q.W != obligations || q.C != 6 || q.R != 0 || q.D != 3 || q.O != 0 || q.T != 6 || q.A != 0 || q.FailureCount != 0 || q.FailureWeight != 0 || !report.Gates.AllRequiredGatesPass() || report.Gates.Success.Bit != certificate.BitOne || report.Gates.Success.Reason != certificate.ReasonOK || report.Outcome != certificate.DONE_SUCCESS {
			t.Fatalf("hand-summed W=2+3+1=6, C=6, R=0, all gates 1: %+v", report)
		}
		if certificate.ResultExitCode(report.Outcome) != 0 || agg.readMan.Load() != 1 || agg.readSnaps.Load() != 1 || harv.reads.Load() != 1 {
			t.Fatalf("unexpected exit code or producer read count: %+v", report)
		}
		spy.unchanged(t)
	})

	t.Run("Green_FailedShardClosesAccountingButNotSuccess", func(t *testing.T) {
		m, s, a := fixture()
		s[2].Units[1] = certificate.Unit{ID: "beta", Weight: 3, State: certificate.F}
		s[2].States["beta"] = certificate.F
		failureResidual := int64(3)
		s[2].ReportedR = &failureResidual
		spy := new(sinkSpy)
		agg, harv := sources(m, s, a, spy)
		report, err := proto1cert.Evaluate(agg, harv)
		if err != nil {
			t.Fatalf("valid terminal-failure evidence refused: %v", err)
		}
		q := report.Quantities
		// Independent arithmetic: alpha(2) + gamma(1) pass; beta(3) fails.
		if q.W != 6 || q.C != 3 || q.R != 3 || q.T != 6 || q.A != 0 || q.D != 3 || q.O != 0 || q.FailureCount != 1 || q.FailureWeight != 3 || !report.Gates.AllRequiredGatesPass() {
			t.Fatalf("accounting closure did not hold: %+v", report)
		}
		assertFailedGate(t, report, report.Gates.Success, certificate.ReasonAccountedFailuresPresent, certificate.DONE_ACCOUNTED_FAILURE)
		if certificate.ResultExitCode(report.Outcome) != 5 {
			t.Fatalf("accounted failure has nonstandard exit code: %s", report.Outcome)
		}
		spy.unchanged(t)
	})

	t.Run("MustFAIL_MissingRequiredPort", func(t *testing.T) {
		m, s, a := fixture()
		spy := new(sinkSpy)
		agg, harv := sources(m, s, a, spy)
		var typedNil *aggregatorSource
		for _, port := range []certificate.AggregatorPort{nil, typedNil} {
			report, err := proto1cert.Evaluate(port, harv)
			assertRefusal(t, err, 2, certificate.ReasonRequiredMissing)
			if report.Outcome != "" || report.Gates.Success.Bit == certificate.BitOne {
				t.Fatalf("missing port fabricated outcome: %+v", report)
			}
		}
		report, err := proto1cert.Evaluate(agg, (*harvestSource)(nil))
		assertRefusal(t, err, 2, certificate.ReasonRequiredMissing)
		if report.Outcome != "" || agg.readMan.Load() != 0 || harv.reads.Load() != 0 {
			t.Fatalf("missing port crossed a producer boundary: %+v", report)
		}
		spy.unchanged(t)
	})

	// Seven mandatory content mutations and two unresolved-counter regressions,
	// plus the missing-port mutant above. Gate reasons are independent
	// expected observations, not recomputed from certificate.Compute.
	type evidenceMutation struct {
		name    string
		change  func(*certificate.Manifest, *[]certificate.Snapshot, *certificate.HarvestAccount)
		gate    func(certificate.EvaluationReport) certificate.GateVerdict
		reason  certificate.Reason
		outcome certificate.Result
	}
	mutants := []evidenceMutation{
		{"MissingSequenceInterval", func(_ *certificate.Manifest, _ *[]certificate.Snapshot, a *certificate.HarvestAccount) {
			a.Coverage.Sources[0].Ticks = append(a.Coverage.Sources[0].Ticks[:1], a.Coverage.Sources[0].Ticks[2:]...)
		}, func(r certificate.EvaluationReport) certificate.GateVerdict { return r.Gates.Coverage }, certificate.ReasonSeqGap, certificate.INDETERMINATE},
		{"DuplicatedObligation", func(m *certificate.Manifest, _ *[]certificate.Snapshot, _ *certificate.HarvestAccount) {
			m.Units = append(m.Units, m.Units[0])
		}, func(r certificate.EvaluationReport) certificate.GateVerdict { return r.Gates.Schema }, certificate.ReasonDuplicateKey, certificate.INDETERMINATE},
		{"DiscontinuousStage", func(_ *certificate.Manifest, _ *[]certificate.Snapshot, a *certificate.HarvestAccount) {
			a.Stages[0].InventoryAfter++
		}, func(r certificate.EvaluationReport) certificate.GateVerdict { return r.Gates.Continuity }, certificate.ReasonStageConservation, certificate.INDETERMINATE},
		{"ForgedOrigin", func(_ *certificate.Manifest, s *[]certificate.Snapshot, _ *certificate.HarvestAccount) {
			(*s)[2].Units[0].OriginBound = 0
		}, func(r certificate.EvaluationReport) certificate.GateVerdict { return r.Gates.Origin }, certificate.ReasonMissingEvidenceBit, certificate.INDETERMINATE},
		{"OpenLease", func(_ *certificate.Manifest, _ *[]certificate.Snapshot, a *certificate.HarvestAccount) {
			a.Coverage.OpenLeases = 1
		}, func(r certificate.EvaluationReport) certificate.GateVerdict { return r.Gates.Coverage }, certificate.ReasonOpenLeases, certificate.INDETERMINATE},
		{"UnresolvedRevocation", func(_ *certificate.Manifest, _ *[]certificate.Snapshot, a *certificate.HarvestAccount) {
			a.UnresolvedRevocations = 1
		}, func(r certificate.EvaluationReport) certificate.GateVerdict { return r.Gates.Success }, certificate.ReasonUnresolvedRevocations, certificate.INDETERMINATE},
		{"UnknownCleanupUnfencedMember", func(_ *certificate.Manifest, _ *[]certificate.Snapshot, a *certificate.HarvestAccount) {
			a.Coverage.UnfencedOrUncleanMembers = 1
		}, func(r certificate.EvaluationReport) certificate.GateVerdict { return r.Gates.Coverage }, certificate.ReasonUnfencedMembers, certificate.INDETERMINATE},
		{"UnresolvedViolation", func(_ *certificate.Manifest, _ *[]certificate.Snapshot, a *certificate.HarvestAccount) {
			a.UnresolvedViolations = 1
		}, func(r certificate.EvaluationReport) certificate.GateVerdict { return r.Gates.Success }, certificate.ReasonUnresolvedViolations, certificate.INDETERMINATE},
		{"DeadlineViolation", func(_ *certificate.Manifest, _ *[]certificate.Snapshot, a *certificate.HarvestAccount) {
			a.DeadlineViolations = 1
		}, func(r certificate.EvaluationReport) certificate.GateVerdict { return r.Gates.Success }, certificate.ReasonDeadlineViolations, certificate.INDETERMINATE},
	}
	for _, tc := range mutants {
		t.Run("MustFAIL_"+tc.name, func(t *testing.T) {
			m, s, a := fixture()
			tc.change(&m, &s, &a)
			spy := new(sinkSpy)
			agg, harv := sources(m, s, a, spy)
			// Serialize before and after using the test's standard library,
			// not shared Go slices that could mask an in-place mutation.
			before, err := json.Marshal(struct {
				M certificate.Manifest
				S []certificate.Snapshot
				A certificate.HarvestAccount
			}{m, s, a})
			if err != nil {
				t.Fatal(err)
			}
			report, err := proto1cert.Evaluate(agg, harv)
			if err != nil {
				t.Fatalf("fixture prerequisite/compilation error cannot kill mutant: %v", err)
			}
			assertFailedGate(t, report, tc.gate(report), tc.reason, tc.outcome)
			after, err := json.Marshal(struct {
				M certificate.Manifest
				S []certificate.Snapshot
				A certificate.HarvestAccount
			}{m, s, a})
			if err != nil || !bytes.Equal(before, after) {
				t.Fatal("projection mutated source evidence")
			}
			if agg.readMan.Load() != 1 || agg.readSnaps.Load() != 1 || harv.reads.Load() != 1 {
				t.Fatal("mutant did not traverse all three real projection ports")
			}
			spy.unchanged(t)
		})
	}

	t.Run("MustFAIL_UnknownStallActivityWithResidual", func(t *testing.T) {
		m, s, a := fixture()
		s[2].Units[1] = certificate.Unit{ID: "beta", Weight: 3, State: certificate.F}
		s[2].States["beta"] = certificate.F
		r := int64(3)
		s[2].ReportedR = &r
		a.Stall.Now = 250
		a.Stall.LastVerifiedDecrease = 80
		a.Stall.ActivityKnown = false
		spy := new(sinkSpy)
		agg, harv := sources(m, s, a, spy)
		report, err := proto1cert.Evaluate(agg, harv)
		if err != nil {
			t.Fatalf("unknown stall is a gate failure, not a construction error: %v", err)
		}
		assertFailedGate(t, report, report.Gates.Warnings, certificate.ReasonStallActivityUnknown, certificate.INDETERMINATE)
		spy.unchanged(t)
	})

	t.Run("RequiredFieldsNeverBecomePassingZeroValues", func(t *testing.T) {
		type badInput struct {
			name  string
			alter func(*certificate.Manifest, *[]certificate.Snapshot, *certificate.HarvestAccount)
		}
		cases := []badInput{
			{"manifestSHA", func(m *certificate.Manifest, _ *[]certificate.Snapshot, _ *certificate.HarvestAccount) {
				m.ManifestSHA256 = ""
			}},
			{"manifestUnits", func(m *certificate.Manifest, _ *[]certificate.Snapshot, _ *certificate.HarvestAccount) { m.Units = nil }},
			{"snapshots", func(_ *certificate.Manifest, s *[]certificate.Snapshot, _ *certificate.HarvestAccount) { *s = nil }},
			{"snapshotState", func(_ *certificate.Manifest, s *[]certificate.Snapshot, _ *certificate.HarvestAccount) {
				(*s)[0].Units, (*s)[0].States = nil, nil
			}},
			{"stages", func(_ *certificate.Manifest, _ *[]certificate.Snapshot, a *certificate.HarvestAccount) {
				a.Stages = nil
			}},
			{"origin", func(_ *certificate.Manifest, _ *[]certificate.Snapshot, a *certificate.HarvestAccount) {
				a.Origin.TrustRoot = ""
			}},
			{"coverage", func(_ *certificate.Manifest, _ *[]certificate.Snapshot, a *certificate.HarvestAccount) {
				a.Coverage.Sources = nil
			}},
			{"monitorBoot", func(_ *certificate.Manifest, _ *[]certificate.Snapshot, a *certificate.HarvestAccount) {
				a.Coverage.Sources[0].Ticks[0].BootID = ""
			}},
			{"stallBudget", func(_ *certificate.Manifest, _ *[]certificate.Snapshot, a *certificate.HarvestAccount) {
				a.Stall.ThresholdD = 0
			}},
		}
		for _, tc := range cases {
			t.Run(tc.name, func(t *testing.T) {
				mm, ss, aa := fixture()
				tc.alter(&mm, &ss, &aa)
				spy := new(sinkSpy)
				agg, harv := sources(mm, ss, aa, spy)
				report, err := proto1cert.Evaluate(agg, harv)
				assertRefusal(t, err, 2, certificate.ReasonRequiredMissing)
				if report.Outcome != "" || report.Gates.Success.Bit == certificate.BitOne {
					t.Fatalf("missing source fabricated success: %+v", report)
				}
				spy.unchanged(t)
			})
		}
	})

	t.Run("TypedAndUntypedProducerRefusalsPropagateNormalized", func(t *testing.T) {
		m, s, a := fixture()
		cases := []struct {
			name                                  string
			manifestErr, snapshotsErr, accountErr error
			code                                  int
			reason                                certificate.Reason
		}{
			{"unavailableAuthority", &certificate.Error{Code: 4, Reason: certificate.ReasonBlockedEvidence, Detail: "authority unavailable"}, nil, nil, 4, certificate.ReasonBlockedEvidence},
			{"verificationRejected", nil, &certificate.Error{Code: 5, Reason: certificate.ReasonVerifierRejection, Detail: "bad signature"}, nil, 5, certificate.ReasonVerifierRejection},
			{"uncertainCleanup", nil, nil, &certificate.Error{Code: 6, Reason: certificate.ReasonBlockedEvidence, Detail: "unknown cleanup"}, 6, certificate.ReasonBlockedEvidence},
			{"timeout", nil, nil, context.DeadlineExceeded, 6, certificate.ReasonBlockedEvidence},
			{"unknownError", nil, errors.New("source verification failed"), nil, 5, certificate.ReasonVerifierRejection},
			{"legacyRC1", &certificate.Error{Code: 1, Reason: certificate.ReasonTypeError, Detail: "old failure"}, nil, nil, 5, certificate.ReasonVerifierRejection},
		}
		for _, tc := range cases {
			t.Run(tc.name, func(t *testing.T) {
				spy := new(sinkSpy)
				agg, harv := sources(m, s, a, spy)
				agg.manErr, agg.snapErr, harv.err = tc.manifestErr, tc.snapshotsErr, tc.accountErr
				report, err := proto1cert.Evaluate(agg, harv)
				assertRefusal(t, err, tc.code, tc.reason)
				if report.Outcome != "" || report.Gates.Success.Bit != certificate.BitZero {
					t.Fatalf("port error minted a report: %+v", report)
				}
				if tc.manifestErr != nil && (agg.readSnaps.Load() != 0 || harv.reads.Load() != 0) || tc.snapshotsErr != nil && harv.reads.Load() != 0 {
					t.Fatalf("failed port did not stop dependent producer reads")
				}
				spy.unchanged(t)
			})
		}
	})

	t.Run("DefensiveCopyOfAllNestedEvidenceAndZeroValuePorts", func(t *testing.T) {
		m, s, a := fixture()
		frozenAgg, err := proto1cert.NewAggregatorEvidence(m, s)
		if err != nil {
			t.Fatal(err)
		}
		frozenHarv, err := proto1cert.NewHarvestEvidence(a)
		if err != nil {
			t.Fatal(err)
		}
		// Mutate all caller-owned slices, pointer payloads, maps and monitor ticks.
		m.Units[0].ID = "forged"
		m.CarriedUnitIDs = append(m.CarriedUnitIDs, "forged")
		s[0].Units[0].State = certificate.F
		s[0].States["alpha"] = certificate.F
		*s[0].ReportedR = 999
		a.Stages[0].Arrivals = 99
		a.Coverage.Sources[0].Ticks[0].Seq = 77
		a.Coverage.Sources[0].SourceID = "foreign"
		a.UnresolvedRevocations = 33
		// Also mutate every caller-readable returned copy to attempt a second alias.
		mm, err := frozenAgg.Manifest()
		if err != nil {
			t.Fatal(err)
		}
		mm.Units[0].ID = "tampered"
		ss, err := frozenAgg.Snapshots()
		if err != nil {
			t.Fatal(err)
		}
		ss[2].Units[0].VerifiedIO = 0
		ss[2].States["alpha"] = certificate.F
		*ss[2].ReportedR = -1
		aa, err := frozenHarv.Account()
		if err != nil {
			t.Fatal(err)
		}
		aa.Stages[1].Departures = 99
		aa.Coverage.Sources[0].Ticks[1].Counter = 0
		aa.UnresolvedViolations = 33
		report, err := proto1cert.Evaluate(frozenAgg, frozenHarv)
		if err != nil || report.Outcome != certificate.DONE_SUCCESS || report.Quantities.W != obligations || report.Quantities.R != 0 {
			t.Fatalf("frozen accepted evidence changed after caller mutation: report=%+v err=%v", report, err)
		}
		// Carried IDs must be independent even though the normal control
		// has no carried obligations.
		carriedManifest, _, _ := fixture()
		carriedManifest.CarriedWeight = 2
		carriedManifest.CarriedUnitIDs = []string{"alpha"}
		carriedProjection, err := proto1cert.NewAggregatorEvidence(carriedManifest, s)
		if err != nil {
			t.Fatal(err)
		}
		carriedManifest.CarriedUnitIDs[0] = "forged"
		carriedRead, err := carriedProjection.Manifest()
		if err != nil || len(carriedRead.CarriedUnitIDs) != 1 || carriedRead.CarriedUnitIDs[0] != "alpha" || carriedRead.CarriedWeight != 2 || carriedRead.PreviousEpoch != 10 || carriedRead.RevokedEpoch != 0 {
			t.Fatalf("manifest fields narrowed or aliased: %+v: %v", carriedRead, err)
		}
		carriedRead.CarriedUnitIDs[0] = "tampered"
		carriedAgain, err := carriedProjection.Manifest()
		if err != nil || carriedAgain.CarriedUnitIDs[0] != "alpha" {
			t.Fatal("returned carried IDs alias accepted evidence")
		}
		if _, err := (&proto1cert.AggregatorEvidence{}).Manifest(); err == nil {
			t.Fatal("zero-value aggregator passed")
		}
		if _, err := (&proto1cert.AggregatorEvidence{}).Snapshots(); err == nil {
			t.Fatal("zero-value snapshots passed")
		}
		if _, err := (&proto1cert.HarvestEvidence{}).Account(); err == nil {
			t.Fatal("zero-value harvest passed")
		}
	})

	t.Run("FiniteInventoryBound", func(t *testing.T) {
		m, s, _ := fixture()
		// Frozen N=3 authorizes at most 1+2*N=7 records. Eight refuse.
		for len(s) < 8 {
			last := s[len(s)-1]
			last.Seq++
			s = append(s, last)
		}
		_, err := proto1cert.NewAggregatorEvidence(m, s)
		assertRefusal(t, err, 2, certificate.ReasonRangeError)
	})

	t.Run("ConcurrentFrozenReadsBarrierFixedSeed", func(t *testing.T) {
		m, s, a := fixture()
		frozenAgg, err := proto1cert.NewAggregatorEvidence(m, s)
		if err != nil {
			t.Fatal(err)
		}
		frozenHarv, err := proto1cert.NewHarvestEvidence(a)
		if err != nil {
			t.Fatal(err)
		}
		const goroutines = 8
		seed := int64(0x2140f78c)
		order := rand.New(rand.NewSource(seed)).Perm(goroutines)
		barrier := make(chan struct{})
		errs := make(chan error, goroutines)
		var wg sync.WaitGroup
		for _, ticket := range order {
			wg.Add(1)
			go func(ticket int) {
				defer wg.Done()
				<-barrier // exact barrier, no sleep or nondeterministic timing condition
				rep, e := proto1cert.Evaluate(frozenAgg, frozenHarv)
				if e != nil || rep.Outcome != certificate.DONE_SUCCESS || rep.Quantities.W != 6 {
					errs <- fmt.Errorf("reader %d: report=%+v error=%v", ticket, rep, e)
					return
				}
				sn, e := frozenAgg.Snapshots()
				if e != nil {
					errs <- e
					return
				}
				sn[0].States["alpha"] = certificate.F
			}(ticket)
		}
		close(barrier)
		wg.Wait()
		close(errs)
		for e := range errs {
			t.Error(e)
		}
	})
}

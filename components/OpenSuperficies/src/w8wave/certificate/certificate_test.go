package certificate_test

import (
	"bytes"
	"math"
	"os"
	"path/filepath"
	"testing"

	"opensuperficies.local/srf/src/w8wave/certificate"
)

type fakeClock struct {
	nowNs uint64
}

func (c *fakeClock) Now() uint64       { return c.nowNs }
func (c *fakeClock) Advance(dt uint64) { c.nowNs += dt }

type inMemoryJournal struct {
	entries [][]byte
}

func (j *inMemoryJournal) Append(payload any) error {
	b, err := certificate.CanonicalSidecar(payload)
	if err != nil {
		return err
	}
	j.entries = append(j.entries, b)
	return nil
}

type fakeAggregator struct {
	manifest  certificate.Manifest
	snapshots []certificate.Snapshot
}

func (f *fakeAggregator) Manifest() (certificate.Manifest, error)     { return f.manifest, nil }
func (f *fakeAggregator) Snapshots() ([]certificate.Snapshot, error) { return f.snapshots, nil }

type fakeHarvestAccount struct {
	account certificate.HarvestAccount
}

func (f *fakeHarvestAccount) Account() (certificate.HarvestAccount, error) {
	return f.account, nil
}

func passedUnit(id string, weight uint64) certificate.Unit {
	return certificate.Unit{
		ID:               id,
		Weight:           weight,
		State:            certificate.P,
		VerifiedIO:       1,
		OriginBound:      1,
		FreshRun:         1,
		AuthorityCurrent: 1,
	}
}

func validCoverage() certificate.CoverageEvidence {
	return certificate.CoverageEvidence{
		Sources: []certificate.MonitorSource{
			{
				SourceID: "trusted-test-monitor-0",
				Ticks: []certificate.MonitorTick{
					{Seq: 1, ClockNs: 100, BootID: "boot-a", Counter: 10, BilledTicks: 5, CoveredTicks: 5},
					{Seq: 2, ClockNs: 200, BootID: "boot-a", Counter: 20, BilledTicks: 5, CoveredTicks: 5},
					{Seq: 3, ClockNs: 300, BootID: "boot-a", Counter: 30, BilledTicks: 5, CoveredTicks: 5},
				},
			},
		},
	}
}

func TestPositiveControls(t *testing.T) {
	t.Run("Weights2And1GiveRSequence3_1_0AfterDistinctVerifiedPasses", func(t *testing.T) {
		m := certificate.Manifest{
			ManifestSHA256: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
			Epoch:          1,
			Units: []certificate.Unit{
				{ID: "u1", Weight: 2, State: certificate.Q},
				{ID: "u2", Weight: 1, State: certificate.Q},
			},
			CarriedWeight: 0,
			TrustRoot:     "sha256:trusted-root-v1",
		}
		series := []certificate.Snapshot{
			{
				Seq: 1,
				Units: []certificate.Unit{
					{ID: "u1", Weight: 2, State: certificate.Q},
					{ID: "u2", Weight: 1, State: certificate.Q},
				},
			},
			{
				Seq: 2,
				Units: []certificate.Unit{
					passedUnit("u1", 2),
					{ID: "u2", Weight: 1, State: certificate.Q},
				},
			},
			{
				Seq: 3,
				Units: []certificate.Unit{
					passedUnit("u1", 2),
					passedUnit("u2", 1),
				},
			},
		}

		rSeq, verdict := certificate.EvaluateMonotone(m, series)
		if verdict.Bit != certificate.BitOne || verdict.Reason != certificate.ReasonOK {
			t.Fatalf("EvaluateMonotone failed: %+v", verdict)
		}
		want := []uint64{3, 1, 0}
		if len(rSeq) != len(want) {
			t.Fatalf("len(rSeq)=%d, want %d", len(rSeq), len(want))
		}
		for i := range want {
			if rSeq[i] != want[i] {
				t.Fatalf("rSeq[%d]=%d, want %d", i, rSeq[i], want[i])
			}
		}
	})

	t.Run("EveryRequestedIDAppearsExactlyOnceUnderLegalState", func(t *testing.T) {
		m := certificate.Manifest{
			ManifestSHA256: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
			Epoch:          1,
			Units: []certificate.Unit{
				{ID: "u-q", Weight: 1, State: certificate.Q},
				{ID: "u-x", Weight: 2, State: certificate.Q},
				{ID: "u-p", Weight: 3, State: certificate.Q},
				{ID: "u-f", Weight: 4, State: certificate.Q},
			},
		}
		snap := certificate.Snapshot{
			Seq: 1,
			Units: []certificate.Unit{
				{ID: "u-q", Weight: 1, State: certificate.Q},
				{ID: "u-x", Weight: 2, State: certificate.X},
				passedUnit("u-p", 3),
				{ID: "u-f", Weight: 4, State: certificate.F},
			},
		}
		q, err := certificate.Compute(m, snap)
		if err != nil {
			t.Fatalf("Compute failed: %v", err)
		}
		if q.N != 4 || q.I != 4 || q.W != 10 {
			t.Fatalf("unexpected N/I/W: %+v", q)
		}
		if q.D != 2 || q.O != 2 || q.D+q.O != q.N {
			t.Fatalf("unit conservation violated: D=%d O=%d N=%d", q.D, q.O, q.N)
		}
		if q.T != 7 || q.A != 3 || q.T+q.A != q.W {
			t.Fatalf("weight conservation violated: T=%d A=%d W=%d", q.T, q.A, q.W)
		}
		if q.C != 3 || q.R != 7 || q.C+q.R != q.W {
			t.Fatalf("residual conservation violated: C=%d R=%d W=%d", q.C, q.R, q.W)
		}
		idVerdict := certificate.EvaluateIdentity(m, []certificate.Snapshot{snap})
		if idVerdict.Bit != certificate.BitOne {
			t.Fatalf("EvaluateIdentity failed: %+v", idVerdict)
		}
	})

	t.Run("AllFailedFixtureReportsDoneAccountedFailureWithPositiveR", func(t *testing.T) {
		m := certificate.Manifest{
			ManifestSHA256: "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
			Epoch:          1,
			Units: []certificate.Unit{
				{ID: "u1", Weight: 2, State: certificate.Q},
				{ID: "u2", Weight: 3, State: certificate.Q},
			},
			TrustRoot: "sha256:trusted-root-v1",
		}
		series := []certificate.Snapshot{
			{
				Seq: 1,
				Units: []certificate.Unit{
					{ID: "u1", Weight: 2, State: certificate.Q},
					{ID: "u2", Weight: 3, State: certificate.Q},
				},
			},
			{
				Seq: 2,
				Units: []certificate.Unit{
					{ID: "u1", Weight: 2, State: certificate.F},
					{ID: "u2", Weight: 3, State: certificate.F},
				},
			},
		}
		rep := certificate.Evaluate(certificate.EvaluationInput{
			Manifest: m,
			Series:   series,
			Origin:   certificate.OriginEvidence{TrustRoot: "sha256:trusted-root-v1"},
			Coverage: validCoverage(),
			Stall: certificate.StallInput{
				ThresholdD:           500,
				Now:                  200,
				LastVerifiedDecrease: 100,
				ActivityKnown:        true,
				ObservedActivityOps:  1,
			},
		})
		if rep.Quantities.A != 0 || rep.Quantities.R != 5 || rep.Quantities.FailureCount != 2 || rep.Quantities.FailureWeight != 5 {
			t.Fatalf("unexpected quantities: %+v", rep.Quantities)
		}
		if rep.Gates.Success.Bit != certificate.BitZero {
			t.Fatalf("expected Success.Bit == 0 on all-failed fixture, got %+v", rep.Gates.Success)
		}
		if rep.Outcome != certificate.DONE_ACCOUNTED_FAILURE {
			t.Fatalf("Outcome=%s, want DONE_ACCOUNTED_FAILURE", rep.Outcome)
		}
		if rc := certificate.ResultExitCode(rep.Outcome); rc != 5 {
			t.Fatalf("ResultExitCode=%d, want 5", rc)
		}
	})

	t.Run("CompleteSyntheticSuiteWithTrustedMonitorReachesDoneSuccess", func(t *testing.T) {
		clk := &fakeClock{nowNs: 100}
		journal := &inMemoryJournal{}
		tmp := t.TempDir()

		m := certificate.Manifest{
			ManifestSHA256: "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
			Epoch:          1,
			Units: []certificate.Unit{
				{ID: "u1", Weight: 2, State: certificate.Q},
				{ID: "u2", Weight: 1, State: certificate.Q},
			},
			TrustRoot: "sha256:trusted-monitor-root",
		}
		clk.Advance(50)
		s1 := certificate.Snapshot{
			Seq: 1,
			Units: []certificate.Unit{
				{ID: "u1", Weight: 2, State: certificate.Q},
				{ID: "u2", Weight: 1, State: certificate.Q},
			},
		}
		clk.Advance(50)
		s2 := certificate.Snapshot{
			Seq: 2,
			Units: []certificate.Unit{
				passedUnit("u1", 2),
				{ID: "u2", Weight: 1, State: certificate.X},
			},
		}
		clk.Advance(50)
		s3 := certificate.Snapshot{
			Seq: 3,
			Units: []certificate.Unit{
				passedUnit("u1", 2),
				passedUnit("u2", 1),
			},
		}

		agg := &fakeAggregator{manifest: m, snapshots: []certificate.Snapshot{s1, s2, s3}}
		harv := &fakeHarvestAccount{
			account: certificate.HarvestAccount{
				Stages: []certificate.StageFlow{
					{StageID: "queue", InventoryBefore: 2, Arrivals: 0, InventoryAfter: 0, Departures: 2},
					{StageID: "exec", InventoryBefore: 0, Arrivals: 2, InventoryAfter: 0, Departures: 2},
				},
				Origin:   certificate.OriginEvidence{TrustRoot: "sha256:trusted-monitor-root"},
				Coverage: validCoverage(),
				Stall: certificate.StallInput{
					ThresholdD:           1000,
					Now:                  clk.Now(),
					LastVerifiedDecrease: clk.Now(),
					ActivityKnown:        true,
					ObservedActivityOps:  2,
				},
			},
		}

		rep, err := certificate.EvaluateFromPorts(agg, harv)
		if err != nil {
			t.Fatalf("EvaluateFromPorts failed: %v", err)
		}
		if rep.Outcome != certificate.DONE_SUCCESS {
			t.Fatalf("Outcome=%s, want DONE_SUCCESS (gates=%+v)", rep.Outcome, rep.Gates)
		}
		if rep.Gates.Success.Bit != certificate.BitOne {
			t.Fatalf("Success gate bit=%d, want 1", rep.Gates.Success.Bit)
		}
		num, den := certificate.Fraction(rep.Quantities)
		if num != 3 || den != 3 || !certificate.IsTargetComplete(rep.Quantities) {
			t.Fatalf("Fraction=(%d,%d), want (3,3) complete", num, den)
		}
		if err := journal.Append(rep); err != nil {
			t.Fatalf("journal.Append failed: %v", err)
		}
		sidecarPath := filepath.Join(tmp, "certificate.sidecar.json")
		if err := os.WriteFile(sidecarPath, journal.entries[0], 0600); err != nil {
			t.Fatalf("WriteFile failed: %v", err)
		}
		readBack, err := os.ReadFile(sidecarPath)
		if err != nil || !bytes.Equal(readBack, journal.entries[0]) {
			t.Fatalf("sidecar readback mismatch: err=%v", err)
		}
		if rc := certificate.ResultExitCode(rep.Outcome); rc != 0 {
			t.Fatalf("ResultExitCode=%d, want 0", rc)
		}
	})

	t.Run("LargeValuesProcessedExactlyOrRejectedBeforeFixedWidthOverflow", func(t *testing.T) {
		half := uint64(math.MaxUint64 / 2)
		mExact := certificate.Manifest{
			ManifestSHA256: "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
			Epoch:          1,
			Units: []certificate.Unit{
				{ID: "big-1", Weight: half, State: certificate.Q},
				{ID: "big-2", Weight: half + 1, State: certificate.Q},
			},
		}
		sExact := certificate.Snapshot{
			Seq: 1,
			Units: []certificate.Unit{
				passedUnit("big-1", half),
				passedUnit("big-2", half+1),
			},
		}
		q, err := certificate.Compute(mExact, sExact)
		if err != nil {
			t.Fatalf("expected exact MaxUint64 computation to succeed, got: %v", err)
		}
		if q.W != math.MaxUint64 || q.C != math.MaxUint64 || q.R != 0 {
			t.Fatalf("unexpected exact quantities at MaxUint64: %+v", q)
		}
		if !certificate.IsTargetComplete(q) {
			t.Fatalf("expected IsTargetComplete true at MaxUint64/MaxUint64")
		}

		// Overflow by 1 must be refused before wrapping around.
		mOverflow := certificate.Manifest{
			ManifestSHA256: "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
			Epoch:          1,
			Units: []certificate.Unit{
				{ID: "ov-1", Weight: math.MaxUint64, State: certificate.Q},
				{ID: "ov-2", Weight: 1, State: certificate.Q},
			},
		}
		_, err = certificate.Compute(mOverflow, sExact)
		if err == nil {
			t.Fatalf("expected overflow error, got nil")
		}
		if rc := certificate.ExitCode(err); rc != 5 {
			t.Fatalf("ExitCode(overflow)=%d, want 5", rc)
		}
	})
}

func TestMustFailFixtures(t *testing.T) {
	t.Run("NullCounterRejected", func(t *testing.T) {
		payload := []byte(`{"manifest":{"manifest_sha256":"aaaa","epoch":1,"carried_weight":null,"units":[{"id":"u1","weight":1,"state":"Q"}]},"series":[{"seq":1,"units":[{"id":"u1","weight":1,"state":"Q"}]}],"origin":{"trust_root":"root","replay_count":0,"verifier_rejections_among_p":0},"coverage":{"duplicates":0,"clock_regressions":0,"uncovered_billed_ticks":0,"counter_resets_without_boot_change":0,"open_leases":0,"unfenced_or_unclean_members":0,"sources":[{"source_id":"s1","ticks":[{"seq":1,"clock_ns":10,"boot_id":"b1","counter":1,"billed_ticks":1,"covered_ticks":1}]}]}}`)
		rep := certificate.EvaluateJSON(payload)
		if rep.Gates.Schema.Bit != certificate.BitZero || rep.Gates.Schema.Reason != certificate.ReasonTypeError {
			t.Fatalf("expected SCHEMA fail with TYPE_ERROR on null counter, got %+v", rep.Gates.Schema)
		}
		if rep.Outcome != certificate.INDETERMINATE {
			t.Fatalf("expected INDETERMINATE, got %s", rep.Outcome)
		}
	})

	t.Run("AbsentKeyRejected", func(t *testing.T) {
		payload := []byte(`{"manifest":{"epoch":1,"carried_weight":0,"units":[{"id":"u1","weight":1,"state":"Q"}]},"series":[{"seq":1,"units":[{"id":"u1","weight":1,"state":"Q"}]}],"origin":{"trust_root":"root","replay_count":0,"verifier_rejections_among_p":0},"coverage":{"duplicates":0,"clock_regressions":0,"uncovered_billed_ticks":0,"counter_resets_without_boot_change":0,"open_leases":0,"unfenced_or_unclean_members":0,"sources":[]}}`)
		rep := certificate.EvaluateJSON(payload)
		if rep.Gates.Schema.Bit != certificate.BitZero || rep.Gates.Schema.Reason != certificate.ReasonRequiredMissing {
			t.Fatalf("expected SCHEMA fail with REQUIRED_MISSING, got %+v", rep.Gates.Schema)
		}
	})

	t.Run("EmptyLedgerRejected", func(t *testing.T) {
		rep := certificate.EvaluateJSON([]byte(`{}`))
		if rep.Gates.Schema.Bit != certificate.BitZero || rep.Gates.Schema.Reason != certificate.ReasonEmptyLedger {
			t.Fatalf("expected SCHEMA fail with EMPTY_LEDGER, got %+v", rep.Gates.Schema)
		}
	})

	t.Run("EmptySuiteRejected", func(t *testing.T) {
		m := certificate.Manifest{ManifestSHA256: "aaaa", Epoch: 1, Units: nil}
		verdict := certificate.EvaluateSchema(certificate.SchemaStats{}, m, certificate.Snapshot{})
		if verdict.Bit != certificate.BitZero || verdict.Reason != certificate.ReasonEmptySuite {
			t.Fatalf("expected EMPTY_SUITE, got %+v", verdict)
		}
	})

	t.Run("TruncatedJSONRejected", func(t *testing.T) {
		rep := certificate.EvaluateJSON([]byte(`{"manifest":{"manifest_sha256":"aaaa"`))
		if rep.Gates.Schema.Bit != certificate.BitZero || rep.Gates.Schema.Reason != certificate.ReasonTypeError {
			t.Fatalf("expected SCHEMA fail on truncated JSON, got %+v", rep.Gates.Schema)
		}
	})

	t.Run("ZeroWeightRejected", func(t *testing.T) {
		m := certificate.Manifest{
			ManifestSHA256: "aaaa",
			Epoch:          1,
			Units:          []certificate.Unit{{ID: "u1", Weight: 0, State: certificate.Q}},
		}
		verdict := certificate.EvaluateSchema(certificate.SchemaStats{}, m, certificate.Snapshot{})
		if verdict.Bit != certificate.BitZero || verdict.Reason != certificate.ReasonZeroWeight {
			t.Fatalf("expected ZERO_WEIGHT, got %+v", verdict)
		}
	})

	t.Run("FloatOrStringCoercionRejected", func(t *testing.T) {
		floatPayload := []byte(`{"manifest":{"manifest_sha256":"aaaa","epoch":1,"carried_weight":0,"units":[{"id":"u1","weight":1.5,"state":"Q"}]},"series":[],"origin":{},"coverage":{}}`)
		repFloat := certificate.EvaluateJSON(floatPayload)
		if repFloat.Gates.Schema.Bit != certificate.BitZero || repFloat.Gates.Schema.Reason != certificate.ReasonTypeError {
			t.Fatalf("expected TYPE_ERROR on float weight, got %+v", repFloat.Gates.Schema)
		}

		strPayload := []byte(`{"manifest":{"manifest_sha256":"aaaa","epoch":"1","carried_weight":0,"units":[{"id":"u1","weight":1,"state":"Q"}]},"series":[{"seq":1,"units":[{"id":"u1","weight":1,"state":"Q"}]}],"origin":{"trust_root":"root","replay_count":0,"verifier_rejections_among_p":0},"coverage":{"duplicates":0,"clock_regressions":0,"uncovered_billed_ticks":0,"counter_resets_without_boot_change":0,"open_leases":0,"unfenced_or_unclean_members":0,"sources":[]}}`)
		repStr := certificate.EvaluateJSON(strPayload)
		if repStr.Gates.Schema.Bit != certificate.BitZero || repStr.Gates.Schema.Reason != certificate.ReasonTypeError {
			t.Fatalf("expected TYPE_ERROR on string-coerced integer, got %+v", repStr.Gates.Schema)
		}
	})

	t.Run("DuplicateKeyRejected", func(t *testing.T) {
		dupPayload := []byte(`{"manifest":{"manifest_sha256":"aaaa","epoch":1,"epoch":1,"carried_weight":0,"units":[{"id":"u1","weight":1,"state":"Q"}]},"series":[],"origin":{},"coverage":{}}`)
		rep := certificate.EvaluateJSON(dupPayload)
		if rep.Gates.Schema.Bit != certificate.BitZero || rep.Gates.Schema.Reason != certificate.ReasonDuplicateKey {
			t.Fatalf("expected DUPLICATE_KEY, got %+v", rep.Gates.Schema)
		}
	})

	t.Run("OverflowInSumRejected", func(t *testing.T) {
		m := certificate.Manifest{
			ManifestSHA256: "aaaa",
			Epoch:          1,
			Units: []certificate.Unit{
				{ID: "u1", Weight: math.MaxUint64 - 1, State: certificate.Q},
				{ID: "u2", Weight: 5, State: certificate.Q},
			},
		}
		v := certificate.EvaluateSchema(certificate.SchemaStats{}, m, certificate.Snapshot{})
		if v.Bit != certificate.BitZero || v.Reason != certificate.ReasonOverflow {
			t.Fatalf("expected OVERFLOW, got %+v", v)
		}
	})

	t.Run("DuplicatePassCoveringMissingUnitRejected", func(t *testing.T) {
		m := certificate.Manifest{
			ManifestSHA256: "aaaa",
			Epoch:          1,
			Units: []certificate.Unit{
				{ID: "u1", Weight: 1, State: certificate.Q},
				{ID: "u2", Weight: 1, State: certificate.Q},
			},
		}
		snap := certificate.Snapshot{
			Seq: 1,
			Units: []certificate.Unit{
				passedUnit("u1", 1),
				passedUnit("u1", 1),
			},
		}
		v := certificate.EvaluateIdentity(m, []certificate.Snapshot{snap})
		if v.Bit != certificate.BitZero || v.Reason != certificate.ReasonStateMultiplicity {
			t.Fatalf("expected STATE_MULTIPLICITY on duplicate pass covering missing unit, got %+v", v)
		}
	})

	t.Run("ForeignIDRejected", func(t *testing.T) {
		m := certificate.Manifest{
			ManifestSHA256: "aaaa",
			Epoch:          1,
			Units:          []certificate.Unit{{ID: "u1", Weight: 1, State: certificate.Q}},
		}
		snap := certificate.Snapshot{
			Seq: 1,
			Units: []certificate.Unit{
				passedUnit("u1", 1),
				passedUnit("u-foreign", 1),
			},
		}
		v := certificate.EvaluateIdentity(m, []certificate.Snapshot{snap})
		if v.Bit != certificate.BitZero || v.Reason != certificate.ReasonForeignID {
			t.Fatalf("expected FOREIGN_ID, got %+v", v)
		}
	})

	t.Run("SequenceReuseRejected", func(t *testing.T) {
		m := certificate.Manifest{
			ManifestSHA256: "aaaa",
			Epoch:          1,
			Units:          []certificate.Unit{{ID: "u1", Weight: 1, State: certificate.Q}},
		}
		s1 := certificate.Snapshot{Seq: 5, Units: []certificate.Unit{{ID: "u1", Weight: 1, State: certificate.Q}}}
		s2 := certificate.Snapshot{Seq: 5, Units: []certificate.Unit{passedUnit("u1", 1)}}
		_, v := certificate.EvaluateMonotone(m, []certificate.Snapshot{s1, s2})
		if v.Bit != certificate.BitZero || v.Reason != certificate.ReasonDuplicateSeq {
			t.Fatalf("expected DUPLICATE_SEQ on sequence reuse, got %+v", v)
		}
	})

	t.Run("OppositeLocalDeficitsCancellingGloballyRejected", func(t *testing.T) {
		q := certificate.Quantities{N: 2, W: 2, I: 2, D: 2, O: 0, T: 2, A: 0, S: 2, C: 2, R: 0}
		// Stage 1 has deficit -1 (2+2=4 != 3+0=3), Stage 2 has opposite surplus +1 (1+1=2 != 3+0=3).
		// Global sum of lhs (4+2=6) equals global sum of rhs (3+3=6), but per-stage continuity must fail.
		stages := []certificate.StageFlow{
			{StageID: "stage-1", InventoryBefore: 2, Arrivals: 2, InventoryAfter: 3, Departures: 0},
			{StageID: "stage-2", InventoryBefore: 1, Arrivals: 1, InventoryAfter: 3, Departures: 0},
		}
		v := certificate.EvaluateContinuity(q, stages)
		if v.Bit != certificate.BitZero || v.Reason != certificate.ReasonStageConservation {
			t.Fatalf("expected STAGE_CONSERVATION_VIOLATION, got %+v", v)
		}
	})

	t.Run("AllUnitsFailedWithAZeroAndSuccessRequestedRejectedForSuccess", func(t *testing.T) {
		m := certificate.Manifest{
			ManifestSHA256: "aaaa",
			Epoch:          1,
			Units: []certificate.Unit{
				{ID: "u1", Weight: 2, State: certificate.Q},
				{ID: "u2", Weight: 3, State: certificate.Q},
			},
			TrustRoot: "sha256:trusted-root",
		}
		s1 := certificate.Snapshot{
			Seq: 1,
			Units: []certificate.Unit{
				{ID: "u1", Weight: 2, State: certificate.Q},
				{ID: "u2", Weight: 3, State: certificate.Q},
			},
		}
		s2 := certificate.Snapshot{
			Seq: 2,
			Units: []certificate.Unit{
				{ID: "u1", Weight: 2, State: certificate.F},
				{ID: "u2", Weight: 3, State: certificate.F},
			},
		}
		rep := certificate.Evaluate(certificate.EvaluationInput{
			Manifest:        m,
			Series:          []certificate.Snapshot{s1, s2},
			Origin:          certificate.OriginEvidence{TrustRoot: "sha256:trusted-root"},
			Coverage:        validCoverage(),
			ClaimedComplete: true,
		})
		if rep.Gates.Success.Bit != certificate.BitZero {
			t.Fatalf("Success gate must be 0 when all units failed, got %+v", rep.Gates.Success)
		}
		if rep.Outcome == certificate.DONE_SUCCESS {
			t.Fatalf("Outcome must never be DONE_SUCCESS when all units failed")
		}
	})

	t.Run("SingletonOrNegativeSeriesOrRDecreasingOnFailedAttemptOrDispatchRejected", func(t *testing.T) {
		m := certificate.Manifest{
			ManifestSHA256: "aaaa",
			Epoch:          1,
			Units: []certificate.Unit{
				{ID: "u1", Weight: 2, State: certificate.Q},
				{ID: "u2", Weight: 1, State: certificate.Q},
			},
		}
		s0 := certificate.Snapshot{
			Seq: 1,
			Units: []certificate.Unit{
				{ID: "u1", Weight: 2, State: certificate.Q},
				{ID: "u2", Weight: 1, State: certificate.Q},
			},
		}
		// 1. Singleton series
		if _, v := certificate.EvaluateMonotone(m, []certificate.Snapshot{s0}); v.Bit != certificate.BitZero || v.Reason != certificate.ReasonSingletonLineage {
			t.Fatalf("expected SINGLETON_LINEAGE, got %+v", v)
		}
		// 2. Negative residual series
		negR := int64(-1)
		sNeg := certificate.Snapshot{
			Seq:       2,
			Units:     s0.Units,
			ReportedR: &negR,
		}
		if _, v := certificate.EvaluateMonotone(m, []certificate.Snapshot{s0, sNeg}); v.Bit != certificate.BitZero || v.Reason != certificate.ReasonNegativeResidual {
			t.Fatalf("expected NEGATIVE_RESIDUAL, got %+v", v)
		}
		// 3. R decreasing on dispatch (Q -> X)
		wrongDispatchR := int64(1)
		sDispatch := certificate.Snapshot{
			Seq: 2,
			Units: []certificate.Unit{
				{ID: "u1", Weight: 2, State: certificate.X},
				{ID: "u2", Weight: 1, State: certificate.Q},
			},
			ReportedR: &wrongDispatchR,
		}
		if _, v := certificate.EvaluateMonotone(m, []certificate.Snapshot{s0, sDispatch}); v.Bit != certificate.BitZero || v.Reason != certificate.ReasonNonMonotoneStep {
			t.Fatalf("expected NON_MONOTONE_STEP on R decreasing on dispatch, got %+v", v)
		}
		// 4. R decreasing on a failed attempt (X -> F)
		wrongFailR := int64(1)
		sFail := certificate.Snapshot{
			Seq: 2,
			Units: []certificate.Unit{
				{ID: "u1", Weight: 2, State: certificate.F},
				{ID: "u2", Weight: 1, State: certificate.Q},
			},
			ReportedR: &wrongFailR,
		}
		if _, v := certificate.EvaluateMonotone(m, []certificate.Snapshot{s0, sFail}); v.Bit != certificate.BitZero || v.Reason != certificate.ReasonNonMonotoneStep {
			t.Fatalf("expected NON_MONOTONE_STEP on R decreasing on failure, got %+v", v)
		}
	})

	t.Run("RoundedPercentOrPositiveResidualCalledCompleteRejected", func(t *testing.T) {
		// 199 / 200 = 99.5% which rounds to 100% in float/rounded arithmetic, but R = 1 > 0.
		q := certificate.Quantities{
			N: 200, W: 200, I: 200, D: 199, O: 1, T: 199, A: 1, S: 199, C: 199, R: 1,
		}
		if certificate.IsTargetComplete(q) {
			t.Fatalf("199/200 with R=1 must never be called complete")
		}
		cmp, err := certificate.CompareFraction(q.C, q.W, 1, 1)
		if err != nil || cmp != -1 {
			t.Fatalf("CompareFraction(199,200,1,1)=%d, err=%v; want -1", cmp, err)
		}
	})

	t.Run("PassRemovedSilentlyWhileDescentPreservedRejected", func(t *testing.T) {
		m := certificate.Manifest{
			ManifestSHA256: "aaaa",
			Epoch:          1,
			Units: []certificate.Unit{
				{ID: "u1", Weight: 1, State: certificate.Q},
				{ID: "u2", Weight: 2, State: certificate.Q},
			},
		}
		// Step 1: u1 (weight 1) is P, u2 (weight 2) is Q -> R = 2.
		s1 := certificate.Snapshot{
			Seq: 1,
			Units: []certificate.Unit{
				passedUnit("u1", 1),
				{ID: "u2", Weight: 2, State: certificate.Q},
			},
		}
		// Step 2: u1 is silently reverted to Q while u2 (weight 2) becomes P -> R = 1 (descent 2 -> 1 preserved!).
		s2 := certificate.Snapshot{
			Seq: 2,
			Units: []certificate.Unit{
				{ID: "u1", Weight: 1, State: certificate.Q},
				passedUnit("u2", 2),
			},
		}
		idV := certificate.EvaluateIdentity(m, []certificate.Snapshot{s1, s2})
		if idV.Bit != certificate.BitZero || idV.Reason != certificate.ReasonSilentPassRemoval {
			t.Fatalf("EvaluateIdentity must fail with SILENT_PASS_REMOVAL, got %+v", idV)
		}
	})

	t.Run("MissingSourceTickClockRegressionUncoveredBillableIntervalRejected", func(t *testing.T) {
		// 1. Missing source tick (seq 1 -> 3)
		gapEv := certificate.CoverageEvidence{
			Sources: []certificate.MonitorSource{
				{
					SourceID: "m1",
					Ticks: []certificate.MonitorTick{
						{Seq: 1, ClockNs: 100, BootID: "b1", Counter: 1, BilledTicks: 1, CoveredTicks: 1},
						{Seq: 3, ClockNs: 200, BootID: "b1", Counter: 2, BilledTicks: 1, CoveredTicks: 1},
					},
				},
			},
		}
		if v := certificate.EvaluateCoverage(gapEv); v.Bit != certificate.BitZero || v.Reason != certificate.ReasonSeqGap {
			t.Fatalf("expected SEQ_GAP, got %+v", v)
		}

		// 2. Clock regression (200 -> 150)
		regEv := certificate.CoverageEvidence{
			Sources: []certificate.MonitorSource{
				{
					SourceID: "m1",
					Ticks: []certificate.MonitorTick{
						{Seq: 1, ClockNs: 200, BootID: "b1", Counter: 1, BilledTicks: 1, CoveredTicks: 1},
						{Seq: 2, ClockNs: 150, BootID: "b1", Counter: 2, BilledTicks: 1, CoveredTicks: 1},
					},
				},
			},
		}
		if v := certificate.EvaluateCoverage(regEv); v.Bit != certificate.BitZero || v.Reason != certificate.ReasonClockRegression {
			t.Fatalf("expected CLOCK_REGRESSION, got %+v", v)
		}

		// 3. Uncovered billable interval (covered 4 < billed 5)
		uncovEv := certificate.CoverageEvidence{
			Sources: []certificate.MonitorSource{
				{
					SourceID: "m1",
					Ticks: []certificate.MonitorTick{
						{Seq: 1, ClockNs: 100, BootID: "b1", Counter: 1, BilledTicks: 5, CoveredTicks: 4},
					},
				},
			},
		}
		if v := certificate.EvaluateCoverage(uncovEv); v.Bit != certificate.BitZero || v.Reason != certificate.ReasonUncoveredBilledTicks {
			t.Fatalf("expected UNCOVERED_BILLED_TICKS, got %+v", v)
		}
	})

	t.Run("NoSuppliedTrustRootBlocksOriginAndNeverBecomesAccountedFailure", func(t *testing.T) {
		m := certificate.Manifest{
			ManifestSHA256: "aaaa",
			Epoch:          1,
			Units:          []certificate.Unit{{ID: "u1", Weight: 1, State: certificate.Q}},
		}
		s1 := certificate.Snapshot{Seq: 1, Units: []certificate.Unit{{ID: "u1", Weight: 1, State: certificate.Q}}}
		s2 := certificate.Snapshot{Seq: 2, Units: []certificate.Unit{{ID: "u1", Weight: 1, State: certificate.F}}}
		rep := certificate.Evaluate(certificate.EvaluationInput{
			Manifest: m,
			Series:   []certificate.Snapshot{s1, s2},
			Origin:   certificate.OriginEvidence{TrustRoot: ""},
			Coverage: validCoverage(),
		})
		if rep.Gates.Origin.Bit != certificate.BitZero || rep.Gates.Origin.Reason != certificate.ReasonBlockedEvidence {
			t.Fatalf("expected ORIGIN fail with BLOCKED_EVIDENCE, got %+v", rep.Gates.Origin)
		}
		if rep.Outcome != certificate.BLOCKED_EVIDENCE {
			t.Fatalf("expected Outcome BLOCKED_EVIDENCE (never DONE_ACCOUNTED_FAILURE), got %s", rep.Outcome)
		}
	})
}

func TestStallAndRevocationAndPrerequisiteOrder(t *testing.T) {
	t.Run("StallZeroSpinAndUnknownPreventSuccess", func(t *testing.T) {
		q := certificate.Quantities{N: 2, W: 3, I: 2, D: 1, O: 1, T: 2, A: 1, S: 2, C: 2, R: 1}

		kindZero, vZero := certificate.EvaluateStall(q, certificate.StallInput{
			ThresholdD:           100,
			Now:                  250,
			LastVerifiedDecrease: 100,
			ActivityKnown:        true,
			ObservedActivityOps:  0,
		})
		if kindZero != certificate.StallZero || vZero.Bit != certificate.BitZero || vZero.Reason != certificate.ReasonStallZero {
			t.Fatalf("expected STALL_ZERO, got kind=%s verdict=%+v", kindZero, vZero)
		}

		kindSpin, vSpin := certificate.EvaluateStall(q, certificate.StallInput{
			ThresholdD:           100,
			Now:                  200,
			LastVerifiedDecrease: 100,
			ActivityKnown:        true,
			ObservedActivityOps:  42,
		})
		if kindSpin != certificate.StallSpin || vSpin.Bit != certificate.BitZero || vSpin.Reason != certificate.ReasonStallSpin {
			t.Fatalf("expected STALL_SPIN, got kind=%s verdict=%+v", kindSpin, vSpin)
		}

		kindUnk, vUnk := certificate.EvaluateStall(q, certificate.StallInput{
			ThresholdD:           100,
			Now:                  200,
			LastVerifiedDecrease: 100,
			ActivityKnown:        false,
		})
		if kindUnk != certificate.StallActivityUnknown || vUnk.Bit != certificate.BitZero || vUnk.Reason != certificate.ReasonStallActivityUnknown {
			t.Fatalf("expected STALL_ACTIVITY_UNKNOWN, got kind=%s verdict=%+v", kindUnk, vUnk)
		}
	})

	t.Run("ForgedAcceptedReceiptRevokesEpochAndOpensLinkedRecoveryEpoch", func(t *testing.T) {
		m := certificate.Manifest{
			ManifestSHA256: "1111111111111111111111111111111111111111111111111111111111111111",
			Epoch:          1,
			Units: []certificate.Unit{
				{ID: "u1", Weight: 2, State: certificate.Q},
				{ID: "u2", Weight: 1, State: certificate.Q},
			},
			TrustRoot: "sha256:trusted-root",
		}
		// At the end of epoch 1, both u1 and u2 were marked P (R = 0), but u2's receipt is found to be forged.
		sForged := certificate.Snapshot{
			Seq: 3,
			Units: []certificate.Unit{
				passedUnit("u1", 2),
				passedUnit("u2", 1),
			},
		}

		rec, recManifest, recSnap0, err := certificate.RevokeAndOpenRecoveryEpoch(m, sForged, []string{"u2"}, 2)
		if err != nil {
			t.Fatalf("RevokeAndOpenRecoveryEpoch failed: %v", err)
		}
		if rec.RevokedEpoch != 1 || rec.RecoveryEpoch != 2 || rec.RestoredWeight != 1 || rec.RecoveryBaselineR0 != 1 || rec.RecoveryCarriedW != 2 {
			t.Fatalf("unexpected RevocationRecord: %+v", rec)
		}
		if recManifest.PreviousEpoch != 1 || recManifest.RevokedEpoch != 1 {
			t.Fatalf("expected linked recovery epoch metadata, got %+v", recManifest)
		}

		// When u2 genuinely passes in recovery epoch 2, the recovery lineage (R0=1 -> R1=0) is monotone and reaches DONE_SUCCESS.
		recSnap1 := certificate.Snapshot{
			Seq: recSnap0.Seq + 1,
			Units: []certificate.Unit{
				passedUnit("u1", 2),
				passedUnit("u2", 1),
			},
		}
		rSeq, vMono := certificate.EvaluateMonotone(recManifest, []certificate.Snapshot{recSnap0, recSnap1})
		if vMono.Bit != certificate.BitOne || len(rSeq) != 2 || rSeq[0] != 1 || rSeq[1] != 0 {
			t.Fatalf("expected recovery R sequence [1, 0], got rSeq=%v verdict=%+v", rSeq, vMono)
		}
	})

	t.Run("FailedPrerequisiteStopsDependentGates", func(t *testing.T) {
		rep := certificate.Evaluate(certificate.EvaluationInput{
			SchemaStats: certificate.SchemaStats{EmptyLedger: true},
		})
		if rep.Gates.Schema.Bit != certificate.BitZero {
			t.Fatalf("expected Schema bit 0")
		}
		for name, v := range map[string]certificate.GateVerdict{
			"Identity":     rep.Gates.Identity,
			"Coverage":     rep.Gates.Coverage,
			"Ranges":       rep.Gates.Ranges,
			"Continuity":   rep.Gates.Continuity,
			"Origin":       rep.Gates.Origin,
			"Monotone":     rep.Gates.Monotone,
			"Warnings":     rep.Gates.Warnings,
			"LeaseClosure": rep.Gates.LeaseClosure,
			"Success":      rep.Gates.Success,
		} {
			if v.Bit != certificate.BitZero || v.Reason != certificate.ReasonPrerequisiteFailed {
				t.Fatalf("gate %s should be stopped with PREREQUISITE_FAILED, got %+v", name, v)
			}
		}
	})
}

package p2flake

import (
	"bytes"
	"encoding/json"
	"errors"
	"math"
	"math/rand"
	"reflect"
	"strings"
	"testing"

	"opensuperficies.local/srf/src/workerbridge"
)

const (
	hexA    = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	hexB    = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	hexC    = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
	commitA = "0123456789abcdef0123456789abcdef01234567"
	commitB = "89abcdef0123456789abcdef0123456789abcdef"
)

func pointer[T any](x T) *T { return &x }
func inventory(commit string, baseline *string, n int) Inventory {
	return Inventory{
		Schema: InventorySchema, FarmID: "farm-7", Repo: "oguirado/OpenSuperficies",
		Commit: commit, BaselineCommit: baseline, SuiteProjectionSHA256: hexB,
		ExecutionContractSHA256: hexC, DeclaredTrials: n,
		RequiredCases: []string{"case-A", "case-B"}, CreatedByHost: true,
	}
}
func observation(trial string, outcome Outcome) Observation {
	o := Observation{
		Schema: ObservationSchema, FarmID: "farm-7", TrialID: trial,
		AttemptID: "attempt-" + trial, CapsuleID: "capsule-" + trial,
		Incarnation: 1, Commit: commitA, ReceiptSHA256: hexA,
		GrantSHA256: hexB, ExecutionContractSHA256: hexC,
		HostValidationRef: "host-gate:revision-locked", CaseResults: []CaseResult{},
		Cleanup: CleanupEvidence{Verified: true, StateLeaked: false},
		Outcome: outcome,
	}
	switch outcome {
	case Pass:
		o.GateExitCode = pointer(0)
		o.CaseResults = []CaseResult{{"case-A", Pass}, {"case-B", Pass}}
	case Fail:
		o.GateExitCode = pointer(1)
		o.CaseResults = []CaseResult{{"case-A", Pass}, {"case-B", Fail}}
	}
	return o
}
func makeSample(n int, outcomes ...Outcome) Sample {
	s := Sample{Inventory: inventory(commitA, nil, n)}
	for j, o := range outcomes {
		s.Observations = append(s.Observations, observation(string(rune('A'+j)), o))
	}
	return s
}
func mustClass(t *testing.T, s Sample, base *Sample, want Classification, k, n, missing, infra int) Result {
	t.Helper()
	r, err := Classify(s, base)
	if err != nil {
		t.Fatalf("unexpected rejection: %v", err)
	}
	if r.Classification != want || r.Current.K != k || r.Current.N != n ||
		r.Current.Missing != missing || r.Current.InfrastructureTrials != infra || r.Current.Declared != s.Inventory.DeclaredTrials {
		t.Fatalf("class/counts got (%s,k=%d,n=%d,declared=%d,missing=%d,infra=%d); want (%s,k=%d,n=%d,declared=%d,missing=%d,infra=%d)",
			r.Classification, r.Current.K, r.Current.N, r.Current.Declared, r.Current.Missing, r.Current.InfrastructureTrials,
			want, k, n, s.Inventory.DeclaredTrials, missing, infra)
	}
	return r
}
func mustReject(t *testing.T, current Sample, base *Sample, class string) {
	t.Helper()
	before, err := json.Marshal(struct {
		C Sample
		B *Sample
	}{current, base})
	if err != nil {
		t.Fatal(err)
	}
	r, err := Classify(current, base)
	var reject *workerbridge.Reject
	if !errors.As(err, &reject) || reject.Class != class || reject.Code < 2 || reject.Code > 7 {
		t.Fatalf("wanted %s rejection with source exit code; got result=%+v err=%v", class, r, err)
	}
	if !reflect.DeepEqual(r, Result{}) {
		t.Fatalf("rejection published a partial verdict: %+v", r)
	}
	after, err := json.Marshal(struct {
		C Sample
		B *Sample
	}{current, base})
	if err != nil || !bytes.Equal(before, after) {
		t.Fatalf("pure classifier changed caller's evidence: %v", err)
	}
}
func near(t *testing.T, got, want float64) {
	t.Helper()
	if math.IsNaN(got) || math.IsInf(got, 0) || math.Abs(got-want) > 2e-11 {
		t.Fatalf("got %.17g, want %.17g", got, want)
	}
}

// literalBinomial evaluates explicit fixed-degree polynomial controls, not
// the mode-centered tail recurrence used in the production inversion.
func literalBinomial(n, k int, p float64, highTail bool) float64 {
	switch {
	case n == 2 && k == 1 && highTail:
		return 2*p - p*p
	case n == 2 && k == 1 && !highTail:
		return 1 - p*p
	case n == 3 && k == 1 && highTail:
		return 3*p - 3*p*p + p*p*p
	case n == 3 && k == 1 && !highTail:
		return 1 - 3*p*p + 2*p*p*p
	case n == 4 && k == 2 && highTail:
		return 6*p*p - 8*p*p*p + 3*p*p*p*p
	case n == 4 && k == 2 && !highTail:
		return 1 - 4*p*p*p + 3*p*p*p*p
	default:
		panic("oracle only defined for fixed explicit small polynomials")
	}
}

func TestProto2Unit(t *testing.T) {
	positive := 0
	negative := 0
	t.Run("positive/mixed_same_commit", func(t *testing.T) {
		positive++
		r := mustClass(t, makeSample(2, Pass, Fail), nil, FlakeObserved, 1, 2, 0, 0)
		if r.Current.Interval95 == nil || len(r.Current.Trials) != 2 || r.Current.ObservedTrials != 2 {
			t.Fatal("mixed independent population missing")
		}
		near(t, r.Current.Interval95.Low, 1-math.Sqrt(0.975))
		near(t, r.Current.Interval95.High, math.Sqrt(0.975))
	})
	t.Run("positive/all_pass_finite_sample", func(t *testing.T) {
		positive++
		r := mustClass(t, makeSample(3, Pass, Pass, Pass), nil, CompletePass, 0, 3, 0, 0)
		if r.Current.Interval95 == nil || r.Current.Interval95.High <= 0 {
			t.Fatal("cannot interpret all-pass as zero flake probability")
		}
		near(t, r.Current.Interval95.High, 1-math.Pow(0.025, 1.0/3.0))
		near(t, r.Current.Interval95.Low, 0)
	})
	t.Run("positive/all_fail_boundary_baseline_absent", func(t *testing.T) {
		positive++
		r := mustClass(t, makeSample(4, Fail, Fail, Fail, Fail), nil, BaselineAbsent, 4, 4, 0, 0)
		if r.Current.Interval95 == nil {
			t.Fatal("all-fail missing interval")
		}
		near(t, r.Current.Interval95.Low, math.Pow(0.025, 0.25))
		near(t, r.Current.Interval95.High, 1)
	})
	t.Run("positive/comparable_revision_regression_observed", func(t *testing.T) {
		positive++
		baseline := makeSample(2, Pass, Pass)
		baseline.Inventory.Commit = commitB
		baseline.Inventory.FarmID = "older-farm"
		for j := range baseline.Observations {
			baseline.Observations[j].FarmID = "older-farm"
			baseline.Observations[j].Commit = commitB
		}
		current := makeSample(2, Fail, Fail)
		current.Inventory.BaselineCommit = pointer(commitB)
		r := mustClass(t, current, &baseline, RegressionObserved, 2, 2, 0, 0)
		if r.Baseline == nil || r.Baseline.K != 0 || r.Baseline.N != 2 {
			t.Fatal("baseline counts not supplied")
		}
	})
	t.Run("positive/old_failure_not_new_regression", func(t *testing.T) {
		positive++
		baseline := makeSample(2, Fail, Fail)
		current := makeSample(2, Fail, Fail)
		current.Inventory.BaselineCommit = pointer(commitA)
		mustClass(t, current, &baseline, ExistingFailure, 2, 2, 0, 0)
	})
	t.Run("positive/same_receipt_digest_is_not_trial_identity", func(t *testing.T) {
		positive++
		current := makeSample(2, Pass, Fail)
		if current.Observations[0].ReceiptSHA256 != current.Observations[1].ReceiptSHA256 {
			t.Fatal("fixture does not share receipt digest")
		}
		mustClass(t, current, nil, FlakeObserved, 1, 2, 0, 0)
	})
	t.Run("positive/infrastructure_and_missing_excluded", func(t *testing.T) {
		positive++
		r := mustClass(t, makeSample(5, Pass, Fail, Infrastructure), nil, FlakeObserved, 1, 2, 2, 1)
		if r.Current.InfrastructureAttempts != 1 || r.Current.ObservedTrials != 3 || r.Current.Interval95 == nil {
			t.Fatal("invalid population accounting")
		}
	})
	t.Run("positive/unknown_independent_population", func(t *testing.T) {
		positive++
		r := mustClass(t, makeSample(2), nil, Inconclusive, 0, 0, 2, 0)
		if r.Current.Interval95 != nil {
			t.Fatal("empty independent population acquired probability")
		}
	})
	t.Run("positive/missing_failed_trial_cannot_complete", func(t *testing.T) {
		positive++
		mustClass(t, makeSample(2, Pass), nil, Incomplete, 0, 1, 1, 0)
	})
	t.Run("positive/infra_without_valid_trials", func(t *testing.T) {
		positive++
		r := mustClass(t, makeSample(2, Infrastructure, Unknown), nil, Inconclusive, 0, 0, 0, 1)
		if r.Current.UnknownTrials != 1 || r.Current.Interval95 != nil {
			t.Fatal("invalid empty evidence")
		}
	})
	t.Run("positive/missing_baseline_never_regression", func(t *testing.T) {
		positive++
		s := makeSample(2, Fail, Fail)
		s.Inventory.BaselineCommit = pointer(commitB)
		mustClass(t, s, nil, BaselineAbsent, 2, 2, 0, 0)
	})
	t.Run("positive/retry_preserves_all_attempts_without_flake", func(t *testing.T) {
		positive++
		s := makeSample(1, Pass)
		retry := observation("A", Fail)
		retry.AttemptID = "retry-A"
		s.Observations = append(s.Observations, retry)
		r := mustClass(t, s, nil, Inconclusive, 0, 0, 0, 0)
		if r.Current.RetriedTrials != 1 || r.Current.CorrelatedTrials != 1 || len(r.Current.Trials[0].Attempts) != 2 || r.Current.Interval95 != nil {
			t.Fatal("retry was grouped as independent or discarded")
		}
	})
	t.Run("positive/state_leak_preserves_stratum_inconclusive", func(t *testing.T) {
		positive++
		s := makeSample(2, Pass, Fail)
		s.Observations[1].Cleanup.StateLeaked = true
		r := mustClass(t, s, nil, Inconclusive, 0, 1, 0, 0)
		if r.Current.CorrelatedTrials != 1 || r.Current.Interval95 != nil || len(r.Current.Trials) != 2 {
			t.Fatal("leaked state accepted as independent")
		}
	})
	t.Run("positive/shared_capsule_incarnation_inconclusive", func(t *testing.T) {
		positive++
		s := makeSample(2, Pass, Fail)
		s.Observations[1].CapsuleID = s.Observations[0].CapsuleID
		r := mustClass(t, s, nil, Inconclusive, 0, 0, 0, 0)
		if r.Current.CorrelatedTrials != 2 || r.Current.Interval95 != nil {
			t.Fatal("correlated capsule accepted")
		}
	})
	t.Run("positive/seeded_permutation_invariance", func(t *testing.T) {
		positive++
		s := makeSample(6, Pass, Fail, Pass, Infrastructure, Unknown, Fail)
		want, err := Classify(s, nil)
		if err != nil {
			t.Fatal(err)
		}
		for seed := int64(1); seed <= 50; seed++ {
			shuffled := makeSample(6, Pass, Fail, Pass, Infrastructure, Unknown, Fail)
			rand.New(rand.NewSource(seed)).Shuffle(len(shuffled.Observations), func(i, j int) {
				shuffled.Observations[i], shuffled.Observations[j] = shuffled.Observations[j], shuffled.Observations[i]
			})
			got, err := Classify(shuffled, nil)
			if err != nil || !reflect.DeepEqual(got, want) {
				t.Fatalf("permutation seed=%d: err=%v, got %+v", seed, err, got)
			}
		}
	})
	t.Run("positive/independent_polynomial_and_deterministic_bisection", func(t *testing.T) {
		positive++
		for _, v := range []struct{ n, k int }{{2, 1}, {3, 1}, {4, 2}} {
			got, err := ClopperPearson95(v.k, v.n)
			if err != nil {
				t.Fatal(err)
			}
			near(t, literalBinomial(v.n, v.k, got.Low, true), 0.025)
			near(t, literalBinomial(v.n, v.k, got.High, false), 0.025)
			again, err := ClopperPearson95(v.k, v.n)
			if err != nil || math.Float64bits(again.Low) != math.Float64bits(got.Low) || math.Float64bits(again.High) != math.Float64bits(got.High) {
				t.Fatal("nondeterministic bisection")
			}
		}
	})
	t.Run("positive/source_parser_round_trip", func(t *testing.T) {
		positive++
		s := makeSample(2, Pass, Fail)
		iData, err := json.Marshal(s.Inventory)
		if err != nil {
			t.Fatal(err)
		}
		parsedInv, err := ParseInventory(iData)
		if err != nil || !reflect.DeepEqual(parsedInv, s.Inventory) {
			t.Fatalf("inventory parser: %v", err)
		}
		for _, obs := range s.Observations {
			data, err := json.Marshal(obs)
			if err != nil {
				t.Fatal(err)
			}
			parsedObs, err := ParseObservation(data)
			if err != nil || !reflect.DeepEqual(parsedObs, obs) {
				t.Fatalf("observation parser: %v", err)
			}
		}
	})
	t.Run("must_fail/foreign_commit", func(t *testing.T) {
		negative++
		s := makeSample(2, Pass, Fail)
		s.Observations[0].Commit = commitB
		mustReject(t, s, nil, "FOREIGN_COMMIT")
	})
	t.Run("must_fail/foreign_execution_projection", func(t *testing.T) {
		negative++
		s := makeSample(2, Pass, Fail)
		s.Observations[1].ExecutionContractSHA256 = hexA
		mustReject(t, s, nil, "FOREIGN_PROJECTION")
	})
	t.Run("must_fail/foreign_farm", func(t *testing.T) {
		negative++
		s := makeSample(2, Pass, Fail)
		s.Observations[0].FarmID = "other-farm"
		mustReject(t, s, nil, "FOREIGN_FARM")
	})
	t.Run("must_fail/too_many_trial_groups", func(t *testing.T) {
		negative++
		mustReject(t, makeSample(1, Pass, Fail), nil, "EXCESS_TRIALS")
	})
	t.Run("must_fail/baseline_projection_mismatch", func(t *testing.T) {
		negative++
		s := makeSample(2, Fail, Fail)
		s.Inventory.BaselineCommit = pointer(commitB)
		b := makeSample(2, Pass, Pass)
		b.Inventory.Commit = commitB
		b.Inventory.ExecutionContractSHA256 = hexA
		mustReject(t, s, &b, "BASELINE_PROJECTION_MISMATCH")
	})
	t.Run("must_fail/baseline_foreign_commit", func(t *testing.T) {
		negative++
		s := makeSample(2, Fail, Fail)
		s.Inventory.BaselineCommit = pointer(commitB)
		b := makeSample(2, Pass, Pass)
		mustReject(t, s, &b, "BASELINE_PROJECTION_MISMATCH")
	})
	t.Run("must_fail/inconsistent_gate_outcome", func(t *testing.T) {
		negative++
		s := makeSample(2, Pass, Fail)
		s.Observations[0].GateExitCode = pointer(3)
		mustReject(t, s, nil, "INCONSISTENT_OUTCOME")
	})
	t.Run("must_fail/missing_case", func(t *testing.T) {
		negative++
		s := makeSample(2, Pass, Fail)
		s.Observations[1].CaseResults = s.Observations[1].CaseResults[:1]
		mustReject(t, s, nil, "CASE_PROJECTION_MISMATCH")
	})
	t.Run("must_fail/invalid_k_n_refused_without_interval", func(t *testing.T) {
		negative++
		for _, v := range []struct{ k, n int }{{0, 0}, {-1, 2}, {3, 2}, {1, MaxTrials + 1}} {
			got, err := ClopperPearson95(v.k, v.n)
			var reject *workerbridge.Reject
			if !errors.As(err, &reject) || reject.Class != "INVALID_SAMPLE" || !reflect.DeepEqual(got, Interval{}) {
				t.Fatalf("invalid sample %+v admitted: %+v %v", v, got, err)
			}
		}
	})
	t.Run("must_fail/undeclared_empty_obligations", func(t *testing.T) {
		negative++
		s := makeSample(0)
		mustReject(t, s, nil, "MALFORMED")
	})
	t.Run("must_fail/unknown_observation_key", func(t *testing.T) {
		negative++
		data, err := json.Marshal(observation("A", Pass))
		if err != nil {
			t.Fatal(err)
		}
		data = bytes.Replace(data, []byte(`"trial_id":`), []byte(`"intruder":123,"trial_id":`), 1)
		got, err := ParseObservation(data)
		var reject *workerbridge.Reject
		if !errors.As(err, &reject) || reject.Class != "UNKNOWN_KEY" || reject.Code != 2 || !reflect.DeepEqual(got, Observation{}) {
			t.Fatalf("unknown field admitted: %+v %v", got, err)
		}
	})
	t.Run("must_fail/duplicate_key_retains_source_code", func(t *testing.T) {
		negative++
		data, err := json.Marshal(observation("A", Pass))
		if err != nil {
			t.Fatal(err)
		}
		data = bytes.Replace(data, []byte(`"trial_id":`), []byte(`"trial_id":"forged","trial_id":`), 1)
		got, err := ParseObservation(data)
		var reject *workerbridge.Reject
		if !errors.As(err, &reject) || reject.Class != "DUPLICATE_KEY" || reject.Code != 5 || !reflect.DeepEqual(got, Observation{}) {
			t.Fatalf("source rejection changed: %+v %v", got, err)
		}
	})
	t.Run("must_fail/bad_hash_cannot_be_custody", func(t *testing.T) {
		negative++
		data, err := json.Marshal(observation("A", Pass))
		if err != nil {
			t.Fatal(err)
		}
		data = bytes.Replace(data, []byte(hexA), []byte(strings.Repeat("z", 64)), 1)
		got, err := ParseObservation(data)
		var reject *workerbridge.Reject
		if !errors.As(err, &reject) || reject.Class != "MALFORMED" || !reflect.DeepEqual(got, Observation{}) {
			t.Fatalf("invalid digest admitted: %+v %v", got, err)
		}
	})
	if positive != 17 || negative != 13 {
		t.Fatalf("dead-instrument control: executed positive=%d negative=%d, expected 17/13", positive, negative)
	}
}

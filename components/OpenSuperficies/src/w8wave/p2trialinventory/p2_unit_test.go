package p2trialinventory

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"reflect"
	"strings"
	"testing"

	"opensuperficies.local/srf/src/workerbridge"
)

const (
	fixtureInventoryTemplate   = "eyJzY2hlbWEiOiJwMi50cmlhbF9pbnZlbnRvcnkvdjEiLCJmYXJtX2lkIjoiZiIsInJlcG8iOiJyIiwiY29tbWl0IjoiYyIsImJhc2VsaW5lX2NvbW1pdCI6bnVsbCwic3VpdGVfcHJvamVjdGlvbl9zaGEyNTYiOiJaIiwiZXhlY3V0aW9uX2NvbnRyYWN0X3NoYTI1NiI6Ik8iLCJkZWNsYXJlZF90cmlhbHMiOltdLCJyZXF1aXJlZF9jYXNlcyI6W10sImNyZWF0ZWRfYnlfaG9zdCI6bnVsbH0="
	fixtureObservationTemplate = "eyJzY2hlbWEiOiJwMi5mYXJtX29ic2VydmF0aW9uL3YxIiwiZmFybV9pZCI6ImYiLCJ0cmlhbF9pZCI6InQiLCJhdHRlbXB0X2lkIjoiYSIsImNhcHN1bGVfaWQiOiJjIiwiaW5jYXJuYXRpb24iOiJpIiwiY29tbWl0IjoiYyIsInJlY2VpcHRfc2hhMjU2IjoiVCIsImdyYW50X3NoYTI1NiI6IkgiLCJleGVjdXRpb25fY29udHJhY3Rfc2hhMjU2IjoiTyIsImhvc3RfdmFsaWRhdGlvbl9yZWYiOiIiLCJnYXRlX2V4aXRfY29kZSI6bnVsbCwiY2FzZV9yZXN1bHRzIjpbXSwiY2xlYW51cCI6e30sInRlbGVtZXRyeV9yZWYiOm51bGwsIm91dGNvbWUiOiJVTktOT1dOIn0="
)

func mustJSON(t *testing.T, v any) []byte {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return b
}
func independentHash(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}
func demandReject(t *testing.T, err error, class string, code int) {
	t.Helper()
	var r *workerbridge.Reject
	if !errors.As(err, &r) {
		t.Fatalf("want %s/%d rejection; got %v", class, code, err)
	}
	if r.Class != class || r.Code != code {
		t.Fatalf("want %s/%d, got %s/%d: %s", class, code, r.Class, r.Code, r.Detail)
	}
}
func checkedAuthority(t *testing.T) *HostAuthority {
	t.Helper()
	a, err := NewHostAuthority(bytes.Repeat([]byte{0x5a}, 32))
	if err != nil {
		t.Fatal(err)
	}
	return a
}
func testContract(commit, manifest string) ExecutionContract {
	return ExecutionContract{
		Cases: []string{"case-a", "case-b"}, GateArgv: []string{"go", "test", "./..."},
		Environment:     []string{"GOOS=linux", "TZ=UTC"},
		ToolchainSHA256: strings.Repeat("1", 64), ImageSHA256: strings.Repeat("2", 64), RuntimeSHA256: strings.Repeat("3", 64),
		CapabilityClass: "capsule-a", SeedPolicySHA256: strings.Repeat("4", 64), IsolationResourceClass: "mem-small",
		IntendedCommit: commit, FullManifestSHA256: manifest,
	}
}
func testSuite() SuiteProjection {
	return SuiteProjection{RequiredCases: []string{"case-a", "case-b"}, Shards: []string{"shard-alpha", "shard-beta"}}
}
func testInventory(c ExecutionContract, s SuiteProjection) TrialInventory {
	return TrialInventory{
		Schema: TrialInventorySchema, FarmID: "farm-a", Repo: "oguirado/OpenSuperficies", Commit: c.IntendedCommit,
		SuiteProjectionSHA256: independentHash(marshalForTest(s)), ExecutionContractSHA256: independentHash(marshalForTest(c)),
		DeclaredTrials: []TrialDeclaration{
			{TrialID: "trial-a1", ShardID: "shard-alpha", RepetitionID: "rep-1", CapsuleClass: "capsule-a", SeedPolicySHA256: c.SeedPolicySHA256, ExpectedCaseIDs: []string{"case-a", "case-b"}},
			{TrialID: "trial-a2", ShardID: "shard-alpha", RepetitionID: "rep-2", CapsuleClass: "capsule-a", SeedPolicySHA256: c.SeedPolicySHA256, ExpectedCaseIDs: []string{"case-a", "case-b"}},
			{TrialID: "trial-b1", ShardID: "shard-beta", RepetitionID: "rep-1", CapsuleClass: "capsule-a", SeedPolicySHA256: c.SeedPolicySHA256, ExpectedCaseIDs: []string{"case-a", "case-b"}},
		},
		RequiredCases: []string{"case-a", "case-b"},
	}
}
func marshalForTest(v any) []byte {
	b, e := json.Marshal(v)
	if e != nil {
		panic(e)
	}
	return b
}
func signedSeal(t *testing.T, a *HostAuthority, c ExecutionContract, s SuiteProjection) (SealedInventory, TrialInventory, []byte) {
	t.Helper()
	inv := testInventory(c, s)
	wire, err := a.AttestInventory(inv, "host-a")
	if err != nil {
		t.Fatal(err)
	}
	seal, err := SealTrialInventory(wire, a, c, s)
	if err != nil {
		t.Fatal(err)
	}
	return seal, inv, wire
}
func testAttempt(label string) TrialAttempt {
	return TrialAttempt{AttemptID: "attempt-" + label, CapsuleID: "capsule-" + label, Incarnation: "inc-1", LeaseID: "lease-" + label, Nonce: "nonce-" + label}
}
func signedDelivery(t *testing.T, a *HostAuthority, inv TrialInventory, trial string, attempt TrialAttempt, outcome string, receipt string) json.RawMessage {
	t.Helper()
	// Independent grant oracle: typed JSON and stdlib SHA-256, not GrantSHA256.
	grant := struct {
		TrialID string       `json:"trial_id"`
		Attempt TrialAttempt `json:"attempt"`
	}{trial, attempt}
	zero := int64(0)
	results := []CaseResult{{CaseID: "case-a", Status: "PASS", FrameNum: 100}, {CaseID: "case-b", Status: "PASS", FrameNum: 101}}
	var exit *int64 = &zero
	if outcome == "INFRA" {
		results = []CaseResult{}
		exit = nil
	}
	if outcome == "FAIL" {
		results[1].Status = "FAIL"
		code := int64(1)
		exit = &code
	}
	obs := FarmObservation{
		Schema: FarmObservationSchema, FarmID: inv.FarmID, TrialID: trial,
		AttemptID: attempt.AttemptID, CapsuleID: attempt.CapsuleID, Incarnation: attempt.Incarnation, Commit: inv.Commit,
		ReceiptSHA256: independentHash([]byte(receipt)), GrantSHA256: independentHash(mustJSON(t, grant)),
		ExecutionContractSHA256: inv.ExecutionContractSHA256, GateExitCode: exit, CaseResults: results,
		Cleanup: CleanupEvidence{Disposed: true, NoForbiddenIO: true}, Outcome: outcome,
	}
	wire, err := a.AttestObservation(obs)
	if err != nil {
		t.Fatal(err)
	}
	return json.RawMessage(mustJSON(t, Delivery{TrialID: trial, Attempt: attempt, Observation: wire}))
}
func bundleBytes(t *testing.T, wire []byte, c ExecutionContract, s SuiteProjection, d ...json.RawMessage) []byte {
	t.Helper()
	if d == nil {
		d = []json.RawMessage{}
	}
	return mustJSON(t, FarmBundle{Schema: FarmBundleSchema, TrialInventory: wire, ExecutionContract: c, SuiteProjection: s, Deliveries: d})
}
func assertPreserved(t *testing.T, r FarmReport, expected []string) {
	t.Helper()
	got := []string{}
	for _, o := range r.Obligations {
		got = append(got, o.TrialID)
	}
	if !reflect.DeepEqual(got, expected) {
		t.Fatalf("lost/changed trial obligations: got %v want %v", got, expected)
	}
	if r.Declared != len(expected) {
		t.Fatalf("declared=%d, expected=%d", r.Declared, len(expected))
	}
}
func expandedVector(t *testing.T, b64 string) []byte {
	t.Helper()
	v, err := base64.StdEncoding.DecodeString(b64)
	if err != nil {
		t.Fatal(err)
	}
	for _, pair := range [][2]string{{"Z", "0"}, {"O", "1"}, {"T", "2"}, {"H", "3"}} {
		v = bytes.ReplaceAll(v, []byte("\""+pair[0]+"\""), []byte("\""+strings.Repeat(pair[1], 64)+"\""))
	}
	return v
}

func TestProto2Unit(t *testing.T) {
	a := checkedAuthority(t)
	c := testContract(strings.Repeat("a", 40), strings.Repeat("a", 64))
	s := testSuite()
	seal, inv, wire := signedSeal(t, a, c, s)
	expectedIDs := []string{"trial-a1", "trial-a2", "trial-b1"} // literal independent denominator
	one := signedDelivery(t, a, inv, "trial-a1", testAttempt("a1"), "PASS", "receipt-one")
	two := signedDelivery(t, a, inv, "trial-a2", testAttempt("a2"), "PASS", "receipt-two")
	three := signedDelivery(t, a, inv, "trial-b1", testAttempt("b1"), "PASS", "receipt-three")
	retry := signedDelivery(t, a, inv, "trial-a1", testAttempt("a1-retry"), "PASS", "receipt-retry")

	t.Run("out_of_order_repeated_shards_and_exact_duplicate", func(t *testing.T) {
		original := bundleBytes(t, wire, c, s, three, two, one, retry, one)
		before := bytes.Clone(original)
		report, err := ReconcileFarmBundle(seal, original, a)
		if err != nil {
			t.Fatal(err)
		}
		assertPreserved(t, report, expectedIDs)
		if report.Verdict != CompletePass || report.Declared != 3 || report.ValidIndependent != 3 || report.Attempts != 4 || report.Missing != 0 || report.Infrastructure != 0 || report.Conflicted != 0 || report.Hold {
			t.Fatalf("incorrect independent population/attempt reconciliation: %+v", report)
		}
		if !bytes.Equal(original, before) {
			t.Fatal("parser mutated source bytes")
		}
		if len(report.Obligations[0].Attempts) != 2 {
			t.Fatal("retry lost or counted as new trial")
		}
		if err := AssertComplete(report); err != nil {
			t.Fatal(err)
		}
	})
	t.Run("permutation_is_idempotent", func(t *testing.T) {
		first, err := ReconcileFarmBundle(seal, bundleBytes(t, wire, c, s, one, retry, two, three), a)
		if err != nil {
			t.Fatal(err)
		}
		second, err := ReconcileFarmBundle(seal, bundleBytes(t, wire, c, s, three, two, one, retry), a)
		if err != nil {
			t.Fatal(err)
		}
		if first.Verdict != second.Verdict || first.Attempts != second.Attempts || first.ValidIndependent != second.ValidIndependent {
			t.Fatalf("delivery order changed verdict/counts: %+v %+v", first, second)
		}
	})
	t.Run("baseline_comparison_excludes_only_revision_and_manifest", func(t *testing.T) {
		baseline := testContract(strings.Repeat("b", 40), strings.Repeat("e", 64))
		baseSeal, baseInv, baseWire := signedSeal(t, a, baseline, s)
		cmp, err := ComparableExecution(c, baseline, s, s)
		if err != nil || !cmp {
			t.Fatalf("legitimate revision comparison refused: %v, %v", cmp, err)
		}
		fullA, err := FullExecutionPin(c)
		if err != nil {
			t.Fatal(err)
		}
		fullB, err := FullExecutionPin(baseline)
		if err != nil {
			t.Fatal(err)
		}
		if fullA == fullB {
			t.Fatal("revision and manifest were lost from full pin")
		}
		baseReport, err := ReconcileFarmBundle(baseSeal, bundleBytes(t, baseWire, baseline, s,
			signedDelivery(t, a, baseInv, "trial-b1", testAttempt("baseline-b1"), "PASS", "base-1"),
			signedDelivery(t, a, baseInv, "trial-a2", testAttempt("baseline-a2"), "PASS", "base-2"),
			signedDelivery(t, a, baseInv, "trial-a1", testAttempt("baseline-a1"), "PASS", "base-3")), a)
		if err != nil {
			t.Fatal(err)
		}
		current, err := ReconcileFarmBundle(seal, bundleBytes(t, wire, c, s, one, two, three), a)
		if err != nil {
			t.Fatal(err)
		}
		comparison, err := CompareFarmReports(current, &baseReport, c, baseline, s, s)
		if err != nil || comparison != CompletePass {
			t.Fatalf("comparable commits: %s %v", comparison, err)
		}
		projected, err := CanonicalComparisonProjection(c)
		if err != nil {
			t.Fatal(err)
		}
		// Independent literal bytes in frozen comparison order (not computed from another production output).
		literal := `{"cases":["case-a","case-b"],"gate_argv":["go","test","./..."],"environment":["GOOS=linux","TZ=UTC"],"toolchain_sha256":"` + strings.Repeat("1", 64) + `","image_sha256":"` + strings.Repeat("2", 64) + `","runtime_sha256":"` + strings.Repeat("3", 64) + `","capability_class":"capsule-a","seed_policy_sha256":"` + strings.Repeat("4", 64) + `","isolation_resource_class":"mem-small"}`
		if string(projected) != literal {
			t.Fatalf("comparison projection differs from independent literal: %s", projected)
		}
		if independentHash(projected) != "0a38979691e027339099fb4d74ee291ecab9ef704010f1c6a73e72dfa0592a2a" {
			t.Fatalf("projection canonical hash changed: %s", independentHash(projected))
		}
	})
	t.Run("baseline_absent_and_regression_vocabulary", func(t *testing.T) {
		ok, err := ReconcileFarmBundle(seal, bundleBytes(t, wire, c, s, one, two, three), a)
		if err != nil {
			t.Fatal(err)
		}
		fail, err := ReconcileFarmBundle(seal, bundleBytes(t, wire, c, s, signedDelivery(t, a, inv, "trial-a1", testAttempt("f"), "FAIL", "fail"), two, three), a)
		if err != nil {
			t.Fatal(err)
		}
		v, err := CompareFarmReports(fail, nil, c, c, s, s)
		if err != nil || v != BaselineAbsent {
			t.Fatalf("missing baseline became success: %s %v", v, err)
		}
		v, err = CompareFarmReports(fail, &ok, c, c, s, s)
		if err != nil || v != RegressionObserved {
			t.Fatalf("regression incorrectly classified: %s %v", v, err)
		}
		v, err = CompareFarmReports(fail, &fail, c, c, s, s)
		if err != nil || v != ExistingFailure {
			t.Fatalf("existing failure incorrectly classified: %s %v", v, err)
		}
	})
	t.Run("missing_and_infrastructure_population", func(t *testing.T) {
		empty, err := ReconcileFarmBundle(seal, bundleBytes(t, wire, c, s), a)
		if err != nil {
			t.Fatal(err)
		}
		assertPreserved(t, empty, expectedIDs)
		if empty.Verdict != Incomplete || empty.Missing != 3 || empty.ValidIndependent != 0 {
			t.Fatalf("missing became successful: %+v", empty)
		}
		demandReject(t, AssertComplete(empty), "INCOMPLETE_TRIAL_POPULATION", 5)
		infra := signedDelivery(t, a, inv, "trial-b1", testAttempt("infra"), "INFRA", "host-infra")
		report, err := ReconcileFarmBundle(seal, bundleBytes(t, wire, c, s, infra), a)
		if err != nil {
			t.Fatal(err)
		}
		assertPreserved(t, report, expectedIDs)
		if report.Verdict != Incomplete || report.Infrastructure != 1 || report.Missing != 2 || report.ValidIndependent != 0 {
			t.Fatalf("infra over-counted success: %+v", report)
		}
		demandReject(t, AssertComplete(report), "INCOMPLETE_TRIAL_POPULATION", 5)
	})
	t.Run("removed_trial_is_a_hold_not_a_smaller_denominator", func(t *testing.T) {
		mut := testInventory(c, s)
		mut.DeclaredTrials = mut.DeclaredTrials[:2]
		altered, err := a.AttestInventory(mut, "host-a")
		if err != nil {
			t.Fatal(err)
		}
		r, e := ReconcileFarmBundle(seal, bundleBytes(t, altered, c, s, one, two), a)
		demandReject(t, e, "INVENTORY_CHANGED", 5)
		assertPreserved(t, r, expectedIDs)
		if !r.Hold || r.Verdict != Inconclusive {
			t.Fatal("removed repetition cleared reservation")
		}
	})
	t.Run("removed_expected_case_is_a_hold", func(t *testing.T) {
		mut := testInventory(c, s)
		mut.DeclaredTrials[0].ExpectedCaseIDs = []string{"case-a"}
		altered, err := a.AttestInventory(mut, "host-a")
		if err != nil {
			t.Fatal(err)
		}
		r, e := ReconcileFarmBundle(seal, bundleBytes(t, altered, c, s, one), a)
		demandReject(t, e, "INVENTORY_CHANGED", 5)
		assertPreserved(t, r, expectedIDs)
		if !r.Hold {
			t.Fatal("removed case released reservation")
		}
	})
	t.Run("retry_cannot_allocate_another_independent_trial", func(t *testing.T) {
		wrong := signedDelivery(t, a, inv, "trial-a2", testAttempt("a1"), "PASS", "other-receipt")
		r, e := ReconcileFarmBundle(seal, bundleBytes(t, wire, c, s, one, wrong), a)
		demandReject(t, e, "RETRY_AS_INDEPENDENT", 5)
		assertPreserved(t, r, expectedIDs)
		if !r.Hold || r.Conflicted != 1 {
			t.Fatalf("retry was counted as a separate valid trial: %+v", r)
		}
	})
	t.Run("conflicting_duplicate_bytes_hold_trial", func(t *testing.T) {
		changed := signedDelivery(t, a, inv, "trial-a1", testAttempt("a1"), "PASS", "different-receipt")
		r, e := ReconcileFarmBundle(seal, bundleBytes(t, wire, c, s, one, changed), a)
		demandReject(t, e, "CONFLICTING_DELIVERY", 5)
		assertPreserved(t, r, expectedIDs)
		if !r.Hold || r.Conflicted != 1 || r.Verdict != Inconclusive {
			t.Fatalf("conflicting byte claim lost: %+v", r)
		}
	})
	t.Run("guest_created_host_claim_is_not_authority", func(t *testing.T) {
		mut := bytes.Replace(wire, []byte(`"created_by_host":{"host_id":"host-a","mac_sha256":"`), []byte(`"created_by_host":{"host_id":"host-a","mac_sha256":"0`), 1)
		_, e := ParseTrialInventory(mut, a)
		demandReject(t, e, "BAD_HOST_ATTESTATION", 5)
		untrusted := testInventory(c, s)
		untrusted.CreatedByHost = &HostAttestation{HostID: "guest", MACSHA256: strings.Repeat("0", 64)}
		_, e = ParseTrialInventory(mustJSON(t, untrusted), a)
		demandReject(t, e, "BAD_HOST_ATTESTATION", 5)
		_, e = ParseTrialInventory(wire, nil)
		demandReject(t, e, "BAD_HOST_ATTESTATION", 5)
	})
	t.Run("foreign_farm_or_commit_is_rejected", func(t *testing.T) {
		for _, which := range []string{"farm", "commit"} {
			var d Delivery
			if err := json.Unmarshal(one, &d); err != nil {
				t.Fatal(err)
			}
			var o FarmObservation
			if err := json.Unmarshal(d.Observation, &o); err != nil {
				t.Fatal(err)
			}
			if which == "farm" {
				o.FarmID = "foreign-farm"
			} else {
				o.Commit = strings.Repeat("f", 40)
			}
			altered, err := a.AttestObservation(o)
			if err != nil {
				t.Fatal(err)
			}
			d.Observation = altered
			r, e := ReconcileFarmBundle(seal, bundleBytes(t, wire, c, s, mustJSON(t, d)), a)
			demandReject(t, e, "FOREIGN_OBSERVATION", 5)
			assertPreserved(t, r, expectedIDs)
			if !r.Hold || r.Conflicted != 1 {
				t.Fatalf("foreign %s escaped HOLD: %+v", which, r)
			}
		}
	})
	t.Run("seed_image_resource_comparison_changes_refused", func(t *testing.T) {
		for _, which := range []string{"seed", "image", "resource"} {
			mut := c
			switch which {
			case "seed":
				mut.SeedPolicySHA256 = strings.Repeat("9", 64)
			case "image":
				mut.ImageSHA256 = strings.Repeat("8", 64)
			case "resource":
				mut.IsolationResourceClass = "mem-large"
			}
			comparable, err := ComparableExecution(c, mut, s, s)
			if err != nil || comparable {
				t.Fatalf("changed %s was comparable: %v %v", which, comparable, err)
			}
			r, e := ReconcileFarmBundle(seal, bundleBytes(t, wire, mut, s, one, two, three), a)
			demandReject(t, e, "EXECUTION_PROJECTION_MISMATCH", 5)
			assertPreserved(t, r, expectedIDs)
			if !r.Hold || r.Verdict != Inconclusive {
				t.Fatalf("changed %s released reservation: %+v", which, r)
			}
		}
	})
	t.Run("missing_unknown_cases_and_bad_pins_refused", func(t *testing.T) {
		var d Delivery
		if err := json.Unmarshal(one, &d); err != nil {
			t.Fatal(err)
		}
		var o FarmObservation
		if err := json.Unmarshal(d.Observation, &o); err != nil {
			t.Fatal(err)
		}
		o.CaseResults = o.CaseResults[:1]
		w, err := a.AttestObservation(o)
		if err != nil {
			t.Fatal(err)
		}
		d.Observation = w
		r, e := ReconcileFarmBundle(seal, bundleBytes(t, wire, c, s, mustJSON(t, d)), a)
		demandReject(t, e, "CASE_MEMBERSHIP_MISMATCH", 5)
		assertPreserved(t, r, expectedIDs)
		mut := testInventory(c, s)
		mut.ExecutionContractSHA256 = "not-a-hash"
		_, e = a.AttestInventory(mut, "host-a")
		demandReject(t, e, "INVALID_PIN", 2)
		mut = testInventory(c, s)
		mut.DeclaredTrials[0].ExpectedCaseIDs = []string{"case-z"}
		_, e = a.AttestInventory(mut, "host-a")
		demandReject(t, e, "UNKNOWN_CASE", 2)
		mut = testInventory(c, s)
		mut.DeclaredTrials[1].TrialID = "trial-a1"
		_, e = a.AttestInventory(mut, "host-a")
		demandReject(t, e, "DUPLICATE_TRIAL", 2)
	})
	t.Run("signed_mixed_pass_fail_is_flake_not_pass", func(t *testing.T) {
		failed := signedDelivery(t, a, inv, "trial-a1", testAttempt("a1-second"), "FAIL", "fail-observed")
		r, err := ReconcileFarmBundle(seal, bundleBytes(t, wire, c, s, one, failed, two, three), a)
		if err != nil {
			t.Fatal(err)
		}
		if r.Verdict != FlakeObserved || r.ValidIndependent != 3 || r.Attempts != 4 {
			t.Fatalf("flake hidden or retry inflated denominator: %+v", r)
		}
		demandReject(t, AssertComplete(r), "INCOMPLETE_TRIAL_POPULATION", 5)
	})
	t.Run("shared_frozen_sha256_vectors_and_negative_admission", func(t *testing.T) {
		inventoryRaw := expandedVector(t, fixtureInventoryTemplate)
		if independentHash(inventoryRaw) != "cc7a8b9e12fb11b1a38a27122dc1241028c3cfa8e306409da24709e69a3ba6e9" {
			t.Fatal("independent inventory vector digest mismatch")
		}
		invTemplate := TrialInventory{Schema: TrialInventorySchema, FarmID: "f", Repo: "r", Commit: "c", SuiteProjectionSHA256: strings.Repeat("0", 64), ExecutionContractSHA256: strings.Repeat("1", 64), DeclaredTrials: []TrialDeclaration{}, RequiredCases: []string{}}
		if !bytes.Equal(inventoryRaw, mustJSON(t, invTemplate)) {
			t.Fatal("Go vs independent Python template bytes differ")
		}
		_, err := ParseTrialInventory(inventoryRaw, a)
		demandReject(t, err, "MALFORMED", 2) // short repo/commit invalid before empty trials
		observationRaw := expandedVector(t, fixtureObservationTemplate)
		if independentHash(observationRaw) != "f645d38f21ee759525d0d419bcc917706852f2d8bc3b1b55864196ef3daa2b4b" {
			t.Fatal("independent observation vector digest mismatch")
		}
		// Test-only independently authored serializer reproduces the invalid {}
		// cleanup control; the production CleanupEvidence intentionally cannot.
		obsTemplate := struct {
			Schema                  string         `json:"schema"`
			FarmID                  string         `json:"farm_id"`
			TrialID                 string         `json:"trial_id"`
			AttemptID               string         `json:"attempt_id"`
			CapsuleID               string         `json:"capsule_id"`
			Incarnation             string         `json:"incarnation"`
			Commit                  string         `json:"commit"`
			ReceiptSHA256           string         `json:"receipt_sha256"`
			GrantSHA256             string         `json:"grant_sha256"`
			ExecutionContractSHA256 string         `json:"execution_contract_sha256"`
			HostValidationRef       string         `json:"host_validation_ref"`
			GateExitCode            *int64         `json:"gate_exit_code"`
			CaseResults             []CaseResult   `json:"case_results"`
			Cleanup                 map[string]any `json:"cleanup"`
			TelemetryRef            *string        `json:"telemetry_ref"`
			Outcome                 string         `json:"outcome"`
		}{Schema: FarmObservationSchema, FarmID: "f", TrialID: "t", AttemptID: "a", CapsuleID: "c", Incarnation: "i", Commit: "c", ReceiptSHA256: strings.Repeat("2", 64), GrantSHA256: strings.Repeat("3", 64), ExecutionContractSHA256: strings.Repeat("1", 64), CaseResults: []CaseResult{}, Cleanup: map[string]any{}, Outcome: "UNKNOWN"}
		if !bytes.Equal(observationRaw, mustJSON(t, obsTemplate)) {
			t.Fatal("Go vs independent Python observation bytes differ")
		}
		_, err = ParseFarmObservation(observationRaw, a)
		demandReject(t, err, "NONCANONICAL_WIRE", 2) // empty cleanup is not the ordered signed form
		if bytes.Equal(inventoryRaw, append(bytes.Clone(inventoryRaw), '\n')) {
			t.Fatal("LF is supposed to change bytes")
		}
		if independentHash(append(bytes.Clone(inventoryRaw), '\n')) == independentHash(inventoryRaw) {
			t.Fatal("LF did not change digest")
		}
	})
}

// Package p2flake provides a pure, host-evidence-bound classification of fixed
// independent trial populations. It does not launch, admit or release capsules.
package p2flake

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math"
	"regexp"
	"sort"
	"strconv"
	"strings"

	"opensuperficies.local/srf/src/workerbridge"
)

const (
	InventorySchema   = "p2.farm_inventory/v1"
	ObservationSchema = "p2.farm_observation/v1"
	// MaxTrials bounds numerical work and rejects unbounded or undeclared samples.
	MaxTrials = 10000
)

// Outcome is a host observation, not a diagnosis of the cause of failure.
type Outcome string

const (
	Pass           Outcome = "PASS"
	Fail           Outcome = "FAIL"
	Infrastructure Outcome = "INFRASTRUCTURE"
	Unknown        Outcome = "UNKNOWN"
)

// Classification is a sample-level verdict, not an admission or release order.
type Classification string

const (
	CompletePass       Classification = "COMPLETE_PASS"
	Incomplete         Classification = "INCOMPLETE"
	RegressionObserved Classification = "REGRESSION_OBSERVED"
	ObservedFailure    Classification = "OBSERVED_FAILURE"
	BaselineAbsent     Classification = "BASELINE_ABSENT"
	ExistingFailure    Classification = "EXISTING_FAILURE"
	FlakeObserved      Classification = "FLAKE_OBSERVED"
	Inconclusive       Classification = "INCONCLUSIVE"
)

// Inventory freezes the independently supplied, host-declared trial obligations.
// BaselineCommit is required on the wire but may be JSON null. DeclaredTrials
// is the number of distinct, precommitted trial IDs, never an attempt count.
type Inventory struct {
	Schema                  string   `json:"schema"`
	FarmID                  string   `json:"farm_id"`
	Repo                    string   `json:"repo"`
	Commit                  string   `json:"commit"`
	BaselineCommit          *string  `json:"baseline_commit"`
	SuiteProjectionSHA256   string   `json:"suite_projection_sha256"`
	ExecutionContractSHA256 string   `json:"execution_contract_sha256"`
	DeclaredTrials          int      `json:"declared_trials"`
	RequiredCases           []string `json:"required_cases"`
	CreatedByHost           bool     `json:"created_by_host"`
}

// CaseResult reports a single required case. CaseID is unique per attempt.
type CaseResult struct {
	CaseID  string  `json:"case_id"`
	Outcome Outcome `json:"outcome"`
}

// CleanupEvidence records explicit host verification of isolation after a run.
// A false verified or true state_leaked invalidates independence; it is not
// evidence of a test failure by itself.
type CleanupEvidence struct {
	Verified    bool `json:"verified"`
	StateLeaked bool `json:"state_leaked"`
}

// Observation is one attempt's evidence. TrialID, not receipt digest or
// AttemptID, identifies an intended independent repetition. Incarnation must
// be fresh for a reused capsule; reusing (capsule_id,incarnation) is correlated.
type Observation struct {
	Schema                  string          `json:"schema"`
	FarmID                  string          `json:"farm_id"`
	TrialID                 string          `json:"trial_id"`
	AttemptID               string          `json:"attempt_id"`
	CapsuleID               string          `json:"capsule_id"`
	Incarnation             int64           `json:"incarnation"`
	Commit                  string          `json:"commit"`
	ReceiptSHA256           string          `json:"receipt_sha256"`
	GrantSHA256             string          `json:"grant_sha256"`
	ExecutionContractSHA256 string          `json:"execution_contract_sha256"`
	HostValidationRef       string          `json:"host_validation_ref"`
	GateExitCode            *int            `json:"gate_exit_code"`
	CaseResults             []CaseResult    `json:"case_results"`
	Cleanup                 CleanupEvidence `json:"cleanup"`
	TelemetryRef            *string         `json:"telemetry_ref"`
	Outcome                 Outcome         `json:"outcome"`
}

// Sample couples a fixed inventory to its observations. Baseline is a separate
// Sample whose inventory commit equals the current inventory's baseline_commit.
type Sample struct {
	Inventory    Inventory
	Observations []Observation
}

// Interval is a two-sided exact frequentist 95% Clopper-Pearson interval for
// failure probability among the valid independent n, never a beta posterior.
type Interval struct {
	Low  float64 `json:"low"`
	High float64 `json:"high"`
}

// TrialEvidence preserves every attempt even if none can be counted in n.
type TrialEvidence struct {
	TrialID  string        `json:"trial_id"`
	Attempts []Observation `json:"attempts"`
	Excluded []string      `json:"excluded"`
}

// Stratum pins comparable observations to code and execution projections.
type Stratum struct {
	Commit                  string `json:"commit"`
	SuiteProjectionSHA256   string `json:"suite_projection_sha256"`
	ExecutionContractSHA256 string `json:"execution_contract_sha256"`
	ObservedTrials          int    `json:"observed_trials"`
	Attempts                int    `json:"attempts"`
}

// Summary reports the whole declared population alongside valid failures k
// out of independent comparable attempts n. An absent interval is unknown.
type Summary struct {
	K                      int             `json:"k"`
	N                      int             `json:"n"`
	Declared               int             `json:"declared"`
	ObservedTrials         int             `json:"observed_trials"`
	Missing                int             `json:"missing"`
	InfrastructureTrials   int             `json:"infrastructure_trials"`
	InfrastructureAttempts int             `json:"infrastructure_attempts"`
	UnknownTrials          int             `json:"unknown_trials"`
	RetriedTrials          int             `json:"retried_trials"`
	CorrelatedTrials       int             `json:"correlated_trials"`
	UnverifiedTrials       int             `json:"unverified_trials"`
	Interval95             *Interval       `json:"interval_95"`
	Strata                 []Stratum       `json:"strata"`
	Trials                 []TrialEvidence `json:"trials"`
}

// Result never changes host capacity, execution, admission or receipt custody.
type Result struct {
	Classification Classification `json:"classification"`
	Current        Summary        `json:"current"`
	Baseline       *Summary       `json:"baseline"`
}

var (
	idRE     = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`)
	commitRE = regexp.MustCompile(`^[0-9a-f]{40}$`)
	digestRE = regexp.MustCompile(`^[0-9a-f]{64}$`)
	repoRE   = regexp.MustCompile(`^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$`)
)

func refusal(class, detail string) error {
	return &workerbridge.Reject{Code: 5, Class: class, Detail: detail}
}
func malformed(detail string) error {
	return &workerbridge.Reject{Code: 2, Class: "MALFORMED", Detail: detail}
}

// closedDocument preserves workerbridge's duplicate-key/noncanonical-number
// refusals and their original exit codes, then enforces a closed field set.
func closedDocument(data []byte, keys []string) (map[string]any, error) {
	doc, err := workerbridge.Parse(data)
	if err != nil {
		return nil, err
	}
	allowed := make(map[string]bool, len(keys))
	for _, k := range keys {
		allowed[k] = true
	}
	for k := range doc {
		if !allowed[k] {
			return nil, &workerbridge.Reject{Code: 2, Class: "UNKNOWN_KEY", Detail: k}
		}
	}
	for _, k := range keys {
		if _, ok := doc[k]; !ok {
			return nil, malformed("missing " + k)
		}
	}
	return doc, nil
}
func fieldString(doc map[string]any, k string, rule *regexp.Regexp) (string, error) {
	v, ok := doc[k].(string)
	if !ok || v == "" || (rule != nil && !rule.MatchString(v)) {
		return "", malformed("invalid " + k)
	}
	return v, nil
}
func nullableString(doc map[string]any, k string, rule *regexp.Regexp) (*string, error) {
	if doc[k] == nil {
		return nil, nil
	}
	v, err := fieldString(doc, k, rule)
	if err != nil {
		return nil, err
	}
	return &v, nil
}
func intField(doc map[string]any, k string, lo, hi int64) (int64, error) {
	n, ok := doc[k].(json.Number)
	if !ok {
		return 0, malformed("invalid integer " + k)
	}
	v, err := strconv.ParseInt(string(n), 10, 64)
	if err != nil || v < lo || v > hi {
		return 0, malformed("out of range " + k)
	}
	return v, nil
}
func outcome(v any) (Outcome, error) {
	s, ok := v.(string)
	if !ok {
		return "", malformed("invalid outcome")
	}
	o := Outcome(s)
	if o != Pass && o != Fail && o != Infrastructure && o != Unknown {
		return "", malformed("unknown outcome " + s)
	}
	return o, nil
}
func stringArray(v any, field string) ([]string, error) {
	a, ok := v.([]any)
	if !ok {
		return nil, malformed("invalid " + field)
	}
	out := make([]string, 0, len(a))
	seen := map[string]bool{}
	for _, item := range a {
		s, ok := item.(string)
		if !ok || !idRE.MatchString(s) || seen[s] {
			return nil, malformed("invalid or duplicate " + field)
		}
		seen[s] = true
		out = append(out, s)
	}
	if len(out) == 0 {
		return nil, malformed("empty " + field)
	}
	return out, nil
}

// ParseInventory accepts exactly the ordered p2.farm_inventory/v1 projection.
// created_by_host=false remains parseable but cannot support independent CI.
func ParseInventory(data []byte) (Inventory, error) {
	keys := []string{"schema", "farm_id", "repo", "commit", "baseline_commit", "suite_projection_sha256", "execution_contract_sha256", "declared_trials", "required_cases", "created_by_host"}
	d, err := closedDocument(data, keys)
	if err != nil {
		return Inventory{}, err
	}
	var i Inventory
	if i.Schema, err = fieldString(d, "schema", nil); err != nil {
		return Inventory{}, err
	}
	if i.Schema != InventorySchema {
		return Inventory{}, malformed("inventory schema")
	}
	for _, f := range []struct {
		key string
		ptr *string
		re  *regexp.Regexp
	}{
		{"farm_id", &i.FarmID, idRE}, {"repo", &i.Repo, repoRE}, {"commit", &i.Commit, commitRE},
		{"suite_projection_sha256", &i.SuiteProjectionSHA256, digestRE}, {"execution_contract_sha256", &i.ExecutionContractSHA256, digestRE},
	} {
		if *f.ptr, err = fieldString(d, f.key, f.re); err != nil {
			return Inventory{}, err
		}
	}
	if i.BaselineCommit, err = nullableString(d, "baseline_commit", commitRE); err != nil {
		return Inventory{}, err
	}
	n, err := intField(d, "declared_trials", 1, MaxTrials)
	if err != nil {
		return Inventory{}, err
	}
	i.DeclaredTrials = int(n)
	if i.RequiredCases, err = stringArray(d["required_cases"], "required_cases"); err != nil {
		return Inventory{}, err
	}
	var ok bool
	i.CreatedByHost, ok = d["created_by_host"].(bool)
	if !ok {
		return Inventory{}, malformed("created_by_host must be boolean")
	}
	return i, nil
}

// ParseObservation strictly decodes a single p2.farm_observation/v1 attempt.
// gate_exit_code and telemetry_ref alone may be null, as documented below.
func ParseObservation(data []byte) (Observation, error) {
	keys := []string{"schema", "farm_id", "trial_id", "attempt_id", "capsule_id", "incarnation", "commit", "receipt_sha256", "grant_sha256", "execution_contract_sha256", "host_validation_ref", "gate_exit_code", "case_results", "cleanup", "telemetry_ref", "outcome"}
	d, err := closedDocument(data, keys)
	if err != nil {
		return Observation{}, err
	}
	var o Observation
	for _, f := range []struct {
		key string
		ptr *string
		re  *regexp.Regexp
	}{
		{"schema", &o.Schema, nil}, {"farm_id", &o.FarmID, idRE}, {"trial_id", &o.TrialID, idRE},
		{"attempt_id", &o.AttemptID, idRE}, {"capsule_id", &o.CapsuleID, idRE},
		{"commit", &o.Commit, commitRE}, {"receipt_sha256", &o.ReceiptSHA256, digestRE},
		{"grant_sha256", &o.GrantSHA256, digestRE}, {"execution_contract_sha256", &o.ExecutionContractSHA256, digestRE},
		{"host_validation_ref", &o.HostValidationRef, nil},
	} {
		if *f.ptr, err = fieldString(d, f.key, f.re); err != nil {
			return Observation{}, err
		}
	}
	if o.Schema != ObservationSchema {
		return Observation{}, malformed("observation schema")
	}
	if o.Incarnation, err = intField(d, "incarnation", 1, math.MaxInt64); err != nil {
		return Observation{}, err
	}
	if d["gate_exit_code"] != nil {
		n, e := intField(d, "gate_exit_code", 0, 255)
		if e != nil {
			return Observation{}, e
		}
		x := int(n)
		o.GateExitCode = &x
	}
	if o.TelemetryRef, err = nullableString(d, "telemetry_ref", nil); err != nil {
		return Observation{}, err
	}
	if o.Outcome, err = outcome(d["outcome"]); err != nil {
		return Observation{}, err
	}
	cases, ok := d["case_results"].([]any)
	if !ok {
		return Observation{}, malformed("case_results must be array")
	}
	o.CaseResults = make([]CaseResult, 0, len(cases))
	seen := map[string]bool{}
	for _, c := range cases {
		m, ok := c.(map[string]any)
		if !ok || len(m) != 2 {
			return Observation{}, malformed("case_result object")
		}
		for k := range m {
			if k != "case_id" && k != "outcome" {
				return Observation{}, malformed("unknown case_result field")
			}
		}
		id, e := fieldString(m, "case_id", idRE)
		if e != nil || seen[id] {
			return Observation{}, malformed("invalid or duplicate case_id")
		}
		seen[id] = true
		v, e := outcome(m["outcome"])
		if e != nil {
			return Observation{}, e
		}
		o.CaseResults = append(o.CaseResults, CaseResult{CaseID: id, Outcome: v})
	}
	cleanup, ok := d["cleanup"].(map[string]any)
	if !ok || len(cleanup) != 2 {
		return Observation{}, malformed("cleanup object")
	}
	for k := range cleanup {
		if k != "verified" && k != "state_leaked" {
			return Observation{}, malformed("unknown cleanup field")
		}
	}
	if o.Cleanup.Verified, ok = cleanup["verified"].(bool); !ok {
		return Observation{}, malformed("cleanup.verified")
	}
	if o.Cleanup.StateLeaked, ok = cleanup["state_leaked"].(bool); !ok {
		return Observation{}, malformed("cleanup.state_leaked")
	}
	return o, nil
}

// ClopperPearson95 returns the exact equal-tailed binomial inversion for k
// failures among n comparable independent trials. n=0 is unknown, not k/n=0.
// Bisection is bounded to 128 steps with a finite-domain and residual check.
func ClopperPearson95(k, n int) (Interval, error) {
	if n <= 0 || n > MaxTrials || k < 0 || k > n {
		return Interval{}, refusal("INVALID_SAMPLE", fmt.Sprintf("k=%d n=%d", k, n))
	}
	const halfAlpha = 0.025
	if k == 0 {
		return Interval{Low: 0, High: -math.Expm1(math.Log(halfAlpha) / float64(n))}, nil
	}
	if k == n {
		return Interval{Low: math.Exp(math.Log(halfAlpha) / float64(n)), High: 1}, nil
	}
	lower, err := invertTail(n, k, halfAlpha, true)
	if err != nil {
		return Interval{}, err
	}
	upper, err := invertTail(n, k, halfAlpha, false)
	if err != nil {
		return Interval{}, err
	}
	if !(lower >= 0 && lower < upper && upper <= 1) {
		return Interval{}, refusal("NUMERIC_FAILURE", "invalid interval ordering")
	}
	return Interval{Low: lower, High: upper}, nil
}

// binomialTail sums weights from the binomial mode outward, normalizing by
// their sum. This avoids factorial overflow and underflow from a zero end tail.
// upper=true yields P[X>=k], upper=false P[X<=k].
func binomialTail(n, k int, p float64, upper bool) (float64, error) {
	if n < 1 || k < 0 || k > n || math.IsNaN(p) || math.IsInf(p, 0) || p < 0 || p > 1 {
		return 0, refusal("NUMERIC_DOMAIN", "binomial tail")
	}
	if p == 0 {
		if upper {
			if k == 0 {
				return 1, nil
			}
			return 0, nil
		}
		return 1, nil
	}
	if p == 1 {
		if upper {
			return 1, nil
		}
		if k == n {
			return 1, nil
		}
		return 0, nil
	}
	mode := int(math.Floor((float64(n) + 1) * p))
	if mode > n {
		mode = n
	}
	total, target := 1.0, 0.0
	if (upper && mode >= k) || (!upper && mode <= k) {
		target = 1
	}
	weight := 1.0
	for j := mode - 1; j >= 0; j-- {
		weight *= float64(j+1) * (1 - p) / (float64(n-j) * p)
		total += weight
		if (upper && j >= k) || (!upper && j <= k) {
			target += weight
		}
	}
	weight = 1.0
	for j := mode + 1; j <= n; j++ {
		weight *= float64(n-j+1) * p / (float64(j) * (1 - p))
		total += weight
		if (upper && j >= k) || (!upper && j <= k) {
			target += weight
		}
	}
	if math.IsNaN(total) || math.IsInf(total, 0) || total < 1 || target < 0 || target > total*(1+1e-12) {
		return 0, refusal("NUMERIC_FAILURE", "unstable binomial weights")
	}
	v := math.Min(1, target/total)
	return v, nil
}
func invertTail(n, k int, alpha float64, upper bool) (float64, error) {
	lo, hi := 0.0, 1.0
	for step := 0; step < 128; step++ {
		mid := lo + (hi-lo)/2
		if mid == lo || mid == hi {
			break
		}
		tail, err := binomialTail(n, k, mid, upper)
		if err != nil {
			return 0, err
		}
		if upper {
			if tail < alpha {
				lo = mid
			} else {
				hi = mid
			}
		} else {
			if tail > alpha {
				lo = mid
			} else {
				hi = mid
			}
		}
	}
	candidate := lo + (hi-lo)/2
	if candidate == 0 || candidate == 1 || math.IsNaN(candidate) {
		return 0, refusal("NUMERIC_FAILURE", "bisection did not converge")
	}
	got, err := binomialTail(n, k, candidate, upper)
	if err != nil {
		return 0, err
	}
	if math.Abs(got-alpha) > 1e-9 {
		return 0, refusal("NUMERIC_FAILURE", "binomial inversion residual")
	}
	return candidate, nil
}

func validateInventory(i Inventory) error {
	b, err := json.Marshal(i)
	if err != nil {
		return malformed("inventory encoding")
	}
	_, err = ParseInventory(b)
	return err
}
func validateObservation(o Observation) error {
	b, err := json.Marshal(o)
	if err != nil {
		return malformed("observation encoding")
	}
	_, err = ParseObservation(b)
	return err
}
func validateCases(o Observation, required []string) error {
	if o.Outcome != Pass && o.Outcome != Fail {
		return nil
	}
	if len(o.CaseResults) != len(required) {
		return refusal("CASE_PROJECTION_MISMATCH", o.TrialID)
	}
	cases := make(map[string]Outcome, len(o.CaseResults))
	for _, c := range o.CaseResults {
		cases[c.CaseID] = c.Outcome
	}
	failedCase := false
	for _, id := range required {
		r, ok := cases[id]
		if !ok {
			return refusal("CASE_PROJECTION_MISMATCH", o.TrialID+": "+id)
		}
		if o.Outcome == Pass && r != Pass {
			return refusal("INCONSISTENT_OUTCOME", o.TrialID+": PASS with non-PASS case")
		}
		if r == Fail {
			failedCase = true
		}
	}
	if o.Outcome == Pass && o.GateExitCode != nil && *o.GateExitCode != 0 {
		return refusal("INCONSISTENT_OUTCOME", o.TrialID+": PASS with nonzero gate")
	}
	if o.Outcome == Fail && !failedCase && (o.GateExitCode == nil || *o.GateExitCode == 0) {
		return refusal("INCONSISTENT_OUTCOME", o.TrialID+": FAIL without failure evidence")
	}
	return nil
}

func summarize(s Sample) (Summary, error) {
	if err := validateInventory(s.Inventory); err != nil {
		return Summary{}, err
	}
	i := s.Inventory
	out := Summary{Declared: i.DeclaredTrials, Strata: []Stratum{{Commit: i.Commit, SuiteProjectionSHA256: i.SuiteProjectionSHA256, ExecutionContractSHA256: i.ExecutionContractSHA256, Attempts: len(s.Observations)}}}
	groups := map[string][]Observation{}
	for _, o := range s.Observations {
		if err := validateObservation(o); err != nil {
			return Summary{}, err
		}
		if o.FarmID != i.FarmID {
			return Summary{}, refusal("FOREIGN_FARM", o.TrialID)
		}
		if o.Commit != i.Commit {
			return Summary{}, refusal("FOREIGN_COMMIT", o.TrialID)
		}
		if o.ExecutionContractSHA256 != i.ExecutionContractSHA256 {
			return Summary{}, refusal("FOREIGN_PROJECTION", o.TrialID)
		}
		if err := validateCases(o, i.RequiredCases); err != nil {
			return Summary{}, err
		}
		groups[o.TrialID] = append(groups[o.TrialID], o)
		if o.Outcome == Infrastructure {
			out.InfrastructureAttempts++
		}
	}
	if len(groups) > i.DeclaredTrials {
		return Summary{}, refusal("EXCESS_TRIALS", "more trial IDs than host obligation")
	}
	out.ObservedTrials = len(groups)
	out.Missing = i.DeclaredTrials - len(groups)
	out.Strata[0].ObservedTrials = len(groups)
	ids := make([]string, 0, len(groups))
	for id := range groups {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	// Cross-trial reuse of the same capsule incarnation or attempt identity
	// invalidates independence; a fresh receipt hash does not repair reuse.
	capsules, attempts := map[string]string{}, map[string]string{}
	reasons := map[string]map[string]bool{}
	mark := func(trial, reason string) {
		if reasons[trial] == nil {
			reasons[trial] = map[string]bool{}
		}
		reasons[trial][reason] = true
	}
	for _, id := range ids {
		group := groups[id]
		if len(group) > 1 {
			mark(id, "RETRIED_TRIAL")
			out.RetriedTrials++
		}
		for _, o := range group {
			key := fmt.Sprintf("%s:%d", o.CapsuleID, o.Incarnation)
			if owner, ok := capsules[key]; ok && owner != id {
				mark(id, "SHARED_INCARNATION")
				mark(owner, "SHARED_INCARNATION")
			} else {
				capsules[key] = id
			}
			if owner, ok := attempts[o.AttemptID]; ok && owner != id {
				mark(id, "SHARED_ATTEMPT")
				mark(owner, "SHARED_ATTEMPT")
			} else {
				attempts[o.AttemptID] = id
			}
			if o.Cleanup.StateLeaked {
				mark(id, "LEAKED_STATE")
			}
			if !o.Cleanup.Verified {
				mark(id, "UNVERIFIED_CLEANUP")
			}
			if !i.CreatedByHost {
				mark(id, "UNATTESTED_INVENTORY")
			}
		}
	}
	for _, id := range ids {
		group := append([]Observation(nil), groups[id]...)
		sort.Slice(group, func(a, b int) bool {
			// Full JSON ordering makes conflicting retries permutation-invariant.
			x, _ := json.Marshal(group[a])
			y, _ := json.Marshal(group[b])
			return bytes.Compare(x, y) < 0
		})
		trial := TrialEvidence{TrialID: id, Attempts: group, Excluded: []string{}}
		for reason := range reasons[id] {
			trial.Excluded = append(trial.Excluded, reason)
		}
		sort.Strings(trial.Excluded)
		for _, o := range group {
			if o.Outcome == Infrastructure {
				out.InfrastructureTrials++
				break
			}
		}
		for _, o := range group {
			if o.Outcome == Unknown {
				out.UnknownTrials++
				break
			}
		}
		if len(trial.Excluded) != 0 {
			out.CorrelatedTrials++
			for _, reason := range trial.Excluded {
				if strings.HasPrefix(reason, "UNVERIFIED") || reason == "UNATTESTED_INVENTORY" {
					out.UnverifiedTrials++
					break
				}
			}
		} else {
			switch group[0].Outcome {
			case Pass:
				out.N++
			case Fail:
				out.N++
				out.K++
			}
		}
		out.Trials = append(out.Trials, trial)
	}
	if out.N > 0 && out.CorrelatedTrials == 0 && i.CreatedByHost {
		ci, err := ClopperPearson95(out.K, out.N)
		if err != nil {
			return Summary{}, err
		}
		out.Interval95 = &ci
	}
	return out, nil
}

func sameRequired(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for j := range a {
		if a[j] != b[j] {
			return false
		}
	}
	return true
}
func complete(s Summary) bool {
	return s.N == s.Declared && s.Missing == 0 && s.InfrastructureTrials == 0 && s.UnknownTrials == 0 && s.CorrelatedTrials == 0
}

// Classify is deterministic and side-effect-free. A mixed PASS/FAIL current
// sample in one comparable stratum establishes observed flakiness; otherwise
// no causal regression is asserted without an independently comparable baseline.
// Errors return a zero Result and preserve workerbridge.Reject exit codes.
func Classify(current Sample, baseline *Sample) (Result, error) {
	c, err := summarize(current)
	if err != nil {
		return Result{}, err
	}
	var b *Summary
	if baseline != nil {
		i, j := current.Inventory, baseline.Inventory
		if i.BaselineCommit == nil || *i.BaselineCommit != j.Commit || i.Repo != j.Repo ||
			i.SuiteProjectionSHA256 != j.SuiteProjectionSHA256 ||
			i.ExecutionContractSHA256 != j.ExecutionContractSHA256 ||
			!sameRequired(i.RequiredCases, j.RequiredCases) {
			return Result{}, refusal("BASELINE_PROJECTION_MISMATCH", "baseline commit or non-code execution projection")
		}
		v, e := summarize(*baseline)
		if e != nil {
			return Result{}, e
		}
		b = &v
	}
	res := Result{Current: c, Baseline: b}
	switch {
	case c.CorrelatedTrials != 0 || !current.Inventory.CreatedByHost:
		res.Classification = Inconclusive
	case c.N == 0:
		res.Classification = Inconclusive
	case c.K > 0 && c.K < c.N:
		res.Classification = FlakeObserved
	case !complete(c):
		res.Classification = Incomplete
	case c.K == 0:
		res.Classification = CompletePass
	case baseline == nil:
		res.Classification = BaselineAbsent
	case b.CorrelatedTrials != 0 || b.N == 0 || !baseline.Inventory.CreatedByHost:
		res.Classification = ObservedFailure
	case !complete(*b):
		res.Classification = ObservedFailure
	case b.K > 0:
		res.Classification = ExistingFailure
	case b.K == 0:
		res.Classification = RegressionObserved
	default:
		res.Classification = Inconclusive
	}
	return res, nil
}

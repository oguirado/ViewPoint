// Package p2trialinventory implements pure, host-attested PROTO2 Area F trial
// declarations, receipt reconciliation, and comparable execution projections.
// It grants no execution, filesystem, admission, or network authority.
package p2trialinventory

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"regexp"
	"slices"
	"sort"
	"strings"

	"opensuperficies.local/srf/src/workerbridge"
)

const (
	TrialInventorySchema  = "p2.trial_inventory/v1"
	FarmObservationSchema = "p2.farm_observation/v1"
	FarmBundleSchema      = "p2.farm_bundle/v1"
	CompletePass          = "COMPLETE_PASS"
	Incomplete            = "INCOMPLETE"
	RegressionObserved    = "REGRESSION_OBSERVED"
	ObservedFailure       = "OBSERVED_FAILURE"
	BaselineAbsent        = "BASELINE_ABSENT"
	ExistingFailure       = "EXISTING_FAILURE"
	FlakeObserved         = "FLAKE_OBSERVED"
	Inconclusive          = "INCONCLUSIVE"
)

var (
	nameRE   = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`)
	repoRE   = regexp.MustCompile(`^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$`)
	commitRE = regexp.MustCompile(`^[0-9a-f]{40}$`)
	pinRE    = regexp.MustCompile(`^[0-9a-f]{64}$`)
)

// TrialDeclaration allocates one independent repetition before any outcome.
// The order of these JSON fields is part of the public wire contract.
type TrialDeclaration struct {
	TrialID          string   `json:"trial_id"`
	ShardID          string   `json:"shard_id"`
	RepetitionID     string   `json:"repetition_id"`
	CapsuleClass     string   `json:"capsule_class"`
	SeedPolicySHA256 string   `json:"seed_policy_sha256"`
	ExpectedCaseIDs  []string `json:"expected_case_ids"`
}

// HostAttestation is an HMAC authenticated claim, not a guest bool.
// Its signing key must remain exclusively inside the trusted host boundary.
type HostAttestation struct {
	HostID    string `json:"host_id"`
	MACSHA256 string `json:"mac_sha256"`
}

// TrialInventory's field order is frozen for p2.trial_inventory/v1.
type TrialInventory struct {
	Schema                  string             `json:"schema"`
	FarmID                  string             `json:"farm_id"`
	Repo                    string             `json:"repo"`
	Commit                  string             `json:"commit"`
	BaselineCommit          *string            `json:"baseline_commit"`
	SuiteProjectionSHA256   string             `json:"suite_projection_sha256"`
	ExecutionContractSHA256 string             `json:"execution_contract_sha256"`
	DeclaredTrials          []TrialDeclaration `json:"declared_trials"`
	RequiredCases           []string           `json:"required_cases"`
	CreatedByHost           *HostAttestation   `json:"created_by_host"`
}

// ExecutionContract seals full inputs. IntendedCommit and FullManifestSHA256
// participate in the full hash but not the baseline comparability projection.
type ExecutionContract struct {
	Cases                  []string `json:"cases"`
	GateArgv               []string `json:"gate_argv"`
	Environment            []string `json:"environment"`
	ToolchainSHA256        string   `json:"toolchain_sha256"`
	ImageSHA256            string   `json:"image_sha256"`
	RuntimeSHA256          string   `json:"runtime_sha256"`
	CapabilityClass        string   `json:"capability_class"`
	SeedPolicySHA256       string   `json:"seed_policy_sha256"`
	IsolationResourceClass string   `json:"isolation_resource_class"`
	IntendedCommit         string   `json:"intended_commit"`
	FullManifestSHA256     string   `json:"full_manifest_sha256"`
}

// SuiteProjection binds the test population and independently named shards.
type SuiteProjection struct {
	RequiredCases []string `json:"required_cases"`
	Shards        []string `json:"shards"`
}

// TrialAttempt names a retry; no attempt can allocate a new TrialDeclaration.
type TrialAttempt struct {
	AttemptID   string `json:"attempt_id"`
	CapsuleID   string `json:"capsule_id"`
	Incarnation string `json:"incarnation"`
	LeaseID     string `json:"lease_id"`
	Nonce       string `json:"nonce"`
}

type CaseResult struct {
	CaseID   string `json:"case_id"`
	Status   string `json:"status"`
	FrameNum int64  `json:"frame_num"`
}

type CleanupEvidence struct {
	Disposed      bool `json:"disposed"`
	NoForbiddenIO bool `json:"no_forbidden_io"`
}

// FarmObservation uses the shared p2.farm_observation/v1 ordered envelope.
// HostValidationRef is the host HMAC of the otherwise identical envelope with
// host_validation_ref=""; grant_sha256 binds all five attempt fields.
type FarmObservation struct {
	Schema                  string          `json:"schema"`
	FarmID                  string          `json:"farm_id"`
	TrialID                 string          `json:"trial_id"`
	AttemptID               string          `json:"attempt_id"`
	CapsuleID               string          `json:"capsule_id"`
	Incarnation             string          `json:"incarnation"`
	Commit                  string          `json:"commit"`
	ReceiptSHA256           string          `json:"receipt_sha256"`
	GrantSHA256             string          `json:"grant_sha256"`
	ExecutionContractSHA256 string          `json:"execution_contract_sha256"`
	HostValidationRef       string          `json:"host_validation_ref"`
	GateExitCode            *int64          `json:"gate_exit_code"`
	CaseResults             []CaseResult    `json:"case_results"`
	Cleanup                 CleanupEvidence `json:"cleanup"`
	TelemetryRef            *string         `json:"telemetry_ref"`
	Outcome                 string          `json:"outcome"`
}

// Delivery preserves the raw, host-authenticated observation bytes.
type Delivery struct {
	TrialID     string          `json:"trial_id"`
	Attempt     TrialAttempt    `json:"attempt"`
	Observation json.RawMessage `json:"observation"`
}

// FarmBundle carries the entire declaration, execution inputs, and deliveries.
// Missing deliveries remain obligations in the returned FarmReport.
type FarmBundle struct {
	Schema            string            `json:"schema"`
	TrialInventory    json.RawMessage   `json:"trial_inventory"`
	ExecutionContract ExecutionContract `json:"execution_contract"`
	SuiteProjection   SuiteProjection   `json:"suite_projection"`
	Deliveries        []json.RawMessage `json:"deliveries"`
}

type AttemptSummary struct {
	AttemptID string
	Outcome   string
}

type TrialObligation struct {
	TrialID         string
	ExpectedCaseIDs []string
	State           string // MISSING, VALID, FAILURE, FLAKE, INFRASTRUCTURE, or CONFLICT
	Attempts        []AttemptSummary
}

type FarmReport struct {
	InventorySHA256  string
	Declared         int
	ValidIndependent int
	Missing          int
	Infrastructure   int
	Conflicted       int
	Attempts         int
	Verdict          string
	Hold             bool
	Obligations      []TrialObligation
}

// HostAuthority is a host-held cryptographic provenance boundary. Only callers
// holding its unexported key can sign inventories and observations. Construct
// and use it on the trusted host, never in a guest. No key is serialized.
type HostAuthority struct{ key []byte }

// SealedInventory is the immutable, independently committed denominator.
// Its fields are unexported so consumers cannot reduce the declared population.
type SealedInventory struct {
	wire      []byte
	inventory TrialInventory
	contract  ExecutionContract
	suite     SuiteProjection
	digest    string
}

func refuse(code int, class, detail string) error {
	return &workerbridge.Reject{Code: code, Class: class, Detail: detail}
}
func digest(data []byte) string { sum := sha256.Sum256(data); return hex.EncodeToString(sum[:]) }
func validName(s string) bool   { return nameRE.MatchString(s) }
func validPin(s string) bool    { return pinRE.MatchString(s) }

// NewHostAuthority refuses weak/absent host keys; a synthetic fixed key may be
// used by offline tests only. Production key ingress/custody belongs to the host.
func NewHostAuthority(hostKey []byte) (*HostAuthority, error) {
	if len(hostKey) < 32 {
		return nil, refuse(5, "HOST_AUTHORITY_REQUIRED", "host-held key must be at least 32 bytes")
	}
	return &HostAuthority{key: bytes.Clone(hostKey)}, nil
}
func (a *HostAuthority) mac(domain, host string, unsigned []byte) string {
	m := hmac.New(sha256.New, a.key)
	m.Write([]byte(domain))
	m.Write([]byte{0})
	m.Write([]byte(host))
	m.Write([]byte{0})
	m.Write(unsigned)
	return hex.EncodeToString(m.Sum(nil))
}
func (a *HostAuthority) active() bool { return a != nil && len(a.key) >= 32 }

// strictWire uses the merged real workerbridge JSON parser (duplicate-key,
// UTF-8 and integer checks) and the typed decoder (unknown nested fields).
// Byte equality enforces canonical declared field order, no whitespace or LF.
func strictWire(data []byte, target any) error {
	if _, err := workerbridge.Parse(data); err != nil {
		return err
	}
	d := json.NewDecoder(bytes.NewReader(data))
	d.DisallowUnknownFields()
	if err := d.Decode(target); err != nil {
		return refuse(2, "MALFORMED", err.Error())
	}
	var trailing any
	if err := d.Decode(&trailing); err != io.EOF {
		return refuse(2, "MALFORMED", "trailing JSON content")
	}
	canonical, err := json.Marshal(target)
	if err != nil {
		return refuse(2, "MALFORMED", err.Error())
	}
	if !bytes.Equal(canonical, data) {
		return refuse(2, "NONCANONICAL_WIRE", "field order, bytes, null or whitespace differs")
	}
	return nil
}
func uniqueNames(a []string) bool {
	if len(a) == 0 {
		return false
	}
	seen := map[string]bool{}
	for _, s := range a {
		if !validName(s) || seen[s] {
			return false
		}
		seen[s] = true
	}
	return true
}
func validateInventory(inv TrialInventory) error {
	if inv.Schema != TrialInventorySchema {
		return refuse(2, "SCHEMA", "invalid inventory schema")
	}
	if !validName(inv.FarmID) || !repoRE.MatchString(inv.Repo) || !commitRE.MatchString(inv.Commit) {
		return refuse(2, "MALFORMED", "farm, repo or commit")
	}
	if inv.BaselineCommit != nil && !commitRE.MatchString(*inv.BaselineCommit) {
		return refuse(2, "MALFORMED", "baseline_commit")
	}
	if !validPin(inv.SuiteProjectionSHA256) || !validPin(inv.ExecutionContractSHA256) {
		return refuse(2, "INVALID_PIN", "suite/execution pin must be lowercase hex64")
	}
	if !uniqueNames(inv.RequiredCases) || len(inv.DeclaredTrials) == 0 {
		return refuse(2, "MISSING_CASES", "missing or duplicate required cases/trials")
	}
	cases := map[string]bool{}
	for _, c := range inv.RequiredCases {
		cases[c] = true
	}
	seenTrials := map[string]bool{}
	seenRepetitions := map[string]bool{}
	covered := map[string]bool{}
	for _, tr := range inv.DeclaredTrials {
		if !validName(tr.TrialID) || !validName(tr.ShardID) || !validName(tr.RepetitionID) || !validName(tr.CapsuleClass) {
			return refuse(2, "MALFORMED", "trial identity or capsule class")
		}
		if !validPin(tr.SeedPolicySHA256) {
			return refuse(2, "INVALID_PIN", "trial seed policy")
		}
		pair := tr.ShardID + "\x00" + tr.RepetitionID
		if seenTrials[tr.TrialID] || seenRepetitions[pair] {
			return refuse(2, "DUPLICATE_TRIAL", "trial_id or independent shard/repetition pair")
		}
		seenTrials[tr.TrialID] = true
		seenRepetitions[pair] = true
		if !uniqueNames(tr.ExpectedCaseIDs) {
			return refuse(2, "MISSING_CASES", "empty/duplicate expected_case_ids")
		}
		for _, c := range tr.ExpectedCaseIDs {
			if !cases[c] {
				return refuse(2, "UNKNOWN_CASE", "case outside required_cases")
			}
			covered[c] = true
		}
	}
	for _, c := range inv.RequiredCases {
		if !covered[c] {
			return refuse(2, "MISSING_CASES", "required case without declared trial: "+c)
		}
	}
	return nil
}
func unsignedInventory(inv TrialInventory) ([]byte, error) {
	inv.CreatedByHost = nil
	return json.Marshal(inv)
}

// AttestInventory is a trusted-host operation; guests do not receive authority.
// It signs the complete outcome-independent declaration, including its pins.
func (a *HostAuthority) AttestInventory(inv TrialInventory, hostID string) ([]byte, error) {
	if !a.active() {
		return nil, refuse(5, "HOST_AUTHORITY_REQUIRED", "no trusted host boundary")
	}
	if !validName(hostID) {
		return nil, refuse(2, "MALFORMED", "host_id")
	}
	if err := validateInventory(inv); err != nil {
		return nil, err
	}
	unsigned, err := unsignedInventory(inv)
	if err != nil {
		return nil, err
	}
	inv.CreatedByHost = &HostAttestation{HostID: hostID, MACSHA256: a.mac(TrialInventorySchema, hostID, unsigned)}
	return json.Marshal(inv)
}

// ParseTrialInventory fails closed without an independent host-held HMAC key.
func ParseTrialInventory(data []byte, a *HostAuthority) (TrialInventory, error) {
	var inv TrialInventory
	if err := strictWire(data, &inv); err != nil {
		return TrialInventory{}, err
	}
	if err := validateInventory(inv); err != nil {
		return TrialInventory{}, err
	}
	if !a.active() || inv.CreatedByHost == nil || !validName(inv.CreatedByHost.HostID) || !validPin(inv.CreatedByHost.MACSHA256) {
		return TrialInventory{}, refuse(5, "BAD_HOST_ATTESTATION", "missing/untrusted host provenance")
	}
	unsigned, _ := unsignedInventory(inv)
	expected := a.mac(TrialInventorySchema, inv.CreatedByHost.HostID, unsigned)
	if !hmac.Equal([]byte(expected), []byte(inv.CreatedByHost.MACSHA256)) {
		return TrialInventory{}, refuse(5, "BAD_HOST_ATTESTATION", "host HMAC mismatch")
	}
	return inv, nil
}

func validateExecution(c ExecutionContract) error {
	if !uniqueNames(c.Cases) || len(c.GateArgv) == 0 || len(c.Environment) == 0 {
		return refuse(2, "MALFORMED", "execution cases/argv/environment")
	}
	for _, s := range append(slices.Clone(c.GateArgv), c.Environment...) {
		if s == "" || strings.ContainsRune(s, 0) {
			return refuse(2, "MALFORMED", "empty/invalid execution argument")
		}
	}
	for _, p := range []string{c.ToolchainSHA256, c.ImageSHA256, c.RuntimeSHA256, c.SeedPolicySHA256, c.FullManifestSHA256} {
		if !validPin(p) {
			return refuse(2, "INVALID_PIN", "execution pin")
		}
	}
	if !validName(c.CapabilityClass) || !validName(c.IsolationResourceClass) || !commitRE.MatchString(c.IntendedCommit) {
		return refuse(2, "MALFORMED", "execution capability/resource/commit")
	}
	return nil
}
func validateSuite(s SuiteProjection) error {
	if !uniqueNames(s.RequiredCases) || !uniqueNames(s.Shards) {
		return refuse(2, "MALFORMED", "suite cases/shards")
	}
	return nil
}

// FullExecutionPin includes intended commit and full manifest SHA-256.
func FullExecutionPin(c ExecutionContract) (string, error) {
	if err := validateExecution(c); err != nil {
		return "", err
	}
	b, err := json.Marshal(c)
	if err != nil {
		return "", err
	}
	return digest(b), nil
}

type comparableProjection struct {
	Cases                  []string `json:"cases"`
	GateArgv               []string `json:"gate_argv"`
	Environment            []string `json:"environment"`
	ToolchainSHA256        string   `json:"toolchain_sha256"`
	ImageSHA256            string   `json:"image_sha256"`
	RuntimeSHA256          string   `json:"runtime_sha256"`
	CapabilityClass        string   `json:"capability_class"`
	SeedPolicySHA256       string   `json:"seed_policy_sha256"`
	IsolationResourceClass string   `json:"isolation_resource_class"`
}

// CanonicalComparisonProjection is the independently inspectable ordered JSON
// comparison oracle. Only intended_commit and full_manifest_sha256 are dropped.
func CanonicalComparisonProjection(c ExecutionContract) ([]byte, error) {
	if err := validateExecution(c); err != nil {
		return nil, err
	}
	return json.Marshal(comparableProjection{c.Cases, c.GateArgv, c.Environment, c.ToolchainSHA256, c.ImageSHA256, c.RuntimeSHA256, c.CapabilityClass, c.SeedPolicySHA256, c.IsolationResourceClass})
}
func suitePin(s SuiteProjection) (string, error) {
	if err := validateSuite(s); err != nil {
		return "", err
	}
	b, err := json.Marshal(s)
	if err != nil {
		return "", err
	}
	return digest(b), nil
}

// ComparableExecution permits distinct intended commits/full manifests, but
// refuses every other comparison-field difference including suite population.
func ComparableExecution(a, b ExecutionContract, sa, sb SuiteProjection) (bool, error) {
	left, err := CanonicalComparisonProjection(a)
	if err != nil {
		return false, err
	}
	right, err := CanonicalComparisonProjection(b)
	if err != nil {
		return false, err
	}
	la, err := suitePin(sa)
	if err != nil {
		return false, err
	}
	lb, err := suitePin(sb)
	if err != nil {
		return false, err
	}
	return bytes.Equal(left, right) && la == lb, nil
}

// SealTrialInventory fixes the denominator with host authentication and pins
// actual execution/suite bytes before any attempt or observed outcome exists.
func SealTrialInventory(raw []byte, a *HostAuthority, c ExecutionContract, s SuiteProjection) (SealedInventory, error) {
	inv, err := ParseTrialInventory(raw, a)
	if err != nil {
		return SealedInventory{}, err
	}
	full, err := FullExecutionPin(c)
	if err != nil {
		return SealedInventory{}, err
	}
	suiteHash, err := suitePin(s)
	if err != nil {
		return SealedInventory{}, err
	}
	if full != inv.ExecutionContractSHA256 || suiteHash != inv.SuiteProjectionSHA256 {
		return SealedInventory{}, refuse(5, "PIN_MISMATCH", "declared pins differ from trusted contract/suite bytes")
	}
	if inv.Commit != c.IntendedCommit || !slices.Equal(inv.RequiredCases, c.Cases) || !slices.Equal(inv.RequiredCases, s.RequiredCases) {
		return SealedInventory{}, refuse(5, "EXECUTION_PROJECTION_MISMATCH", "commit or required cases changed")
	}
	shards := map[string]bool{}
	for _, tr := range inv.DeclaredTrials {
		shards[tr.ShardID] = true
		if tr.SeedPolicySHA256 != c.SeedPolicySHA256 || tr.CapsuleClass != c.CapabilityClass {
			return SealedInventory{}, refuse(5, "EXECUTION_PROJECTION_MISMATCH", "seed or capsule capability changed")
		}
	}
	expectedShards := make([]string, 0, len(shards))
	for sh := range shards {
		expectedShards = append(expectedShards, sh)
	}
	sort.Strings(expectedShards)
	actualShards := slices.Clone(s.Shards)
	sort.Strings(actualShards)
	if !slices.Equal(expectedShards, actualShards) {
		return SealedInventory{}, refuse(5, "EXECUTION_PROJECTION_MISMATCH", "suite shards changed")
	}
	return SealedInventory{wire: bytes.Clone(raw), inventory: inv, contract: c, suite: s, digest: digest(raw)}, nil
}
func (s SealedInventory) InventorySHA256() string { return s.digest }

func validateObservation(o FarmObservation) error {
	if o.Schema != FarmObservationSchema {
		return refuse(2, "SCHEMA", "farm observation schema")
	}
	for _, v := range []string{o.FarmID, o.TrialID, o.AttemptID, o.CapsuleID, o.Incarnation} {
		if !validName(v) {
			return refuse(2, "MALFORMED", "observation identity")
		}
	}
	if !commitRE.MatchString(o.Commit) {
		return refuse(2, "MALFORMED", "observation commit")
	}
	for _, p := range []string{o.ReceiptSHA256, o.GrantSHA256, o.ExecutionContractSHA256} {
		if !validPin(p) {
			return refuse(2, "INVALID_PIN", "observation pin")
		}
	}
	if !o.Cleanup.Disposed || !o.Cleanup.NoForbiddenIO {
		return refuse(5, "CLEANUP_UNVERIFIED", "cleanup/forbidden-I/O attestation missing")
	}
	if o.TelemetryRef != nil && *o.TelemetryRef == "" {
		return refuse(2, "MALFORMED", "empty telemetry ref")
	}
	seen := map[string]bool{}
	var previous int64
	for _, c := range o.CaseResults {
		if !validName(c.CaseID) || seen[c.CaseID] || (c.Status != "PASS" && c.Status != "FAIL") || c.FrameNum <= previous {
			return refuse(2, "MALFORMED", "duplicate/invalid case or nonincreasing frame_num")
		}
		seen[c.CaseID] = true
		previous = c.FrameNum
	}
	switch o.Outcome {
	case "PASS":
		if o.GateExitCode == nil || *o.GateExitCode != 0 || len(o.CaseResults) == 0 {
			return refuse(2, "OUTCOME_INCONSISTENT", "PASS requires exit zero and cases")
		}
		for _, c := range o.CaseResults {
			if c.Status != "PASS" {
				return refuse(2, "OUTCOME_INCONSISTENT", "PASS contains failed case")
			}
		}
	case "FAIL":
		if o.GateExitCode == nil || len(o.CaseResults) == 0 {
			return refuse(2, "OUTCOME_INCONSISTENT", "FAIL requires executed cases")
		}
		bad := *o.GateExitCode != 0
		for _, c := range o.CaseResults {
			bad = bad || c.Status == "FAIL"
		}
		if !bad {
			return refuse(2, "OUTCOME_INCONSISTENT", "FAIL has no failure")
		}
	case "INFRA":
		if len(o.CaseResults) != 0 {
			return refuse(2, "OUTCOME_INCONSISTENT", "infra evidence cannot claim case results")
		}
	default:
		return refuse(2, "UNKNOWN_OUTCOME", "unknown/guest outcome cannot be admitted")
	}
	return nil
}
func unsignedObservation(o FarmObservation) ([]byte, error) {
	o.HostValidationRef = ""
	return json.Marshal(o)
}

// AttestObservation authenticates the complete validated observation, without
// trusting a guest-side created_by_host boolean or unsigned outcome.
func (a *HostAuthority) AttestObservation(o FarmObservation) ([]byte, error) {
	if !a.active() {
		return nil, refuse(5, "HOST_AUTHORITY_REQUIRED", "no trusted host boundary")
	}
	if err := validateObservation(o); err != nil {
		return nil, err
	}
	unsigned, err := unsignedObservation(o)
	if err != nil {
		return nil, err
	}
	o.HostValidationRef = a.mac(FarmObservationSchema, o.FarmID, unsigned)
	return json.Marshal(o)
}

// ParseFarmObservation is the only admission parser for result bytes.
func ParseFarmObservation(raw []byte, a *HostAuthority) (FarmObservation, error) {
	var o FarmObservation
	if err := strictWire(raw, &o); err != nil {
		return FarmObservation{}, err
	}
	if err := validateObservation(o); err != nil {
		return FarmObservation{}, err
	}
	if !a.active() || !validPin(o.HostValidationRef) {
		return FarmObservation{}, refuse(5, "BAD_HOST_ATTESTATION", "no host observation authentication")
	}
	unsigned, _ := unsignedObservation(o)
	if !hmac.Equal([]byte(o.HostValidationRef), []byte(a.mac(FarmObservationSchema, o.FarmID, unsigned))) {
		return FarmObservation{}, refuse(5, "BAD_HOST_ATTESTATION", "observation HMAC mismatch")
	}
	return o, nil
}
func validateAttempt(a TrialAttempt) error {
	for _, s := range []string{a.AttemptID, a.CapsuleID, a.Incarnation, a.LeaseID, a.Nonce} {
		if !validName(s) {
			return refuse(2, "MALFORMED", "attempt identity or lease/nonce")
		}
	}
	return nil
}

// GrantSHA256 links all attempt fields to their independently allocated trial.
func GrantSHA256(trialID string, a TrialAttempt) (string, error) {
	if !validName(trialID) {
		return "", refuse(2, "MALFORMED", "trial_id")
	}
	if err := validateAttempt(a); err != nil {
		return "", err
	}
	v := struct {
		TrialID      string       `json:"trial_id"`
		TrialAttempt TrialAttempt `json:"attempt"`
	}{trialID, a}
	b, err := json.Marshal(v)
	if err != nil {
		return "", err
	}
	return digest(b), nil
}

// ParseFarmBundle uses the real nested decoder, retaining all exact delivery
// bytes for independent duplicate/conflict detection.
func ParseFarmBundle(raw []byte) (FarmBundle, error) {
	var b FarmBundle
	if err := strictWire(raw, &b); err != nil {
		return FarmBundle{}, err
	}
	if b.Schema != FarmBundleSchema || len(b.TrialInventory) == 0 || b.Deliveries == nil {
		return FarmBundle{}, refuse(2, "MALFORMED", "bundle schema, inventory or deliveries missing")
	}
	return b, nil
}

func initialReport(s SealedInventory) FarmReport {
	r := FarmReport{InventorySHA256: s.digest, Declared: len(s.inventory.DeclaredTrials), Verdict: Incomplete}
	for _, t := range s.inventory.DeclaredTrials {
		r.Obligations = append(r.Obligations, TrialObligation{TrialID: t.TrialID, ExpectedCaseIDs: slices.Clone(t.ExpectedCaseIDs), State: "MISSING"})
	}
	return r
}
func finalize(r *FarmReport) {
	r.ValidIndependent = 0
	r.Missing = 0
	r.Infrastructure = 0
	r.Conflicted = 0
	r.Attempts = 0
	fail := false
	flake := false
	for _, o := range r.Obligations {
		r.Attempts += len(o.Attempts)
		switch o.State {
		case "VALID":
			r.ValidIndependent++
		case "FAILURE":
			r.ValidIndependent++
			fail = true
		case "FLAKE":
			r.ValidIndependent++
			flake = true
		case "MISSING":
			r.Missing++
		case "INFRASTRUCTURE":
			r.Infrastructure++
		case "CONFLICT":
			r.Conflicted++
		}
	}
	if r.Hold || r.Conflicted > 0 {
		r.Verdict = Inconclusive
	} else if r.Missing > 0 || r.Infrastructure > 0 {
		r.Verdict = Incomplete
	} else if flake {
		r.Verdict = FlakeObserved
	} else if fail {
		r.Verdict = ObservedFailure
	} else if r.ValidIndependent == r.Declared && r.Declared > 0 {
		r.Verdict = CompletePass
	} else {
		r.Verdict = Inconclusive
	}
}
func hold(r FarmReport, idx int, class, detail string) (FarmReport, error) {
	if idx >= 0 && idx < len(r.Obligations) {
		r.Obligations[idx].State = "CONFLICT"
	}
	r.Hold = true
	finalize(&r)
	return r, refuse(5, class, detail)
}
func checkCaseSet(expected []string, got []CaseResult) bool {
	if len(expected) != len(got) {
		return false
	}
	m := map[string]bool{}
	for _, e := range expected {
		m[e] = true
	}
	for _, v := range got {
		if !m[v.CaseID] {
			return false
		}
		delete(m, v.CaseID)
	}
	return len(m) == 0
}

// ReconcileFarmBundle consumes complete real wire parsers/reducers. Its sealed
// denominator never shrinks on missing, conflicting, foreign or invalid bytes;
// errors return a HOLD report with every originally declared obligation.
func ReconcileFarmBundle(seal SealedInventory, raw []byte, a *HostAuthority) (FarmReport, error) {
	if len(seal.wire) == 0 || seal.digest == "" {
		return FarmReport{Verdict: Inconclusive, Hold: true}, refuse(5, "UNSEALED_INVENTORY", "host seal required")
	}
	r := initialReport(seal)
	bundle, err := ParseFarmBundle(raw)
	if err != nil {
		return hold(r, -1, "BAD_BUNDLE", err.Error())
	}
	if !bytes.Equal(bundle.TrialInventory, seal.wire) {
		return hold(r, -1, "INVENTORY_CHANGED", "declared obligations changed after sealing")
	}
	if _, err := ParseTrialInventory(bundle.TrialInventory, a); err != nil {
		return hold(r, -1, "BAD_HOST_ATTESTATION", err.Error())
	}
	full, err := FullExecutionPin(bundle.ExecutionContract)
	if err != nil {
		return hold(r, -1, "EXECUTION_PROJECTION_MISMATCH", err.Error())
	}
	sp, err := suitePin(bundle.SuiteProjection)
	if err != nil {
		return hold(r, -1, "EXECUTION_PROJECTION_MISMATCH", err.Error())
	}
	if full != seal.inventory.ExecutionContractSHA256 || sp != seal.inventory.SuiteProjectionSHA256 {
		return hold(r, -1, "EXECUTION_PROJECTION_MISMATCH", "full execution or suite pin changed")
	}
	ok, err := ComparableExecution(seal.contract, bundle.ExecutionContract, seal.suite, bundle.SuiteProjection)
	if err != nil || !ok {
		return hold(r, -1, "EXECUTION_PROJECTION_MISMATCH", "execution comparison fields changed")
	}
	idx := map[string]int{}
	for i, t := range seal.inventory.DeclaredTrials {
		idx[t.TrialID] = i
	}
	attempts := map[string]map[string][]byte{}
	attemptOwners := map[string]string{}
	for _, wire := range bundle.Deliveries {
		var d Delivery
		if err := strictWire(wire, &d); err != nil {
			return hold(r, -1, "BAD_DELIVERY", err.Error())
		}
		i, exists := idx[d.TrialID]
		if !exists {
			return hold(r, -1, "UNDECLARED_TRIAL", "delivery cannot create a new repetition")
		}
		if err := validateAttempt(d.Attempt); err != nil {
			return hold(r, i, "BAD_ATTEMPT", err.Error())
		}
		if owner, exists := attemptOwners[d.Attempt.AttemptID]; exists && owner != d.TrialID {
			return hold(r, i, "RETRY_AS_INDEPENDENT", "attempt reused under another independent trial")
		}
		attemptOwners[d.Attempt.AttemptID] = d.TrialID
		if attempts[d.TrialID] == nil {
			attempts[d.TrialID] = map[string][]byte{}
		}
		if prior, exists := attempts[d.TrialID][d.Attempt.AttemptID]; exists {
			if bytes.Equal(prior, wire) {
				continue
			}
			return hold(r, i, "CONFLICTING_DELIVERY", "same attempt identity has different bytes")
		}
		attempts[d.TrialID][d.Attempt.AttemptID] = bytes.Clone(wire)
		o, err := ParseFarmObservation(d.Observation, a)
		if err != nil {
			return hold(r, i, "BAD_OBSERVATION", err.Error())
		}
		if o.FarmID != seal.inventory.FarmID || o.Commit != seal.inventory.Commit || o.TrialID != d.TrialID {
			return hold(r, i, "FOREIGN_OBSERVATION", "farm, commit or trial_id does not match seal")
		}
		if o.AttemptID != d.Attempt.AttemptID || o.CapsuleID != d.Attempt.CapsuleID || o.Incarnation != d.Attempt.Incarnation {
			return hold(r, i, "BAD_ATTEMPT", "attempt fields do not match observation")
		}
		grant, err := GrantSHA256(d.TrialID, d.Attempt)
		if err != nil {
			return hold(r, i, "BAD_ATTEMPT", err.Error())
		}
		if o.GrantSHA256 != grant || o.ExecutionContractSHA256 != seal.inventory.ExecutionContractSHA256 {
			return hold(r, i, "PIN_MISMATCH", "grant or execution pin changed")
		}
		if o.Outcome != "INFRA" && !checkCaseSet(r.Obligations[i].ExpectedCaseIDs, o.CaseResults) {
			return hold(r, i, "CASE_MEMBERSHIP_MISMATCH", "missing/unknown case in trial attempt")
		}
		r.Obligations[i].Attempts = append(r.Obligations[i].Attempts, AttemptSummary{AttemptID: d.Attempt.AttemptID, Outcome: o.Outcome})
	}
	for i := range r.Obligations {
		obs := r.Obligations[i].Attempts
		pass, fail, infra := false, false, false
		for _, a := range obs {
			switch a.Outcome {
			case "PASS":
				pass = true
			case "FAIL":
				fail = true
			case "INFRA":
				infra = true
			}
		}
		switch {
		case pass && fail:
			r.Obligations[i].State = "FLAKE"
		case infra && !(pass || fail):
			r.Obligations[i].State = "INFRASTRUCTURE"
		case pass:
			r.Obligations[i].State = "VALID"
		case fail:
			r.Obligations[i].State = "FAILURE"
		}
	}
	finalize(&r)
	return r, nil
}

// CompareFarmReports refuses changed comparison projection, absent baselines,
// and incomplete evidence before making any success or regression assertion.
func CompareFarmReports(current FarmReport, baseline *FarmReport, currentExecution, baselineExecution ExecutionContract, currentSuite, baselineSuite SuiteProjection) (string, error) {
	if baseline == nil {
		return BaselineAbsent, nil
	}
	ok, err := ComparableExecution(currentExecution, baselineExecution, currentSuite, baselineSuite)
	if err != nil {
		return Inconclusive, err
	}
	if !ok {
		return Inconclusive, refuse(5, "INCOMPARABLE_BASELINE", "seed/image/resource or other comparison field changed")
	}
	if current.Hold || baseline.Hold {
		return Inconclusive, nil
	}
	if current.Verdict == Incomplete {
		return Incomplete, nil
	}
	if baseline.Verdict == Incomplete || baseline.Verdict == Inconclusive {
		return Inconclusive, nil
	}
	switch current.Verdict {
	case CompletePass:
		return CompletePass, nil
	case FlakeObserved:
		return FlakeObserved, nil
	case ObservedFailure:
		if baseline.Verdict == CompletePass {
			return RegressionObserved, nil
		}
		if baseline.Verdict == ObservedFailure || baseline.Verdict == ExistingFailure {
			return ExistingFailure, nil
		}
	}
	return Inconclusive, nil
}

// AssertComplete is the Area F gate: it refuses missing, infra, conflicts,
// failed, flaking or foreign obligations instead of shrinking denominators.
func AssertComplete(r FarmReport) error {
	if r.Hold || r.Declared == 0 || r.ValidIndependent != r.Declared || r.Missing != 0 || r.Infrastructure != 0 || r.Conflicted != 0 || r.Verdict != CompletePass {
		return refuse(5, "INCOMPLETE_TRIAL_POPULATION", fmt.Sprintf("verdict=%s declared=%d valid=%d missing=%d infra=%d conflict=%d", r.Verdict, r.Declared, r.ValidIndependent, r.Missing, r.Infrastructure, r.Conflicted))
	}
	return nil
}

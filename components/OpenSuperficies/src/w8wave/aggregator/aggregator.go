// Package aggregator implements the W8-P2-07 local deduplication store,
// canonical w8-repro/1 identity, contiguous sequence acknowledgement pipeline,
// wave summary accounting, and local-only bridge receipt projection fixtures.
package aggregator

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math"
	"regexp"
	"slices"
	"sort"
	"strconv"
	"strings"
	"sync"
	"unicode/utf8"

	"opensuperficies.local/srf/src/workerbridge"
)

const (
	// ReproVersion is the mandatory canonical reproduction identity domain and version.
	ReproVersion = "w8-repro/1"
	// OSSurface is the canonical OS capsule surface identifier.
	OSSurface = workerbridge.OSSurface
	// ReasonAusente marks an unavailable measurement or cost without inventing zero.
	ReasonAusente = "AUSENTE"
)

var (
	idPattern      = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`)
	commitPattern  = regexp.MustCompile(`^[0-9a-f]{40}$`)
	sha256Pattern  = regexp.MustCompile(`^[0-9a-f]{64}$`)
	repoPattern    = regexp.MustCompile(`^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$`)
	addressPattern = regexp.MustCompile(`^0x[0-9a-f]+$`)
)

// HashFunc computes a hex digest for canonical identity bytes.
// Injecting a custom HashFunc allows deterministic collision testing.
type HashFunc func([]byte) string

// DefaultHash computes the lowercase 64-character hex SHA-256 of data.
func DefaultHash(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

func refusal(code int, class, detail string) error {
	if code < 2 || code > 7 {
		code = 5
	}
	return &workerbridge.Reject{Code: code, Class: class, Detail: detail}
}

// ExitCode maps nil to 0 and errors to normalized W8 exit codes 2..7, never 1.
func ExitCode(err error) int {
	return workerbridge.ExitCode(err)
}

// Class returns the rejection class for an error, or empty string on nil.
func Class(err error) string {
	return workerbridge.Class(err)
}

// AffectedTarget pairs a repository-relative path with an affected symbol.
type AffectedTarget struct {
	Path   string `json:"path"`
	Symbol string `json:"symbol"`
}

// ReproFrame is an ordered stack frame; Address is typed input-only volatility.
type ReproFrame struct {
	Path    string `json:"path"`
	Symbol  string `json:"symbol"`
	Line    int64  `json:"line"`
	Address string `json:"address,omitempty"`
}

// ReproRecord implements the closed C3 record. Volatile input fields never enter identity.
type ReproRecord struct {
	Normalizer          string       `json:"normalizer"`
	Repo                string       `json:"repo"`
	Commit              string       `json:"commit"`
	BaseImageDigest     string       `json:"base_image_digest"`
	BaseRecipeSHA256    string       `json:"base_recipe_sha256"`
	CacheManifestSHA256 string       `json:"cache_manifest_sha256"`
	GuestKernelSHA256   *string      `json:"guest_kernel_sha256"`
	GuestRootfsSHA256   *string      `json:"guest_rootfs_sha256"`
	ExtractPolicySHA256 string       `json:"extract_policy_sha256"`
	GateArgv            []string     `json:"gate_argv"`
	GateCwd             string       `json:"gate_cwd"`
	FailureKind         string       `json:"failure_kind"`
	NormalizedExit      *int64       `json:"normalized_exit"`
	Affected            []string     `json:"affected"`
	Assertion           string       `json:"assertion"`
	Frames              []ReproFrame `json:"frames"`
	ReproSteps          []string     `json:"repro_steps"`
	WorkspacePrefix     string       `json:"workspace_prefix,omitempty"`
	PID                 int64        `json:"pid,omitempty"`
	TimeMS              int64        `json:"time_ms,omitempty"`
}

func cloneReproRecord(r ReproRecord) ReproRecord {
	r.GateArgv = slices.Clone(r.GateArgv)
	r.Affected = slices.Clone(r.Affected)
	r.Frames = slices.Clone(r.Frames)
	r.ReproSteps = slices.Clone(r.ReproSteps)
	if r.NormalizedExit != nil {
		v := *r.NormalizedExit
		r.NormalizedExit = &v
	}
	if r.GuestKernelSHA256 != nil {
		v := *r.GuestKernelSHA256
		r.GuestKernelSHA256 = &v
	}
	if r.GuestRootfsSHA256 != nil {
		v := *r.GuestRootfsSHA256
		r.GuestRootfsSHA256 = &v
	}
	return r
}

// Provenance holds volatile per-attempt and per-message metadata kept outside identity.
type Provenance struct {
	TaskID       string `json:"task_id"`
	AttemptID    string `json:"attempt_id"`
	LeaseID      string `json:"lease_id"`
	WorkerID     string `json:"worker_id,omitempty"`
	Sender       string `json:"sender"`
	StartedAtMS  *int64 `json:"started_at_ms,omitempty"`
	FinishedAtMS *int64 `json:"finished_at_ms,omitempty"`
	CostMicroUSD *int64 `json:"cost_micro_usd,omitempty"`
	Pulse        int64  `json:"pulse"`
}

// ReproIdentity contains the computed w8-repro/1 digest and exact canonical record bytes.
type ReproIdentity struct {
	ReproSHA256     string      `json:"repro_sha256"`
	CanonicalRecord []byte      `json:"canonical_record"`
	FramedBytes     []byte      `json:"framed_bytes"`
	Record          ReproRecord `json:"record"`
}

var requiredReproKeys = []string{
	"normalizer", "repo", "commit", "base_image_digest", "base_recipe_sha256",
	"cache_manifest_sha256", "guest_kernel_sha256", "guest_rootfs_sha256", "extract_policy_sha256",
	"gate_argv", "gate_cwd", "failure_kind", "normalized_exit", "affected", "assertion", "frames", "repro_steps",
}

var forbiddenProvenanceKeys = map[string]bool{
	"task_id":        true,
	"attempt_id":     true,
	"lease_id":       true,
	"worker_id":      true,
	"sender":         true,
	"timestamp":      true,
	"timestamps":     true,
	"started_at_ms":  true,
	"finished_at_ms": true,
	"cost":           true,
	"cost_micro_usd": true,
	"total_cost_usd": true,
	"pulse":          true,
}

func cleanRepoPath(p string) bool {
	if p == "" || strings.HasPrefix(p, "/") || strings.ContainsAny(p, "\\\x00") || !utf8.ValidString(p) {
		return false
	}
	for _, char := range p {
		if char < 32 {
			return false
		}
	}
	for _, seg := range strings.Split(p, "/") {
		if seg == "" || seg == "." || seg == ".." || seg == ".git" {
			return false
		}
	}
	return true
}

func validCwd(cwd string) bool {
	if cwd == "." {
		return true
	}
	return cleanRepoPath(cwd)
}

// IsComplete distinguishes absent identity from an observation ready to hash.
func (r ReproRecord) IsComplete() bool {
	return r.Normalizer != "" && r.Repo != "" && r.Commit != "" &&
		r.BaseImageDigest != "" && r.BaseRecipeSHA256 != "" && r.CacheManifestSHA256 != "" &&
		r.ExtractPolicySHA256 != "" && len(r.GateArgv) > 0 && r.GateCwd != "" &&
		r.FailureKind != "" && r.NormalizedExit != nil && r.Affected != nil &&
		r.Assertion != "" && r.Frames != nil && len(r.ReproSteps) > 0
}

// Validate checks C3 input types and paths without folding assertion or argv literals.
func (r ReproRecord) Validate() error {
	bad := func(detail string) error { return refusal(2, "MALFORMED", detail) }
	if !r.IsComplete() {
		return refusal(2, "MISSING_IDENTITY_FIELD", "incomplete C3 identity")
	}
	if r.Normalizer != ReproVersion {
		return refusal(2, "INVALID_VERSION", "normalizer must be w8-repro/1")
	}
	if !repoPattern.MatchString(r.Repo) || !commitPattern.MatchString(r.Commit) {
		return bad("repo or commit")
	}
	for _, digest := range []string{r.BaseImageDigest, r.BaseRecipeSHA256, r.CacheManifestSHA256, r.ExtractPolicySHA256} {
		if !sha256Pattern.MatchString(digest) {
			return bad("digest")
		}
	}
	if (r.GuestKernelSHA256 == nil) != (r.GuestRootfsSHA256 == nil) {
		return bad("guest pins must be paired")
	}
	for _, digest := range []*string{r.GuestKernelSHA256, r.GuestRootfsSHA256} {
		if digest != nil && !sha256Pattern.MatchString(*digest) {
			return bad("guest digest")
		}
	}
	if *r.NormalizedExit != 0 && (*r.NormalizedExit < 2 || *r.NormalizedExit > 7) {
		return bad("normalized_exit")
	}
	if !validCwd(r.GateCwd) {
		return bad("gate_cwd")
	}
	if r.WorkspacePrefix != "" && (!strings.HasPrefix(r.WorkspacePrefix, "/") || !strings.HasSuffix(r.WorkspacePrefix, "/") || !utf8.ValidString(r.WorkspacePrefix)) {
		return bad("workspace_prefix")
	}
	if r.PID < 0 || r.TimeMS < 0 || r.PID > 9007199254740991 || r.TimeMS > 9007199254740991 {
		return bad("volatile integer")
	}
	if len(r.Affected) > 64 || len(r.Frames) > 64 || len(r.GateArgv) > 64 || len(r.ReproSteps) > 64 {
		return bad("list bound")
	}
	for _, path := range r.Affected {
		if !cleanRepoPath(path) {
			return bad("affected path")
		}
	}
	for _, list := range [][]string{r.GateArgv, r.ReproSteps, {r.Assertion, r.FailureKind}} {
		for _, text := range list {
			if text == "" || !utf8.ValidString(text) {
				return bad("text")
			}
		}
	}
	for _, frame := range r.Frames {
		path := frame.Path
		if r.WorkspacePrefix != "" {
			path = strings.TrimPrefix(path, r.WorkspacePrefix)
		}
		if !cleanRepoPath(path) || frame.Symbol == "" || !utf8.ValidString(frame.Symbol) || frame.Line < 1 || frame.Line > 9007199254740991 {
			return bad("frame")
		}
		if frame.Address != "" && !addressPattern.MatchString(frame.Address) {
			return bad("frame address")
		}
	}
	return nil
}

func (r ReproRecord) toCanonicalDocument() workerbridge.Document {
	stringsDoc := func(values []string) []any {
		if values == nil {
			return nil
		}
		out := make([]any, len(values))
		for i, v := range values {
			out[i] = v
		}
		return out
	}
	nullableDigest := func(value *string) any {
		if value == nil {
			return nil
		}
		return *value
	}
	var exit any
	if r.NormalizedExit != nil {
		exit = json.Number(strconv.FormatInt(*r.NormalizedExit, 10))
	}
	doc := workerbridge.Document{
		"normalizer":            r.Normalizer,
		"repo":                  r.Repo,
		"commit":                r.Commit,
		"base_image_digest":     r.BaseImageDigest,
		"base_recipe_sha256":    r.BaseRecipeSHA256,
		"cache_manifest_sha256": r.CacheManifestSHA256,
		"extract_policy_sha256": r.ExtractPolicySHA256,
		"guest_kernel_sha256":   nullableDigest(r.GuestKernelSHA256),
		"guest_rootfs_sha256":   nullableDigest(r.GuestRootfsSHA256),
		"gate_argv":             stringsDoc(r.GateArgv),
		"gate_cwd":              r.GateCwd,
		"failure_kind":          r.FailureKind,
		"normalized_exit":       exit,
		"affected":              stringsDoc(r.Affected),
		"assertion":             r.Assertion,
		"repro_steps":           stringsDoc(r.ReproSteps),
	}
	frames := make([]any, len(r.Frames))
	for i, f := range r.Frames {
		path := f.Path
		if r.WorkspacePrefix != "" {
			path = strings.TrimPrefix(path, r.WorkspacePrefix)
		}
		frames[i] = map[string]any{"path": path, "symbol": f.Symbol, "line": json.Number(strconv.FormatInt(f.Line, 10))}
	}
	doc["frames"] = frames
	return doc
}

// FrameReproBytes retains the compatibility accessor; C3 hashes JSON with no prefix.
func FrameReproBytes(canonicalJSON []byte) []byte { return bytes.Clone(canonicalJSON) }

// ComputeIdentity hashes the C3 canonical JSON directly with no length or version prefix.
func ComputeIdentity(r ReproRecord, hashFn HashFunc) (ReproIdentity, error) {
	if err := r.Validate(); err != nil {
		return ReproIdentity{}, err
	}
	if hashFn == nil {
		hashFn = DefaultHash
	}
	doc := r.toCanonicalDocument()
	canon, err := workerbridge.Canonical(doc)
	if err != nil {
		return ReproIdentity{}, refusal(2, workerbridge.Class(err), err.Error())
	}
	framed := FrameReproBytes(canon)
	digest := hashFn(framed)
	if !sha256Pattern.MatchString(digest) {
		return ReproIdentity{}, refusal(5, "INVALID_HASH", "injected hash function returned non-sha256 digest")
	}
	return ReproIdentity{
		ReproSHA256:     digest,
		CanonicalRecord: canon,
		FramedBytes:     framed,
		Record:          cloneReproRecord(r),
	}, nil
}

// ParseReproRecord refuses ambiguous JSON and unknown keys. Missing fields remain unkeyed.
func ParseReproRecord(data []byte) (rec ReproRecord, complete bool, err error) {
	doc, err := workerbridge.Parse(data)
	if err != nil {
		return rec, false, refusal(2, workerbridge.Class(err), err.Error())
	}
	allowed := map[string]bool{"workspace_prefix": true, "pid": true, "time_ms": true}
	for _, key := range requiredReproKeys {
		allowed[key] = true
	}
	for key := range doc {
		if forbiddenProvenanceKeys[key] {
			return rec, false, refusal(5, "PROVENANCE_IN_IDENTITY", key)
		}
		if !allowed[key] {
			return rec, false, refusal(2, "UNKNOWN_KEY", key)
		}
	}
	for _, key := range []string{"workspace_prefix", "pid", "time_ms"} {
		if value, exists := doc[key]; exists && value == nil {
			return rec, false, refusal(2, "MALFORMED", key+" cannot be null")
		}
	}
	if frames, ok := doc["frames"].([]any); ok {
		for _, value := range frames {
			frame, ok := value.(map[string]any)
			if !ok {
				return rec, false, refusal(2, "MALFORMED", "frame must be an object")
			}
			for key, value := range frame {
				if key != "path" && key != "symbol" && key != "line" && key != "address" {
					return rec, false, refusal(2, "UNKNOWN_KEY", key)
				}
				if value == nil {
					return rec, false, refusal(2, "MALFORMED", "null frame field")
				}
			}
		}
	}
	// Decode typed fields even on incomplete input so malformed values never pass as absence.
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&rec); err != nil {
		return rec, false, refusal(2, "MALFORMED", err.Error())
	}
	for _, key := range requiredReproKeys {
		value, present := doc[key]
		if !present || (value == nil && key != "guest_kernel_sha256" && key != "guest_rootfs_sha256") {
			return rec, false, nil
		}
	}
	if !rec.IsComplete() {
		return rec, false, nil
	}
	if err := rec.Validate(); err != nil {
		return rec, false, err
	}
	return rec, true, nil
}

// ParseAndComputeIdentity parses strict JSON bytes and computes the w8-repro/1 identity.
// Missing required identity fields fail closed here rather than emitting a null or shared hash.
func ParseAndComputeIdentity(data []byte, hashFn HashFunc) (ReproIdentity, error) {
	rec, complete, err := ParseReproRecord(data)
	if err != nil {
		return ReproIdentity{}, err
	}
	if !complete {
		return ReproIdentity{}, refusal(2, "MISSING_IDENTITY_FIELD", "missing required identity fields cannot produce a hash")
	}
	return ComputeIdentity(rec, hashFn)
}

// EqualRepro reports whether two identities are both keyed and match on both
// repro_sha256 and exact canonical record bytes. Null/unkeyed identities are never equal.
func EqualRepro(a, b *ReproIdentity) bool {
	if a == nil || b == nil {
		return false
	}
	if a.ReproSHA256 == "" || b.ReproSHA256 == "" || len(a.CanonicalRecord) == 0 || len(b.CanonicalRecord) == 0 {
		return false
	}
	return a.ReproSHA256 == b.ReproSHA256 && bytes.Equal(a.CanonicalRecord, b.CanonicalRecord)
}

// ByteAnchor specifies a verbatim quoted byte slice that must occur inside an evidence segment.
type ByteAnchor struct {
	Quote     string `json:"quote"`
	StartByte *int   `json:"start_byte,omitempty"`
	EndByte   *int   `json:"end_byte,omitempty"`
}

// VerifyAnchors validates that segment hashes to expectedSegmentSHA256 and that
// every quoted anchor occurs verbatim inside segment.
func VerifyAnchors(segment []byte, expectedSegmentSHA256 string, anchors []ByteAnchor) error {
	if len(anchors) == 0 && expectedSegmentSHA256 == "" && len(segment) == 0 {
		return nil
	}
	if !sha256Pattern.MatchString(expectedSegmentSHA256) {
		return refusal(5, "EVIDENCE_DIGEST_INVALID", "evidence segment digest must be 64 lowercase hex chars")
	}
	if actual := DefaultHash(segment); actual != expectedSegmentSHA256 {
		return refusal(5, "EVIDENCE_DIGEST_MISMATCH", "evidence segment digest mismatch")
	}
	for _, a := range anchors {
		if a.Quote == "" {
			return refusal(5, "ANCHOR_EMPTY", "quoted byte anchor cannot be empty")
		}
		qBytes := []byte(a.Quote)
		if a.StartByte != nil || a.EndByte != nil {
			if a.StartByte == nil || a.EndByte == nil {
				return refusal(5, "ANCHOR_RANGE", "both start_byte and end_byte are required when range is set")
			}
			start, end := *a.StartByte, *a.EndByte
			if start < 0 || end < start || end > len(segment) {
				return refusal(5, "ANCHOR_RANGE", "anchor byte range out of bounds")
			}
			if !bytes.Equal(segment[start:end], qBytes) {
				return refusal(5, "ANCHOR_MISMATCH", "quoted byte anchor does not match segment slice")
			}
		} else {
			if !bytes.Contains(segment, qBytes) {
				return refusal(5, "ANCHOR_MISMATCH", "quoted byte anchor not found in evidence segment")
			}
		}
	}
	return nil
}

// AttemptReceiptRef records an attempt's terminal receipt reference and accounting metadata.
type AttemptReceiptRef struct {
	TaskID                string `json:"task_id"`
	AttemptID             string `json:"attempt_id"`
	LeaseID               string `json:"lease_id"`
	ReceiptSHA256         string `json:"receipt_sha256"`
	BodyRef               string `json:"body_ref,omitempty"`
	ExitCode              int    `json:"exit_code"`
	FailureReason         string `json:"failure_reason,omitempty"`
	StartedAtMS           *int64 `json:"started_at_ms,omitempty"`
	FinishedAtMS          *int64 `json:"finished_at_ms,omitempty"`
	CostMicroUSD          *int64 `json:"cost_micro_usd"`
	CostReason            string `json:"cost_reason,omitempty"`
	CostUnavailableAsZero bool   `json:"-"`
}

func validateAttemptReceiptRef(r AttemptReceiptRef) error {
	if !idPattern.MatchString(r.TaskID) || !idPattern.MatchString(r.AttemptID) || !idPattern.MatchString(r.LeaseID) {
		return refusal(2, "INVALID_BINDING", "invalid task_id, attempt_id, or lease_id in attempt receipt")
	}
	if !sha256Pattern.MatchString(r.ReceiptSHA256) {
		return refusal(2, "MALFORMED", "receipt_sha256 must be 64 lowercase hex chars")
	}
	if r.BodyRef != "" && !sha256Pattern.MatchString(r.BodyRef) {
		return refusal(2, "MALFORMED", "body_ref must be 64 lowercase hex chars")
	}
	if r.ExitCode != 0 && (r.ExitCode < 2 || r.ExitCode > 7) {
		return refusal(5, "INVALID_EXIT_CODE", "normalized exit_code must be 0 or 2..7, never 1")
	}
	if r.StartedAtMS != nil || r.FinishedAtMS != nil {
		if r.StartedAtMS == nil || r.FinishedAtMS == nil || *r.StartedAtMS < 0 || *r.FinishedAtMS < *r.StartedAtMS {
			return refusal(2, "INVALID_INTERVAL", "invalid attempt started_at_ms/finished_at_ms interval")
		}
	}
	if r.CostUnavailableAsZero {
		return refusal(5, "MISSING_COST_AS_ZERO", "unavailable cost must be null with reason AUSENTE, never zero")
	}
	if r.CostMicroUSD == nil {
		if r.CostReason != "" && r.CostReason != ReasonAusente {
			return refusal(2, "INVALID_COST_REASON", "null cost must have reason AUSENTE")
		}
	} else {
		if *r.CostMicroUSD < 0 {
			return refusal(2, "INVALID_COST", "cost_micro_usd cannot be negative")
		}
		if r.CostReason == ReasonAusente {
			return refusal(5, "MISSING_COST_AS_ZERO", "cost marked AUSENTE must be null, never an integer value")
		}
	}
	return nil
}

// Observation represents a structured worker observation or finding submitted to the aggregator.
type Observation struct {
	WaveID                string             `json:"wave_id"`
	StreamKey             string             `json:"stream_key,omitempty"`
	Seq                   int64              `json:"seq"`
	BodyRef               string             `json:"body_ref"`
	ClaimedReproSHA256    string             `json:"claimed_repro_sha256,omitempty"`
	Identity              *ReproRecord       `json:"identity,omitempty"`
	IdentityRaw           []byte             `json:"identity_raw,omitempty"`
	Provenance            Provenance         `json:"provenance"`
	AffectedTargets       []AffectedTarget   `json:"affected_targets,omitempty"`
	EvidenceSegment       []byte             `json:"evidence_segment,omitempty"`
	EvidenceSegmentSHA256 string             `json:"evidence_segment_sha256,omitempty"`
	QuotedAnchors         []ByteAnchor       `json:"quoted_anchors,omitempty"`
	AttemptReceipt        *AttemptReceiptRef `json:"attempt_receipt,omitempty"`
	Summary               string             `json:"summary,omitempty"`
}

func (o Observation) streamID() string {
	if o.StreamKey != "" {
		return o.StreamKey
	}
	if o.Provenance.Sender != "" {
		return o.Provenance.Sender
	}
	if o.Provenance.TaskID != "" && o.Provenance.AttemptID != "" {
		return o.Provenance.TaskID + "/" + o.Provenance.AttemptID
	}
	return o.WaveID
}

func (o Observation) canonicalWire() ([]byte, error) {
	var idDoc any
	if len(o.IdentityRaw) > 0 {
		parsed, err := workerbridge.Decode(o.IdentityRaw)
		if err != nil {
			return nil, err
		}
		idDoc = parsed
	} else if o.Identity != nil {
		idDoc = o.Identity.toCanonicalDocument()
	}
	anchors := make([]any, len(o.QuotedAnchors))
	for i, a := range o.QuotedAnchors {
		m := workerbridge.Document{"quote": a.Quote}
		if a.StartByte != nil {
			m["start_byte"] = json.Number(strconv.Itoa(*a.StartByte))
		}
		if a.EndByte != nil {
			m["end_byte"] = json.Number(strconv.Itoa(*a.EndByte))
		}
		anchors[i] = m
	}
	targets := make([]any, len(o.AffectedTargets))
	for i, t := range o.AffectedTargets {
		targets[i] = workerbridge.Document{"path": t.Path, "symbol": t.Symbol}
	}
	prov := workerbridge.Document{
		"task_id":    o.Provenance.TaskID,
		"attempt_id": o.Provenance.AttemptID,
		"lease_id":   o.Provenance.LeaseID,
		"worker_id":  o.Provenance.WorkerID,
		"sender":     o.Provenance.Sender,
		"pulse":      json.Number(strconv.FormatInt(o.Provenance.Pulse, 10)),
	}
	if o.Provenance.StartedAtMS != nil {
		prov["started_at_ms"] = json.Number(strconv.FormatInt(*o.Provenance.StartedAtMS, 10))
	} else {
		prov["started_at_ms"] = nil
	}
	if o.Provenance.FinishedAtMS != nil {
		prov["finished_at_ms"] = json.Number(strconv.FormatInt(*o.Provenance.FinishedAtMS, 10))
	} else {
		prov["finished_at_ms"] = nil
	}
	if o.Provenance.CostMicroUSD != nil {
		prov["cost_micro_usd"] = json.Number(strconv.FormatInt(*o.Provenance.CostMicroUSD, 10))
	} else {
		prov["cost_micro_usd"] = nil
	}
	var receiptDoc any
	if o.AttemptReceipt != nil {
		r := *o.AttemptReceipt
		rm := workerbridge.Document{
			"task_id":        r.TaskID,
			"attempt_id":     r.AttemptID,
			"lease_id":       r.LeaseID,
			"receipt_sha256": r.ReceiptSHA256,
			"body_ref":       r.BodyRef,
			"exit_code":      json.Number(strconv.Itoa(r.ExitCode)),
			"failure_reason": r.FailureReason,
			"cost_reason":    r.CostReason,
		}
		if r.StartedAtMS != nil {
			rm["started_at_ms"] = json.Number(strconv.FormatInt(*r.StartedAtMS, 10))
		} else {
			rm["started_at_ms"] = nil
		}
		if r.FinishedAtMS != nil {
			rm["finished_at_ms"] = json.Number(strconv.FormatInt(*r.FinishedAtMS, 10))
		} else {
			rm["finished_at_ms"] = nil
		}
		if r.CostMicroUSD != nil {
			rm["cost_micro_usd"] = json.Number(strconv.FormatInt(*r.CostMicroUSD, 10))
		} else {
			rm["cost_micro_usd"] = nil
		}
		receiptDoc = rm
	}
	doc := workerbridge.Document{
		"wave_id":                 o.WaveID,
		"stream_key":              o.streamID(),
		"seq":                     json.Number(strconv.FormatInt(o.Seq, 10)),
		"body_ref":                o.BodyRef,
		"claimed_repro_sha256":    o.ClaimedReproSHA256,
		"identity":                idDoc,
		"provenance":              prov,
		"affected_targets":        targets,
		"evidence_segment_sha256": o.EvidenceSegmentSHA256,
		"quoted_anchors":          anchors,
		"attempt_receipt":         receiptDoc,
		"summary":                 o.Summary,
	}
	return workerbridge.Canonical(doc)
}

func cloneObservation(o Observation) Observation {
	cp := o
	if o.Identity != nil {
		id := *o.Identity
		id = cloneReproRecord(id)
		cp.Identity = &id
	}
	if len(o.IdentityRaw) > 0 {
		cp.IdentityRaw = bytes.Clone(o.IdentityRaw)
	}
	if o.Provenance.StartedAtMS != nil {
		v := *o.Provenance.StartedAtMS
		cp.Provenance.StartedAtMS = &v
	}
	if o.Provenance.FinishedAtMS != nil {
		v := *o.Provenance.FinishedAtMS
		cp.Provenance.FinishedAtMS = &v
	}
	if o.Provenance.CostMicroUSD != nil {
		v := *o.Provenance.CostMicroUSD
		cp.Provenance.CostMicroUSD = &v
	}
	if len(o.AffectedTargets) > 0 {
		cp.AffectedTargets = append([]AffectedTarget(nil), o.AffectedTargets...)
	}
	if len(o.EvidenceSegment) > 0 {
		cp.EvidenceSegment = bytes.Clone(o.EvidenceSegment)
	}
	if len(o.QuotedAnchors) > 0 {
		cp.QuotedAnchors = make([]ByteAnchor, len(o.QuotedAnchors))
		for i, a := range o.QuotedAnchors {
			ca := ByteAnchor{Quote: a.Quote}
			if a.StartByte != nil {
				s := *a.StartByte
				ca.StartByte = &s
			}
			if a.EndByte != nil {
				e := *a.EndByte
				ca.EndByte = &e
			}
			cp.QuotedAnchors[i] = ca
		}
	}
	if o.AttemptReceipt != nil {
		r := cloneAttemptReceiptRef(*o.AttemptReceipt)
		cp.AttemptReceipt = &r
	}
	return cp
}

func cloneAttemptReceiptRef(r AttemptReceiptRef) AttemptReceiptRef {
	cp := r
	if r.StartedAtMS != nil {
		v := *r.StartedAtMS
		cp.StartedAtMS = &v
	}
	if r.FinishedAtMS != nil {
		v := *r.FinishedAtMS
		cp.FinishedAtMS = &v
	}
	if r.CostMicroUSD != nil {
		v := *r.CostMicroUSD
		cp.CostMicroUSD = &v
	}
	if cp.CostMicroUSD == nil && cp.CostReason == "" {
		cp.CostReason = ReasonAusente
	}
	return cp
}

// Manifestation is a deduplicated entry inside a repro_sha256 hash bucket,
// identified by its exact canonical record bytes and holding the set-union of
// all observations, attempt receipts, and affected targets.
type Manifestation struct {
	ReproSHA256     string              `json:"repro_sha256"`
	CanonicalRecord []byte              `json:"canonical_record"`
	FramedBytes     []byte              `json:"framed_bytes"`
	Record          ReproRecord         `json:"record"`
	Observations    []Observation       `json:"observations"`
	AttemptReceipts []AttemptReceiptRef `json:"attempt_receipts"`
	AffectedTargets []AffectedTarget    `json:"affected_targets"`
}

func cloneManifestation(m Manifestation) Manifestation {
	cp := Manifestation{
		ReproSHA256:     m.ReproSHA256,
		CanonicalRecord: bytes.Clone(m.CanonicalRecord),
		FramedBytes:     bytes.Clone(m.FramedBytes),
		Record:          m.Record,
		Observations:    make([]Observation, len(m.Observations)),
		AttemptReceipts: make([]AttemptReceiptRef, len(m.AttemptReceipts)),
		AffectedTargets: append([]AffectedTarget(nil), m.AffectedTargets...),
	}
	cp.Record = cloneReproRecord(cp.Record)
	for i, o := range m.Observations {
		cp.Observations[i] = cloneObservation(o)
	}
	for i, r := range m.AttemptReceipts {
		cp.AttemptReceipts[i] = cloneAttemptReceiptRef(r)
	}
	return cp
}

func unionObservations(a, b []Observation) []Observation {
	byWire := make(map[string]Observation, len(a)+len(b))
	keys := make([]string, 0, len(a)+len(b))
	for _, list := range [][]Observation{a, b} {
		for _, item := range list {
			w, err := item.canonicalWire()
			if err != nil {
				w = []byte(fmt.Sprintf("%s:%d:%s", item.streamID(), item.Seq, item.BodyRef))
			}
			k := string(w)
			if _, exists := byWire[k]; !exists {
				byWire[k] = cloneObservation(item)
				keys = append(keys, k)
			}
		}
	}
	sort.Strings(keys)
	out := make([]Observation, len(keys))
	for i, k := range keys {
		out[i] = byWire[k]
	}
	return out
}

func unionAttemptReceipts(a, b []AttemptReceiptRef) ([]AttemptReceiptRef, error) {
	byAttemptKey := make(map[string]AttemptReceiptRef, len(a)+len(b))
	keys := make([]string, 0, len(a)+len(b))
	for _, list := range [][]AttemptReceiptRef{a, b} {
		for _, item := range list {
			norm := cloneAttemptReceiptRef(item)
			ak := norm.TaskID + "\x00" + norm.AttemptID + "\x00" + norm.LeaseID
			if existing, exists := byAttemptKey[ak]; exists {
				if existing.ReceiptSHA256 != norm.ReceiptSHA256 || existing.ExitCode != norm.ExitCode {
					return nil, refusal(5, "ATTEMPT_OVERWRITE", "conflicting receipt for existing attempt is forbidden; never overwrite or delete an attempt")
				}
				continue
			}
			byAttemptKey[ak] = norm
			keys = append(keys, ak)
		}
	}
	sort.Strings(keys)
	out := make([]AttemptReceiptRef, len(keys))
	for i, k := range keys {
		out[i] = byAttemptKey[k]
	}
	return out, nil
}

func unionTargets(a, b []AffectedTarget) []AffectedTarget {
	seen := make(map[string]AffectedTarget, len(a)+len(b))
	keys := make([]string, 0, len(a)+len(b))
	for _, list := range [][]AffectedTarget{a, b} {
		for _, t := range list {
			if t.Path == "" && t.Symbol == "" {
				continue
			}
			k := t.Path + "\x00" + t.Symbol
			if _, exists := seen[k]; !exists {
				seen[k] = t
				keys = append(keys, k)
			}
		}
	}
	sort.Strings(keys)
	out := make([]AffectedTarget, len(keys))
	for i, k := range keys {
		out[i] = seen[k]
	}
	return out
}

// MergeManifestations computes the commutative, associative, idempotent set-union
// of two Manifestation records ONLY if both are keyed and share both repro_sha256
// and exact canonical record bytes. A forced collision with different bytes is refused.
func MergeManifestations(a, b Manifestation) (Manifestation, error) {
	if a.ReproSHA256 == "" || b.ReproSHA256 == "" || len(a.CanonicalRecord) == 0 || len(b.CanonicalRecord) == 0 {
		return Manifestation{}, refusal(5, "NULL_REPRO_MERGE", "unkeyed or null repro observations cannot merge")
	}
	if a.ReproSHA256 != b.ReproSHA256 {
		return Manifestation{}, refusal(5, "REPRO_HASH_MISMATCH", "cannot merge manifestations with different repro_sha256")
	}
	if !bytes.Equal(a.CanonicalRecord, b.CanonicalRecord) || !bytes.Equal(a.FramedBytes, b.FramedBytes) {
		return Manifestation{}, refusal(5, "COLLISION_BYTE_MISMATCH", "hash hit with different canonical record bytes cannot merge")
	}
	receipts, err := unionAttemptReceipts(a.AttemptReceipts, b.AttemptReceipts)
	if err != nil {
		return Manifestation{}, err
	}
	merged := cloneManifestation(a)
	merged.Observations = unionObservations(a.Observations, b.Observations)
	merged.AttemptReceipts = receipts
	merged.AffectedTargets = unionTargets(a.AffectedTargets, b.AffectedTargets)
	return merged, nil
}

// Store is an append-only deduplication store mapping repro_sha256 to a collision-safe
// bucket of canonical record entries, plus unkeyed pending observations and all attempt receipts.
type Store struct {
	mu       sync.RWMutex
	hashFn   HashFunc
	buckets  map[string][]Manifestation
	unkeyed  []Observation
	attempts []AttemptReceiptRef
}

// NewStore constructs an append-only deduplication store with the given hash function.
// If hashFn is nil, DefaultHash (SHA-256) is used.
func NewStore(hashFn HashFunc) *Store {
	if hashFn == nil {
		hashFn = DefaultHash
	}
	return &Store{
		hashFn:  hashFn,
		buckets: make(map[string][]Manifestation),
	}
}

// Bucket returns a copy of the collision bucket for a given repro_sha256.
func (s *Store) Bucket(reproSHA256 string) []Manifestation {
	s.mu.RLock()
	defer s.mu.RUnlock()
	entries := s.buckets[reproSHA256]
	out := make([]Manifestation, len(entries))
	for i, m := range entries {
		out[i] = cloneManifestation(m)
	}
	return out
}

func (s *Store) addKeyedLocked(id ReproIdentity, obs Observation) error {
	targets := append([]AffectedTarget(nil), obs.AffectedTargets...)
	for _, path := range id.Record.Affected {
		targets = append(targets, AffectedTarget{Path: path})
	}
	var receipts []AttemptReceiptRef
	if obs.AttemptReceipt != nil {
		receipts = []AttemptReceiptRef{cloneAttemptReceiptRef(*obs.AttemptReceipt)}
	}
	incoming := Manifestation{
		ReproSHA256:     id.ReproSHA256,
		CanonicalRecord: bytes.Clone(id.CanonicalRecord),
		FramedBytes:     bytes.Clone(id.FramedBytes),
		Record:          cloneReproRecord(id.Record),
		Observations:    []Observation{cloneObservation(obs)},
		AttemptReceipts: receipts,
		AffectedTargets: unionTargets(nil, targets),
	}
	bucket := s.buckets[id.ReproSHA256]
	for i, existing := range bucket {
		if bytes.Equal(existing.CanonicalRecord, incoming.CanonicalRecord) && bytes.Equal(existing.FramedBytes, incoming.FramedBytes) {
			merged, err := MergeManifestations(existing, incoming)
			if err != nil {
				return err
			}
			bucket[i] = merged
			s.buckets[id.ReproSHA256] = bucket
			if obs.AttemptReceipt != nil {
				if err := s.recordAttemptLocked(*obs.AttemptReceipt); err != nil {
					return err
				}
			}
			return nil
		}
	}
	// Hash miss or hash collision with distinct canonical record bytes:
	// append as a separate manifestation in the bucket and sort by canonical bytes.
	if obs.AttemptReceipt != nil {
		if err := s.recordAttemptLocked(*obs.AttemptReceipt); err != nil {
			return err
		}
	}
	bucket = append(bucket, incoming)
	sort.Slice(bucket, func(i, j int) bool {
		return bytes.Compare(bucket[i].CanonicalRecord, bucket[j].CanonicalRecord) < 0
	})
	s.buckets[id.ReproSHA256] = bucket
	return nil
}

func (s *Store) addUnkeyedLocked(obs Observation) error {
	if obs.AttemptReceipt != nil {
		if err := s.recordAttemptLocked(*obs.AttemptReceipt); err != nil {
			return err
		}
	}
	s.unkeyed = unionObservations(s.unkeyed, []Observation{obs})
	return nil
}

func (s *Store) recordAttemptLocked(ref AttemptReceiptRef) error {
	if err := validateAttemptReceiptRef(ref); err != nil {
		return err
	}
	merged, err := unionAttemptReceipts(s.attempts, []AttemptReceiptRef{ref})
	if err != nil {
		return err
	}
	s.attempts = merged
	return nil
}

// MergeStore merges all manifestations, unkeyed observations, and attempt receipts
// from other into s using set union.
func (s *Store) MergeStore(other *Store) error {
	if other == nil {
		return nil
	}
	other.mu.RLock()
	otherBuckets := make(map[string][]Manifestation, len(other.buckets))
	for k, v := range other.buckets {
		cp := make([]Manifestation, len(v))
		for i, m := range v {
			cp[i] = cloneManifestation(m)
		}
		otherBuckets[k] = cp
	}
	otherUnkeyed := unionObservations(nil, other.unkeyed)
	otherAttempts := make([]AttemptReceiptRef, len(other.attempts))
	for i, r := range other.attempts {
		otherAttempts[i] = cloneAttemptReceiptRef(r)
	}
	other.mu.RUnlock()

	s.mu.Lock()
	defer s.mu.Unlock()
	mergedAttempts, err := unionAttemptReceipts(s.attempts, otherAttempts)
	if err != nil {
		return err
	}
	for hashKey, entries := range otherBuckets {
		bucket := s.buckets[hashKey]
		for _, inc := range entries {
			matched := false
			for i, ex := range bucket {
				if bytes.Equal(ex.CanonicalRecord, inc.CanonicalRecord) && bytes.Equal(ex.FramedBytes, inc.FramedBytes) {
					m, err := MergeManifestations(ex, inc)
					if err != nil {
						return err
					}
					bucket[i] = m
					matched = true
					break
				}
			}
			if !matched {
				bucket = append(bucket, cloneManifestation(inc))
			}
		}
		sort.Slice(bucket, func(i, j int) bool {
			return bytes.Compare(bucket[i].CanonicalRecord, bucket[j].CanonicalRecord) < 0
		})
		s.buckets[hashKey] = bucket
	}
	s.unkeyed = unionObservations(s.unkeyed, otherUnkeyed)
	s.attempts = mergedAttempts
	return nil
}

// Manifestations returns all unique manifestations across all hash buckets in deterministic order.
func (s *Store) Manifestations() []Manifestation {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.manifestationsLocked()
}

func (s *Store) manifestationsLocked() []Manifestation {
	hashes := make([]string, 0, len(s.buckets))
	for h := range s.buckets {
		hashes = append(hashes, h)
	}
	sort.Strings(hashes)
	var out []Manifestation
	for _, h := range hashes {
		for _, m := range s.buckets[h] {
			out = append(out, cloneManifestation(m))
		}
	}
	return out
}

// Config configures a wave Aggregator instance.
type Config struct {
	WaveID          string
	BaseSeq         int64
	HashFn          HashFunc
	AllowedBindings []Binding
}

// Binding identifies an authorized (TaskID, AttemptID, LeaseID) tuple.
type Binding struct {
	TaskID    string `json:"task_id"`
	AttemptID string `json:"attempt_id"`
	LeaseID   string `json:"lease_id"`
}

// JournalEntry is an append-only record in the aggregator's in-memory journal.
type JournalEntry struct {
	Index          int         `json:"index"`
	StreamKey      string      `json:"stream_key"`
	Seq            int64       `json:"seq"`
	CanonicalBytes []byte      `json:"canonical_bytes"`
	BodyRef        string      `json:"body_ref"`
	ReceiptRef     string      `json:"receipt_ref,omitempty"`
	ReproSHA256    string      `json:"repro_sha256,omitempty"`
	Unkeyed        bool        `json:"unkeyed"`
	Acknowledged   bool        `json:"acknowledged"`
	Observation    Observation `json:"observation"`
}

// SeqInterval represents a missing sequence range [From, To] for a stream.
type SeqInterval struct {
	StreamKey string `json:"stream_key"`
	From      int64  `json:"from"`
	To        int64  `json:"to"`
}

// WaveSummary summarizes all attempt receipts, unique manifestations, unkeyed observations,
// sequence gaps, wall time, and total cost over all attempts.
type WaveSummary struct {
	WaveID                   string              `json:"wave_id"`
	AttemptReceipts          []AttemptReceiptRef `json:"attempt_receipts"`
	UniqueManifestationCount int                 `json:"unique_manifestation_count"`
	Manifestations           []Manifestation     `json:"manifestations"`
	UnkeyedObservations      []Observation       `json:"unkeyed_observations"`
	MissingIntervals         []SeqInterval       `json:"missing_intervals"`
	AckedSeq                 map[string]int64    `json:"acked_seq"`
	WallTimeMS               *int64              `json:"wall_time_ms"`
	WallTimeReason           string              `json:"wall_time_reason,omitempty"`
	TotalCostMicroUSD        *int64              `json:"total_cost_micro_usd"`
	CostReason               string              `json:"cost_reason,omitempty"`
}

type pendingItem struct {
	obs            Observation
	canonicalBytes []byte
	id             *ReproIdentity
	journalIdx     int
	applied        bool
}

type streamState struct {
	started  bool
	ackedSeq int64
	bySeq    map[int64]*pendingItem
}

// Aggregator implements the W8-P2-07 local deduplication and receipts aggregation pipeline.
type Aggregator struct {
	mu              sync.RWMutex
	waveID          string
	baseSeq         int64
	hashFn          HashFunc
	allowedBindings map[string]bool
	store           *Store
	journal         []JournalEntry
	streams         map[string]*streamState
	ackedRefs       map[string]bool
	knownRefs       map[string]bool
}

// NewAggregator creates a new local wave aggregator.
func NewAggregator(cfg Config) (*Aggregator, error) {
	if cfg.WaveID != "" && !idPattern.MatchString(cfg.WaveID) {
		return nil, refusal(2, "INVALID_WAVE_ID", "invalid wave_id")
	}
	if cfg.BaseSeq < 0 {
		return nil, refusal(2, "INVALID_BASE_SEQ", "base_seq cannot be negative")
	}
	hashFn := cfg.HashFn
	if hashFn == nil {
		hashFn = DefaultHash
	}
	bindings := make(map[string]bool, len(cfg.AllowedBindings))
	for _, b := range cfg.AllowedBindings {
		if !idPattern.MatchString(b.TaskID) || !idPattern.MatchString(b.AttemptID) || !idPattern.MatchString(b.LeaseID) {
			return nil, refusal(2, "INVALID_BINDING", "invalid allowed binding tuple")
		}
		bindings[b.TaskID+"\x00"+b.AttemptID+"\x00"+b.LeaseID] = true
	}
	return &Aggregator{
		waveID:          cfg.WaveID,
		baseSeq:         cfg.BaseSeq,
		hashFn:          hashFn,
		allowedBindings: bindings,
		store:           NewStore(hashFn),
		streams:         make(map[string]*streamState),
		ackedRefs:       make(map[string]bool),
		knownRefs:       make(map[string]bool),
	}, nil
}

// Store returns the underlying deduplication store.
func (a *Aggregator) Store() *Store {
	return a.store
}

func (a *Aggregator) validateObservationBinding(obs Observation) error {
	if obs.WaveID == "" || !idPattern.MatchString(obs.WaveID) {
		return refusal(2, "INVALID_BINDING", "invalid or missing wave_id")
	}
	if a.waveID != "" && obs.WaveID != a.waveID {
		return refusal(5, "BINDING_MISMATCH", "observation wave_id does not match aggregator wave_id")
	}
	p := obs.Provenance
	if !idPattern.MatchString(p.TaskID) || !idPattern.MatchString(p.AttemptID) || !idPattern.MatchString(p.LeaseID) {
		return refusal(2, "INVALID_BINDING", "invalid task_id, attempt_id, or lease_id in provenance")
	}
	if len(a.allowedBindings) > 0 {
		bk := p.TaskID + "\x00" + p.AttemptID + "\x00" + p.LeaseID
		if !a.allowedBindings[bk] {
			return refusal(5, "UNAUTHORIZED_BINDING", "provenance binding is not authorized for this wave")
		}
	}
	if obs.BodyRef != "" && !sha256Pattern.MatchString(obs.BodyRef) {
		return refusal(2, "MALFORMED", "body_ref must be 64 lowercase hex chars")
	}
	if obs.Seq < a.baseSeq {
		return refusal(2, "INVALID_SEQ", "sequence number precedes base_seq")
	}
	if p.StartedAtMS != nil || p.FinishedAtMS != nil {
		if p.StartedAtMS == nil || p.FinishedAtMS == nil || *p.StartedAtMS < 0 || *p.FinishedAtMS < *p.StartedAtMS {
			return refusal(2, "INVALID_INTERVAL", "invalid provenance started_at_ms/finished_at_ms interval")
		}
	}
	if p.CostMicroUSD != nil && *p.CostMicroUSD < 0 {
		return refusal(2, "INVALID_COST", "provenance cost_micro_usd cannot be negative")
	}
	if obs.AttemptReceipt != nil {
		r := *obs.AttemptReceipt
		if r.TaskID != p.TaskID || r.AttemptID != p.AttemptID || r.LeaseID != p.LeaseID {
			return refusal(5, "BINDING_MISMATCH", "attempt_receipt binding differs from observation provenance")
		}
		if err := validateAttemptReceiptRef(r); err != nil {
			return err
		}
	}
	return nil
}

// Ingest validates binding and sequence, verifies quoted-byte anchors against the
// evidence segment digest, recomputes w8-repro/1 identity, appends to the in-memory
// journal, and acknowledges only the contiguous applied sequence.
func (a *Aggregator) Ingest(obs Observation) error {
	a.mu.Lock()
	defer a.mu.Unlock()

	// 1. Validate binding and sequence.
	if err := a.validateObservationBinding(obs); err != nil {
		return err
	}

	// 2. Verify quoted-byte anchors against the evidence segment digest.
	if len(obs.QuotedAnchors) > 0 || obs.EvidenceSegmentSHA256 != "" || len(obs.EvidenceSegment) > 0 {
		if err := VerifyAnchors(obs.EvidenceSegment, obs.EvidenceSegmentSHA256, obs.QuotedAnchors); err != nil {
			return err
		}
	}

	// 3. Recompute identity (or classify as unkeyed pending observation if required fields are missing).
	var computedID *ReproIdentity
	if len(obs.IdentityRaw) > 0 {
		rec, complete, err := ParseReproRecord(obs.IdentityRaw)
		if err != nil {
			return err
		}
		if complete {
			id, err := ComputeIdentity(rec, a.hashFn)
			if err != nil {
				return err
			}
			computedID = &id
			cpRec := rec
			obs.Identity = &cpRec
		} else {
			cpRec := rec
			obs.Identity = &cpRec
		}
	} else if obs.Identity != nil {
		if obs.Identity.IsComplete() {
			id, err := ComputeIdentity(*obs.Identity, a.hashFn)
			if err != nil {
				return err
			}
			computedID = &id
		}
	}

	if computedID != nil {
		if obs.ClaimedReproSHA256 != "" && obs.ClaimedReproSHA256 != computedID.ReproSHA256 {
			return refusal(5, "REPRO_HASH_MISMATCH", "claimed repro_sha256 does not match recomputed w8-repro/1 digest")
		}
	} else {
		if obs.ClaimedReproSHA256 != "" {
			return refusal(5, "UNKEYED_CLAIMED_HASH", "observation missing required identity fields cannot claim a repro_sha256")
		}
	}

	// Compute canonical bytes of the observation for equivocation / redelivery checking.
	wireBytes, err := obs.canonicalWire()
	if err != nil {
		return refusal(2, workerbridge.Class(err), err.Error())
	}

	streamKey := obs.streamID()
	st, exists := a.streams[streamKey]
	if !exists {
		st = &streamState{
			started:  false,
			ackedSeq: a.baseSeq - 1,
			bySeq:    make(map[int64]*pendingItem),
		}
		a.streams[streamKey] = st
	}

	// Check for same sequence with same or different bytes.
	if prev, seen := st.bySeq[obs.Seq]; seen {
		if !bytes.Equal(prev.canonicalBytes, wireBytes) {
			return refusal(5, "SEQ_EQUIVOCATION", "same sequence with different bytes is rejected")
		}
		// Exact redelivery: idempotent no-op.
		return nil
	}

	// Also ensure attempt receipts across different sequences do not conflict with existing attempts.
	if obs.AttemptReceipt != nil {
		a.store.mu.RLock()
		_, checkErr := unionAttemptReceipts(a.store.attempts, []AttemptReceiptRef{*obs.AttemptReceipt})
		a.store.mu.RUnlock()
		if checkErr != nil {
			return checkErr
		}
	}

	// 4. Append to in-memory journal.
	reproSHA := ""
	if computedID != nil {
		reproSHA = computedID.ReproSHA256
	}
	receiptRef := ""
	if obs.AttemptReceipt != nil {
		receiptRef = obs.AttemptReceipt.ReceiptSHA256
	}
	entryIdx := len(a.journal)
	entry := JournalEntry{
		Index:          entryIdx,
		StreamKey:      streamKey,
		Seq:            obs.Seq,
		CanonicalBytes: wireBytes,
		BodyRef:        obs.BodyRef,
		ReceiptRef:     receiptRef,
		ReproSHA256:    reproSHA,
		Unkeyed:        computedID == nil,
		Acknowledged:   false,
		Observation:    cloneObservation(obs),
	}
	a.journal = append(a.journal, entry)

	if obs.BodyRef != "" {
		a.knownRefs[obs.BodyRef] = true
	}
	if receiptRef != "" {
		a.knownRefs[receiptRef] = true
		if obs.AttemptReceipt.BodyRef != "" {
			a.knownRefs[obs.AttemptReceipt.BodyRef] = true
		}
	}

	item := &pendingItem{
		obs:            cloneObservation(obs),
		canonicalBytes: wireBytes,
		id:             computedID,
		journalIdx:     entryIdx,
		applied:        false,
	}
	st.bySeq[obs.Seq] = item

	// 5. Acknowledge only the contiguous applied sequence; a gap stops acknowledgement.
	return a.advanceContiguousLocked(st)
}

func (a *Aggregator) advanceContiguousLocked(st *streamState) error {
	if !st.started {
		if _, hasBase := st.bySeq[a.baseSeq]; hasBase {
			st.started = true
			st.ackedSeq = a.baseSeq - 1
		} else if _, hasNext := st.bySeq[a.baseSeq+1]; hasNext {
			st.started = true
			st.ackedSeq = a.baseSeq
		} else {
			return nil
		}
	}

	// Apply any newly received baseSeq element if a 1-based stream later receives seq == baseSeq.
	if baseItem, hasBase := st.bySeq[a.baseSeq]; hasBase && !baseItem.applied && st.ackedSeq >= a.baseSeq {
		if err := a.applyPendingItemLocked(baseItem); err != nil {
			return err
		}
	}

	for {
		nextSeq := st.ackedSeq + 1
		item, ok := st.bySeq[nextSeq]
		if !ok {
			break
		}
		if !item.applied {
			if err := a.applyPendingItemLocked(item); err != nil {
				return err
			}
		}
		st.ackedSeq = nextSeq
	}
	return nil
}

func (a *Aggregator) applyPendingItemLocked(item *pendingItem) error {
	a.store.mu.Lock()
	var err error
	if item.id != nil {
		err = a.store.addKeyedLocked(*item.id, item.obs)
	} else {
		err = a.store.addUnkeyedLocked(item.obs)
	}
	a.store.mu.Unlock()
	if err != nil {
		return err
	}
	item.applied = true
	a.journal[item.journalIdx].Acknowledged = true
	if item.obs.BodyRef != "" {
		a.ackedRefs[item.obs.BodyRef] = true
	}
	if item.obs.AttemptReceipt != nil {
		a.ackedRefs[item.obs.AttemptReceipt.ReceiptSHA256] = true
		if item.obs.AttemptReceipt.BodyRef != "" {
			a.ackedRefs[item.obs.AttemptReceipt.BodyRef] = true
		}
	}
	return nil
}

// RecordAttempt records a standalone attempt receipt directly into the store and acknowledges its ref.
func (a *Aggregator) RecordAttempt(ref AttemptReceiptRef) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.store.mu.Lock()
	err := a.store.recordAttemptLocked(ref)
	a.store.mu.Unlock()
	if err != nil {
		return err
	}
	a.knownRefs[ref.ReceiptSHA256] = true
	a.ackedRefs[ref.ReceiptSHA256] = true
	if ref.BodyRef != "" {
		a.knownRefs[ref.BodyRef] = true
		a.ackedRefs[ref.BodyRef] = true
	}
	return nil
}

// Acknowledged reports whether ref (a body or receipt SHA-256 reference) has been
// contiguously applied and acknowledged by the aggregator. Used by the P2-05 bodies janitor.
func (a *Aggregator) Acknowledged(ref string) bool {
	if !sha256Pattern.MatchString(ref) {
		return false
	}
	a.mu.RLock()
	defer a.mu.RUnlock()
	return a.ackedRefs[ref]
}

// VerifyAcknowledged returns nil if ref is acknowledged, or rc 5 if ref is unknown or
// blocked behind a sequence gap.
func (a *Aggregator) VerifyAcknowledged(ref string) error {
	if !sha256Pattern.MatchString(ref) {
		return refusal(2, "MALFORMED", "reference must be 64 lowercase hex chars")
	}
	a.mu.RLock()
	defer a.mu.RUnlock()
	if a.ackedRefs[ref] {
		return nil
	}
	if a.knownRefs[ref] {
		return refusal(5, "GAP_UNACKNOWLEDGED", "reference sits behind a sequence gap and cannot be acknowledged")
	}
	return refusal(5, "UNKNOWN_REF", "reference is unknown and not acknowledged")
}

// Journal returns a snapshot of the in-memory aggregator journal.
func (a *Aggregator) Journal() []JournalEntry {
	a.mu.RLock()
	defer a.mu.RUnlock()
	out := make([]JournalEntry, len(a.journal))
	for i, e := range a.journal {
		cp := e
		cp.CanonicalBytes = bytes.Clone(e.CanonicalBytes)
		cp.Observation = cloneObservation(e.Observation)
		out[i] = cp
	}
	return out
}

// Summary computes the deterministic WaveSummary over all contiguously applied observations
// and attempt receipts, plus any missing sequence intervals across streams.
func (a *Aggregator) Summary() WaveSummary {
	a.mu.RLock()
	defer a.mu.RUnlock()

	a.store.mu.RLock()
	manifestations := a.store.manifestationsLocked()
	unkeyed := unionObservations(nil, a.store.unkeyed)
	attempts := make([]AttemptReceiptRef, len(a.store.attempts))
	for i, r := range a.store.attempts {
		attempts[i] = cloneAttemptReceiptRef(r)
	}
	a.store.mu.RUnlock()

	streamKeys := make([]string, 0, len(a.streams))
	for k := range a.streams {
		streamKeys = append(streamKeys, k)
	}
	sort.Strings(streamKeys)

	ackedSeq := make(map[string]int64, len(streamKeys))
	var missing []SeqInterval
	for _, sk := range streamKeys {
		st := a.streams[sk]
		if st.started {
			ackedSeq[sk] = st.ackedSeq
		}
		var maxSeq int64 = -1
		for seq := range st.bySeq {
			if seq > maxSeq {
				maxSeq = seq
			}
		}
		if maxSeq < a.baseSeq {
			continue
		}
		startCheck := a.baseSeq + 1
		if st.started {
			startCheck = st.ackedSeq + 1
		} else if _, hasZero := st.bySeq[a.baseSeq]; hasZero {
			startCheck = a.baseSeq
		}
		var gapStart int64 = -1
		for seq := startCheck; seq <= maxSeq; seq++ {
			_, present := st.bySeq[seq]
			if !present {
				if gapStart == -1 {
					gapStart = seq
				}
			} else if gapStart != -1 {
				missing = append(missing, SeqInterval{StreamKey: sk, From: gapStart, To: seq - 1})
				gapStart = -1
			}
		}
		if gapStart != -1 {
			missing = append(missing, SeqInterval{StreamKey: sk, From: gapStart, To: maxSeq})
		}
	}

	// Compute wall time across all attempts.
	var wallTime *int64
	wallTimeReason := ReasonAusente
	if len(attempts) > 0 {
		allHaveIntervals := true
		var minStart int64 = math.MaxInt64
		var maxFinish int64 = -1
		for _, att := range attempts {
			if att.StartedAtMS == nil || att.FinishedAtMS == nil {
				allHaveIntervals = false
				break
			}
			if *att.StartedAtMS < minStart {
				minStart = *att.StartedAtMS
			}
			if *att.FinishedAtMS > maxFinish {
				maxFinish = *att.FinishedAtMS
			}
		}
		if allHaveIntervals && maxFinish >= minStart {
			wt := maxFinish - minStart
			wallTime = &wt
			wallTimeReason = ""
		}
	}

	// Compute total cost over all attempts; unavailable cost is null with reason AUSENTE, never zero.
	var totalCost *int64
	costReason := ReasonAusente
	if len(attempts) > 0 {
		allHaveCost := true
		var sum int64
		for _, att := range attempts {
			if att.CostMicroUSD == nil {
				allHaveCost = false
				break
			}
			sum += *att.CostMicroUSD
		}
		if allHaveCost {
			totalCost = &sum
			costReason = ""
		}
	}

	return WaveSummary{
		WaveID:                   a.waveID,
		AttemptReceipts:          attempts,
		UniqueManifestationCount: len(manifestations),
		Manifestations:           manifestations,
		UnkeyedObservations:      unkeyed,
		MissingIntervals:         missing,
		AckedSeq:                 ackedSeq,
		WallTimeMS:               wallTime,
		WallTimeReason:           wallTimeReason,
		TotalCostMicroUSD:        totalCost,
		CostReason:               costReason,
	}
}

// ValidateSummary checks that a WaveSummary adheres to W8-P2-07 invariants,
// specifically refusing any summary where unavailable cost is represented as zero instead of null.
func ValidateSummary(s WaveSummary) error {
	if len(s.AttemptReceipts) == 0 {
		if s.TotalCostMicroUSD != nil {
			return refusal(5, "MISSING_COST_AS_ZERO", "wave with no attempt receipts must have null total cost with reason AUSENTE")
		}
		if s.CostReason != ReasonAusente {
			return refusal(5, "MISSING_COST_REASON", "unavailable total cost must carry reason AUSENTE")
		}
		return nil
	}
	anyMissingCost := false
	var expectedSum int64
	for _, r := range s.AttemptReceipts {
		if err := validateAttemptReceiptRef(r); err != nil {
			return err
		}
		if r.CostMicroUSD == nil {
			anyMissingCost = true
		} else {
			expectedSum += *r.CostMicroUSD
		}
	}
	if anyMissingCost {
		if s.TotalCostMicroUSD != nil {
			return refusal(5, "MISSING_COST_AS_ZERO", "unavailable attempt cost must yield null total_cost_micro_usd, never zero")
		}
		if s.CostReason != ReasonAusente {
			return refusal(5, "MISSING_COST_REASON", "unavailable total cost must carry reason AUSENTE")
		}
	} else {
		if s.TotalCostMicroUSD == nil || *s.TotalCostMicroUSD != expectedSum {
			return refusal(5, "COST_SUM_MISMATCH", "total_cost_micro_usd does not equal sum of all attempts")
		}
	}
	return nil
}

// ProjectBridgeFixture constructs the local-only bridge-result-v1 projection fixture.
// It copies unchanged task_id, thread, agent, worker_id, attempt_id, lease_id, key,
// base_seq, done_signal plus role, and sets surface to "os-capsule" (never relabeled as jules or chatgpt).
func ProjectBridgeFixture(workerTaskJSON, receiptJSON []byte, promptID string, requestedSurface string) (workerbridge.Envelope, error) {
	var env workerbridge.Envelope
	if requestedSurface != "" && requestedSurface != OSSurface {
		return env, refusal(5, "SURFACE_RELABEL", "bridge projection surface must be os-capsule, never relabeled as "+requestedSurface)
	}
	wDoc, err := workerbridge.Parse(workerTaskJSON)
	if err != nil {
		return env, refusal(2, workerbridge.Class(err), err.Error())
	}
	bridgeObj, ok := wDoc["bridge"].(map[string]any)
	if !ok || bridgeObj == nil {
		return env, refusal(2, "MALFORMED", "worker task missing embedded bridge task")
	}

	parityEnv, err := workerbridge.EncodeBridgeResult(workerTaskJSON, receiptJSON, promptID, false)
	if err != nil {
		return env, err
	}
	for _, k := range append(workerbridge.SharedIDs(), "role") {
		if fmt.Sprint(parityEnv.Result[k]) != fmt.Sprint(bridgeObj[k]) {
			return env, refusal(5, "IDENTIFIER_DRIFT", "bridge identifier mutated during projection: "+k)
		}
	}
	parityEnv.Mode = "fixture"
	parityEnv.Result["surface"] = OSSurface
	return parityEnv, nil
}

// Publish refuses all live publication calls with rc 4 (unavailable) because live publication
// waits for C3 and the canonical os-capsule surface, and refuses any relabeled or unauthorized
// envelope with rc 5.
func Publish(env workerbridge.Envelope) ([]byte, error) {
	if env.Result == nil {
		return nil, refusal(5, "UNAUTHORIZED_PUBLISH", "empty envelope cannot be published")
	}
	surface, _ := env.Result["surface"].(string)
	if surface != OSSurface {
		return nil, refusal(5, "SURFACE_RELABEL", "relabeled publish refused: surface must be os-capsule")
	}
	for _, k := range append(workerbridge.SharedIDs(), "role") {
		if _, ok := env.Result[k]; !ok || env.Result[k] == nil || env.Result[k] == "" {
			return nil, refusal(5, "UNAUTHORIZED_PUBLISH", "missing required bridge identifier: "+k)
		}
	}
	return nil, refusal(4, "unavailable", "live publication waits for C3 and canonical os-capsule surface")
}

// Publish on Aggregator enforces the same local-only publication refusal.
func (a *Aggregator) Publish(env workerbridge.Envelope) ([]byte, error) {
	return Publish(env)
}

// Package harvest coordinates host work through the existing Muse seat path.
// It owns no launcher, inference process, lifecycle log, or repro normalizer.
package harvest

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"strings"
	"sync"
	"time"

	"opensuperficies.local/srf/src/orchestratorbook/wal"
)

type Stage string

const (
	Generate Stage = "Generate"
	AdmitExecute Stage = "AdmitExecute"
	ExtractVerify Stage = "ExtractVerify"
	Deduplicate Stage = "Deduplicate"
	CollectPublish Stage = "CollectPublish"
)

var stages = [...]Stage{Generate, AdmitExecute, ExtractVerify, Deduplicate, CollectPublish}

const (
	MuseSeatRoute = "srf-w8-muse/seat"
	HostMuse = "host-muse"
	CanonicalSurface = "os-capsule"
	ReproNormalizer = "w8-repro/1"
	DefaultAttemptLimit = 3 // TARGET, including the original attempt.
)

type Error struct { Code int; Detail string }
func (e *Error) Error() string { return fmt.Sprintf("harvest: rc %d: %s", e.Code, e.Detail) }
func refusal(code int, detail string) error { return &Error{Code: code, Detail: detail} }

// ExitCode never exposes the legacy motor's non-W8 codes.
func ExitCode(err error) int {
	if err == nil { return 0 }
	var e *Error
	if errors.As(err, &e) && e.Code >= 2 && e.Code <= 7 { return e.Code }
	if errors.Is(err, context.Canceled) { return 7 }
	if errors.Is(err, context.DeadlineExceeded) { return 6 }
	return 3
}

type Obligation struct { ID string; Weight uint64 }
type Wave struct {
	WaveID string
	Repo string
	BaseCommit string
	Objective string
	GateArgv []string
	InputSHA256 string
	ResourcePolicySHA256 string
	Obligations []Obligation
}

// FrozenWave has no exported mutable storage. Every accessor makes a copy.
type FrozenWave struct { wave Wave; fields map[string]string; seal string }
func (f FrozenWave) Wave() Wave { return cloneWave(f.wave) }
func (f FrozenWave) FieldSHA256() map[string]string {
	m := make(map[string]string, len(f.fields))
	for k, v := range f.fields { m[k] = v }; return m
}
func (f FrozenWave) SHA256() string { return f.seal }
func cloneWave(w Wave) Wave {
	w.GateArgv = append([]string(nil), w.GateArgv...)
	w.Obligations = append([]Obligation(nil), w.Obligations...)
	return w
}
func digest(b []byte) string { h := sha256.Sum256(b); return hex.EncodeToString(h[:]) }
func validHex(s string, size int) bool {
	if len(s) != size { return false }
	for _, c := range s { if !(c >= '0' && c <= '9') && !(c >= 'a' && c <= 'f') { return false } }
	return true
}
func present(s string) bool { return strings.TrimSpace(s) != "" && !strings.ContainsRune(s, 0) }

// Freeze hashes the JSON encoding of each named field before any dispatch.
// Ordered inventories and argv remain ordered; no whitespace is normalized.
func Freeze(w Wave) (FrozenWave, error) {
	if !present(w.WaveID) || !present(w.Repo) || !present(w.Objective) || !validHex(w.BaseCommit, 40) || !validHex(w.InputSHA256, 64) || !validHex(w.ResourcePolicySHA256, 64) || len(w.GateArgv) == 0 || len(w.Obligations) == 0 {
		return FrozenWave{}, refusal(2, "incomplete frozen wave or empty obligation inventory")
	}
	for _, a := range w.GateArgv { if !present(a) { return FrozenWave{}, refusal(2, "empty gate argument") } }
	seen := map[string]bool{}; var weight uint64
	for _, o := range w.Obligations {
		if !present(o.ID) || o.Weight == 0 || seen[o.ID] || math.MaxUint64-weight < o.Weight { return FrozenWave{}, refusal(2, "invalid obligation inventory") }
		seen[o.ID] = true; weight += o.Weight
	}
	w = cloneWave(w)
	values := map[string]any{"WaveID": w.WaveID, "Repo": w.Repo, "BaseCommit": w.BaseCommit, "Objective": w.Objective, "GateArgv": w.GateArgv, "InputSHA256": w.InputSHA256, "ResourcePolicySHA256": w.ResourcePolicySHA256, "Obligations": w.Obligations}
	f := FrozenWave{wave:w, fields:make(map[string]string, len(values))}
	for k,v := range values { b, e := json.Marshal(v); if e != nil { return FrozenWave{}, e }; f.fields[k] = digest(b) }
	b,e := json.Marshal(f.fields); if e != nil { return FrozenWave{},e }; f.seal = digest(b)
	return f,nil
}

type Backend string
const (
	ProcessBackend Backend = "docker"
	KVMBackend Backend = "microvm"
)

// Attempt is an immutable host binding passed to the supervisor adapter.
// Key uses the existing Muse task/attempt/lease tuple. The frozen wave seal is
// recorded separately in the same consumption WAL intention.
type Attempt struct {
	Key string
	ObligationID string
	ParentAttemptID string
	TaskID string
	AttemptID string
	LeaseID string
	Nonce string
	WorkerTask []byte
	InputSHA256 string
	FrameOrder []string
	ResourcePolicySHA256 string
	Backend Backend
	RequiresKVM bool
}
type Candidate struct {
	ID string
	Attempt Attempt
	Route string
	GuestInference bool
	BindMounts []string
	EnvFile string
	RuntimeSocket bool
}
type GenerationRequest struct { Limit int; Cursor uint64; Attempts []AttemptSummary }
type Batch struct { Candidates []Candidate; Done bool }
type Generator interface { Generate(context.Context, FrozenWave, GenerationRequest) (Batch,error) }

type Submission struct { Route string; Wave FrozenWave; Attempt Attempt }
type Execution struct {
	Handle string
	Accepted bool
	Grant []byte
	Usage Cost
}
// Submitter adapts P2-05 to w8muse.Run / the B3 seat socket, never a runner.
// A call is an irreversible send. Reconciliation belongs to Extractor.
type Submitter interface { Submit(context.Context, Submission) (Execution,error) }

// CleanupProof is returned by trusted host code, never decoded from guest JSON.
type CleanupProof struct {
	AttemptKey string
	LeaseID string
	Nonce string
	Known bool
	Verified bool
}
type Extraction struct {
	AttemptKey string
	Nonce string
	HostValidated bool
	A4Validated bool
	Receipt []byte
	Grant []byte
	Payload []byte
	Files map[string][]byte
	Cleanup CleanupProof
	Repros []ReproInput
	Usage Cost
	ResolutionVerified bool
	ResolutionEvidence []byte
}
// Extractor authenticates host collection, A4 probes and purge. It must observe
// ctx and can query an uncertain attempt by its stable key even without a handle.
type Extractor interface { Extract(context.Context, Attempt, Execution) (Extraction,error) }

type ReproBinding struct {
	Repo string
	Commit string
	InputSHA256 string
	GateArgv []string
	FrameOrder []string
}
type ReproInput struct { Binding ReproBinding; Record []byte }
type CanonicalRepro struct { Normalizer string; Key string; Canonical []byte }
// Identity owns the C3 w8-repro/1 normalizer and hash. Harvest never substitutes
// a digest or a length-prefixed encoding for the returned canonical bytes.
type Identity interface { Canonical(context.Context, ReproInput) (CanonicalRepro,error) }
type Witness struct {
	TaskID string
	AttemptID string
	LeaseID string
	Worker string
	Outcome string
	ExitCode int
	ReceiptSHA256 string
	Evidence []byte
}
type Observation struct { Binding ReproBinding; Repro CanonicalRepro; Witness Witness }
type Finding struct {
	ID string
	Binding ReproBinding
	Repro CanonicalRepro
	Witnesses []Witness
	Duplicate bool
}
type LocalReceipt struct { Attempt Attempt; Receipt []byte; Usage Cost; Cleanup CleanupProof }
type Capability struct {
	ConsumerVerifier string
	ConsumerVerifierSHA256 string
	Surface string
	WriterAuthorization string
}
type Publication struct { ID string; Surface string; Provenance string; Finding Finding; Capability Capability }
type PublicationAck struct { ID string; Accepted bool; Surface string; ConsumerVerifierSHA256 string }
type PublicationStatus string
const (
	PublicationPending PublicationStatus = "pending"
	PublicationAccepted PublicationStatus = "accepted"
)
type Reservation struct { Fresh bool; Status PublicationStatus; Publication Publication; Ack PublicationAck }
// Sink must durably collect each receipt independently of finding dedup. Merge
// is an exact-bytes collision bucket plus a witness-set union. Reserve stores an
// immutable publication before a write; an existing reservation is query-only.
type Sink interface {
	Collect(context.Context, LocalReceipt) error
	Merge(context.Context, Observation) (Finding,error)
	Reserve(context.Context, Publication) (Reservation,error)
	Confirm(context.Context, PublicationAck) error
}
type Publisher interface {
	Publish(context.Context, Publication) (PublicationAck,error)
	Reconcile(context.Context, Publication) (PublicationAck,error)
}
type Clock interface { Now() time.Time }

// ConsumptionLog is implemented directly by the existing *wal.LibroConsumo.
// The host supplies that SAME log, not a second harvest lifecycle journal.
type ConsumptionLog interface {
	EscribirIntencion(txID, clave string, detalles any) (uint64,error)
	EscribirDesenlace(txID, clave string, d wal.SrfDesenlace, rc wal.SrfRCMotor, detalles any) (uint64,error)
	EscribirResolucion(clave string, d wal.SrfDesenlace, detalles any) (uint64,error)
}
var _ ConsumptionLog = (*wal.LibroConsumo)(nil)
type History interface { Records(context.Context) ([]wal.Registro,error) }

// WaveBarrier must be shared by all pipelines belonging to one coordinator.
// An unresolved wave keeps its owner token, including between Run calls.
type WaveBarrier struct { mu sync.Mutex; owner *Pipeline; changed chan struct{} }
func NewWaveBarrier() *WaveBarrier { return &WaveBarrier{changed:make(chan struct{})} }
func (b *WaveBarrier) acquire(ctx context.Context, p *Pipeline) error {
	for {
		b.mu.Lock()
		if b.changed == nil { b.changed = make(chan struct{}) }
		if b.owner == nil || b.owner == p { b.owner = p; b.mu.Unlock(); return nil }
		c := b.changed; b.mu.Unlock()
		select { case <-ctx.Done(): return ctx.Err(); case <-c: }
	}
}
func (b *WaveBarrier) release(p *Pipeline) {
	b.mu.Lock(); defer b.mu.Unlock()
	if b.owner == p { b.owner = nil; close(b.changed); b.changed = make(chan struct{}) }
}

type Config struct {
	Wave FrozenWave
	QueueCap map[Stage]int
	MaxAttemptsPerObligation int
	WaveDeadline time.Time
	MaxCandidates int
	Capability *Capability
}
type Ports struct {
	Generator Generator
	Submitter Submitter
	Extractor Extractor
	Identity Identity
	Sink Sink
	Publisher Publisher
	Clock Clock
	Log ConsumptionLog
	History History
	Waves *WaveBarrier
}

type ObligationState string
const (
	Succeeded ObligationState = "Succeeded"
	ExplainedFailure ObligationState = "ExplainedFailure"
	Unresolved ObligationState = "Unresolved"
	MissingEvidence ObligationState = "MissingEvidence"
)
type Cost struct { Microunits *uint64; Unit string; Reason string }
func MissingCost() Cost { return Cost{Reason:"AUSENTE"} }
type Disposition struct { CandidateID string; ObligationID string; AttemptID string; Stage Stage; Code int; Reason string }
type AttemptSummary struct { Key string; ObligationID string; AttemptID string; ParentAttemptID string; State ObligationState; ExitCode int; CapacityHeld bool }
type AttemptRecord struct {
	AttemptSummary
	TaskID string
	LeaseID string
	Submitted bool
	ExecutionObserved bool
	Collected bool
	Usage Cost
	UsageConflict bool
	FailureObserved bool
	ReceiptEvidence [][]byte
	Receipt []byte
}
type ObligationAccount struct { Obligation Obligation; State ObligationState; Attempts []string }
type Rate struct { VerifiedDistinctRepros uint64; WallNanoseconds *int64 }
type Account struct {
	WaveID string
	Obligations []ObligationAccount
	Attempts []AttemptRecord
	Dispositions []Disposition
	GeneratedCandidates uint64
	SubmittedAttempts uint64
	ExecutedAttempts uint64
	Duplicates uint64
	Failures uint64
	Pending uint64
	CapacityHeld uint64
	Accounted bool
	Successful bool
	Closed bool
	FirstAdmitted *time.Time
	CollectedAndCleaned *time.Time
	UsefulHarvest Rate
	TotalCost Cost
	QueuePeaks map[Stage]int
	QueueHeld map[Stage]int
}

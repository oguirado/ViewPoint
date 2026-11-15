package gobernador

import (
	"errors"
	"fmt"
	"math"
	"strings"
	"sync"
	"time"
)

// W8 normalized exit codes per docs/design/W8_WORKER_BANK.md section 6.
// Exit code 1 is forbidden and never emitted.
const (
	W8RCOK          = 0
	W8RCInput       = 2
	W8RCExecution   = 3
	W8RCUnavailable = 4
	W8RCContainment = 5
	W8RCQuarantined = 6
	W8RCOperator    = 7
)

// NormalizeW8RC normalizes any exit code into the closed W8 range {0, 2..7}, never 1.
func NormalizeW8RC(rc int) int {
	if rc == 0 {
		return W8RCOK
	}
	if rc >= 2 && rc <= 7 {
		return rc
	}
	return W8RCContainment
}

// W8DecisionKind represents the categorical admission outcome.
type W8DecisionKind string

const (
	W8DecisionAdmit  W8DecisionKind = "ADMIT"
	W8DecisionRefuse W8DecisionKind = "REFUSE"
	W8DecisionHold   W8DecisionKind = "HOLD"
)

var (
	ErrVectorOverflow             = errors.New("gobernador: resource vector arithmetic overflow or underflow")
	ErrNullOrZeroCeiling          = errors.New("gobernador: null or zero ceiling")
	ErrInvalidPlacement           = errors.New("gobernador: invalid cgroup placement chain")
	ErrMissingTelemetry           = errors.New("gobernador: missing telemetry snapshot")
	ErrStaleTelemetry             = errors.New("gobernador: stale telemetry snapshot")
	ErrNonFiniteTelemetry         = errors.New("gobernador: non-finite telemetry value")
	ErrUnboundedSibling           = errors.New("gobernador: unmatched sibling usage cannot be bounded")
	ErrUnknownReservation         = errors.New("gobernador: unknown reservation id")
	ErrStillQuarantined           = errors.New("gobernador: premature release of quarantined reservation")
	ErrMissingIdempotencyKey      = errors.New("gobernador: missing release idempotency key")
	ErrIdempotencyKeyMismatch     = errors.New("gobernador: release idempotency key mismatch")
	ErrDuplicateReservationID     = errors.New("gobernador: duplicate reservation id")
	ErrStaleDecisionSnapshotMatch = errors.New("gobernador: decision snapshot version mismatch")
)

// W8Error carries a normalized W8 exit code (2..7, never 1) and structured reason.
type W8Error struct {
	RC     int    `json:"rc"`
	Reason string `json:"reason"`
	Detail string `json:"detail"`
}

func (e *W8Error) Error() string {
	if e == nil {
		return ""
	}
	return fmt.Sprintf("gobernador w8 (rc=%d, reason=%s): %s", NormalizeW8RC(e.RC), e.Reason, e.Detail)
}

func w8Err(rc int, reason, detail string) *W8Error {
	return &W8Error{
		RC:     NormalizeW8RC(rc),
		Reason: reason,
		Detail: detail,
	}
}

// W8ExitCode extracts the normalized W8 exit code from an error (0 if nil, 2..7 otherwise, never 1).
func W8ExitCode(err error) int {
	if err == nil {
		return W8RCOK
	}
	var we *W8Error
	if errors.As(err, &we) {
		return NormalizeW8RC(we.RC)
	}
	return W8RCContainment
}

// CheckedAddInt64 performs overflow-checked addition of two non-negative int64 values.
func CheckedAddInt64(a, b int64) (int64, error) {
	if a < 0 || b < 0 {
		return 0, ErrVectorOverflow
	}
	if a > math.MaxInt64-b {
		return 0, ErrVectorOverflow
	}
	return a + b, nil
}

// CheckedSubInt64 performs underflow-checked subtraction of two non-negative int64 values.
func CheckedSubInt64(a, b int64) (int64, error) {
	if a < 0 || b < 0 || b > a {
		return 0, ErrVectorOverflow
	}
	return a - b, nil
}

// ResourceVector is the 4-dimensional checked integer resource vector for W8 admission:
// memory bytes, CPU quota milli-units, PIDs, and open files (nofile).
type ResourceVector struct {
	MemoryBytes int64 `json:"memory_bytes"`
	CPUMilli    int64 `json:"cpu_milli"`
	PIDs        int64 `json:"pids"`
	OpenFiles   int64 `json:"open_files"`
}

// Valid reports whether every dimension is non-negative.
func (v ResourceVector) Valid() bool {
	return v.MemoryBytes >= 0 && v.CPUMilli >= 0 && v.PIDs >= 0 && v.OpenFiles >= 0
}

// StrictlyPositive reports whether every dimension is strictly greater than zero (> 0).
func (v ResourceVector) StrictlyPositive() bool {
	return v.MemoryBytes > 0 && v.CPUMilli > 0 && v.PIDs > 0 && v.OpenFiles > 0
}

// IsZero reports whether all four dimensions are zero.
func (v ResourceVector) IsZero() bool {
	return v.MemoryBytes == 0 && v.CPUMilli == 0 && v.PIDs == 0 && v.OpenFiles == 0
}

// Add returns the component-wise sum of v and other with overflow checking on every dimension.
func (v ResourceVector) Add(other ResourceVector) (ResourceVector, error) {
	mem, err := CheckedAddInt64(v.MemoryBytes, other.MemoryBytes)
	if err != nil {
		return ResourceVector{}, err
	}
	cpu, err := CheckedAddInt64(v.CPUMilli, other.CPUMilli)
	if err != nil {
		return ResourceVector{}, err
	}
	pids, err := CheckedAddInt64(v.PIDs, other.PIDs)
	if err != nil {
		return ResourceVector{}, err
	}
	files, err := CheckedAddInt64(v.OpenFiles, other.OpenFiles)
	if err != nil {
		return ResourceVector{}, err
	}
	return ResourceVector{
		MemoryBytes: mem,
		CPUMilli:    cpu,
		PIDs:        pids,
		OpenFiles:   files,
	}, nil
}

// Sub returns the component-wise difference v - other with underflow checking on every dimension.
func (v ResourceVector) Sub(other ResourceVector) (ResourceVector, error) {
	mem, err := CheckedSubInt64(v.MemoryBytes, other.MemoryBytes)
	if err != nil {
		return ResourceVector{}, err
	}
	cpu, err := CheckedSubInt64(v.CPUMilli, other.CPUMilli)
	if err != nil {
		return ResourceVector{}, err
	}
	pids, err := CheckedSubInt64(v.PIDs, other.PIDs)
	if err != nil {
		return ResourceVector{}, err
	}
	files, err := CheckedSubInt64(v.OpenFiles, other.OpenFiles)
	if err != nil {
		return ResourceVector{}, err
	}
	return ResourceVector{
		MemoryBytes: mem,
		CPUMilli:    cpu,
		PIDs:        pids,
		OpenFiles:   files,
	}, nil
}

// Max returns the component-wise maximum of v and other after validating non-negativity.
func (v ResourceVector) Max(other ResourceVector) (ResourceVector, error) {
	if !v.Valid() || !other.Valid() {
		return ResourceVector{}, ErrVectorOverflow
	}
	res := v
	if other.MemoryBytes > res.MemoryBytes {
		res.MemoryBytes = other.MemoryBytes
	}
	if other.CPUMilli > res.CPUMilli {
		res.CPUMilli = other.CPUMilli
	}
	if other.PIDs > res.PIDs {
		res.PIDs = other.PIDs
	}
	if other.OpenFiles > res.OpenFiles {
		res.OpenFiles = other.OpenFiles
	}
	return res, nil
}

// FitsWithin reports whether v is valid, ceiling is strictly positive, and v <= ceiling in all dimensions.
func (v ResourceVector) FitsWithin(ceiling ResourceVector) bool {
	if !v.Valid() || !ceiling.StrictlyPositive() {
		return false
	}
	return v.MemoryBytes <= ceiling.MemoryBytes &&
		v.CPUMilli <= ceiling.CPUMilli &&
		v.PIDs <= ceiling.PIDs &&
		v.OpenFiles <= ceiling.OpenFiles
}

// CgroupLimit binds a cgroup identifier/path to its per-dimension resource ceiling.
type CgroupLimit struct {
	Cgroup string          `json:"cgroup"`
	Limit  *ResourceVector `json:"limit"`
}

// PlacementSpec names a child cgroup and its ordered ancestor chain, each with its per-dimension limit.
type PlacementSpec struct {
	Child     CgroupLimit   `json:"child"`
	Ancestors []CgroupLimit `json:"ancestors"`
}

func validateCeiling(limit *ResourceVector) error {
	if limit == nil || !limit.StrictlyPositive() {
		return ErrNullOrZeroCeiling
	}
	return nil
}

func validatePlacement(p PlacementSpec) error {
	childName := strings.TrimSpace(p.Child.Cgroup)
	if childName == "" {
		return ErrInvalidPlacement
	}
	if err := validateCeiling(p.Child.Limit); err != nil {
		return err
	}
	if len(p.Ancestors) == 0 {
		return ErrInvalidPlacement
	}
	seen := map[string]bool{childName: true}
	for _, anc := range p.Ancestors {
		name := strings.TrimSpace(anc.Cgroup)
		if name == "" || seen[name] {
			return ErrInvalidPlacement
		}
		if err := validateCeiling(anc.Limit); err != nil {
			return err
		}
		seen[name] = true
	}
	return nil
}

// W8SiblingUsage describes unmatched sibling usage observed under an ancestor cgroup.
type W8SiblingUsage struct {
	Sibling  string          `json:"sibling,omitempty"`
	Observed ResourceVector  `json:"observed"`
	MaxBound *ResourceVector `json:"max_bound,omitempty"`
	Bounded  bool            `json:"bounded"`
}

// W8CgroupTelemetry carries observed resource usage and unmatched sibling state for a cgroup.
type W8CgroupTelemetry struct {
	Observed          ResourceVector   `json:"observed"`
	UnmatchedSiblings []W8SiblingUsage `json:"unmatched_siblings,omitempty"`
	NonFinite         bool             `json:"non_finite,omitempty"`
}

// W8TelemetrySnapshot represents a versioned, monotonic-timestamped telemetry sample.
type W8TelemetrySnapshot struct {
	Version           uint64                       `json:"version"`
	MonotonicNS       int64                        `json:"monotonic_ns"`
	MemAvailableBytes int64                        `json:"mem_available_bytes"`
	CapsuleUsageBytes int64                        `json:"capsule_usage_bytes"`
	CPUPSI10          float64                      `json:"cpu_psi10"`
	NonFinite         bool                         `json:"non_finite,omitempty"`
	Cgroups           map[string]W8CgroupTelemetry `json:"cgroups,omitempty"`
}

// Clone returns a deep copy of the telemetry snapshot.
func (s *W8TelemetrySnapshot) Clone() *W8TelemetrySnapshot {
	if s == nil {
		return nil
	}
	cp := *s
	if s.Cgroups != nil {
		cp.Cgroups = make(map[string]W8CgroupTelemetry, len(s.Cgroups))
		for k, v := range s.Cgroups {
			vcp := v
			if len(v.UnmatchedSiblings) > 0 {
				vcp.UnmatchedSiblings = make([]W8SiblingUsage, len(v.UnmatchedSiblings))
				for i, sib := range v.UnmatchedSiblings {
					scp := sib
					if sib.MaxBound != nil {
						mb := *sib.MaxBound
						scp.MaxBound = &mb
					}
					vcp.UnmatchedSiblings[i] = scp
				}
			}
			cp.Cgroups[k] = vcp
		}
	}
	return &cp
}

// W8TelemetrySource supplies the latest versioned telemetry snapshot.
type W8TelemetrySource func() (*W8TelemetrySnapshot, error)

// W8SnapshotStore is a concurrency-safe telemetry snapshot holder for the W8 adapter.
type W8SnapshotStore struct {
	mu   sync.RWMutex
	snap *W8TelemetrySnapshot
	err  error
}

// NewW8SnapshotStore creates a concurrency-safe telemetry snapshot store.
func NewW8SnapshotStore(initial *W8TelemetrySnapshot) *W8SnapshotStore {
	return &W8SnapshotStore{
		snap: initial.Clone(),
	}
}

// Set updates the stored snapshot and error atomically.
func (s *W8SnapshotStore) Set(snap *W8TelemetrySnapshot, err error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.snap = snap.Clone()
	s.err = err
}

// Source returns a W8TelemetrySource reading from this store.
func (s *W8SnapshotStore) Source() W8TelemetrySource {
	return func() (*W8TelemetrySnapshot, error) {
		s.mu.RLock()
		defer s.mu.RUnlock()
		if s.err != nil {
			return nil, s.err
		}
		return s.snap.Clone(), nil
	}
}

// W8AdmissionRequest specifies a reservation request for the W8 governor adapter.
type W8AdmissionRequest struct {
	ReservationID string         `json:"reservation_id"`
	Clase         string         `json:"clase,omitempty"`
	Resources     ResourceVector `json:"resources"`
	Placement     PlacementSpec  `json:"placement"`
	LockDenied    bool           `json:"lock_denied,omitempty"`
	ParentResID   string         `json:"parent_res_id,omitempty"`
}

// W8ReservationCertificate attests a committed reservation under the governor lock.
type W8ReservationCertificate struct {
	ReservationID   string         `json:"reservation_id"`
	SnapshotVersion uint64         `json:"snapshot_version"`
	SampledAtMonoNS int64          `json:"sampled_at_mono_ns"`
	CommitSeq       uint64         `json:"commit_seq"`
	Resources       ResourceVector `json:"resources"`
	ChildCgroup     string         `json:"child_cgroup"`
	Ancestors       []string       `json:"ancestors"`
	Recomputed      bool           `json:"recomputed"`
}

// W8AdmissionDecision represents the full decision returned by the W8 governor adapter.
type W8AdmissionDecision struct {
	Admitted    bool                      `json:"admitted"`
	Decision    W8DecisionKind            `json:"decision"`
	RC          int                       `json:"rc"`
	Reason      string                    `json:"reason,omitempty"`
	Certificate *W8ReservationCertificate `json:"certificate,omitempty"`
	SrfDecision SrfDecisionAdmision       `json:"srf_decision"`
}

// W8ReleaseResult reports the outcome of a reservation release operation.
type W8ReleaseResult struct {
	ReservationID string `json:"reservation_id"`
	Released      bool   `json:"released"`
	NoOp          bool   `json:"no_op"`
	RC            int    `json:"rc"`
}

// W8ReservationState represents the lifecycle state of a committed reservation.
type W8ReservationState string

const (
	W8StateActive      W8ReservationState = "ACTIVE"
	W8StateQuarantined W8ReservationState = "QUARANTINED"
)

type w8ReservationRecord struct {
	id               string
	clase            string
	resources        ResourceVector
	placement        PlacementSpec
	cgroups          map[string]bool
	parentResID      string
	chargedToHost    bool
	snapshotVersion  uint64
	sampledAtMonoNS  int64
	commitSeq        uint64
	state            W8ReservationState
	rc               int
	quarantineReason string
}

type w8ReleaseRecord struct {
	idempotencyKey string
	verifiedClean  bool
	releasedAtMono int64
}

// W8Config configures the W8 adapter extending SrfGobernador.
type W8Config struct {
	HostCeiling      *ResourceVector
	MaxTelemetryAge  time.Duration
	MemoryFloorBytes int64
	Telemetry        W8TelemetrySource
	NowMono          func() int64
	CgroupLimits     map[string]ResourceVector
	// OnDecisionEvaluated is an optional test seam invoked after decision-time evaluation
	// and before acquiring the governor lock for reservation commit.
	OnDecisionEvaluated func(snapshotVersion uint64, commitSeq uint64)
}

var monotonicEpoch = time.Now()

func defaultNowMono() int64 {
	d := time.Since(monotonicEpoch)
	if d <= 0 {
		return 1
	}
	return d.Nanoseconds()
}

// W8Adapter extends an existing *SrfGobernador with 4D checked integer resource vectors,
// hierarchical cgroup placement, telemetry freshness and snapshot version serialization,
// conservative unmatched sibling accounting, and rc 6 quarantine management.
type W8Adapter struct {
	gov          *SrfGobernador
	cfg          W8Config
	commitSeq    uint64
	reserved     ResourceVector
	reservations map[string]*w8ReservationRecord
	released     map[string]w8ReleaseRecord
}

// NewW8Adapter creates a W8Adapter extending the provided *SrfGobernador.
// It shares the underlying governor's mutex and counters rather than creating a parallel governor.
func NewW8Adapter(gov *SrfGobernador, cfg W8Config) *W8Adapter {
	if cfg.MaxTelemetryAge <= 0 {
		cfg.MaxTelemetryAge = 5 * time.Second
	}
	if cfg.NowMono == nil {
		cfg.NowMono = defaultNowMono
	}
	return &W8Adapter{
		gov:          gov,
		cfg:          cfg,
		reservations: make(map[string]*w8ReservationRecord),
		released:     make(map[string]w8ReleaseRecord),
	}
}

// Governor returns the underlying *SrfGobernador extended by this adapter.
func (a *W8Adapter) Governor() *SrfGobernador {
	return a.gov
}

// ReservedVector returns the current total ResourceVector charged across active and quarantined reservations.
func (a *W8Adapter) ReservedVector() ResourceVector {
	if a == nil || a.gov == nil {
		return ResourceVector{}
	}
	a.gov.mu.Lock()
	defer a.gov.mu.Unlock()
	return a.reserved
}

// CgroupCharge returns the current ResourceVector charged to a specific cgroup (child or ancestor),
// counting nested charges once and never summing them twice.
func (a *W8Adapter) CgroupCharge(cgroup string) (ResourceVector, error) {
	if a == nil || a.gov == nil {
		return ResourceVector{}, w8Err(W8RCInput, "nil_governor", "governor is nil")
	}
	a.gov.mu.Lock()
	defer a.gov.mu.Unlock()
	return a.cgroupReservedLocked(strings.TrimSpace(cgroup))
}

func (a *W8Adapter) cgroupReservedLocked(cgroup string) (ResourceVector, error) {
	var total ResourceVector
	for _, rec := range a.reservations {
		if !rec.cgroups[cgroup] {
			continue
		}
		if rec.parentResID != "" {
			if parent, ok := a.reservations[rec.parentResID]; ok && parent.cgroups[cgroup] {
				// Nested reservation inside an already-charged parent reservation on this cgroup:
				// count the nested charge once, never sum it twice.
				continue
			}
		}
		var err error
		total, err = total.Add(rec.resources)
		if err != nil {
			return ResourceVector{}, err
		}
	}
	return total, nil
}

func (a *W8Adapter) hostReservedWithLegacyLocked() (ResourceVector, error) {
	total := a.reserved

	// Synchronize with any direct SrfGobernador.Admitir calls on a.gov (e.g. legacy memory & CPU at line 249).
	if a.gov.reservado > uint64(math.MaxInt64) {
		return ResourceVector{}, ErrVectorOverflow
	}
	govMem := int64(a.gov.reservado)
	if govMem > total.MemoryBytes {
		total.MemoryBytes = govMem
	}

	if math.IsNaN(a.gov.cpus) || math.IsInf(a.gov.cpus, 0) || a.gov.cpus < 0 {
		return ResourceVector{}, ErrNonFiniteTelemetry
	}
	legacyCPUMilliFloat := math.Round(a.gov.cpus * 1000.0)
	if legacyCPUMilliFloat > float64(math.MaxInt64) {
		return ResourceVector{}, ErrVectorOverflow
	}
	govCPUMilli := int64(legacyCPUMilliFloat)
	if govCPUMilli > total.CPUMilli {
		total.CPUMilli = govCPUMilli
	}

	return total, nil
}

func (a *W8Adapter) validateTelemetryLocked(snap *W8TelemetrySnapshot) *W8Error {
	if snap == nil || snap.Version == 0 || snap.MonotonicNS <= 0 {
		return w8Err(W8RCUnavailable, "missing_telemetry", ErrMissingTelemetry.Error())
	}
	if snap.NonFinite || math.IsNaN(snap.CPUPSI10) || math.IsInf(snap.CPUPSI10, 0) || snap.CPUPSI10 < 0 ||
		snap.MemAvailableBytes < 0 || snap.CapsuleUsageBytes < 0 {
		return w8Err(W8RCUnavailable, "non_finite_telemetry", ErrNonFiniteTelemetry.Error())
	}
	for _, cgTel := range snap.Cgroups {
		if cgTel.NonFinite || !cgTel.Observed.Valid() {
			return w8Err(W8RCUnavailable, "non_finite_telemetry", ErrNonFiniteTelemetry.Error())
		}
		for _, sib := range cgTel.UnmatchedSiblings {
			if !sib.Observed.Valid() || (sib.MaxBound != nil && !sib.MaxBound.Valid()) {
				return w8Err(W8RCUnavailable, "non_finite_telemetry", ErrNonFiniteTelemetry.Error())
			}
		}
	}

	if a.gov.Tel != nil {
		_, err1 := a.gov.Tel.MemDisponible()
		_, err2 := a.gov.Tel.UsoCapsulas()
		psi, err3 := a.gov.Tel.PresionCPU10()
		if err1 != nil || err2 != nil || err3 != nil {
			return w8Err(W8RCUnavailable, "missing_telemetry", "underlying SrfTelemetria error")
		}
		if math.IsNaN(psi) || math.IsInf(psi, 0) || psi < 0 {
			return w8Err(W8RCUnavailable, "non_finite_telemetry", "underlying SrfTelemetria non-finite PSI")
		}
	}

	nowMono := a.cfg.NowMono()
	maxAgeNS := a.cfg.MaxTelemetryAge.Nanoseconds()
	if nowMono <= 0 || maxAgeNS <= 0 || snap.MonotonicNS > nowMono || nowMono-snap.MonotonicNS > maxAgeNS {
		return w8Err(W8RCUnavailable, "stale_telemetry", ErrStaleTelemetry.Error())
	}

	return nil
}

// evaluateUnmatchedSiblingsLocked computes the conservative resource bound for unmatched siblings
// under an ancestor cgroup. If any unmatched sibling usage cannot be bounded, it returns hold=true.
func (a *W8Adapter) evaluateUnmatchedSiblingsLocked(
	cgroup string,
	knownReserved ResourceVector,
	snap *W8TelemetrySnapshot,
) (conservativeBound ResourceVector, hold bool, overflowErr error) {
	if snap == nil || snap.Cgroups == nil {
		return ResourceVector{}, false, nil
	}
	cgTel, ok := snap.Cgroups[cgroup]
	if !ok {
		return ResourceVector{}, false, nil
	}

	for _, sib := range cgTel.UnmatchedSiblings {
		if !sib.Bounded || sib.MaxBound == nil || !sib.MaxBound.StrictlyPositive() {
			return ResourceVector{}, true, nil
		}
		if !sib.Observed.FitsWithin(*sib.MaxBound) {
			return ResourceVector{}, true, nil
		}
		charge, err := sib.Observed.Max(*sib.MaxBound)
		if err != nil {
			return ResourceVector{}, false, err
		}
		conservativeBound, err = conservativeBound.Add(charge)
		if err != nil {
			return ResourceVector{}, false, err
		}
	}

	// Also check if ancestor observed usage exceeds known reservations + bounded siblings:
	// any residual observed usage without a sibling bound is unbounded unmatched sibling usage -> HOLD.
	accounted, err := knownReserved.Add(conservativeBound)
	if err != nil {
		return ResourceVector{}, false, err
	}
	if cgTel.Observed.MemoryBytes > accounted.MemoryBytes ||
		cgTel.Observed.CPUMilli > accounted.CPUMilli ||
		cgTel.Observed.PIDs > accounted.PIDs ||
		cgTel.Observed.OpenFiles > accounted.OpenFiles {
		return ResourceVector{}, true, nil
	}

	return conservativeBound, false, nil
}

func refuseDecision(rc int, reason string) W8AdmissionDecision {
	return W8AdmissionDecision{
		Admitted: false,
		Decision: W8DecisionRefuse,
		RC:       NormalizeW8RC(rc),
		Reason:   reason,
		SrfDecision: SrfDecisionAdmision{
			Admitida: false,
			Motivo:   reason,
		},
	}
}

func holdDecision(reason string) W8AdmissionDecision {
	return W8AdmissionDecision{
		Admitted: false,
		Decision: W8DecisionHold,
		RC:       W8RCUnavailable,
		Reason:   reason,
		SrfDecision: SrfDecisionAdmision{
			Admitida: false,
			Motivo:   reason,
		},
	}
}

// evaluateLocked performs the complete admission evaluation while holding a.gov.mu.
func (a *W8Adapter) evaluateLocked(req W8AdmissionRequest, snap *W8TelemetrySnapshot) W8AdmissionDecision {
	if a == nil || a.gov == nil {
		return refuseDecision(W8RCInput, "nil_governor")
	}

	if strings.TrimSpace(req.ReservationID) == "" {
		return refuseDecision(W8RCInput, "invalid_reservation_id")
	}
	if _, exists := a.reservations[req.ReservationID]; exists {
		return refuseDecision(W8RCInput, "duplicate_reservation_id")
	}
	if _, wasReleased := a.released[req.ReservationID]; wasReleased {
		return refuseDecision(W8RCInput, "duplicate_reservation_id")
	}

	// 1. Validate ceilings and placement before any other check.
	if err := validateCeiling(a.cfg.HostCeiling); err != nil {
		return refuseDecision(W8RCInput, "null_or_zero_ceiling")
	}
	if err := validatePlacement(req.Placement); err != nil {
		if errors.Is(err, ErrNullOrZeroCeiling) {
			return refuseDecision(W8RCInput, "null_or_zero_ceiling")
		}
		return refuseDecision(W8RCInput, "invalid_placement")
	}
	if !req.Resources.StrictlyPositive() {
		return refuseDecision(W8RCInput, "null_or_zero_ceiling")
	}
	if req.ParentResID != "" {
		parent, ok := a.reservations[req.ParentResID]
		if !ok || !req.Resources.FitsWithin(parent.resources) {
			return refuseDecision(W8RCInput, "invalid_placement")
		}
	}

	// 2. Telemetry freshness and finiteness check.
	if werr := a.validateTelemetryLocked(snap); werr != nil {
		return refuseDecision(werr.RC, werr.Reason)
	}

	// 3. Underlying SrfGobernador lock, capsule count, microVM ceiling, PSI, and latency checks.
	if req.LockDenied {
		return refuseDecision(W8RCUnavailable, "candado")
	}
	if a.gov.MaxCapsulas <= 0 || a.gov.vivas >= a.gov.MaxCapsulas {
		return refuseDecision(W8RCUnavailable, "techo")
	}
	if req.Clase == "microvm" && a.gov.microvmVivas >= a.gov.MicrovmActivasMax {
		return refuseDecision(W8RCUnavailable, "techo_microvm")
	}
	if a.gov.UmbralPSI > 0 && snap.CPUPSI10 >= a.gov.UmbralPSI {
		return refuseDecision(W8RCUnavailable, "cpu_psi")
	}
	if a.gov.LatSLA > 0 {
		if a.gov.nlat >= 32 && a.gov.p99() >= a.gov.LatSLA {
			return refuseDecision(W8RCUnavailable, "latencia")
		}
	}

	// 4. Compute host aggregate reserved vector (including any legacy SrfGobernador charges).
	hostReserved, err := a.hostReservedWithLegacyLocked()
	if err != nil {
		return refuseDecision(W8RCInput, "arithmetic_overflow")
	}

	countOnceAtHost := req.ParentResID == ""
	reqForHost := req.Resources
	if !countOnceAtHost {
		reqForHost = ResourceVector{}
	}

	// Close the known gap at gobernador.go line 249: compare accumulated CPUs against the ceiling.
	nextCPU, err := CheckedAddInt64(hostReserved.CPUMilli, reqForHost.CPUMilli)
	if err != nil {
		return refuseDecision(W8RCInput, "arithmetic_overflow")
	}
	if hostReserved.CPUMilli >= a.cfg.HostCeiling.CPUMilli || nextCPU > a.cfg.HostCeiling.CPUMilli {
		return refuseDecision(W8RCUnavailable, "cpu_ceiling")
	}

	// 5. Check Child Cgroup limit per dimension (counting nested charges once).
	childName := strings.TrimSpace(req.Placement.Child.Cgroup)
	childReserved, err := a.cgroupReservedLocked(childName)
	if err != nil {
		return refuseDecision(W8RCInput, "arithmetic_overflow")
	}
	childSiblingBound, childHold, err := a.evaluateUnmatchedSiblingsLocked(childName, childReserved, snap)
	if err != nil {
		return refuseDecision(W8RCInput, "arithmetic_overflow")
	}
	if childHold {
		return holdDecision("unmatched_sibling")
	}
	childBase, err := childReserved.Add(childSiblingBound)
	if err != nil {
		return refuseDecision(W8RCInput, "arithmetic_overflow")
	}
	childNext, err := childBase.Add(req.Resources)
	if err != nil {
		return refuseDecision(W8RCInput, "arithmetic_overflow")
	}
	if !childNext.FitsWithin(*req.Placement.Child.Limit) {
		return refuseDecision(W8RCUnavailable, "child_overflow")
	}
	if regLimit, ok := a.cfg.CgroupLimits[childName]; ok {
		if !regLimit.StrictlyPositive() {
			return refuseDecision(W8RCInput, "null_or_zero_ceiling")
		}
		if !childNext.FitsWithin(regLimit) {
			return refuseDecision(W8RCUnavailable, "child_overflow")
		}
	}

	// 6. Check every ancestor in the ordered ancestor chain per dimension.
	// Nested charges are counted once per reservation in the ancestor's subtree, never summed twice.
	var totalSiblingBound ResourceVector
	seenSiblings := make(map[string]bool)

	for _, anc := range req.Placement.Ancestors {
		ancName := strings.TrimSpace(anc.Cgroup)
		ancReserved, err := a.cgroupReservedLocked(ancName)
		if err != nil {
			return refuseDecision(W8RCInput, "arithmetic_overflow")
		}
		ancSiblingBound, ancHold, err := a.evaluateUnmatchedSiblingsLocked(ancName, ancReserved, snap)
		if err != nil {
			return refuseDecision(W8RCInput, "arithmetic_overflow")
		}
		if ancHold {
			return holdDecision("unmatched_sibling")
		}

		// Accumulate distinct unmatched sibling bounds into host aggregate once.
		if snap != nil && snap.Cgroups != nil {
			if cgTel, ok := snap.Cgroups[ancName]; ok {
				for idx, sib := range cgTel.UnmatchedSiblings {
					key := sib.Sibling
					if key == "" {
						key = fmt.Sprintf("%s#%d", ancName, idx)
					}
					if !seenSiblings[key] {
						seenSiblings[key] = true
						charge, err := sib.Observed.Max(*sib.MaxBound)
						if err != nil {
							return refuseDecision(W8RCInput, "arithmetic_overflow")
						}
						totalSiblingBound, err = totalSiblingBound.Add(charge)
						if err != nil {
							return refuseDecision(W8RCInput, "arithmetic_overflow")
						}
					}
				}
			}
		}

		ancBase, err := ancReserved.Add(ancSiblingBound)
		if err != nil {
			return refuseDecision(W8RCInput, "arithmetic_overflow")
		}
		ancReq := req.Resources
		if req.ParentResID != "" {
			if parent, ok := a.reservations[req.ParentResID]; ok && parent.cgroups[ancName] {
				ancReq = ResourceVector{}
			}
		}
		ancNext, err := ancBase.Add(ancReq)
		if err != nil {
			return refuseDecision(W8RCInput, "arithmetic_overflow")
		}
		if !ancNext.FitsWithin(*anc.Limit) {
			return refuseDecision(W8RCUnavailable, "ancestor_overflow")
		}
		if regLimit, ok := a.cfg.CgroupLimits[ancName]; ok {
			if !regLimit.StrictlyPositive() {
				return refuseDecision(W8RCInput, "null_or_zero_ceiling")
			}
			if !ancNext.FitsWithin(regLimit) {
				return refuseDecision(W8RCUnavailable, "ancestor_overflow")
			}
		}
	}

	// 7. Check host aggregate ceiling per dimension (including conservative sibling bounds).
	hostBase, err := hostReserved.Add(totalSiblingBound)
	if err != nil {
		return refuseDecision(W8RCInput, "arithmetic_overflow")
	}
	hostNext, err := hostBase.Add(reqForHost)
	if err != nil {
		return refuseDecision(W8RCInput, "arithmetic_overflow")
	}
	if !hostNext.FitsWithin(*a.cfg.HostCeiling) {
		if hostNext.CPUMilli > a.cfg.HostCeiling.CPUMilli {
			return refuseDecision(W8RCUnavailable, "cpu_ceiling")
		}
		return refuseDecision(W8RCUnavailable, "ceiling_overflow")
	}

	// 8. Check memory floor / sovereign slack if MemAvailableBytes is reported and floor is configured.
	if snap.MemAvailableBytes > 0 && a.cfg.MemoryFloorBytes > 0 {
		uncommittedGrowth := hostBase.MemoryBytes - snap.CapsuleUsageBytes
		if uncommittedGrowth < 0 {
			uncommittedGrowth = 0
		}
		needed, err := CheckedAddInt64(uncommittedGrowth, reqForHost.MemoryBytes)
		if err != nil {
			return refuseDecision(W8RCInput, "arithmetic_overflow")
		}
		requiredTotal, err := CheckedAddInt64(needed, a.cfg.MemoryFloorBytes)
		if err != nil {
			return refuseDecision(W8RCInput, "arithmetic_overflow")
		}
		if snap.MemAvailableBytes < requiredTotal {
			return refuseDecision(W8RCUnavailable, "ram")
		}
	}

	ancNames := make([]string, len(req.Placement.Ancestors))
	for i, anc := range req.Placement.Ancestors {
		ancNames[i] = strings.TrimSpace(anc.Cgroup)
	}

	cpusFloat := float64(req.Resources.CPUMilli) / 1000.0
	return W8AdmissionDecision{
		Admitted: true,
		Decision: W8DecisionAdmit,
		RC:       W8RCOK,
		Certificate: &W8ReservationCertificate{
			ReservationID:   req.ReservationID,
			SnapshotVersion: snap.Version,
			SampledAtMonoNS: snap.MonotonicNS,
			CommitSeq:       a.commitSeq + 1,
			Resources:       req.Resources,
			ChildCgroup:     childName,
			Ancestors:       ancNames,
		},
		SrfDecision: SrfDecisionAdmision{
			Admitida: true,
			Reserva:  uint64(req.Resources.MemoryBytes),
			CPUs:     cpusFloat,
		},
	}
}

func (a *W8Adapter) readTelemetry() (*W8TelemetrySnapshot, error) {
	if a == nil || a.cfg.Telemetry == nil {
		return nil, ErrMissingTelemetry
	}
	return a.cfg.Telemetry()
}

// Admit evaluates a W8AdmissionRequest at decision time and commits the reservation
// serialized under the governor lock (a.gov.mu). At commit time, it compares the
// snapshot version and commit sequence read at decision time; on mismatch it recomputes
// under the lock and never double-grants.
func (a *W8Adapter) Admit(req W8AdmissionRequest) W8AdmissionDecision {
	if a == nil || a.gov == nil {
		return refuseDecision(W8RCInput, "nil_governor")
	}

	if mutanteSinGobernador {
		a.gov.mu.Lock()
		defer a.gov.mu.Unlock()
		reserva := uint64(4 * 1024 * 1024 * 1024)
		a.gov.reservado += reserva
		a.gov.vivas++
		if req.Clase == "microvm" {
			a.gov.microvmVivas++
		}
		return W8AdmissionDecision{
			Admitted: true,
			Decision: W8DecisionAdmit,
			RC:       W8RCOK,
			SrfDecision: SrfDecisionAdmision{
				Admitida: true,
				Reserva:  reserva,
				CPUs:     float64(req.Resources.CPUMilli) / 1000.0,
			},
		}
	}

	// Step 1: Decision-time snapshot read and evaluation.
	a.gov.mu.Lock()
	decisionSnap, err := a.readTelemetry()
	if err != nil || decisionSnap == nil {
		a.gov.mu.Unlock()
		return refuseDecision(W8RCUnavailable, "missing_telemetry")
	}
	decisionVersion := decisionSnap.Version
	decisionSeq := a.commitSeq
	initialDec := a.evaluateLocked(req, decisionSnap)
	a.gov.mu.Unlock()

	if a.cfg.OnDecisionEvaluated != nil {
		a.cfg.OnDecisionEvaluated(decisionVersion, decisionSeq)
	}

	if !initialDec.Admitted {
		return initialDec
	}

	// Step 2: Serialized reservation commit under the governor lock.
	a.gov.mu.Lock()
	defer a.gov.mu.Unlock()

	commitSnap, err := a.readTelemetry()
	if err != nil || commitSnap == nil {
		return refuseDecision(W8RCUnavailable, "missing_telemetry")
	}

	recomputed := false
	finalDec := initialDec
	if commitSnap.Version != decisionVersion || a.commitSeq != decisionSeq {
		// Mismatch between decision time and commit time: recompute under the governor lock.
		recomputed = true
		finalDec = a.evaluateLocked(req, commitSnap)
		if !finalDec.Admitted {
			return finalDec
		}
	}

	// Commit the reservation atomically under a.gov.mu.
	if err := a.commitReservationLocked(req, finalDec.Certificate, recomputed); err != nil {
		return refuseDecision(W8RCInput, "arithmetic_overflow")
	}

	return finalDec
}

func (a *W8Adapter) commitReservationLocked(
	req W8AdmissionRequest,
	cert *W8ReservationCertificate,
	recomputed bool,
) error {
	countOnce := true
	if req.ParentResID != "" {
		if _, ok := a.reservations[req.ParentResID]; ok {
			countOnce = false
		}
	}

	var nextReserved ResourceVector
	if countOnce {
		var err error
		nextReserved, err = a.reserved.Add(req.Resources)
		if err != nil {
			return err
		}
	} else {
		nextReserved = a.reserved
	}

	cgroups := make(map[string]bool, 1+len(req.Placement.Ancestors))
	cgroups[strings.TrimSpace(req.Placement.Child.Cgroup)] = true
	for _, anc := range req.Placement.Ancestors {
		cgroups[strings.TrimSpace(anc.Cgroup)] = true
	}

	a.commitSeq++
	cert.CommitSeq = a.commitSeq
	cert.Recomputed = recomputed

	a.reserved = nextReserved
	a.reservations[req.ReservationID] = &w8ReservationRecord{
		id:              req.ReservationID,
		clase:           req.Clase,
		resources:       req.Resources,
		placement:       req.Placement,
		cgroups:         cgroups,
		parentResID:     req.ParentResID,
		chargedToHost:   countOnce,
		snapshotVersion: cert.SnapshotVersion,
		sampledAtMonoNS: cert.SampledAtMonoNS,
		commitSeq:       cert.CommitSeq,
		state:           W8StateActive,
		rc:              W8RCOK,
	}

	if countOnce {
		a.gov.reservado += uint64(req.Resources.MemoryBytes)
		a.gov.cpus += float64(req.Resources.CPUMilli) / 1000.0
		a.gov.vivas++
		if req.Clase == "microvm" {
			a.gov.microvmVivas++
		}
	}

	return nil
}

// Quarantine marks an active reservation as quarantined (rc 6).
// A quarantined reservation stays charged in every dimension on its child cgroup,
// every ancestor, and the host governor until an explicit verified-clean release.
func (a *W8Adapter) Quarantine(reservationID string, reason string) error {
	if a == nil || a.gov == nil {
		return w8Err(W8RCInput, "nil_governor", "governor is nil")
	}
	a.gov.mu.Lock()
	defer a.gov.mu.Unlock()

	id := strings.TrimSpace(reservationID)
	rec, ok := a.reservations[id]
	if !ok {
		return w8Err(W8RCInput, "unknown_reservation", ErrUnknownReservation.Error())
	}
	rec.state = W8StateQuarantined
	rec.rc = W8RCQuarantined
	rec.quarantineReason = reason
	return nil
}

// ReservationState returns the current state and normalized rc of a reservation.
func (a *W8Adapter) ReservationState(reservationID string) (W8ReservationState, int, bool) {
	if a == nil || a.gov == nil {
		return "", W8RCInput, false
	}
	a.gov.mu.Lock()
	defer a.gov.mu.Unlock()

	rec, ok := a.reservations[strings.TrimSpace(reservationID)]
	if !ok {
		return "", W8RCInput, false
	}
	return rec.state, rec.rc, true
}

// Release releases a reservation using an explicit idempotency key.
//   - If the reservation is marked quarantined (rc 6) and verifiedClean is false, it refuses
//     and keeps all dimensions charged.
//   - A second release with the same reservationID and idempotencyKey is a no-op.
//   - Release of an unknown reservationID (or with a mismatched/empty idempotency key) refuses.
func (a *W8Adapter) Release(reservationID string, idempotencyKey string, verifiedClean bool) (W8ReleaseResult, error) {
	if a == nil || a.gov == nil {
		return W8ReleaseResult{RC: W8RCInput}, w8Err(W8RCInput, "nil_governor", "governor is nil")
	}
	a.gov.mu.Lock()
	defer a.gov.mu.Unlock()

	id := strings.TrimSpace(reservationID)
	key := strings.TrimSpace(idempotencyKey)
	if id == "" {
		return W8ReleaseResult{RC: W8RCInput}, w8Err(W8RCInput, "unknown_reservation", ErrUnknownReservation.Error())
	}
	if key == "" {
		return W8ReleaseResult{ReservationID: id, RC: W8RCInput}, w8Err(W8RCInput, "missing_idempotency_key", ErrMissingIdempotencyKey.Error())
	}

	if prev, alreadyReleased := a.released[id]; alreadyReleased {
		if prev.idempotencyKey == key {
			return W8ReleaseResult{
				ReservationID: id,
				Released:      false,
				NoOp:          true,
				RC:            W8RCOK,
			}, nil
		}
		return W8ReleaseResult{
			ReservationID: id,
			RC:            W8RCInput,
		}, w8Err(W8RCInput, "idempotency_key_mismatch", ErrIdempotencyKeyMismatch.Error())
	}

	rec, ok := a.reservations[id]
	if !ok {
		return W8ReleaseResult{
			ReservationID: id,
			RC:            W8RCInput,
		}, w8Err(W8RCInput, "unknown_reservation", ErrUnknownReservation.Error())
	}

	if rec.state == W8StateQuarantined && !verifiedClean {
		return W8ReleaseResult{
			ReservationID: id,
			RC:            W8RCContainment,
		}, w8Err(W8RCContainment, "premature_quarantine_release", ErrStillQuarantined.Error())
	}

	// Refuse releasing a parent reservation while a nested child reservation is still active or quarantined.
	for _, other := range a.reservations {
		if other.parentResID == id {
			return W8ReleaseResult{
				ReservationID: id,
				RC:            W8RCContainment,
			}, w8Err(W8RCContainment, "nested_child_active", "cannot release parent with active nested reservation")
		}
	}

	if rec.chargedToHost {
		nextReserved, err := a.reserved.Sub(rec.resources)
		if err != nil {
			return W8ReleaseResult{
				ReservationID: id,
				RC:            W8RCContainment,
			}, w8Err(W8RCContainment, "arithmetic_underflow", err.Error())
		}
		a.reserved = nextReserved

		memBytes := uint64(rec.resources.MemoryBytes)
		if a.gov.reservado >= memBytes {
			a.gov.reservado -= memBytes
		} else {
			a.gov.reservado = 0
		}

		cpusFloat := float64(rec.resources.CPUMilli) / 1000.0
		if a.gov.cpus >= cpusFloat {
			a.gov.cpus -= cpusFloat
		} else {
			a.gov.cpus = 0
		}

		if a.gov.vivas > 0 {
			a.gov.vivas--
		}
		if rec.clase == "microvm" && a.gov.microvmVivas > 0 {
			a.gov.microvmVivas--
		}
	}

	delete(a.reservations, id)
	a.released[id] = w8ReleaseRecord{
		idempotencyKey: key,
		verifiedClean:  verifiedClean,
		releasedAtMono: a.cfg.NowMono(),
	}
	a.commitSeq++

	return W8ReleaseResult{
		ReservationID: id,
		Released:      true,
		NoOp:          false,
		RC:            W8RCOK,
	}, nil
}

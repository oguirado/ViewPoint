// Package govbridge joins pool's synchronous accounting port to auction's
// verified host admission gateway. Neither selection nor a pool entry is a
// lease. A trusted host must supply the distinct authenticated release port.
package govbridge

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"math/big"
	"sync"

	"opensuperficies.local/srf/src/w8wave/auction"
	"opensuperficies.local/srf/src/w8wave/pool"
)

const (
	HardWaveCapacity    uint64 = 64
	HardSeatConcurrency uint64 = 2
)

// HostPort must be installed by the trusted host, not by the bidder or the
// selector. Reserve is auction.Governor's *admission* method, not ReleaseHold.
// A failed Reserve must have no effect, or the host must durably fence any
// in-doubt reservation: the auction interface has no rollback primitive.
// CheckCurrent independently attests a still-live reservation and exact pool
// entry generation. CheckCleanup must validate its own host cleanup record;
// it MUST NOT trust the caller's Verified flag as authentication. ReleaseHold
// must be durably idempotent for the tuple (ID, generation, release key).
type HostPort interface {
	auction.Governor
	CheckCurrent(context.Context, auction.Reservation, string, uint64) error
	CheckCleanup(context.Context, auction.Reservation, string, uint64, pool.CleanupProof, string) error
	// VerifyNoHold independently proves a failed/malformed admission caused
	// no host allocation, including allocations under unexpected host IDs.
	// Failure to prove this causes a conservative, unreleasable local hold.
	VerifyNoHold(context.Context, string, auction.Award, uint64) error
	ReleaseHold(context.Context, auction.Reservation, string, uint64, string) error
}

// Config must be explicit: zero is unavailable, never unbounded. Resource
// ceilings are in bytes, millicores, processes and file descriptors.
type Config struct {
	Ceiling         pool.Vec
	WaveCapacity    uint64
	SeatConcurrency uint64
}

type prepared struct {
	ctx    context.Context
	award  auction.Award
	epoch  auction.Epoch
	vector pool.Vec
}

type permit struct {
	ctx        context.Context
	generation uint64
	key        string
}

type reservation struct {
	value         pool.Vec
	host          auction.Reservation
	generation    uint64
	release       *permit
	released      bool
	indeterminate bool
	releaseKey    string
}

// DecimalVec holds exact nonnegative cumulative totals; decimals do not wrap
// when the sum of released waves exceeds uint64, unlike a live capacity hold.
type DecimalVec struct {
	MemBytes string
	CPUMilli string
	PIDs     string
	Files    string
}

// Balance reports observed, not configured, admitted/released resource totals.
// Pending proposals are not charged and are never leases.
type Balance struct {
	Held          pool.Vec
	Released      DecimalVec
	Reservations  uint64
	Releases      uint64
	Pending       uint64
	ActiveSeats   uint64
	HostAttempts  uint64
	HostAdmits    uint64
	HostReleases  uint64
	Indeterminate uint64
}

// Adapter is an in-memory continuity guard; host persistence/recovery is a
// separate required capability and cannot be inferred from these Go ports.
// Its mutex serializes admission and release across competing pool instances.
type Adapter struct {
	mu           sync.Mutex
	config       Config
	host         HostPort
	staged       map[string]prepared
	entries      map[string]*reservation
	held         pool.Vec
	released     [4]big.Int
	admitted     uint64
	releases     uint64
	hostAttempts uint64
	hostAdmits   uint64
	hostReleases uint64
}

var _ pool.Governor = (*Adapter)(nil)

func fault(rc int, reason string) error { return &pool.Fault{RC: rc, Reason: reason} }

func New(config Config, host HostPort) (*Adapter, error) {
	v := config.Ceiling
	if v.MemBytes == 0 || v.CPUMilli == 0 || v.PIDs == 0 || v.Files == 0 ||
		config.WaveCapacity == 0 || config.SeatConcurrency == 0 {
		return nil, fault(4, "configured capacity unavailable")
	}
	if config.WaveCapacity > HardWaveCapacity || config.SeatConcurrency > HardSeatConcurrency ||
		config.SeatConcurrency > config.WaveCapacity {
		return nil, fault(2, "invalid wave or seat ceiling")
	}
	if host == nil {
		return nil, auction.ErrDenied
	}
	return &Adapter{config: config, host: host, staged: make(map[string]prepared), entries: make(map[string]*reservation)}, nil
}

// ToAuction/ToPool convert *named* unsigned dimensions; there are no casts,
// implicit scales, positional reinterpretations or CPU↔memory unit guesses.
func ToAuction(v pool.Vec) auction.Vec {
	return auction.Vec{MemBytes: v.MemBytes, CPUMilli: v.CPUMilli, PIDs: v.PIDs, Files: v.Files}
}
func ToPool(v auction.Vec) pool.Vec {
	return pool.Vec{MemBytes: v.MemBytes, CPUMilli: v.CPUMilli, PIDs: v.PIDs, Files: v.Files}
}

func Add(a, b pool.Vec) (pool.Vec, error) {
	if b.MemBytes > math.MaxUint64-a.MemBytes ||
		b.CPUMilli > math.MaxUint64-a.CPUMilli ||
		b.PIDs > math.MaxUint64-a.PIDs ||
		b.Files > math.MaxUint64-a.Files {
		return pool.Vec{}, auction.ErrOverflow
	}
	return pool.Vec{
		MemBytes: a.MemBytes + b.MemBytes,
		CPUMilli: a.CPUMilli + b.CPUMilli,
		PIDs:     a.PIDs + b.PIDs,
		Files:    a.Files + b.Files,
	}, nil
}

// SumCertificate checks every dimension before any host admission call.
// VerifyFull separately checks that the members are exactly the ones offered.
func SumCertificate(a auction.Award) (pool.Vec, error) {
	var total pool.Vec
	for _, member := range a.Certificate {
		var err error
		total, err = Add(total, ToPool(member.Member.Vec))
		if err != nil {
			return pool.Vec{}, err
		}
	}
	return total, nil
}

func fits(v, cap pool.Vec) bool {
	return v.MemBytes <= cap.MemBytes && v.CPUMilli <= cap.CPUMilli &&
		v.PIDs <= cap.PIDs && v.Files <= cap.Files
}
func subtract(a, b pool.Vec) pool.Vec {
	return pool.Vec{MemBytes: a.MemBytes - b.MemBytes, CPUMilli: a.CPUMilli - b.CPUMilli,
		PIDs: a.PIDs - b.PIDs, Files: a.Files - b.Files}
}

// Stage freezes the proposal until pool.Reserve makes its version-fenced
// capacity hold. Stage never contacts the host and never grants a lease.
func (a *Adapter) Stage(ctx context.Context, id string, vector pool.Vec, award auction.Award, epoch auction.Epoch) error {
	if ctx == nil || id == "" || vector == (pool.Vec{}) {
		return fault(2, "stage requires context, ID and vector")
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	// Deep copy both documents so caller mutation cannot change the future
	// admission certificate after verification.
	encoded, err := json.Marshal(struct {
		Award auction.Award
		Epoch auction.Epoch
	}{award, epoch})
	if err != nil {
		return fmt.Errorf("%w: encode staged award: %v", auction.ErrInvalid, err)
	}
	var copy struct {
		Award auction.Award
		Epoch auction.Epoch
	}
	if err = json.Unmarshal(encoded, &copy); err != nil {
		return fmt.Errorf("%w: decode staged award: %v", auction.ErrInvalid, err)
	}
	if copy.Award.Versions != copy.Epoch.Versions {
		return errors.Join(fault(5, "stale award versions"), auction.ErrConflict)
	}
	total, err := SumCertificate(copy.Award)
	if err != nil {
		return err
	}
	if total != vector {
		return fault(2, "pool and auction resource dimensions disagree")
	}
	if err = auction.VerifyFull(copy.Award, copy.Epoch); err != nil {
		return err
	}
	if !fits(vector, a.config.Ceiling) {
		return fault(4, "requested resources exceed configured capacity")
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if _, ok := a.staged[id]; ok {
		return fault(5, "proposal ID already staged")
	}
	if _, ok := a.entries[id]; ok {
		return fault(5, "reservation ID already used")
	}
	if uint64(len(a.staged)+len(a.entries)) >= a.config.WaveCapacity {
		return fault(4, "wave capacity exhausted")
	}
	a.staged[id] = prepared{ctx: ctx, vector: vector, award: copy.Award, epoch: copy.Epoch}
	return nil
}

// quarantineAdmission retains capacity even if the pool cannot install its
// entry because the host's reply was indeterminate. Only the trusted host can
// reconcile this orphan; there is intentionally no speculative release API.
func (a *Adapter) quarantineAdmission(id string, v pool.Vec, host auction.Reservation, charged pool.Vec) {
	a.entries[id] = &reservation{value: v, host: host, generation: 1, indeterminate: true}
	a.held = charged
	delete(a.staged, id)
}

// CancelStage discards only a proposal which never entered admission; it
// cannot cancel or refund any host reservation, including shadow holds.
func (a *Adapter) CancelStage(id string) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	if _, ok := a.staged[id]; !ok {
		return fault(5, "no uncommitted proposal")
	}
	delete(a.staged, id)
	return nil
}

func (a *Adapter) seats() uint64 {
	var n uint64
	for _, r := range a.entries {
		if !r.released {
			n++
		}
	}
	return n
}

// Reserve implements pool.Governor. All pre-admission capacity checks happen
// before auction.Commit's full validation and sole host Reserve call. The host
// reservation must echo the exact staged ID and attest generation 1. A failure
// never makes a local entry or returns capacity, and must be reconciled by the
// host according to the HostPort contract if admission became indeterminate.
func (a *Adapter) Reserve(id string, v pool.Vec) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	p, ok := a.staged[id]
	if !ok {
		return fault(5, "unstaged reservation ID")
	}
	if id == "" || v == (pool.Vec{}) || p.vector != v {
		return fault(2, "reservation identity or vector mismatch")
	}
	if err := p.ctx.Err(); err != nil {
		delete(a.staged, id) // no host call occurred; free the uncharged proposal slot
		return err
	}
	if a.seats() >= a.config.SeatConcurrency {
		return fault(4, "seat concurrency exhausted")
	}
	next, err := Add(a.held, v)
	if err != nil {
		return err
	}
	if !fits(next, a.config.Ceiling) {
		return fault(4, "host resource ceiling exhausted")
	}
	// Cancellation during a host transaction cannot implicitly revoke an
	// already-created hold. The host must finish or conservatively fence it.
	a.hostAttempts++
	hostCtx := context.WithoutCancel(p.ctx)
	r, err := auction.Commit(hostCtx, a.host, p.award, p.epoch)
	if err != nil {
		if proofErr := a.host.VerifyNoHold(hostCtx, id, p.award, p.epoch.Versions.Snapshot); proofErr != nil {
			a.quarantineAdmission(id, v, r, next)
			return errors.Join(fault(6, "admission indeterminate; host capacity quarantined"), err, proofErr)
		}
		return err
	}
	if r.ID != id {
		if proofErr := a.host.VerifyNoHold(hostCtx, id, p.award, p.epoch.Versions.Snapshot); proofErr != nil {
			a.quarantineAdmission(id, v, r, next)
			return errors.Join(fault(6, "admission indeterminate; host capacity quarantined"), auction.ErrDenied, proofErr)
		}
		return auction.ErrDenied
	}
	if err = a.host.CheckCurrent(hostCtx, r, id, 1); err != nil {
		if proofErr := a.host.VerifyNoHold(hostCtx, id, p.award, p.epoch.Versions.Snapshot); proofErr != nil {
			a.quarantineAdmission(id, v, r, next)
			return errors.Join(fault(6, "admission indeterminate; host capacity quarantined"), err, proofErr)
		}
		return errors.Join(fault(5, "host reservation not current or generation-bound"), err)
	}
	a.entries[id] = &reservation{value: v, host: r, generation: 1}
	a.held = next
	a.admitted++
	a.hostAdmits++
	delete(a.staged, id)
	return nil
}

// BindGeneration must follow the real pool Claim and an independent host
// generation update. Arbitrary caller-supplied generation changes are never
// accepted without current host reservation authority.
func (a *Adapter) BindGeneration(ctx context.Context, id string, generation uint64) error {
	if ctx == nil || id == "" || generation == 0 {
		return fault(2, "generation and context required")
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	r, ok := a.entries[id]
	if !ok || r.released || r.indeterminate {
		return fault(5, "unknown, quarantined or released reservation")
	}
	if generation < r.generation || generation > r.generation+1 || r.generation == math.MaxUint64 {
		return fault(5, "stale or skipped generation")
	}
	if err := a.host.CheckCurrent(ctx, r.host, id, generation); err != nil {
		return errors.Join(fault(5, "generation authority refused"), err)
	}
	r.generation = generation
	return nil
}

// AuthorizeRelease stages an independent host-verified cleanup capability.
// pool.Release still checks its own ordered cleanup state and witness; this
// port supplies the independent security authority absent from pool.Governor.
func (a *Adapter) AuthorizeRelease(ctx context.Context, id string, proof pool.CleanupProof, key string) error {
	if ctx == nil || id == "" || key == "" {
		return fault(2, "cleanup context, ID and release key required")
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	r, ok := a.entries[id]
	if !ok {
		return fault(5, "missing host reservation")
	}
	if r.indeterminate {
		return fault(6, "admission quarantine requires trusted host reconciliation")
	}
	if r.released {
		if r.releaseKey == key {
			return nil
		}
		return fault(5, "already released with another key")
	}
	if proof == nil || !proof.Verified || proof.EntryID != id || proof.Generation != r.generation {
		return fault(5, "missing or stale cleanup witness")
	}
	if r.release != nil {
		if r.release.key == key && r.release.generation == r.generation {
			return nil
		}
		return fault(5, "conflicting release capability")
	}
	if err := a.host.CheckCurrent(ctx, r.host, id, r.generation); err != nil {
		return errors.Join(fault(5, "host reservation authority expired"), err)
	}
	if err := a.host.CheckCleanup(ctx, r.host, id, r.generation, proof, key); err != nil {
		return errors.Join(fault(6, "cleanup indeterminate; reservation stays charged"), err)
	}
	r.release = &permit{ctx: context.WithoutCancel(ctx), generation: r.generation, key: key}
	return nil
}

// Release is the pool.Governor method: there is intentionally no equivalent
// method on auction.Governor. Host release requires a *previously* authenticated
// capability and an exact reservation/generation binding.
func (a *Adapter) Release(id string, v pool.Vec) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	r, ok := a.entries[id]
	if !ok || r.released || r.indeterminate {
		return fault(5, "unknown, quarantined or already released hold")
	}
	if r.value != v {
		return fault(5, "release vector differs from admitted hold")
	}
	if r.release == nil || r.release.generation != r.generation {
		return fault(6, "no independently verified cleanup capability")
	}
	// Recheck at the last moment; a changed host lease must never be freed.
	if err := a.host.CheckCurrent(r.release.ctx, r.host, id, r.generation); err != nil {
		return errors.Join(fault(5, "host reservation stale at release"), err)
	}
	if err := a.host.ReleaseHold(r.release.ctx, r.host, id, r.generation, r.release.key); err != nil {
		return errors.Join(fault(6, "host release indeterminate; hold retained"), err)
	}
	a.held = subtract(a.held, r.value)
	a.released[0].Add(&a.released[0], new(big.Int).SetUint64(v.MemBytes))
	a.released[1].Add(&a.released[1], new(big.Int).SetUint64(v.CPUMilli))
	a.released[2].Add(&a.released[2], new(big.Int).SetUint64(v.PIDs))
	a.released[3].Add(&a.released[3], new(big.Int).SetUint64(v.Files))
	r.released = true
	r.releaseKey = r.release.key
	r.release = nil
	a.releases++
	a.hostReleases++
	return nil
}

func (a *Adapter) Snapshot() Balance {
	a.mu.Lock()
	defer a.mu.Unlock()
	var uncertain uint64
	for _, r := range a.entries {
		if r.indeterminate {
			uncertain++
		}
	}
	return Balance{
		Held:         a.held,
		Released:     DecimalVec{a.released[0].String(), a.released[1].String(), a.released[2].String(), a.released[3].String()},
		Reservations: a.admitted, Releases: a.releases, Pending: uint64(len(a.staged)),
		ActiveSeats: a.seats(), HostAttempts: a.hostAttempts, HostAdmits: a.hostAdmits, HostReleases: a.hostReleases, Indeterminate: uncertain,
	}
}

// ManagedPool keeps the keyless pool.Governor port private. All release calls
// flow through the same authenticated key and proof; exposing the bare *Pool
// would permit a caller to supply a different pool tombstone key than the host
// release key, which that legacy port cannot observe.
type ManagedPool struct {
	pool    *pool.Pool
	adapter *Adapter
}

func NewManagedPool(config Config, host HostPort, boot pool.BootPermit, cleaner pool.Cleaner, clock pool.Clock, pin string) (*ManagedPool, error) {
	a, err := New(config, host)
	if err != nil {
		return nil, err
	}
	p, err := pool.NewPool(config.Ceiling, a, boot, cleaner, clock, pin)
	if err != nil {
		return nil, err
	}
	return &ManagedPool{pool: p, adapter: a}, nil
}

func (m *ManagedPool) Stage(ctx context.Context, id string, vector pool.Vec, award auction.Award, epoch auction.Epoch) error {
	return m.adapter.Stage(ctx, id, vector, award, epoch)
}
func (m *ManagedPool) CancelStage(id string) error { return m.adapter.CancelStage(id) }
func (m *ManagedPool) Reserve(id string, vector pool.Vec, expectedVersion uint64) error {
	return m.pool.Reserve(id, vector, expectedVersion)
}
func (m *ManagedPool) ColdReady(id string, template pool.Template) error {
	return m.pool.ColdReady(id, template)
}
func (m *ManagedPool) TemplateReady(id string, template pool.Template) error {
	return m.pool.TemplateReady(id, template)
}
func (m *ManagedPool) Claim(id string, expectedVersion uint64) (pool.Lease, error) {
	return m.pool.Claim(id, expectedVersion)
}
func (m *ManagedPool) BindGeneration(ctx context.Context, id string, generation uint64) error {
	return m.adapter.BindGeneration(ctx, id, generation)
}
func (m *ManagedPool) MarkDirty(id string) error                   { return m.pool.MarkDirty(id) }
func (m *ManagedPool) Fence(id string) error                       { return m.pool.Fence(id) }
func (m *ManagedPool) Collect(id string) error                     { return m.pool.Collect(id) }
func (m *ManagedPool) Stop(id string) error                        { return m.pool.Stop(id) }
func (m *ManagedPool) Purge(id string) error                       { return m.pool.Purge(id) }
func (m *ManagedPool) Verify(id string) (pool.CleanupProof, error) { return m.pool.Verify(id) }
func (m *ManagedPool) Quarantine(id string) error                  { return m.pool.Quarantine(id) }
func (m *ManagedPool) Snapshot() pool.PoolSnapshot                 { return m.pool.Snapshot() }
func (m *ManagedPool) Balance() Balance                            { return m.adapter.Snapshot() }

// Release is the only public release entry point of ManagedPool. The host and
// pool receive one identical idempotency key. No cancellation releases a hold.
func (m *ManagedPool) Release(ctx context.Context, id string, proof pool.CleanupProof, key string) error {
	if err := m.adapter.AuthorizeRelease(ctx, id, proof, key); err != nil {
		return err
	}
	return m.pool.Release(id, proof, key)
}

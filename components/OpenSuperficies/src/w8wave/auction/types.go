// Package auction is a deterministic, offline, host-brokered posted-price
// pack selector. Selection never creates an admission or a lease: the host
// Governor is the sole reservation authority.
package auction

import (
	"context"
	"errors"
	"fmt"
	"math"
	"math/bits"
)

var (
	ErrHold     = errors.New("auction: HOLD (closed pressure gate or exhausted work budget)")
	ErrAbstain  = errors.New("auction: ABSTAIN (no fully verified feasible award)")
	ErrInvalid  = errors.New("auction: invalid input or certificate")
	ErrOverflow = errors.New("auction: checked arithmetic overflow")
	ErrConflict = errors.New("auction: stale version or conflicting replay")
	ErrDenied   = errors.New("auction: admission authority denied")
)

// Vec is an unsigned resource vector. All arithmetic is checked.
type Vec struct {
	MemBytes uint64 `json:"mem_bytes"`
	CPUMilli uint64 `json:"cpu_milli"`
	PIDs     uint64 `json:"pids"`
	Files    uint64 `json:"files"`
}

func add(a, b uint64) (uint64, error) {
	if a > math.MaxUint64-b {
		return 0, ErrOverflow
	}
	return a + b, nil
}
func subtract(a, b uint64) (uint64, error) {
	if a < b {
		return 0, ErrInvalid
	}
	return a - b, nil
}
func multiply(a, b uint64) (uint64, error) {
	hi, lo := bits.Mul64(a, b)
	if hi != 0 {
		return 0, ErrOverflow
	}
	return lo, nil
}
func (v Vec) Add(o Vec) (Vec, error) {
	var x Vec
	var err error
	if x.MemBytes, err = add(v.MemBytes, o.MemBytes); err != nil {
		return Vec{}, err
	}
	if x.CPUMilli, err = add(v.CPUMilli, o.CPUMilli); err != nil {
		return Vec{}, err
	}
	if x.PIDs, err = add(v.PIDs, o.PIDs); err != nil {
		return Vec{}, err
	}
	if x.Files, err = add(v.Files, o.Files); err != nil {
		return Vec{}, err
	}
	return x, nil
}
func (v Vec) Sub(o Vec) (Vec, error) {
	var x Vec
	var err error
	if x.MemBytes, err = subtract(v.MemBytes, o.MemBytes); err != nil {
		return Vec{}, err
	}
	if x.CPUMilli, err = subtract(v.CPUMilli, o.CPUMilli); err != nil {
		return Vec{}, err
	}
	if x.PIDs, err = subtract(v.PIDs, o.PIDs); err != nil {
		return Vec{}, err
	}
	if x.Files, err = subtract(v.Files, o.Files); err != nil {
		return Vec{}, err
	}
	return x, nil
}
func (v Vec) Fits(cap Vec) bool {
	return v.MemBytes <= cap.MemBytes && v.CPUMilli <= cap.CPUMilli && v.PIDs <= cap.PIDs && v.Files <= cap.Files
}
func (v Vec) Empty() bool { return v == (Vec{}) }

// Rat represents a nonnegative rational. A zero denominator is invalid.
// Both 64-bit cross-products are compared as exact 128-bit integer pairs.
type Rat struct {
	Num uint64 `json:"num"`
	Den uint64 `json:"den"`
}

func (r Rat) Validate() error {
	if r.Den == 0 {
		return fmt.Errorf("%w: zero denominator", ErrInvalid)
	}
	return nil
}
func (r Rat) Cmp(other Rat) (int, error) {
	if err := r.Validate(); err != nil {
		return 0, err
	}
	if err := other.Validate(); err != nil {
		return 0, err
	}
	ah, al := bits.Mul64(r.Num, other.Den)
	bh, bl := bits.Mul64(other.Num, r.Den)
	if ah < bh || (ah == bh && al < bl) {
		return -1, nil
	}
	if ah > bh || (ah == bh && al > bl) {
		return 1, nil
	}
	return 0, nil
}

// Interval is a half-open interval of discrete host ticks.
type Interval struct {
	StartTick uint64 `json:"start_tick"`
	EndTick   uint64 `json:"end_tick"`
}

func (i Interval) validate() error {
	if i.EndTick <= i.StartTick {
		return fmt.Errorf("%w: empty interval", ErrInvalid)
	}
	return nil
}

// Member is an indivisible capsule inside an economic pack.
type Member struct {
	ID      string `json:"id"`
	Profile string `json:"profile"`
	Arch    string `json:"arch"`
	Backend string `json:"backend"`
	Vec     Vec    `json:"vec"`
}

type Alternative struct {
	ID                string     `json:"id"`
	ValueMicro        uint64     `json:"value_micro"`
	Members           []Member   `json:"members"`
	Intervals         []Interval `json:"intervals"`
	MaxLiabilityMicro uint64     `json:"max_liability_micro"`
}

type PackBid struct {
	Principal         string        `json:"principal"`
	Room              string        `json:"room"`
	EconomicGroup     string        `json:"economic_group"`
	MembershipVersion uint64        `json:"membership_version"`
	BidID             string        `json:"bid_id"`
	Revision          uint64        `json:"revision"`
	SuiteSHA256       string        `json:"suite_sha256"`
	PolicyEpoch       uint64        `json:"policy_epoch"`
	WalletAuth        string        `json:"wallet_auth"`
	EarliestStartTick uint64        `json:"earliest_start_tick"`
	BidExpiryTick     uint64        `json:"bid_expiry_tick"`
	DeadlineTick      uint64        `json:"deadline_tick"`
	Alternatives      []Alternative `json:"alternatives"`
}

// Limits are hard upper bounds independent of untrusted menu choices. Zero
// values use the smaller, hard-coded defaults (never mean unbounded).
type Limits struct {
	MaxInputBytes    uint64 `json:"max_input_bytes"`
	MaxBids          uint64 `json:"max_bids"`
	MaxAlternatives  uint64 `json:"max_alternatives"`
	MaxMembers       uint64 `json:"max_members"`
	MaxHorizonPoints uint64 `json:"max_horizon_points"`
	MaxWork          uint64 `json:"max_work"`
}

func (l Limits) bounded() Limits {
	apply := func(v, cap uint64) uint64 {
		if v == 0 || v > cap {
			return cap
		}
		return v
	}
	return Limits{apply(l.MaxInputBytes, 1<<20), apply(l.MaxBids, 128), apply(l.MaxAlternatives, 256), apply(l.MaxMembers, 1024), apply(l.MaxHorizonPoints, 4096), apply(l.MaxWork, 1000000)}
}

// Versions make award/quote binding explicit. Changing any version voids it.
type Versions struct {
	Snapshot   uint64 `json:"snapshot"`
	Wallet     uint64 `json:"wallet"`
	Membership uint64 `json:"membership"`
	Lease      uint64 `json:"lease"`
	Policy     uint64 `json:"policy"`
}

type CapacityPoint struct {
	Tick      uint64 `json:"tick"`
	Remaining Vec    `json:"remaining"`
}

type WalletAccount struct {
	Principal                  string `json:"principal"`
	Room                       string `json:"room"`
	AvailableMicro             uint64 `json:"available_micro"`
	ApprovedCapMicro           uint64 `json:"approved_cap_micro"`
	SettledMicro               uint64 `json:"settled_micro"`
	OutstandingMicro           uint64 `json:"outstanding_micro"`
	ProtectedFinalizationMicro uint64 `json:"protected_finalization_micro"`
}

// Quote is a fixed posted price, never a dynamically raised winning bid.
type Quote struct {
	AlternativeID string        `json:"alternative_id"`
	AmountMicro   uint64        `json:"amount_micro"`
	HostScope     string        `json:"host_scope"`
	AtTick        uint64        `json:"at_tick"`
	Versions      Versions      `json:"versions"`
	Hold          bool          `json:"hold"`
	Next          PressureLatch `json:"next"`
}

type QuotedAlternative struct {
	BidID         string `json:"bid_id"`
	AlternativeID string `json:"alternative_id"`
	Quote         Quote  `json:"quote"`
}

// Epoch contains all trusted, immutable inputs. Select does not mutate it.
type Epoch struct {
	NowTick       uint64              `json:"now_tick"`
	HostScope     string              `json:"host_scope"`
	Versions      Versions            `json:"versions"`
	Capacity      []CapacityPoint     `json:"capacity"`
	Bids          []PackBid           `json:"bids"`
	Quotes        []QuotedAlternative `json:"quotes"`
	Wallets       []WalletAccount     `json:"wallets"`
	DemandWeights Vec                 `json:"demand_weights"`
	Limits        Limits              `json:"limits"`
}

// CertificateMember binds every selected capsule to its complete reservation.
type CertificateMember struct {
	BidID         string     `json:"bid_id"`
	AlternativeID string     `json:"alternative_id"`
	Member        Member     `json:"member"`
	Intervals     []Interval `json:"intervals"`
}

type Pick struct {
	BidID         string `json:"bid_id"`
	AlternativeID string `json:"alternative_id"`
	QuoteMicro    uint64 `json:"quote_micro"`
}

// Award is only a proposal. Quality must be "heuristic"; there is no optimum
// claim, VCG computation, capability token, lease or resource grant here.
type Award struct {
	Quality         string              `json:"quality"`
	Picks           []Pick              `json:"picks"`
	Certificate     []CertificateMember `json:"certificate"`
	TotalValueMicro uint64              `json:"total_value_micro"`
	TotalQuoteMicro uint64              `json:"total_quote_micro"`
	Versions        Versions            `json:"versions"`
	HostScope       string              `json:"host_scope"`
	Hold            bool                `json:"hold"`
}

type Reservation struct {
	ID                string `json:"id"`
	SnapshotVersion   uint64 `json:"snapshot_version"`
	WalletVersion     uint64 `json:"wallet_version"`
	MembershipVersion uint64 `json:"membership_version"`
	LeaseVersion      uint64 `json:"lease_version"`
	Granted           bool   `json:"granted"`
}

// Governor is the only authority allowed to reserve capacity. An adapter may
// call SrfGobernador.Admitir and SrfArrendador.Conceder under host fencing.
type Governor interface {
	Reserve(context.Context, Award, uint64) (Reservation, error)
}

// Commit is the sole admission gateway, not part of the pure selector. The
// governor must atomically recheck versions and funds before granting.
func Commit(ctx context.Context, governor Governor, award Award, epoch Epoch) (Reservation, error) {
	if governor == nil {
		return Reservation{}, ErrDenied
	}
	if award.Versions != epoch.Versions {
		return Reservation{}, ErrConflict
	}
	if err := VerifyFull(award, epoch); err != nil {
		return Reservation{}, err
	}
	r, err := governor.Reserve(ctx, award, epoch.Versions.Snapshot)
	if err != nil {
		return Reservation{}, err
	}
	if !r.Granted || r.ID == "" || r.SnapshotVersion != epoch.Versions.Snapshot || r.WalletVersion != epoch.Versions.Wallet || r.MembershipVersion != epoch.Versions.Membership || r.LeaseVersion != epoch.Versions.Lease {
		return Reservation{}, ErrDenied
	}
	return r, nil
}

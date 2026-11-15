// Package loopadapter is an offline, fail-closed evidence adapter. Ports are
// injected: nothing here launches workers, writes settlements or grants leases.
package loopadapter

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"math"
	"math/big"
	"sync"
)

var (
	ErrHold       = errors.New("loopadapter: evidence or authority held")
	ErrQuarantine = errors.New("loopadapter: uncertain liability quarantined")
	ErrRefused    = errors.New("loopadapter: invalid or conflicting evidence")
)

// All quantities in these TARGET carriers are integer and policy-bound. A
// missing measurement is not zero: mark it in Unknown with a nonempty reason.
type ResourceVector struct {
	RAMBytes, PIDs, FDs          uint64
	CPUNumerator, CPUDenominator int64
}
type Entry struct {
	EntryID, LeaseID, State          string
	Generation, Epoch, ReservedBytes uint64
	CleanupVerified                  bool
}
type Snapshot struct {
	SampleID, WorkloadCommit                                                  string
	AcquiredMonotonicNS                                                       int64
	CPUNumerator, CPUDenominator                                              int64
	RAMBytes, PIDs, FDs                                                       uint64
	AncestorLimits                                                            ResourceVector
	PhysicalHeadroomBytes, StartupBytes, CollectorBytes, ReactionReserveBytes uint64
	Pressure, TailLatencyNS, DwellNS                                          int64
	LastQualifiedWidth                                                        int
	Entries                                                                   []Entry
	Unknown                                                                   map[string]string
}
type Policy struct {
	MaxAgeNS, MaxTailLatencyNS, MinDwellNS, MaxPressure int64
	MaxWidth                                            int
	MaxRAMBytes, MaxPIDs, MaxFDs                        uint64
	MaxCPUNumerator, MaxCPUDenominator                  int64
}
type Obligation struct {
	ID     string
	Weight uint64
	State  string
}
type BootTarget struct {
	ID       string
	Resource ResourceVector
}
type Proposal struct {
	Targets   []BootTarget
	Resources ResourceVector
	Escrow    uint64
}
type GangGrant struct {
	LeaseID    string
	Epoch      uint64
	HeldIDs    []string // verified acquired holds only
	EscrowHeld uint64
	Complete   bool // attested by the atomic reservation authority
}
type AttemptReceipt struct {
	ObligationID, TaskID, AttemptID, LeaseID, ReceiptSHA256 string
	Epoch                                                   uint64
	Status                                                  string // accepted | failed
	IOConfirmed, Fresh, Paid                                bool
}
type Settlement struct {
	WaveID                                         string
	Obligations                                    []Obligation
	Attempts                                       []AttemptReceipt
	ResourceVector                                 ResourceVector
	Escrow                                         uint64
	LeaseID                                        string
	Epoch                                          uint64
	OriginVerified, GatesVerified, CleanupVerified bool
	SettlementID, BodySHA256, Disposition          string
}
type Review struct {
	SettlementID, BodySHA256, Disposition   string
	GatesVerified, CleanupVerified, Revoked bool
}

// A proposal is never permission. Only LeaseJournal holds reservation authority.
type Auction interface {
	Propose(string, []Obligation) (Proposal, error)
}
type Governor interface {
	Allow(Snapshot, Policy, Proposal) (bool, error)
}
type LeaseJournal interface {
	ReserveGang(string, Proposal) (GangGrant, error)
	Current(GangGrant) (bool, error)
	RollbackVerified(GangGrant, []string) error
	Release(GangGrant) error
}
type Collector interface {
	Verify(AttemptReceipt) (bool, error)
}
type Certificate interface {
	Verify(Settlement) (bool, error)
}
type OriginVerifier interface {
	VerifyOrigin(string, string) (bool, error)
	VerifyReceipt(AttemptReceipt) (bool, error)
}
type Clock interface{ MonotonicNS() int64 }

type Ports struct {
	Auction        Auction
	Governor       Governor
	LeaseJournal   LeaseJournal
	Collector      Collector
	Certificate    Certificate
	OriginVerifier OriginVerifier
	Clock          Clock
}
type Adapter struct {
	mu                          sync.Mutex
	ports                       Ports
	policy                      Policy
	active, quarantine, settled bool
	wave                        string
	inventory                   []Obligation
	byID                        map[string]int
	attempts                    []AttemptReceipt
	attemptIDs                  map[string]AttemptReceipt
	proposal                    Proposal
	grant                       GangGrant
	nextBoot                    int
	origin                      bool
	settlement                  Settlement
	review                      Review
}

func New(ports Ports, policy Policy) (*Adapter, error) {
	if ports.Auction == nil || ports.Governor == nil || ports.LeaseJournal == nil || ports.Collector == nil || ports.Certificate == nil || ports.OriginVerifier == nil || ports.Clock == nil ||
		policy.MaxAgeNS <= 0 || policy.MaxTailLatencyNS <= 0 || policy.MinDwellNS <= 0 || policy.MaxPressure <= 0 || policy.MaxWidth <= 0 || policy.MaxRAMBytes == 0 || policy.MaxPIDs == 0 || policy.MaxFDs == 0 || policy.MaxCPUNumerator <= 0 || policy.MaxCPUDenominator <= 0 {
		return nil, ErrRefused
	}
	return &Adapter{ports: ports, policy: policy}, nil
}
func validDigest(s string) bool {
	if len(s) != 64 {
		return false
	}
	for _, b := range []byte(s) {
		if !((b >= '0' && b <= '9') || (b >= 'a' && b <= 'f')) {
			return false
		}
	}
	return true
}
func validResources(r ResourceVector) bool {
	return r.RAMBytes > 0 && r.PIDs > 0 && r.FDs > 0 && r.CPUNumerator > 0 && r.CPUDenominator > 0
}
func cpuLE(a, b, c, d int64) bool {
	var x, y big.Int
	x.Mul(big.NewInt(a), big.NewInt(d))
	y.Mul(big.NewInt(c), big.NewInt(b))
	return x.Cmp(&y) <= 0
}
func add(a, b uint64) (uint64, bool) {
	if math.MaxUint64-a < b {
		return 0, false
	}
	return a + b, true
}
func (a *Adapter) checkSample(s Snapshot, p Proposal) error {
	now := a.ports.Clock.MonotonicNS()
	if s.SampleID == "" || s.WorkloadCommit == "" || s.AcquiredMonotonicNS <= 0 || now < s.AcquiredMonotonicNS || now-s.AcquiredMonotonicNS > a.policy.MaxAgeNS || len(s.Unknown) > 0 ||
		s.CPUNumerator <= 0 || s.CPUDenominator <= 0 || s.Pressure < 0 || s.Pressure >= a.policy.MaxPressure ||
		s.TailLatencyNS < 0 || s.TailLatencyNS > a.policy.MaxTailLatencyNS || s.DwellNS < a.policy.MinDwellNS ||
		s.LastQualifiedWidth < 1 || s.LastQualifiedWidth > a.policy.MaxWidth ||
		s.RAMBytes == 0 || s.PIDs == 0 || s.FDs == 0 || !validResources(s.AncestorLimits) ||
		!validResources(p.Resources) || len(p.Targets) == 0 || len(p.Targets) > s.LastQualifiedWidth || p.Escrow == 0 {
		return ErrHold
	}
	if p.Resources.RAMBytes > a.policy.MaxRAMBytes || p.Resources.PIDs > a.policy.MaxPIDs || p.Resources.FDs > a.policy.MaxFDs ||
		!cpuLE(p.Resources.CPUNumerator, p.Resources.CPUDenominator, a.policy.MaxCPUNumerator, a.policy.MaxCPUDenominator) ||
		!cpuLE(p.Resources.CPUNumerator, p.Resources.CPUDenominator, s.AncestorLimits.CPUNumerator, s.AncestorLimits.CPUDenominator) ||
		p.Resources.RAMBytes > s.AncestorLimits.RAMBytes || p.Resources.PIDs > s.AncestorLimits.PIDs || p.Resources.FDs > s.AncestorLimits.FDs {
		return ErrHold
	}
	used := uint64(0)
	seen := map[string]bool{}
	for _, e := range s.Entries {
		if e.EntryID == "" || seen[e.EntryID] || e.Generation == 0 || e.LeaseID == "" || e.Epoch == 0 || e.State == "" {
			return ErrHold
		}
		seen[e.EntryID] = true
		// Only verified cleanup removes a liability, including stopping/quarantine.
		if !e.CleanupVerified {
			var ok bool
			used, ok = add(used, e.ReservedBytes)
			if !ok {
				return ErrHold
			}
		}
	}
	required, ok := add(p.Resources.RAMBytes, s.StartupBytes)
	if !ok {
		return ErrHold
	}
	required, ok = add(required, s.CollectorBytes)
	if !ok {
		return ErrHold
	}
	required, ok = add(required, s.ReactionReserveBytes)
	if !ok {
		return ErrHold
	}
	required, ok = add(required, used)
	if !ok {
		return ErrHold
	}
	if required > s.PhysicalHeadroomBytes || required > s.RAMBytes || p.Resources.PIDs > s.PIDs || p.Resources.FDs > s.FDs ||
		!cpuLE(p.Resources.CPUNumerator, p.Resources.CPUDenominator, s.CPUNumerator, s.CPUDenominator) {
		return ErrHold
	}
	targets := map[string]bool{}
	ram, pids, fds := uint64(0), uint64(0), uint64(0)
	for _, t := range p.Targets {
		if t.ID == "" || targets[t.ID] || !validResources(t.Resource) {
			return ErrRefused
		}
		targets[t.ID] = true
		var ok bool
		ram, ok = add(ram, t.Resource.RAMBytes)
		if !ok {
			return ErrRefused
		}
		pids, ok = add(pids, t.Resource.PIDs)
		if !ok {
			return ErrRefused
		}
		fds, ok = add(fds, t.Resource.FDs)
		if !ok {
			return ErrRefused
		}
	}
	if ram != p.Resources.RAMBytes || pids != p.Resources.PIDs || fds != p.Resources.FDs {
		return ErrRefused
	}
	return nil
}
func freeze(xs []Obligation) ([]Obligation, map[string]int, error) {
	if len(xs) == 0 {
		return nil, nil, ErrRefused
	}
	v := append([]Obligation(nil), xs...)
	index := make(map[string]int, len(v))
	total := uint64(0)
	for i, o := range v {
		if o.ID == "" || o.Weight == 0 || o.State != "unresolved" {
			return nil, nil, ErrRefused
		}
		if _, ok := index[o.ID]; ok {
			return nil, nil, ErrRefused
		}
		var ok bool
		total, ok = add(total, o.Weight)
		if !ok {
			return nil, nil, ErrRefused
		}
		index[o.ID] = i
	}
	if total == 0 {
		return nil, nil, ErrRefused
	}
	return v, index, nil
}

// Begin freezes inventory, checks origin and a fresh finite snapshot, then asks
// the authority for an all-or-none gang+escrow+lease reservation.
func (a *Adapter) Begin(wave string, obligations []Obligation, sample Snapshot) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.active || a.quarantine || a.settled || wave == "" {
		return ErrRefused
	}
	inventory, index, err := freeze(obligations)
	if err != nil {
		return err
	}
	origin, err := a.ports.OriginVerifier.VerifyOrigin(wave, sample.WorkloadCommit)
	if err != nil || !origin {
		return ErrHold
	}
	proposal, err := a.ports.Auction.Propose(wave, append([]Obligation(nil), inventory...))
	if err != nil {
		return ErrHold
	}
	if err = a.checkSample(sample, proposal); err != nil {
		return err
	}
	allowed, err := a.ports.Governor.Allow(sample, a.policy, proposal)
	if err != nil || !allowed {
		return ErrHold
	}
	grant, err := a.ports.LeaseJournal.ReserveGang(wave, proposal)
	// Failed, partial, or uncertain reservation never issues a boot permit.
	complete := err == nil && grant.Complete && grant.LeaseID != "" && grant.Epoch > 0 && grant.EscrowHeld == proposal.Escrow && len(grant.HeldIDs) == len(proposal.Targets)
	seen := map[string]bool{}
	for _, id := range grant.HeldIDs {
		if id == "" || seen[id] {
			complete = false
		}
		seen[id] = true
	}
	for _, t := range proposal.Targets {
		if !seen[t.ID] {
			complete = false
		}
	}
	if !complete {
		if len(grant.HeldIDs) > 0 {
			if rollbackErr := a.ports.LeaseJournal.RollbackVerified(grant, append([]string(nil), grant.HeldIDs...)); rollbackErr != nil {
				a.quarantine = true
			}
		}
		// Incomplete grant carries potentially unknown holds. Retain liability.
		if err != nil || !grant.Complete || len(grant.HeldIDs) != len(proposal.Targets) || grant.EscrowHeld != proposal.Escrow {
			a.quarantine = true
		}
		if a.quarantine {
			return ErrQuarantine
		}
		return ErrHold
	}
	current, err := a.ports.LeaseJournal.Current(grant)
	if err != nil || !current {
		a.quarantine = true
		return ErrQuarantine
	}
	a.active = true
	a.wave = wave
	a.inventory = inventory
	a.byID = index
	a.proposal = proposal
	a.grant = grant
	a.origin = true
	a.attemptIDs = make(map[string]AttemptReceipt)
	return nil
}

// NextBoot emits at most one permission per call; this is not a launch action.
func (a *Adapter) NextBoot() (BootTarget, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if !a.active || a.quarantine || a.settled || a.nextBoot >= len(a.proposal.Targets) {
		return BootTarget{}, ErrHold
	}
	current, err := a.ports.LeaseJournal.Current(a.grant)
	if err != nil || !current {
		a.quarantine = true
		return BootTarget{}, ErrQuarantine
	}
	target := a.proposal.Targets[a.nextBoot]
	a.nextBoot++
	return target, nil
}
func (a *Adapter) residual() (uint64, error) {
	var total uint64
	for _, o := range a.inventory {
		if o.State == "unresolved" {
			var ok bool
			total, ok = add(total, o.Weight)
			if !ok {
				return 0, ErrRefused
			}
		}
	}
	return total, nil
}

// Record retains all authenticated paid receipts. A status transition cannot
// be justified by copied evidence or by rounded percentages.
func (a *Adapter) Record(r AttemptReceipt) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	if !a.active || a.quarantine || a.settled || a.nextBoot != len(a.proposal.Targets) {
		return ErrHold
	}
	i, ok := a.byID[r.ObligationID]
	if !ok || r.TaskID == "" || r.AttemptID == "" || !validDigest(r.ReceiptSHA256) || r.LeaseID != a.grant.LeaseID || r.Epoch != a.grant.Epoch ||
		(r.Status != "accepted" && r.Status != "failed") || !r.IOConfirmed || !r.Fresh {
		return ErrRefused
	}
	if old, exists := a.attemptIDs[r.AttemptID]; exists {
		if old == r {
			return nil
		}
		a.quarantine = true
		return ErrQuarantine
	}
	if a.inventory[i].State != "unresolved" {
		return ErrRefused
	}
	current, err := a.ports.LeaseJournal.Current(a.grant)
	if err != nil || !current {
		a.quarantine = true
		return ErrQuarantine
	}
	verified, err := a.ports.OriginVerifier.VerifyReceipt(r)
	if err != nil || !verified {
		return ErrHold
	}
	io, err := a.ports.Collector.Verify(r)
	if err != nil || !io {
		return ErrHold
	}
	previous, err := a.residual()
	if err != nil {
		return err
	}
	a.inventory[i].State = r.Status
	next, err := a.residual()
	if err != nil {
		return err
	}
	if next >= previous {
		a.inventory[i].State = "unresolved"
		return ErrRefused
	}
	a.attemptIDs[r.AttemptID] = r
	a.attempts = append(a.attempts, r)
	return nil
}

// Finish computes conservation with exact integer arithmetic. A failure is
// accounted terminal completion, never success or a duplicate settlement.
func (a *Adapter) Finish(r Review) (Settlement, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.settled {
		if r == a.review {
			return a.copySettlement(), nil
		}
		return Settlement{}, ErrRefused
	}
	if !a.active || a.quarantine || r.SettlementID == "" || !validDigest(r.BodySHA256) || !r.CleanupVerified || !r.GatesVerified || r.Revoked ||
		(r.Disposition != "success" && r.Disposition != "failure") {
		return Settlement{}, ErrHold
	}
	current, err := a.ports.LeaseJournal.Current(a.grant)
	if err != nil || !current {
		a.quarantine = true
		return Settlement{}, ErrQuarantine
	}
	var all, accepted, failed, unresolved uint64
	for _, o := range a.inventory {
		var ok bool
		all, ok = add(all, o.Weight)
		if !ok {
			return Settlement{}, ErrRefused
		}
		switch o.State {
		case "accepted":
			accepted, ok = add(accepted, o.Weight)
		case "failed":
			failed, ok = add(failed, o.Weight)
		case "unresolved":
			unresolved, ok = add(unresolved, o.Weight)
		default:
			return Settlement{}, ErrRefused
		}
		if !ok {
			return Settlement{}, ErrRefused
		}
	}
	t, ok := add(accepted, failed)
	if !ok {
		return Settlement{}, ErrRefused
	}
	t, ok = add(t, unresolved)
	if !ok || t != all || unresolved != 0 || len(a.attempts) != len(a.inventory) {
		return Settlement{}, ErrHold
	}
	if (r.Disposition == "success" && (failed != 0 || accepted != all)) || (r.Disposition == "failure" && failed == 0) {
		return Settlement{}, ErrRefused
	}
	s := Settlement{WaveID: a.wave, Obligations: append([]Obligation(nil), a.inventory...), Attempts: append([]AttemptReceipt(nil), a.attempts...), ResourceVector: a.proposal.Resources, Escrow: a.proposal.Escrow, LeaseID: a.grant.LeaseID, Epoch: a.grant.Epoch, OriginVerified: a.origin, GatesVerified: r.GatesVerified, CleanupVerified: r.CleanupVerified, SettlementID: r.SettlementID, BodySHA256: r.BodySHA256, Disposition: r.Disposition}
	certified, err := a.ports.Certificate.Verify(s)
	if err != nil || !certified {
		return Settlement{}, ErrHold
	}
	if err = a.ports.LeaseJournal.Release(a.grant); err != nil {
		a.quarantine = true
		return Settlement{}, ErrQuarantine
	}
	a.settlement = s
	a.review = r
	a.settled = true
	a.active = false
	return a.copySettlement(), nil
}
func (a *Adapter) copySettlement() Settlement {
	s := a.settlement
	s.Obligations = append([]Obligation(nil), s.Obligations...)
	s.Attempts = append([]AttemptReceipt(nil), s.Attempts...)
	return s
}
func (a *Adapter) State() string {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.quarantine {
		return "quarantined"
	}
	if a.settled {
		return "settled"
	}
	if a.active {
		return "reserved"
	}
	return "idle"
}
func Digest(b []byte) string      { s := sha256.Sum256(b); return hex.EncodeToString(s[:]) }
func (a *Adapter) String() string { return fmt.Sprintf("loopadapter(%s)", a.State()) }

package auction

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"math/bits"
	"reflect"
	"sort"
	"strings"
)

// DecodeEpoch enforces a byte cap before JSON parsing and refuses unknown
// fields, so forwarded fields cannot acquire implicit authority.
func DecodeEpoch(input []byte, limits Limits) (Epoch, error) {
	max := limits.bounded().MaxInputBytes
	if uint64(len(input)) > max {
		return Epoch{}, ErrHold
	}
	d := json.NewDecoder(bytes.NewReader(input))
	d.DisallowUnknownFields()
	var e Epoch
	if err := d.Decode(&e); err != nil {
		return Epoch{}, fmt.Errorf("%w: %v", ErrInvalid, err)
	}
	if err := d.Decode(new(any)); err != io.EOF {
		return Epoch{}, fmt.Errorf("%w: trailing JSON", ErrInvalid)
	}
	e.Limits = limits
	if err := preflight(e); err != nil {
		return Epoch{}, err
	}
	return e, nil
}

func preflight(e Epoch) error {
	l := e.Limits.bounded()
	if len(e.Bids) == 0 || len(e.Capacity) == 0 || len(e.Bids) > int(l.MaxBids) || len(e.Quotes) > int(l.MaxAlternatives) || len(e.Capacity) > int(l.MaxHorizonPoints) {
		return ErrHold
	}
	if e.HostScope == "" || e.DemandWeights.Empty() {
		return fmt.Errorf("%w: missing host scope or demand weights", ErrInvalid)
	}
	var altN, memberN, work uint64
	for _, b := range e.Bids {
		if len(b.Alternatives) == 0 {
			return fmt.Errorf("%w: empty XOR menu", ErrInvalid)
		}
		altN += uint64(len(b.Alternatives))
		if altN > l.MaxAlternatives {
			return ErrHold
		}
		for _, a := range b.Alternatives {
			memberN += uint64(len(a.Members))
			if memberN > l.MaxMembers {
				return ErrHold
			}
			if len(a.Intervals) > int(l.MaxHorizonPoints) {
				return ErrHold
			}
			// A strict worst-case bound is charged even when an early fit fails.
			for _, it := range a.Intervals {
				if err := it.validate(); err != nil {
					return err
				}
				n := it.EndTick - it.StartTick
				if n > l.MaxHorizonPoints {
					return ErrHold
				}
				units, err := multiply(n, uint64(len(a.Members)))
				if err != nil {
					return ErrHold
				}
				units, err = multiply(units, 2)
				if err != nil {
					return ErrHold
				} // selection + verification
				work, err = add(work, units)
				if err != nil || work > l.MaxWork {
					return ErrHold
				}
			}
		}
	}
	// Sorting and singleton scan are charged up front.
	extra, err := multiply(altN, altN)
	if err != nil {
		return ErrHold
	}
	work, err = add(work, extra)
	if err != nil || work > l.MaxWork {
		return ErrHold
	}
	for i, c := range e.Capacity {
		if i > 0 && c.Tick != e.Capacity[i-1].Tick+1 {
			return fmt.Errorf("%w: capacity ticks not contiguous", ErrInvalid)
		}
		if i > 0 && e.Capacity[i-1].Tick == math.MaxUint64 {
			return ErrOverflow
		}
	}
	return nil
}

func validSHA(s string) bool {
	if len(s) != 64 {
		return false
	}
	for _, c := range s {
		if !(c >= '0' && c <= '9') && !(c >= 'a' && c <= 'f') {
			return false
		}
	}
	return true
}

func validateAlternative(b PackBid, a Alternative, e Epoch) error {
	if a.ID == "" || len(a.Members) == 0 || len(a.Intervals) == 0 || a.MaxLiabilityMicro == 0 || b.Principal == "" || b.Room == "" || b.BidID == "" || b.WalletAuth == "" || !validSHA(b.SuiteSHA256) {
		return fmt.Errorf("%w: incomplete bid or pack", ErrInvalid)
	}
	if b.MembershipVersion != e.Versions.Membership || b.PolicyEpoch != e.Versions.Policy || b.EarliestStartTick > e.NowTick && b.EarliestStartTick > b.DeadlineTick || e.NowTick >= b.BidExpiryTick || b.DeadlineTick > b.BidExpiryTick || b.EarliestStartTick >= b.DeadlineTick {
		return ErrConflict
	}
	if e.NowTick > b.DeadlineTick {
		return ErrConflict
	}
	seen := make(map[string]bool, len(a.Members))
	for _, m := range a.Members {
		if m.ID == "" || m.Profile == "" || m.Arch == "" || m.Backend == "" || m.Vec.Empty() || seen[m.ID] {
			return fmt.Errorf("%w: invalid or duplicate member", ErrInvalid)
		}
		seen[m.ID] = true
	}
	var prior uint64
	for i, it := range a.Intervals {
		if err := it.validate(); err != nil {
			return err
		}
		if it.StartTick < b.EarliestStartTick || it.StartTick < e.NowTick || it.EndTick > b.DeadlineTick {
			return ErrConflict
		}
		if i > 0 && it.StartTick < prior {
			return fmt.Errorf("%w: overlapping intervals", ErrInvalid)
		}
		prior = it.EndTick
	}
	return nil
}

func accountOf(e Epoch, principal, room string) (WalletAccount, bool) {
	for _, w := range e.Wallets {
		if w.Principal == principal && w.Room == room {
			return w, true
		}
	}
	return WalletAccount{}, false
}
func roomBase(w WalletAccount) (uint64, error) {
	v, err := add(w.SettledMicro, w.OutstandingMicro)
	if err != nil {
		return 0, err
	}
	return add(v, w.ProtectedFinalizationMicro)
}
func quoteOf(e Epoch, bidID, altID string) (Quote, bool) {
	for _, q := range e.Quotes {
		if q.BidID == bidID && q.AlternativeID == altID {
			return q.Quote, true
		}
	}
	return Quote{}, false
}
func identity(b PackBid) string {
	if b.EconomicGroup != "" {
		return b.EconomicGroup
	}
	return b.BidID
}
func key(bidID, altID string) string { return walletKey(bidID, altID) }

func demandWeight(a Alternative, w Vec) (uint64, error) {
	var total uint64
	for _, m := range a.Members {
		for _, x := range []struct{ value, weight uint64 }{{m.Vec.MemBytes, w.MemBytes}, {m.Vec.CPUMilli, w.CPUMilli}, {m.Vec.PIDs, w.PIDs}, {m.Vec.Files, w.Files}} {
			v, err := multiply(x.value, x.weight)
			if err != nil {
				return 0, err
			}
			total, err = add(total, v)
			if err != nil {
				return 0, err
			}
		}
	}
	if total == 0 {
		return 0, fmt.Errorf("%w: zero demand weight", ErrInvalid)
	}
	return total, nil
}

type candidate struct {
	b               PackBid
	a               Alternative
	q               Quote
	surplus, weight uint64
}

func candidates(e Epoch) ([]candidate, error) {
	seenBids := map[string]bool{}
	seenQuotes := map[string]bool{}
	seenWallets := map[string]bool{}
	for _, w := range e.Wallets {
		k := key(w.Principal, w.Room)
		if w.Principal == "" || w.Room == "" || seenWallets[k] {
			return nil, ErrInvalid
		}
		seenWallets[k] = true
	}
	for _, q := range e.Quotes {
		id := key(q.BidID, q.AlternativeID)
		if seenQuotes[id] {
			return nil, ErrInvalid
		}
		seenQuotes[id] = true
		if q.Quote.AlternativeID != q.AlternativeID || q.Quote.Hold || q.Quote.HostScope != e.HostScope || q.Quote.Versions != e.Versions || q.Quote.AtTick != e.NowTick {
			return nil, ErrConflict
		}
	}
	out := make([]candidate, 0)
	for _, b := range e.Bids {
		if seenBids[b.BidID] || b.BidID == "" {
			return nil, ErrInvalid
		}
		seenBids[b.BidID] = true
		seenAlt := map[string]bool{}
		for _, a := range b.Alternatives {
			if seenAlt[a.ID] {
				return nil, ErrInvalid
			}
			seenAlt[a.ID] = true
			if err := validateAlternative(b, a, e); err != nil {
				return nil, err
			}
			q, ok := quoteOf(e, b.BidID, a.ID)
			if !ok {
				continue
			}
			if a.ValueMicro < q.AmountMicro || a.MaxLiabilityMicro < q.AmountMicro {
				continue
			}
			w, ok := accountOf(e, b.Principal, b.Room)
			if !ok {
				continue
			}
			base, err := roomBase(w)
			if err != nil {
				return nil, err
			}
			cap, err := add(base, a.MaxLiabilityMicro)
			if err != nil {
				return nil, err
			}
			if w.AvailableMicro < a.MaxLiabilityMicro || cap > w.ApprovedCapMicro {
				continue
			}
			weight, err := demandWeight(a, e.DemandWeights)
			if err != nil {
				return nil, err
			}
			out = append(out, candidate{b: b, a: a, q: q, surplus: a.ValueMicro - q.AmountMicro, weight: weight})
		}
	}
	return out, nil
}

// cmpDensity compares ratios without float or lossy overflow.
func cmpDensity(a, b candidate) int {
	hiA, loA := bits.Mul64(a.surplus, b.weight)
	hiB, loB := bits.Mul64(b.surplus, a.weight)
	if hiA != hiB {
		if hiA > hiB {
			return 1
		}
		return -1
	}
	if loA != loB {
		if loA > loB {
			return 1
		}
		return -1
	}
	return 0
}
func frozenLess(a, b candidate) bool {
	return strings.Compare(key(a.b.BidID, a.a.ID), key(b.b.BidID, b.a.ID)) < 0
}
func cloneCapacity(base []CapacityPoint) []CapacityPoint {
	return append([]CapacityPoint(nil), base...)
}
func capacityFit(c candidate, cap []CapacityPoint) bool {
	if len(cap) == 0 {
		return false
	}
	pack := Vec{}
	for _, m := range c.a.Members {
		v, err := pack.Add(m.Vec)
		if err != nil {
			return false
		}
		pack = v
	}
	start, end := cap[0].Tick, cap[len(cap)-1].Tick
	for _, it := range c.a.Intervals {
		if it.StartTick < start || it.EndTick <= it.StartTick || it.EndTick-1 > end {
			return false
		}
		for tick := it.StartTick; tick < it.EndTick; tick++ {
			x := int(tick - start)
			if !pack.Fits(cap[x].Remaining) {
				return false
			}
		}
	}
	return true
}

// place applies a complete all-or-nothing reservation to a private scratch copy.
func place(c candidate, cap []CapacityPoint) ([]CapacityPoint, bool) {
	scratch := cloneCapacity(cap)
	if !capacityFit(c, scratch) {
		return nil, false
	}
	pack := Vec{}
	for _, m := range c.a.Members {
		var err error
		pack, err = pack.Add(m.Vec)
		if err != nil {
			return nil, false
		}
	}
	for _, it := range c.a.Intervals {
		for tick := it.StartTick; tick < it.EndTick; tick++ {
			n := int(tick - scratch[0].Tick)
			next, err := scratch[n].Remaining.Sub(pack)
			if err != nil {
				return nil, false
			}
			scratch[n].Remaining = next
		}
	}
	return scratch, true
}
func walletFits(c candidate, e Epoch, liabilities, spending map[string]uint64) bool {
	w, ok := accountOf(e, c.b.Principal, c.b.Room)
	if !ok {
		return false
	}
	id := key(w.Principal, w.Room)
	base, err := roomBase(w)
	if err != nil {
		return false
	}
	sum, err := add(liabilities[id], c.a.MaxLiabilityMicro)
	if err != nil {
		return false
	}
	cap, err := add(base, sum)
	if err != nil || cap > w.ApprovedCapMicro {
		return false
	}
	sum, err = add(spending[id], c.a.MaxLiabilityMicro)
	if err != nil || sum > w.AvailableMicro {
		return false
	}
	// Also reject double-spending a principal's common balance across rooms.
	var principalSpending uint64
	var minAvail uint64 = math.MaxUint64
	for _, other := range e.Wallets {
		if other.Principal == w.Principal && other.AvailableMicro < minAvail {
			minAvail = other.AvailableMicro
		}
	}
	for k, v := range spending {
		if strings.HasPrefix(k, fmt.Sprintf("%d:%s:", len(w.Principal), w.Principal)) {
			principalSpending, err = add(principalSpending, v)
			if err != nil {
				return false
			}
		}
	}
	principalSpending, err = add(principalSpending, c.a.MaxLiabilityMicro)
	return err == nil && principalSpending <= minAvail
}
func newCert(picks []candidate) []CertificateMember {
	members := make([]CertificateMember, 0)
	for _, c := range picks {
		for _, m := range c.a.Members {
			members = append(members, CertificateMember{BidID: c.b.BidID, AlternativeID: c.a.ID, Member: m, Intervals: append([]Interval(nil), c.a.Intervals...)})
		}
	}
	return members
}
func assemble(picks []candidate, e Epoch) (Award, error) {
	a := Award{Quality: "heuristic", Versions: e.Versions, HostScope: e.HostScope}
	for _, c := range picks {
		a.Picks = append(a.Picks, Pick{BidID: c.b.BidID, AlternativeID: c.a.ID, QuoteMicro: c.q.AmountMicro})
		var err error
		a.TotalValueMicro, err = add(a.TotalValueMicro, c.a.ValueMicro)
		if err != nil {
			return Award{}, err
		}
		a.TotalQuoteMicro, err = add(a.TotalQuoteMicro, c.q.AmountMicro)
		if err != nil {
			return Award{}, err
		}
	}
	a.Certificate = newCert(picks)
	return a, nil
}

// Select implements the bounded F_FAST first-fit density heuristic plus the
// best complete singleton. It never grants or mutates resources. On an
// exhausted budget it returns only HOLD, not a partial award.
func Select(e Epoch) (Award, error) {
	held := Award{Quality: "abstain", Hold: true, Versions: e.Versions, HostScope: e.HostScope}
	if err := preflight(e); err != nil {
		if err == ErrHold {
			return held, ErrHold
		}
		return held, err
	}
	cs, err := candidates(e)
	if err != nil {
		return held, err
	}
	sort.Slice(cs, func(i, j int) bool {
		d := cmpDensity(cs[i], cs[j])
		if d != 0 {
			return d > 0
		}
		return frozenLess(cs[i], cs[j])
	})
	residual := cloneCapacity(e.Capacity)
	usedGroups := map[string]bool{}
	liabilities := map[string]uint64{}
	spending := map[string]uint64{}
	greedy := make([]candidate, 0)
	for _, c := range cs {
		if usedGroups[identity(c.b)] || !walletFits(c, e, liabilities, spending) {
			continue
		}
		next, fit := place(c, residual)
		if !fit {
			continue
		}
		residual = next // Commit scratch only after the entire gang fits.
		usedGroups[identity(c.b)] = true
		k := key(c.b.Principal, c.b.Room)
		liabilities[k] += c.a.MaxLiabilityMicro
		spending[k] += c.a.MaxLiabilityMicro
		greedy = append(greedy, c)
	}
	win, err := assemble(greedy, e)
	if err != nil {
		return held, err
	}
	// Singleton is evaluated against ORIGINAL capacity, never greedy residual.
	for _, c := range cs {
		if !walletFits(c, e, map[string]uint64{}, map[string]uint64{}) || !capacityFit(c, e.Capacity) {
			continue
		}
		singleton, err := assemble([]candidate{c}, e)
		if err != nil {
			return held, err
		}
		if singleton.TotalValueMicro > win.TotalValueMicro || (singleton.TotalValueMicro == win.TotalValueMicro && len(greedy) > 1 && len(singleton.Picks) == 1) {
			win = singleton
			greedy = []candidate{c}
		}
	}
	if len(win.Picks) == 0 {
		return held, ErrAbstain
	}
	if err := VerifyFull(win, e); err != nil {
		return held, fmt.Errorf("%w: full verifier rejected proposed award: %v", ErrAbstain, err)
	}
	return win, nil
}

// VerifyFull recomputes XOR, total liabilities, time, whole-pack resources,
// exact posted quotes and the complete member certificate from base inputs.
// It never relies on a scratch feasibility flag supplied by the selector.
func VerifyFull(a Award, e Epoch) error {
	if err := preflight(e); err != nil {
		return err
	}
	if a.Hold || a.Quality != "heuristic" || a.Versions != e.Versions || a.HostScope != e.HostScope || len(a.Picks) == 0 {
		return ErrInvalid
	}
	cs, err := candidates(e)
	if err != nil {
		return err
	}
	byKey := make(map[string]candidate, len(cs))
	for _, c := range cs {
		byKey[key(c.b.BidID, c.a.ID)] = c
	}
	selected := make([]candidate, 0, len(a.Picks))
	groups := map[string]bool{}
	bidSeen := map[string]bool{}
	liabilities := map[string]uint64{}
	spending := map[string]uint64{}
	remaining := cloneCapacity(e.Capacity)
	for _, p := range a.Picks {
		c, ok := byKey[key(p.BidID, p.AlternativeID)]
		if !ok || p.QuoteMicro != c.q.AmountMicro || groups[identity(c.b)] || bidSeen[p.BidID] || !walletFits(c, e, liabilities, spending) {
			return ErrInvalid
		}
		next, fit := place(c, remaining)
		if !fit {
			return ErrInvalid
		}
		remaining = next
		groups[identity(c.b)] = true
		bidSeen[p.BidID] = true
		k := key(c.b.Principal, c.b.Room)
		liabilities[k] += c.a.MaxLiabilityMicro
		spending[k] += c.a.MaxLiabilityMicro
		selected = append(selected, c)
	}
	check, err := assemble(selected, e)
	if err != nil {
		return err
	}
	if check.TotalValueMicro != a.TotalValueMicro || check.TotalQuoteMicro != a.TotalQuoteMicro || !reflect.DeepEqual(check.Certificate, a.Certificate) {
		return ErrInvalid
	}
	return nil
}

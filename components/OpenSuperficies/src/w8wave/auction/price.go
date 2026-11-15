package auction

import (
	"fmt"
	"math"
	"math/big"
)

// Metric is an explicit, typed and timestamped host sample. Known=false may
// never be treated as a zero reading. Scope must equal the host being quoted.
type Metric struct {
	Value        uint64 `json:"value"`
	ObservedTick uint64 `json:"observed_tick"`
	Scope        string `json:"scope"`
	Known        bool   `json:"known"`
}

type PressureLatch struct {
	Closed       bool   `json:"closed"`
	ClearSamples uint64 `json:"clear_samples"`
}

type PressureSample struct {
	HostScope     string        `json:"host_scope"`
	NowTick       uint64        `json:"now_tick"`
	PSIMilli      Metric        `json:"psi_milli"`
	MemoryEvents  Metric        `json:"memory_events"`
	HeadroomBytes Metric        `json:"headroom_bytes"`
	Previous      PressureLatch `json:"previous"`
}

type Rates struct {
	MemBytes Rat `json:"mem_bytes"`
	CPUMilli Rat `json:"cpu_milli"`
	PIDs     Rat `json:"pids"`
	Files    Rat `json:"files"`
}

func (r Rates) validate() error {
	for _, v := range []Rat{r.MemBytes, r.CPUMilli, r.PIDs, r.Files} {
		if err := v.Validate(); err != nil {
			return err
		}
	}
	return nil
}
func (r Rates) atLeast(other Rates) (bool, error) {
	a := []Rat{r.MemBytes, r.CPUMilli, r.PIDs, r.Files}
	b := []Rat{other.MemBytes, other.CPUMilli, other.PIDs, other.Files}
	for i := range a {
		c, err := a[i].Cmp(b[i])
		if err != nil {
			return false, err
		}
		if c < 0 {
			return false, nil
		}
	}
	return true, nil
}

type RateBand struct {
	MaxPSIMilli uint64 `json:"max_psi_milli"`
	Rates       Rates  `json:"rates"`
}

type PricePolicy struct {
	HostScope          string     `json:"host_scope"`
	MaxAgeTicks        uint64     `json:"max_age_ticks"`
	HardClosePSIMilli  uint64     `json:"hard_close_psi_milli"`
	HeadroomFloorBytes uint64     `json:"headroom_floor_bytes"`
	ApprovedDwell      uint64     `json:"approved_dwell"`
	StartupMicro       uint64     `json:"startup_micro"`
	RateBands          []RateBand `json:"rate_bands"`
}

func validatePolicy(p PricePolicy) error {
	if p.HostScope == "" || p.MaxAgeTicks == 0 || p.HardClosePSIMilli == 0 || p.HeadroomFloorBytes == 0 || p.ApprovedDwell == 0 || len(p.RateBands) == 0 || len(p.RateBands) > 32 {
		return fmt.Errorf("%w: incomplete pricing policy", ErrInvalid)
	}
	for i, b := range p.RateBands {
		if err := b.Rates.validate(); err != nil {
			return err
		}
		if i > 0 {
			prev := p.RateBands[i-1]
			if b.MaxPSIMilli <= prev.MaxPSIMilli {
				return fmt.Errorf("%w: unordered price bands", ErrInvalid)
			}
			ok, err := b.Rates.atLeast(prev.Rates)
			if err != nil {
				return err
			}
			if !ok {
				return fmt.Errorf("%w: non-monotone tariff", ErrInvalid)
			}
		}
	}
	if p.RateBands[len(p.RateBands)-1].MaxPSIMilli < p.HardClosePSIMilli-1 {
		return fmt.Errorf("%w: unpriced pressure interval", ErrInvalid)
	}
	return nil
}

func checkMetric(m Metric, s PressureSample, p PricePolicy) bool {
	return m.Known && m.Scope == s.HostScope && m.ObservedTick <= s.NowTick && s.NowTick-m.ObservedTick <= p.MaxAgeTicks
}

// Price applies the hard close before pricing, even for an arbitrarily high
// bid. On HOLD it returns a non-admissible quote and an updated latch. Missing
// telemetry closes and resets recovery dwell. No float enters price arithmetic.
func Price(s PressureSample, p PricePolicy, a Alternative) (Quote, error) {
	q := Quote{AlternativeID: a.ID, HostScope: s.HostScope, AtTick: s.NowTick, Hold: true, Next: PressureLatch{Closed: true}}
	if err := validatePolicy(p); err != nil {
		return q, err
	}
	if s.HostScope == "" || s.HostScope != p.HostScope || !checkMetric(s.PSIMilli, s, p) || !checkMetric(s.MemoryEvents, s, p) || !checkMetric(s.HeadroomBytes, s, p) {
		return q, ErrHold
	}
	if s.PSIMilli.Value >= p.HardClosePSIMilli || s.MemoryEvents.Value > 0 || s.HeadroomBytes.Value < p.HeadroomFloorBytes {
		return q, ErrHold
	}
	if s.Previous.Closed {
		if s.Previous.ClearSamples >= math.MaxUint64 {
			return q, ErrOverflow
		}
		clear := s.Previous.ClearSamples + 1
		if clear < p.ApprovedDwell {
			q.Next = PressureLatch{Closed: true, ClearSamples: clear}
			return q, ErrHold
		}
	}
	q.Next = PressureLatch{}
	var rates Rates
	found := false
	for _, b := range p.RateBands {
		if s.PSIMilli.Value <= b.MaxPSIMilli {
			rates = b.Rates
			found = true
			break
		}
	}
	if !found {
		return Quote{AlternativeID: a.ID, HostScope: s.HostScope, AtTick: s.NowTick, Hold: true, Next: PressureLatch{Closed: true}}, ErrHold
	}
	if a.ID == "" || len(a.Members) == 0 || len(a.Intervals) == 0 {
		return q, fmt.Errorf("%w: empty pack", ErrInvalid)
	}
	var duration uint64
	for _, it := range a.Intervals {
		if err := it.validate(); err != nil {
			return q, err
		}
		n, err := add(duration, it.EndTick-it.StartTick)
		if err != nil {
			return q, err
		}
		duration = n
	}
	var sum big.Rat
	seenMembers := map[string]bool{}
	for _, m := range a.Members {
		if seenMembers[m.ID] {
			return q, fmt.Errorf("%w: duplicate member in quote", ErrInvalid)
		}
		seenMembers[m.ID] = true
		if m.ID == "" || m.Profile == "" || m.Arch == "" || m.Backend == "" || m.Vec.Empty() {
			return q, fmt.Errorf("%w: incomplete member", ErrInvalid)
		}
		for _, c := range []struct {
			v uint64
			r Rat
		}{{m.Vec.MemBytes, rates.MemBytes}, {m.Vec.CPUMilli, rates.CPUMilli}, {m.Vec.PIDs, rates.PIDs}, {m.Vec.Files, rates.Files}} {
			var n big.Int
			n.SetUint64(c.v)
			n.Mul(&n, new(big.Int).SetUint64(duration))
			n.Mul(&n, new(big.Int).SetUint64(c.r.Num))
			var d big.Int
			d.SetUint64(c.r.Den)
			var part big.Rat
			part.SetFrac(&n, &d)
			sum.Add(&sum, &part)
		}
	}
	var startup big.Int
	startup.SetUint64(p.StartupMicro)
	sum.Add(&sum, new(big.Rat).SetInt(&startup))
	var ceiling big.Int
	rem := new(big.Int)
	ceiling.QuoRem(sum.Num(), sum.Denom(), rem)
	if rem.Sign() > 0 {
		ceiling.Add(&ceiling, big.NewInt(1))
	}
	if !ceiling.IsUint64() {
		return q, ErrOverflow
	}
	q.AmountMicro = ceiling.Uint64()
	q.Hold = false
	return q, nil
}

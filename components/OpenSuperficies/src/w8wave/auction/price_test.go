package auction

import (
	"encoding/json"
	"errors"
	"math"
	"testing"
)

func m(v, t uint64) Metric { return Metric{Value: v, ObservedTick: t, Scope: "host-a", Known: true} }
func policy() PricePolicy {
	zero := Rat{0, 1}
	return PricePolicy{HostScope: "host-a", MaxAgeTicks: 2, HardClosePSIMilli: 900, HeadroomFloorBytes: 50, ApprovedDwell: 3, StartupMicro: 5, RateBands: []RateBand{
		{MaxPSIMilli: 499, Rates: Rates{MemBytes: Rat{1, 3}, CPUMilli: zero, PIDs: zero, Files: zero}},
		{MaxPSIMilli: 899, Rates: Rates{MemBytes: Rat{2, 3}, CPUMilli: zero, PIDs: zero, Files: zero}},
	}}
}
func sample(psi uint64) PressureSample {
	return PressureSample{HostScope: "host-a", NowTick: 7, PSIMilli: m(psi, 7), MemoryEvents: m(0, 7), HeadroomBytes: m(100, 7)}
}
func alt(id string, mem uint64) Alternative {
	return Alternative{ID: id, ValueMicro: 20, MaxLiabilityMicro: 20, Members: []Member{{ID: "m", Profile: "vm", Arch: "arm64", Backend: "microvm", Vec: Vec{MemBytes: mem}}}, Intervals: []Interval{{StartTick: 7, EndTick: 9}}}
}
func TestPriceCeilingAndHigherPSINotCheaper(t *testing.T) {
	low, err := Price(sample(100), policy(), alt("a", 2))
	if err != nil || low.AmountMicro != 7 || low.Hold {
		t.Fatalf("exact ceil: %+v %v", low, err)
	} // 5+ceil(4/3)=7
	high, err := Price(sample(600), policy(), alt("a", 2))
	if err != nil || high.AmountMicro != 8 {
		t.Fatalf("monotone tariff: %+v %v", high, err)
	} // 5+ceil(8/3)=8
}
func TestPressureFailClosedAndSustainedDwell(t *testing.T) {
	p := policy()
	cases := map[string]func(*PressureSample){
		"missing PSI":    func(s *PressureSample) { s.PSIMilli.Known = false },
		"stale":          func(s *PressureSample) { s.HeadroomBytes.ObservedTick = 1 },
		"absent floor":   func(s *PressureSample) { s.HeadroomBytes.Known = false },
		"cross-host":     func(s *PressureSample) { s.MemoryEvents.Scope = "other" },
		"memory event":   func(s *PressureSample) { s.MemoryEvents.Value = 1 },
		"hard PSI close": func(s *PressureSample) { s.PSIMilli.Value = 900 },
		"below floor":    func(s *PressureSample) { s.HeadroomBytes.Value = 49 },
	}
	for name, mutation := range cases {
		t.Run(name, func(t *testing.T) {
			s := sample(100)
			mutation(&s)
			q, err := Price(s, p, alt("a", 1))
			if !errors.Is(err, ErrHold) || !q.Hold || !q.Next.Closed {
				t.Fatalf("unexpected grant %v %+v", err, q)
			}
		})
	}
	closed, err := Price(sample(900), p, alt("a", 1))
	if !errors.Is(err, ErrHold) || !closed.Next.Closed {
		t.Fatal("expected latch to close")
	}
	next := closed.Next
	for i := uint64(1); i < 3; i++ {
		s := sample(100)
		s.Previous = next
		q, err := Price(s, p, alt("a", 1))
		if !errors.Is(err, ErrHold) || !q.Hold || q.Next.ClearSamples != i {
			t.Fatalf("dwell step %d: %v %+v", i, err, q)
		}
		next = q.Next
	}
	s := sample(100)
	s.Previous = next
	q, err := Price(s, p, alt("a", 1))
	if err != nil || q.Hold || q.Next.Closed {
		t.Fatalf("sustained clear did not reopen %v %+v", err, q)
	}
	s = sample(100)
	s.PSIMilli.Known = false
	s.Previous = next
	q, err = Price(s, p, alt("a", 1))
	if !errors.Is(err, ErrHold) || q.Next.ClearSamples != 0 {
		t.Fatalf("missing sample must reset dwell %v %+v", err, q)
	}
}
func TestInvalidTariffAndOverflow(t *testing.T) {
	p := policy()
	p.RateBands[1].Rates.MemBytes = Rat{1, 4}
	if _, err := Price(sample(100), p, alt("a", 2)); !errors.Is(err, ErrInvalid) {
		t.Fatalf("decreasing band accepted %v", err)
	}
	p = policy()
	p.HeadroomFloorBytes = 0
	if _, err := Price(sample(100), p, alt("a", 2)); !errors.Is(err, ErrInvalid) {
		t.Fatalf("missing floor accepted %v", err)
	}
	p = policy()
	p.RateBands[0].Rates.MemBytes = Rat{1, 0}
	if _, err := Price(sample(100), p, alt("a", 2)); !errors.Is(err, ErrInvalid) {
		t.Fatalf("zero denominator accepted %v", err)
	}
	p = policy()
	p.RateBands[0].Rates.MemBytes = Rat{Num: 2, Den: 1}
	p.RateBands[1].Rates.MemBytes = Rat{Num: 2, Den: 1}
	a := alt("x", math.MaxUint64)
	if _, err := Price(sample(100), p, a); !errors.Is(err, ErrOverflow) {
		t.Fatalf("overflow expected: %v", err)
	}
	var neg Rat
	if err := json.Unmarshal([]byte(`{"num":-1,"den":1}`), &neg); err == nil {
		t.Fatal("negative tariff accepted by unsigned rational type")
	}
}
func TestCheckedRatAndVec(t *testing.T) {
	a := Rat{math.MaxUint64, math.MaxUint64}
	b := Rat{1, 1}
	if n, err := a.Cmp(b); err != nil || n != 0 {
		t.Fatalf("128-bit cross product: %d %v", n, err)
	}
	if _, err := (Vec{MemBytes: math.MaxUint64}).Add(Vec{MemBytes: 1}); !errors.Is(err, ErrOverflow) {
		t.Fatal("vector overflow accepted")
	}
	if _, err := (Vec{}).Sub(Vec{PIDs: 1}); err == nil {
		t.Fatal("vector underflow accepted")
	}
}

package auction

import (
	"context"
	"encoding/json"
	"errors"
	"reflect"
	"strings"
	"testing"
)

func pack(bid, id, group string, mem, val uint64) PackBid {
	return PackBid{Principal: "p", Room: "r", EconomicGroup: group, MembershipVersion: 1, BidID: bid, Revision: 1, SuiteSHA256: strings.Repeat("a", 64), PolicyEpoch: 2, WalletAuth: "approved", EarliestStartTick: 0, BidExpiryTick: 12, DeadlineTick: 10, Alternatives: []Alternative{{ID: id, ValueMicro: val, MaxLiabilityMicro: val, Members: []Member{{ID: "m-" + id, Profile: "vm", Arch: "aarch64", Backend: "microvm", Vec: Vec{MemBytes: mem}}}, Intervals: []Interval{{StartTick: 0, EndTick: 1}}}}}
}
func epoch() Epoch {
	bids := []PackBid{pack("A", "a", "groupA", 3, 5), pack("B", "b", "groupB", 2, 3), pack("C", "c", "groupC", 2, 3)}
	v := Versions{Snapshot: 1, Wallet: 2, Membership: 1, Lease: 3, Policy: 2}
	q := []QuotedAlternative{}
	for _, b := range bids {
		q = append(q, QuotedAlternative{b.BidID, b.Alternatives[0].ID, Quote{AlternativeID: b.Alternatives[0].ID, AmountMicro: 0, HostScope: "host-a", Versions: v}})
	}
	return Epoch{HostScope: "host-a", NowTick: 0, Versions: v, Capacity: []CapacityPoint{{Tick: 0, Remaining: Vec{MemBytes: 4}}}, Bids: bids, Quotes: q, Wallets: []WalletAccount{{Principal: "p", Room: "r", AvailableMicro: 100, ApprovedCapMicro: 100}}, DemandWeights: Vec{MemBytes: 1}}
}
func TestFfastHeuristicNeverClaimsOptimum(t *testing.T) {
	e := epoch()
	copyB := append([]PackBid(nil), e.Bids...)
	copyCap := append([]CapacityPoint(nil), e.Capacity...)
	a, err := Select(e)
	if err != nil || a.Quality != "heuristic" || len(a.Picks) != 1 || a.Picks[0].BidID != "A" || a.TotalValueMicro != 5 {
		t.Fatalf("expected density heuristic suboptimal result: %+v %v", a, err)
	}
	if !reflect.DeepEqual(copyB, e.Bids) || !reflect.DeepEqual(copyCap, e.Capacity) {
		t.Fatal("selector mutated base input")
	}
	if err := VerifyFull(a, e); err != nil {
		t.Fatal(err)
	}
	a.Quality = "optimum"
	if err := VerifyFull(a, e); err == nil {
		t.Fatal("forged optimum certificate accepted")
	}
}
func TestFfastXORAndPartialGang(t *testing.T) {
	e := epoch()
	e.Bids = e.Bids[:1]
	e.Quotes = e.Quotes[:1]
	twin := e.Bids[0].Alternatives[0]
	twin.ID = "other"
	twin.Members[0].ID = "other-member"
	twin.ValueMicro = 7
	e.Bids[0].Alternatives = append(e.Bids[0].Alternatives, twin)
	e.Quotes = append(e.Quotes, QuotedAlternative{BidID: "A", AlternativeID: "other", Quote: Quote{AlternativeID: "other", HostScope: e.HostScope, Versions: e.Versions}})
	a, err := Select(e)
	if err != nil || len(a.Picks) != 1 || a.Picks[0].AlternativeID != "other" {
		t.Fatalf("XOR selector %+v %v", a, err)
	}
	forged := a
	forged.Picks = append([]Pick(nil), a.Picks...)
	forged.Picks = append(forged.Picks, Pick{BidID: "A", AlternativeID: "a"})
	if err := VerifyFull(forged, e); err == nil {
		t.Fatal("two XOR alternatives accepted")
	}
	partial := a
	partial.Certificate = append([]CertificateMember(nil), a.Certificate...)
	partial.Certificate = partial.Certificate[:0]
	if err := VerifyFull(partial, e); err == nil {
		t.Fatal("missing gang member accepted")
	}
	bogus := a
	bogus.Certificate = append([]CertificateMember(nil), a.Certificate...)
	bogus.Certificate[0].Member.ID = "forged"
	if err := VerifyFull(bogus, e); err == nil {
		t.Fatal("forged member accepted")
	}
}
func TestIncompletePlacementNeverConsumesResidual(t *testing.T) {
	e := epoch()
	// Large gang A does NOT fit memory (6>4); B and C can BOTH fit.
	e.Bids[0].Alternatives[0].Members = append(e.Bids[0].Alternatives[0].Members, Member{ID: "second", Profile: "vm", Arch: "aarch64", Backend: "microvm", Vec: Vec{MemBytes: 3}})
	a, err := Select(e)
	if err != nil || len(a.Picks) != 2 || a.TotalValueMicro != 6 {
		t.Fatalf("partial scratch placement leaked to residual: %+v %v", a, err)
	}
	if err := VerifyFull(a, e); err != nil {
		t.Fatal(err)
	}
}
func TestAwardVersionWalletAndAuthority(t *testing.T) {
	e := epoch()
	a, err := Select(e)
	if err != nil {
		t.Fatal(err)
	}
	mutated := e
	mutated.Versions.Wallet++
	if err := VerifyFull(a, mutated); err == nil {
		t.Fatal("stale wallet version accepted")
	}
	if _, err := Commit(context.Background(), nil, a, e); !errors.Is(err, ErrDenied) {
		t.Fatalf("nil governor granted %v", err)
	}
	gov := fakeGovernor{versions: e.Versions}
	reservation, err := Commit(context.Background(), gov, a, e)
	if err != nil || !reservation.Granted {
		t.Fatalf("valid host grant failed %+v %v", reservation, err)
	}
	if _, err := Commit(context.Background(), gov, a, mutated); !errors.Is(err, ErrConflict) {
		t.Fatalf("version mismatch bypass %v", err)
	}
	gov.versions.Lease++
	if _, err := Commit(context.Background(), gov, a, e); !errors.Is(err, ErrDenied) {
		t.Fatalf("governor lease-change grant %v", err)
	}
	// Requiring the whole certificate forbids forged partial grants.
	a.Certificate = nil
	if _, err := Commit(context.Background(), fakeGovernor{versions: e.Versions}, a, e); err == nil {
		t.Fatal("no certificate admitted")
	}
}

type fakeGovernor struct{ versions Versions }

func (g fakeGovernor) Reserve(_ context.Context, a Award, snapshot uint64) (Reservation, error) {
	return Reservation{ID: "host-only", Granted: true, SnapshotVersion: snapshot, WalletVersion: g.versions.Wallet, MembershipVersion: g.versions.Membership, LeaseVersion: g.versions.Lease}, nil
}
func TestMenuCapsUnknownFieldsAndBudgetHOLD(t *testing.T) {
	e := epoch()
	e.Limits.MaxAlternatives = 1
	if a, err := Select(e); !errors.Is(err, ErrHold) || !a.Hold || len(a.Picks) != 0 {
		t.Fatalf("oversized menu granted %+v %v", a, err)
	}
	e = epoch()
	e.Limits.MaxWork = 1
	if a, err := Select(e); !errors.Is(err, ErrHold) || !a.Hold {
		t.Fatalf("budget exhaustion granted %+v %v", a, err)
	}
	e = epoch()
	raw, err := json.Marshal(e)
	if err != nil {
		t.Fatal(err)
	}
	raw = append(raw[:len(raw)-1], []byte(`,"unrecognized_permission":true}`)...)
	if _, err := DecodeEpoch(raw, Limits{}); err == nil {
		t.Fatal("unknown field accepted")
	}
	if _, err := DecodeEpoch([]byte(strings.Repeat("x", 1<<20+1)), Limits{}); !errors.Is(err, ErrHold) {
		t.Fatalf("byte budget %v", err)
	}
}
func TestHighBidCannotOverridePressureClose(t *testing.T) {
	p := policy()
	s := sample(900)
	q, err := Price(s, p, alt("x", 1))
	if !errors.Is(err, ErrHold) || !q.Hold {
		t.Fatal("hard close bypassed")
	}
	e := epoch()
	e.Bids = e.Bids[:1]
	e.Quotes = e.Quotes[:1]
	e.Bids[0].Alternatives[0].ValueMicro = 999999999
	e.Quotes[0].Quote = q
	if a, err := Select(e); err == nil || !a.Hold {
		t.Fatalf("high bid bypassed HOLD %+v %v", a, err)
	}
}

func TestQuoteFreshnessAndTemporalConstraints(t *testing.T) {
	e := epoch()
	e.NowTick = 1
	if a, err := Select(e); err == nil || !a.Hold {
		t.Fatalf("old quote accepted: %+v %v", a, err)
	}
	e = epoch()
	e.Bids[0].Alternatives[0].Intervals = []Interval{{StartTick: 10, EndTick: 12}}
	if a, err := Select(e); err == nil || !a.Hold {
		t.Fatalf("after-deadline pack accepted: %+v %v", a, err)
	}
}

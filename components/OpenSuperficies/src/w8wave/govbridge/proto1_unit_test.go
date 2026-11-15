package govbridge

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"math"
	"math/rand"
	"reflect"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"opensuperficies.local/srf/src/w8wave/auction"
	"opensuperficies.local/srf/src/w8wave/pool"
)

// Every test host response originates in this independently controlled port;
// auction.Governor alone cannot authorize cleanup or release. Host counters
// record actual calls. No external process, network, image or disk I/O occurs.
type hostSpy struct {
	mu           sync.Mutex
	live         map[string]uint64
	clean        map[string]uint64
	keys         map[string]string
	denied       bool
	inDoubt      bool
	forge        bool
	versionFault string
	calls        uint64
	releaseCalls uint64
}

func (h *hostSpy) Reserve(_ context.Context, award auction.Award, snapshot uint64) (auction.Reservation, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.calls++
	if h.denied {
		return auction.Reservation{}, auction.ErrDenied
	}
	id := "hold-" + award.Picks[0].BidID
	if h.inDoubt {
		if h.live == nil {
			h.live = make(map[string]uint64)
		}
		h.live[id] = 1 // simulated host allocation despite an error
		return auction.Reservation{}, auction.ErrDenied
	}
	if h.forge {
		id = "forged-" + id
	}
	if !h.forge && h.versionFault == "" {
		if h.live == nil {
			h.live = make(map[string]uint64)
		}
		h.live[id] = 1
	}
	r := auction.Reservation{
		ID: id, SnapshotVersion: snapshot, WalletVersion: award.Versions.Wallet,
		MembershipVersion: award.Versions.Membership, LeaseVersion: award.Versions.Lease, Granted: true,
	}
	switch h.versionFault {
	case "snapshot":
		r.SnapshotVersion++
	case "wallet":
		r.WalletVersion++
	case "membership":
		r.MembershipVersion++
	case "lease":
		r.LeaseVersion++
	}
	return r, nil
}
func (h *hostSpy) VerifyNoHold(_ context.Context, _ string, _ auction.Award, _ uint64) error {
	h.mu.Lock()
	defer h.mu.Unlock()
	// This external trusted port attests *all* allocations of the attempted
	// transaction, not just a bidder-controlled reservation ID.
	if len(h.live) != 0 {
		return auction.ErrConflict
	}
	return nil
}

func (h *hostSpy) CheckCurrent(_ context.Context, r auction.Reservation, id string, generation uint64) error {
	h.mu.Lock()
	defer h.mu.Unlock()
	if r.ID != id || h.live[id] != generation || generation == 0 {
		return auction.ErrConflict
	}
	return nil
}
func (h *hostSpy) CheckCleanup(_ context.Context, r auction.Reservation, id string, generation uint64, proof pool.CleanupProof, key string) error {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.live[id] != generation || h.clean[id] != generation || r.ID != id ||
		proof == nil || !proof.Verified || proof.EntryID != id || proof.Generation != generation || key == "" {
		return errors.New("host cleanup has no independent trusted record")
	}
	return nil
}
func (h *hostSpy) ReleaseHold(_ context.Context, r auction.Reservation, id string, generation uint64, key string) error {
	h.mu.Lock()
	defer h.mu.Unlock()
	if r.ID != id || key == "" || h.live[id] != generation || h.clean[id] != generation {
		return auction.ErrConflict
	}
	if old := h.keys[id]; old != "" && old != key {
		return auction.ErrConflict
	}
	if h.keys == nil {
		h.keys = make(map[string]string)
	}
	h.keys[id] = key
	delete(h.live, id)
	h.releaseCalls++
	return nil
}
func (h *hostSpy) setGeneration(id string, gen uint64) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.live[id] = gen
}
func (h *hostSpy) trustedClean(id string, gen uint64) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.clean == nil {
		h.clean = make(map[string]uint64)
	}
	h.clean[id] = gen
}
func (h *hostSpy) counts() (uint64, uint64, int) {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.calls, h.releaseCalls, len(h.live)
}

type bootSpy struct{}

func (bootSpy) Acquire(string) (func(), error) { return func() {}, nil }

type cleanerSpy struct{ uncertain bool }

func (c *cleanerSpy) Clean(id string, generation uint64) (pool.CleanupProof, error) {
	if c.uncertain {
		return nil, errors.New("unknown host teardown")
	}
	return &pool.CleanupEvidence{EntryID: id, Generation: generation, Verified: true}, nil
}

type clockSpy struct{}

func (clockSpy) Now() time.Time { return time.Unix(44, 0) }

// In-memory sink spy: no frame pipeline is linked or invoked by this package.
// It provides a hard assertion that the test did not persist payload bytes.
type sinkSpy struct{ writes atomic.Uint64 }

func (s *sinkSpy) Write(p []byte) (int, error) {
	s.writes.Add(1)
	return 0, errors.New("no frame persistence permitted")
}

func config(v pool.Vec) Config { return Config{Ceiling: v, WaveCapacity: 64, SeatConcurrency: 2} }
func feasible() pool.Vec       { return pool.Vec{MemBytes: 8, CPUMilli: 6, PIDs: 2, Files: 3} }
func ceiling() pool.Vec        { return pool.Vec{MemBytes: 16, CPUMilli: 12, PIDs: 4, Files: 6} }
func newManaged(t *testing.T, h *hostSpy, cap pool.Vec, c *cleanerSpy) *ManagedPool {
	t.Helper()
	m, err := NewManagedPool(config(cap), h, bootSpy{}, c, clockSpy{}, "fixed-pin")
	if err != nil {
		t.Fatalf("new managed pool: %v", err)
	}
	return m
}
func template() pool.Template {
	return pool.Template{Storage: "verified-noswap-ram", TaskFree: true, Immutable: true, Pin: "fixed-pin", State: "clean"}
}

// A real MemoryLedger fixes the wallet evidence before the selector runs.
// The expected balance is independently computed below, not via production
// accounting code. The fixture's SHA256 comes from the Go standard library.
func fixture(t *testing.T, bid string, requested pool.Vec) (auction.Epoch, auction.Award, *auction.MemoryLedger) {
	t.Helper()
	ledger := auction.NewMemoryLedger()
	if err := ledger.CreateRoom("principal", "room", 100); err != nil {
		t.Fatal(err)
	}
	if err := ledger.FundPrincipal("principal", 100); err != nil {
		t.Fatal(err)
	}
	if _, err := ledger.Lock(auction.EscrowRequest{
		EscrowID: "escrow-" + bid, Principal: "principal", Room: "room", QuoteMicro: 4, MaxLiabilityMicro: 6,
	}); err != nil {
		t.Fatal(err)
	}
	wallet, err := ledger.Snapshot("principal", "room")
	if err != nil || wallet.AvailableMicro != 96 || wallet.EscrowMicro != 4 || wallet.OutstandingLiabilityMicro != 6 {
		t.Fatalf("independent initial wallet expectation: %+v %v", wallet, err)
	}
	v := ToAuction(requested)
	digest := sha256.Sum256([]byte("independent-suite-" + bid))
	member := auction.Member{ID: "member-" + bid, Profile: "vm", Arch: "amd64", Backend: "microvm", Vec: v}
	interval := auction.Interval{StartTick: 0, EndTick: 1}
	versions := auction.Versions{Snapshot: 1, Wallet: wallet.WalletVersion, Membership: 1, Lease: 3, Policy: 2}
	e := auction.Epoch{
		NowTick: 0, HostScope: "host-checked", Versions: versions,
		Capacity: []auction.CapacityPoint{{Tick: 0, Remaining: v}},
		Bids: []auction.PackBid{{
			Principal: "principal", Room: "room", EconomicGroup: bid,
			MembershipVersion: 1, BidID: bid, Revision: 1,
			SuiteSHA256: hex.EncodeToString(digest[:]), PolicyEpoch: 2,
			WalletAuth: "trusted", EarliestStartTick: 0, BidExpiryTick: 3, DeadlineTick: 2,
			Alternatives: []auction.Alternative{{
				ID: "alt-" + bid, ValueMicro: 10, MaxLiabilityMicro: 6,
				Members: []auction.Member{member}, Intervals: []auction.Interval{interval},
			}},
		}},
		Quotes: []auction.QuotedAlternative{{
			BidID: bid, AlternativeID: "alt-" + bid,
			Quote: auction.Quote{AlternativeID: "alt-" + bid, AmountMicro: 4, HostScope: "host-checked", Versions: versions, AtTick: 0},
		}},
		Wallets: []auction.WalletAccount{{
			Principal: "principal", Room: "room", AvailableMicro: wallet.AvailableMicro,
			ApprovedCapMicro: wallet.ApprovedCapMicro, OutstandingMicro: wallet.OutstandingLiabilityMicro,
		}},
		DemandWeights: auction.Vec{MemBytes: 1, CPUMilli: 1, PIDs: 1, Files: 1},
	}
	award, err := auction.Select(e)
	if err != nil {
		t.Fatalf("real auction.Select did not produce green producer fixture: %v", err)
	}
	// Independently constructed expected certificate: never derive expectations
	// by feeding selector or adapter output back into production methods.
	expected := auction.Award{
		Quality: "heuristic", Picks: []auction.Pick{{BidID: bid, AlternativeID: "alt-" + bid, QuoteMicro: 4}},
		Certificate: []auction.CertificateMember{{
			BidID: bid, AlternativeID: "alt-" + bid,
			Member: member, Intervals: []auction.Interval{{StartTick: 0, EndTick: 1}},
		}},
		TotalValueMicro: 10, TotalQuoteMicro: 4, Versions: versions, HostScope: "host-checked",
	}
	if !reflect.DeepEqual(award, expected) {
		t.Fatalf("independently expected award %+v, got %+v", expected, award)
	}
	if err := auction.VerifyFull(award, e); err != nil {
		t.Fatalf("real full verifier: %v", err)
	}
	return e, award, ledger
}

func faultIs(t *testing.T, err error, rc int, reason string) {
	t.Helper()
	var f *pool.Fault
	if !errors.As(err, &f) || f.RC != rc || f.Reason != reason {
		t.Fatalf("fault expected rc=%d reason=%q; got %#v", rc, reason, err)
	}
}
func zeroState(t *testing.T, m *ManagedPool, h *hostSpy, attempted uint64) {
	t.Helper()
	b := m.Balance()
	if b.Held != (pool.Vec{}) || b.Released != (DecimalVec{"0", "0", "0", "0"}) ||
		b.Reservations != 0 || b.Releases != 0 || b.ActiveSeats != 0 || b.HostAdmits != 0 || b.HostReleases != 0 || b.HostAttempts != attempted {
		t.Fatalf("refusal changed resource/host state: %+v", b)
	}
	calls, releases, live := h.counts()
	if calls != attempted || releases != 0 || live != 0 {
		t.Fatalf("refusal leaked host hold or release: calls=%d releases=%d live=%d", calls, releases, live)
	}
}
func reserveFixture(t *testing.T, m *ManagedPool, id string, v pool.Vec) (*auction.MemoryLedger, auction.Epoch) {
	t.Helper()
	e, a, l := fixture(t, id[5:], v) // id = hold-<bid>
	if err := m.Stage(context.Background(), id, v, a, e); err != nil {
		t.Fatal(err)
	}
	if err := m.Reserve(id, v, m.Snapshot().Version); err != nil {
		t.Fatal(err)
	}
	return l, e
}
func advanceToCleanup(t *testing.T, m *ManagedPool, h *hostSpy, id string) (pool.Lease, pool.CleanupProof) {
	t.Helper()
	if err := m.ColdReady(id, template()); err != nil {
		t.Fatal(err)
	}
	lease, err := m.Claim(id, m.Snapshot().Version)
	if err != nil {
		t.Fatal(err)
	}
	h.setGeneration(id, lease.Generation)
	if err := m.BindGeneration(context.Background(), id, lease.Generation); err != nil {
		t.Fatal(err)
	}
	for _, step := range []struct {
		name string
		run  func(string) error
	}{
		{"dirty", m.MarkDirty}, {"fence", m.Fence}, {"collect", m.Collect}, {"stop", m.Stop}, {"purge", m.Purge},
	} {
		if err := step.run(id); err != nil {
			t.Fatalf("step %s: %v", step.name, err)
		}
	}
	proof, err := m.Verify(id)
	if err != nil {
		t.Fatal(err)
	}
	return lease, proof
}

func TestProto1Governor(t *testing.T) {
	// Named, finite test bodies are also the single gate entry point.
	const seed int64 = 13
	t.Logf("proto1-governor: fixed_seed=%d, host_results=AUSENTE, budgets=TARGET, sink=memory-only", seed)

	t.Run("green_real_ledger_selector_commit_cleanup", func(t *testing.T) {
		h := &hostSpy{}
		sink := &sinkSpy{}
		m := newManaged(t, h, ceiling(), &cleanerSpy{})
		v := feasible()
		ledger, _ := reserveFixture(t, m, "hold-positive", v)
		before := m.Balance()
		if before.Held != v || before.Reservations != 1 || before.ActiveSeats != 1 || before.HostAdmits != 1 || before.Releases != 0 {
			t.Fatalf("reservation must stay charged: %+v", before)
		}
		// No expired bid or pool cancellation intent frees a running hold.
		state, err := auction.Transition(auction.Reserved, auction.Event{Expected: auction.Reserved, Kind: "STOP"})
		if err != nil || state != auction.Preempting {
			t.Fatalf("real queue reducer STOP: %s %v", state, err)
		}
		faultIs(t, m.Release(context.Background(), "hold-positive", nil, "release-key"), 5, "missing or stale cleanup witness")
		if m.Balance().Held != v {
			t.Fatal("STOP or missing cleanup refunded a hold")
		}
		lease, proof := advanceToCleanup(t, m, h, "hold-positive")
		// Pool-cleaner proof alone is not host authentication.
		faultIs(t, m.Release(context.Background(), "hold-positive", proof, "release-key"), 6, "cleanup indeterminate; reservation stays charged")
		if m.Balance().Held != v {
			t.Fatal("unverified cleanup reduced capacity")
		}
		h.trustedClean("hold-positive", lease.Generation)
		if err := m.Release(context.Background(), "hold-positive", proof, "release-key"); err != nil {
			t.Fatal(err)
		}
		after := m.Balance()
		wantReleased := DecimalVec{"8", "6", "2", "3"} // independent expected arithmetic
		if after.Held != (pool.Vec{}) || after.Released != wantReleased || after.Releases != 1 || after.HostReleases != 1 || after.ActiveSeats != 0 {
			t.Fatalf("exactly-once release counters: %+v", after)
		}
		if err := m.Release(context.Background(), "hold-positive", proof, "release-key"); err != nil {
			t.Fatalf("same-key replay: %v", err)
		}
		if m.Balance() != after {
			t.Fatal("same-key replay mutated counters")
		}
		faultIs(t, m.Release(context.Background(), "hold-positive", proof, "different-key"), 5, "already released with another key")
		faultIs(t, m.adapter.AuthorizeRelease(context.Background(), "hold-positive", proof, "different-key"), 5, "already released with another key")
		if m.Balance() != after {
			t.Fatal("different-key replay mutated counters")
		}
		calls, releases, live := h.counts()
		if calls != 1 || releases != 1 || live != 0 || sink.writes.Load() != 0 {
			t.Fatalf("actual host/sink counters calls=%d releases=%d live=%d sink=%d", calls, releases, live, sink.writes.Load())
		}
		// Real ledger settlement is separately authenticated: release itself
		// never manufactures an escrow refund or a wallet grant.
		receipt, err := ledger.Settle(auction.SettlementRequest{SettlementID: "settle-positive", EscrowID: "escrow-positive", Principal: "principal", Room: "room", DebitMicro: 3})
		if err != nil || receipt.DebitMicro != 3 || receipt.RefundedMicro != 1 {
			t.Fatalf("real ledger settlement %+v %v", receipt, err)
		}
		wallet, err := ledger.Snapshot("principal", "room")
		if err != nil || wallet.AvailableMicro != 97 || wallet.OutstandingLiabilityMicro != 0 || wallet.EscrowMicro != 0 || wallet.DebitedMicro != 3 {
			t.Fatalf("independent expected wallet (100-4+1) %+v %v", wallet, err)
		}
		t.Logf("GREEN: selected=1, committed=1, held_before=%+v, released=%+v, host_calls=%d, host_releases=%d, sink_writes=%d", before.Held, after.Released, calls, releases, sink.writes.Load())
	})

	t.Run("must_fail_overflow_each_dimension", func(t *testing.T) {
		for _, d := range []string{"mem_bytes", "cpu_milli", "pids", "files"} {
			t.Run(d, func(t *testing.T) {
				var x, y auction.Vec
				switch d {
				case "mem_bytes":
					x.MemBytes = math.MaxUint64
					y.MemBytes = 1
				case "cpu_milli":
					x.CPUMilli = math.MaxUint64
					y.CPUMilli = 1
				case "pids":
					x.PIDs = math.MaxUint64
					y.PIDs = 1
				case "files":
					x.Files = math.MaxUint64
					y.Files = 1
				}
				award := auction.Award{Certificate: []auction.CertificateMember{{Member: auction.Member{Vec: x}}, {Member: auction.Member{Vec: y}}}}
				got, err := SumCertificate(award)
				if got != (pool.Vec{}) || err != auction.ErrOverflow {
					t.Fatalf("must-fail checked overflow %s: %+v %v", d, got, err)
				}
				h := &hostSpy{}
				m := newManaged(t, h, ceiling(), &cleanerSpy{})
				zeroState(t, m, h, 0)
				t.Logf("MUST-FAIL: overflow=%s rc=%d", d, auction.ExitCode(err))
			})
		}
	})

	t.Run("must_fail_swapped_units_and_missing_config", func(t *testing.T) {
		h := &hostSpy{}
		m := newManaged(t, h, ceiling(), &cleanerSpy{})
		e, award, _ := fixture(t, "wrong-units", feasible())
		wrong := pool.Vec{MemBytes: 6, CPUMilli: 8, PIDs: 2, Files: 3}
		faultIs(t, m.Stage(context.Background(), "hold-wrong-units", wrong, award, e), 2, "pool and auction resource dimensions disagree")
		zeroState(t, m, h, 0)
		for _, c := range []Config{{Ceiling: pool.Vec{}, WaveCapacity: 64, SeatConcurrency: 2}, {Ceiling: feasible(), WaveCapacity: 0, SeatConcurrency: 2}} {
			_, err := New(c, h)
			faultIs(t, err, 4, "configured capacity unavailable")
		}
		t.Log("MUST-FAIL: swapped CPU-memory units and absent finite capacity")
	})

	t.Run("must_fail_independent_resource_ceilings", func(t *testing.T) {
		for _, dimension := range []string{"mem_bytes", "cpu_milli", "pids", "files"} {
			t.Run(dimension, func(t *testing.T) {
				h := &hostSpy{}
				m := newManaged(t, h, feasible(), &cleanerSpy{})
				v := feasible()
				switch dimension {
				case "mem_bytes":
					v.MemBytes++
				case "cpu_milli":
					v.CPUMilli++
				case "pids":
					v.PIDs++
				case "files":
					v.Files++
				}
				e, award, _ := fixture(t, dimension, v)
				faultIs(t, m.Stage(context.Background(), "hold-"+dimension, v, award, e), 4, "requested resources exceed configured capacity")
				zeroState(t, m, h, 0)
			})
		}
		t.Log("MUST-FAIL: independent ceiling for each resource dimension; no host calls")
	})

	t.Run("green_cancel_after_admission_retains_hold", func(t *testing.T) {
		h := &hostSpy{}
		m := newManaged(t, h, ceiling(), &cleanerSpy{})
		ctx, cancel := context.WithCancel(context.Background())
		v := feasible()
		e, award, _ := fixture(t, "cancel-after", v)
		if err := m.Stage(ctx, "hold-cancel-after", v, award, e); err != nil {
			t.Fatal(err)
		}
		if err := m.Reserve("hold-cancel-after", v, m.Snapshot().Version); err != nil {
			t.Fatal(err)
		}
		charged := m.Balance()
		cancel()
		if m.Balance() != charged || charged.Held != v || charged.ActiveSeats != 1 || charged.HostReleases != 0 {
			t.Fatalf("cancel after confirmed reservation returned a hold: %+v", m.Balance())
		}
		calls, releases, live := h.counts()
		if calls != 1 || releases != 0 || live != 1 {
			t.Fatal("cancellation changed host lease")
		}
		t.Log("GREEN: cancellation after admission retains host and pool charges until trusted cleanup")
	})

	t.Run("must_fail_stale_versions_before_host", func(t *testing.T) {
		for _, field := range []string{"snapshot", "wallet", "membership", "lease"} {
			t.Run(field, func(t *testing.T) {
				h := &hostSpy{}
				m := newManaged(t, h, ceiling(), &cleanerSpy{})
				e, award, _ := fixture(t, field, feasible())
				switch field {
				case "snapshot":
					e.Versions.Snapshot++
				case "wallet":
					e.Versions.Wallet++
				case "membership":
					e.Versions.Membership++
				case "lease":
					e.Versions.Lease++
				}
				err := m.Stage(context.Background(), "hold-"+field, feasible(), award, e)
				if !errors.Is(err, auction.ErrConflict) {
					t.Fatalf("version %s must conflict: %v", field, err)
				}
				faultIs(t, err, 5, "stale award versions")
				zeroState(t, m, h, 0)
			})
		}
		t.Log("MUST-FAIL: four stale immutable version bindings, zero host calls")
	})

	t.Run("must_fail_forged_denied_and_stale_host_receipts", func(t *testing.T) {
		for _, scenario := range []string{"forged", "denied", "snapshot", "wallet", "membership", "lease"} {
			t.Run(scenario, func(t *testing.T) {
				h := &hostSpy{forge: scenario == "forged", denied: scenario == "denied"}
				if !h.forge && !h.denied {
					h.versionFault = scenario
				}
				m := newManaged(t, h, ceiling(), &cleanerSpy{})
				e, award, _ := fixture(t, scenario, feasible())
				if err := m.Stage(context.Background(), "hold-"+scenario, feasible(), award, e); err != nil {
					t.Fatal(err)
				}
				err := m.Reserve("hold-"+scenario, feasible(), m.Snapshot().Version)
				if !errors.Is(err, auction.ErrDenied) {
					t.Fatalf("bad host reservation admitted: %v", err)
				}
				// An invalid host receipt does not imply host rollback. This host
				// spy's invalid replies are explicitly allocation-free.
				zeroState(t, m, h, 1)
			})
		}
		t.Log("MUST-FAIL: forged/denied/4 stale host replies; 6 attempted, 0 accepted or released")
	})

	t.Run("must_fail_wrong_id_generation_and_expiry", func(t *testing.T) {
		h := &hostSpy{}
		m := newManaged(t, h, ceiling(), &cleanerSpy{})
		v := feasible()
		reserveFixture(t, m, "hold-keep", v)
		before := m.Balance()
		faultIs(t, m.Release(context.Background(), "hold-wrong", &pool.CleanupEvidence{EntryID: "hold-wrong", Generation: 1, Verified: true}, "k"), 5, "missing host reservation")
		faultIs(t, m.Release(context.Background(), "hold-keep", &pool.CleanupEvidence{EntryID: "hold-keep", Generation: 2, Verified: true}, "k"), 5, "missing or stale cleanup witness")
		state, err := auction.Transition(auction.Reserved, auction.Event{Expected: auction.Reserved, Kind: "STOP"})
		if err != nil || state != auction.Preempting {
			t.Fatalf("cancelled reserved queue leaked %s %v", state, err)
		}
		expiredState, expiredErr := auction.Transition(auction.Reserved, auction.Event{Expected: auction.Reserved, Kind: "expire"})
		if expiredState != auction.Reserved || !errors.Is(expiredErr, auction.ErrInvalid) {
			t.Fatalf("expiry after reserve must not free lease: %s %v", expiredState, expiredErr)
		}
		if err := m.Quarantine("hold-keep"); err != nil {
			t.Fatal(err)
		}
		faultIs(t, m.Release(context.Background(), "hold-keep", &pool.CleanupEvidence{EntryID: "hold-keep", Generation: 1, Verified: true}, "k"), 6, "cleanup indeterminate; reservation stays charged")
		if m.Balance() != before {
			t.Fatalf("expired/quarantined entry credited capacity: before=%+v after=%+v", before, m.Balance())
		}
		calls, releases, live := h.counts()
		if calls != 1 || releases != 0 || live != 1 {
			t.Fatalf("wrong cleanup changed host: %d %d %d", calls, releases, live)
		}
		t.Log("MUST-FAIL: wrong ID/generation, canceled/expired, quarantined without cleanup retained exact hold")
	})

	t.Run("must_fail_ambiguous_host_allocation_keeps_shadow_hold", func(t *testing.T) {
		h := &hostSpy{inDoubt: true}
		v := feasible()
		m := newManaged(t, h, v, &cleanerSpy{})
		e, award, _ := fixture(t, "indeterminate", v)
		if err := m.Stage(context.Background(), "hold-indeterminate", v, award, e); err != nil {
			t.Fatal(err)
		}
		err := m.Reserve("hold-indeterminate", v, m.Snapshot().Version)
		if !errors.Is(err, auction.ErrDenied) {
			t.Fatalf("must preserve original denial: %v", err)
		}
		faultIs(t, err, 6, "admission indeterminate; host capacity quarantined")
		b := m.Balance()
		if b.Held != v || b.Indeterminate != 1 || b.ActiveSeats != 1 || b.Reservations != 0 || b.Releases != 0 || b.HostAttempts != 1 || b.HostReleases != 0 {
			t.Fatalf("failed host admission was incorrectly refunded: %+v", b)
		}
		// Pool could not create an entry after governor refusal, therefore the
		// adapter must charge this *shadow* resource until host reconciliation.
		if m.Snapshot().Holds != (pool.Vec{}) {
			t.Fatal("pool falsely admitted an indeterminate reservation")
		}
		if err := m.Stage(context.Background(), "hold-further", v, award, e); err != nil {
			t.Fatal(err)
		}
		faultIs(t, m.Reserve("hold-further", v, m.Snapshot().Version), 4, "host resource ceiling exhausted")
		calls, releases, live := h.counts()
		if calls != 1 || releases != 0 || live != 1 {
			t.Fatalf("unsafe retry consumed host resources: %d %d %d", calls, releases, live)
		}
		t.Log("MUST-FAIL: ambiguous host call retains separately accounted shadow hold; no speculative release")
	})

	t.Run("must_fail_cancellation_and_barrier_capacity", func(t *testing.T) {
		v := pool.Vec{MemBytes: 4, CPUMilli: 4, PIDs: 1, Files: 2}
		cap := pool.Vec{MemBytes: 16, CPUMilli: 16, PIDs: 4, Files: 8}
		h := &hostSpy{}
		m := newManaged(t, h, cap, &cleanerSpy{})
		ctx, cancel := context.WithCancel(context.Background())
		e, award, _ := fixture(t, "cancelled", v)
		if err := m.Stage(ctx, "hold-cancelled", v, award, e); err != nil {
			t.Fatal(err)
		}
		cancel()
		if err := m.Reserve("hold-cancelled", v, m.Snapshot().Version); !errors.Is(err, context.Canceled) {
			t.Fatalf("canceled before admission %v", err)
		}
		zeroState(t, m, h, 0)
		if m.Balance().Pending != 0 {
			t.Fatalf("canceled before host call leaked a staged wave slot: %+v", m.Balance())
		}
		const n = 16
		permutation := rand.New(rand.NewSource(seed)).Perm(n)
		for _, i := range permutation {
			bid := fmt.Sprintf("race-%02d", i)
			e, award, _ := fixture(t, bid, v)
			if err := m.Stage(context.Background(), "hold-"+bid, v, award, e); err != nil {
				t.Fatal(err)
			}
		}
		start := make(chan struct{})
		results := make(chan error, n)
		var wg sync.WaitGroup
		for _, i := range permutation {
			id := fmt.Sprintf("hold-race-%02d", i)
			wg.Add(1)
			go func() {
				defer wg.Done()
				<-start
				for attempt := 0; attempt < n+1; attempt++ {
					err := m.Reserve(id, v, m.Snapshot().Version)
					if pool.CodeOf(err) == 5 && err != nil && err.Error() == "pool rc 5: stale reservation version" {
						continue
					}
					results <- err
					return
				}
				results <- errors.New("unbounded stale retries")
			}()
		}
		close(start) // deterministic barrier: no worker starts before all are ready
		wg.Wait()
		close(results)
		wins, backpressure := 0, 0
		for err := range results {
			switch {
			case err == nil:
				wins++
			case pool.CodeOf(err) == 4:
				backpressure++
			default:
				t.Fatalf("race unexpected result: %v", err)
			}
		}
		if wins != 2 || backpressure != n-2 {
			t.Fatalf("race admitted %d instead of 2, blocked %d instead of %d", wins, backpressure, n-2)
		}
		balance := m.Balance()
		// Independent expected vector = 2 * each field, not a value read
		// back from the implementation itself.
		if balance.Held != (pool.Vec{MemBytes: 8, CPUMilli: 8, PIDs: 2, Files: 4}) ||
			balance.ActiveSeats != 2 || balance.Reservations != 2 || balance.HostAdmits != 2 || balance.HostReleases != 0 {
			t.Fatalf("barrier overcommit/release: %+v", balance)
		}
		calls, releases, live := h.counts()
		if calls != 2 || releases != 0 || live != 2 {
			t.Fatalf("host overcommit calls=%d releases=%d live=%d", calls, releases, live)
		}
		t.Logf("GREEN barrier: seed=%d, workers=%d, accepted=%d, capacity_refused=%d, live_host=%d", seed, n, wins, backpressure, live)
	})
}

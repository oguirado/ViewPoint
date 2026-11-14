package auction

import (
	"errors"
	"sync"
	"testing"
)

func funded(t *testing.T) *MemoryLedger {
	t.Helper()
	l := NewMemoryLedger()
	if err := l.CreateRoom("p", "r", 100); err != nil {
		t.Fatal(err)
	}
	if err := l.FundPrincipal("p", 100); err != nil {
		t.Fatal(err)
	}
	return l
}
func TestEscrowSettlementReplayAndRefund(t *testing.T) {
	l := funded(t)
	req := EscrowRequest{EscrowID: "e", Principal: "p", Room: "r", QuoteMicro: 80, MaxLiabilityMicro: 80}
	if _, err := l.Lock(req); err != nil {
		t.Fatal(err)
	}
	if _, err := l.Lock(req); err != nil {
		t.Fatal("identical lock replay", err)
	}
	settle := SettlementRequest{SettlementID: "s", EscrowID: "e", Principal: "p", Room: "r", DebitMicro: 50}
	r, err := l.Settle(settle)
	if err != nil || r.RefundedMicro != 30 {
		t.Fatalf("settle %+v %v", r, err)
	}
	if r2, err := l.Settle(settle); err != nil || r2 != r {
		t.Fatal("identical replay must be immutable", r2, err)
	}
	balance, err := l.Snapshot("p", "r")
	if err != nil || balance.AvailableMicro != 50 || balance.EscrowMicro != 0 || balance.DebitedMicro != 50 || balance.OutstandingLiabilityMicro != 0 {
		t.Fatalf("100-80+30=50 control: %+v %v", balance, err)
	}
	settle.DebitMicro = 49
	if _, err := l.Settle(settle); !errors.Is(err, ErrConflict) {
		t.Fatal("changed settlement body accepted", err)
	}
	settle.SettlementID = "refund-again"
	settle.DebitMicro = 0
	if _, err := l.Settle(settle); !errors.Is(err, ErrConflict) {
		t.Fatal("duplicate refund minted money", err)
	}
}
func TestSettlementAboveEscrowAndProtectedCap(t *testing.T) {
	l := funded(t)
	req := EscrowRequest{EscrowID: "e", Principal: "p", Room: "r", QuoteMicro: 80, MaxLiabilityMicro: 80}
	if err := l.ProtectFinalization("p", "r", 30); err != nil {
		t.Fatal(err)
	}
	if _, err := l.Lock(req); !errors.Is(err, ErrDenied) {
		t.Fatal("liability above cap accepted", err)
	}
	if err := l.ProtectFinalization("p", "r", 20); err != nil {
		t.Fatal(err)
	}
	if _, err := l.Lock(req); err != nil {
		t.Fatal(err)
	}
	if _, err := l.Settle(SettlementRequest{SettlementID: "s", EscrowID: "e", Principal: "p", Room: "r", DebitMicro: 81}); !errors.Is(err, ErrDenied) {
		t.Fatal("settlement above escrow accepted", err)
	}
	if _, err := l.Settle(SettlementRequest{SettlementID: "s", EscrowID: "e", Principal: "p", Room: "r", DebitMicro: 50}); err != nil {
		t.Fatal("recovery after refused settlement", err)
	}
}
func TestConcurrentSharedPrincipalDoubleSpend(t *testing.T) {
	l := funded(t)
	if err := l.CreateRoom("p", "second", 100); err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	wg.Add(2)
	results := make(chan error, 2)
	for _, room := range []string{"r", "second"} {
		r := room
		go func() {
			defer wg.Done()
			_, err := l.Lock(EscrowRequest{EscrowID: r, Principal: "p", Room: r, QuoteMicro: 80, MaxLiabilityMicro: 80})
			results <- err
		}()
	}
	wg.Wait()
	close(results)
	success, deny := 0, 0
	for err := range results {
		if err == nil {
			success++
		} else if errors.Is(err, ErrDenied) {
			deny++
		} else {
			t.Fatalf("unexpected error %v", err)
		}
	}
	if success != 1 || deny != 1 {
		t.Fatalf("two concurrent escrows spent same 100 credits: successes=%d denials=%d", success, deny)
	}
}

func TestConcurrentSettlementsOnlyOneDebit(t *testing.T) {
	l := funded(t)
	if _, err := l.Lock(EscrowRequest{EscrowID: "e", Principal: "p", Room: "r", QuoteMicro: 80, MaxLiabilityMicro: 80}); err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	wg.Add(2)
	results := make(chan error, 2)
	for _, id := range []string{"settle-a", "settle-b"} {
		id := id
		go func() {
			defer wg.Done()
			_, err := l.Settle(SettlementRequest{SettlementID: id, EscrowID: "e", Principal: "p", Room: "r", DebitMicro: 50})
			results <- err
		}()
	}
	wg.Wait()
	close(results)
	wins, conflicts := 0, 0
	for err := range results {
		if err == nil {
			wins++
		} else if errors.Is(err, ErrConflict) {
			conflicts++
		} else {
			t.Fatal(err)
		}
	}
	s, err := l.Snapshot("p", "r")
	if err != nil || wins != 1 || conflicts != 1 || s.DebitedMicro != 50 || s.EscrowMicro != 0 || s.AvailableMicro != 50 {
		t.Fatalf("double settlement: wins=%d conflicts=%d balance=%+v err=%v", wins, conflicts, s, err)
	}
}

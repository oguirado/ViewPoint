package auction

import (
	"fmt"
	"sync"
)

// Ledger is a host-side port: no credit-to-money or provider-quota conversion
// exists. The in-memory adapter below is a deterministic reference / fake,
// NOT a durable ledger or admission mechanism.
type Ledger interface {
	Lock(EscrowRequest) (EscrowReceipt, error)
	Settle(SettlementRequest) (SettlementReceipt, error)
	Snapshot(string, string) (WalletSnapshot, error)
}

type EscrowRequest struct {
	EscrowID          string `json:"escrow_id"`
	Principal         string `json:"principal"`
	Room              string `json:"room"`
	QuoteMicro        uint64 `json:"quote_micro"`
	MaxLiabilityMicro uint64 `json:"max_liability_micro"`
}

type EscrowReceipt struct {
	EscrowID       string `json:"escrow_id"`
	LockedMicro    uint64 `json:"locked_micro"`
	LiabilityMicro uint64 `json:"liability_micro"`
}

type SettlementRequest struct {
	SettlementID string `json:"settlement_id"`
	EscrowID     string `json:"escrow_id"`
	Principal    string `json:"principal"`
	Room         string `json:"room"`
	DebitMicro   uint64 `json:"debit_micro"`
}

type SettlementReceipt struct {
	SettlementID  string `json:"settlement_id"`
	DebitMicro    uint64 `json:"debit_micro"`
	RefundedMicro uint64 `json:"refunded_micro"`
}

type WalletSnapshot struct {
	Principal                  string `json:"principal"`
	Room                       string `json:"room"`
	AvailableMicro             uint64 `json:"available_micro"`
	EscrowMicro                uint64 `json:"escrow_micro"`
	DebitedMicro               uint64 `json:"debited_micro"`
	OutstandingLiabilityMicro  uint64 `json:"outstanding_liability_micro"`
	ProtectedFinalizationMicro uint64 `json:"protected_finalization_micro"`
	ApprovedCapMicro           uint64 `json:"approved_cap_micro"`
	WalletVersion              uint64 `json:"wallet_version"`
}

type roomWallet struct{ approved, settled, outstanding, protected, escrow uint64 }
type escrowRecord struct {
	request      EscrowRequest
	receipt      EscrowReceipt
	settled      bool
	settlementID string
}
type settlementRecord struct {
	request SettlementRequest
	receipt SettlementReceipt
}

// MemoryLedger atomically guards a shared principal balance and per-room
// liability caps, preventing cross-room double spend under concurrency.
type MemoryLedger struct {
	mu          sync.Mutex
	funds       map[string]uint64
	rooms       map[string]*roomWallet
	escrows     map[string]*escrowRecord
	settlements map[string]settlementRecord
	version     uint64
}

func NewMemoryLedger() *MemoryLedger {
	return &MemoryLedger{funds: map[string]uint64{}, rooms: map[string]*roomWallet{}, escrows: map[string]*escrowRecord{}, settlements: map[string]settlementRecord{}}
}
func walletKey(p, r string) string { return fmt.Sprintf("%d:%s:%s", len(p), p, r) }

// CreateRoom installs an operator-approved cap, without creating any credits.
func (l *MemoryLedger) CreateRoom(principal, room string, cap uint64) error {
	if principal == "" || room == "" || cap == 0 {
		return ErrInvalid
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	id := walletKey(principal, room)
	if _, ok := l.rooms[id]; ok {
		return ErrConflict
	}
	l.rooms[id] = &roomWallet{approved: cap}
	l.version++
	return nil
}

// FundPrincipal is an EXPLICIT trusted funding operation, not a refund path.
// The caller is responsible for validating its external source of credits.
func (l *MemoryLedger) FundPrincipal(principal string, amount uint64) error {
	if principal == "" || amount == 0 {
		return ErrInvalid
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	next, err := add(l.funds[principal], amount)
	if err != nil {
		return err
	}
	l.funds[principal] = next
	l.version++
	return nil
}

// ProtectFinalization reserves part of a room cap for unfinished cleanups.
func (l *MemoryLedger) ProtectFinalization(principal, room string, amount uint64) error {
	l.mu.Lock()
	defer l.mu.Unlock()
	w, ok := l.rooms[walletKey(principal, room)]
	if !ok {
		return ErrInvalid
	}
	capUsed, err := add(w.settled, w.outstanding)
	if err != nil {
		return err
	}
	capUsed, err = add(capUsed, amount)
	if err != nil {
		return err
	}
	if capUsed > w.approved {
		return ErrDenied
	}
	w.protected = amount
	l.version++
	return nil
}

func (l *MemoryLedger) Lock(req EscrowRequest) (EscrowReceipt, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if req.EscrowID == "" || req.Principal == "" || req.Room == "" || req.QuoteMicro == 0 || req.MaxLiabilityMicro < req.QuoteMicro {
		return EscrowReceipt{}, ErrInvalid
	}
	if old, ok := l.escrows[req.EscrowID]; ok {
		if old.request != req {
			return EscrowReceipt{}, ErrConflict
		}
		return old.receipt, nil
	}
	w, ok := l.rooms[walletKey(req.Principal, req.Room)]
	if !ok {
		return EscrowReceipt{}, ErrDenied
	}
	if l.funds[req.Principal] < req.QuoteMicro {
		return EscrowReceipt{}, ErrDenied
	}
	used, err := add(w.settled, w.outstanding)
	if err != nil {
		return EscrowReceipt{}, err
	}
	used, err = add(used, w.protected)
	if err != nil {
		return EscrowReceipt{}, err
	}
	used, err = add(used, req.MaxLiabilityMicro)
	if err != nil {
		return EscrowReceipt{}, err
	}
	if used > w.approved {
		return EscrowReceipt{}, ErrDenied
	}
	escrow, err := add(w.escrow, req.QuoteMicro)
	if err != nil {
		return EscrowReceipt{}, err
	}
	outstanding, err := add(w.outstanding, req.MaxLiabilityMicro)
	if err != nil {
		return EscrowReceipt{}, err
	}
	l.funds[req.Principal] -= req.QuoteMicro
	w.escrow = escrow
	w.outstanding = outstanding
	receipt := EscrowReceipt{req.EscrowID, req.QuoteMicro, req.MaxLiabilityMicro}
	l.escrows[req.EscrowID] = &escrowRecord{request: req, receipt: receipt}
	l.version++
	return receipt, nil
}

// Settle transfers already-locked escrow to a debit plus a refund. Replayed
// settlement IDs are immutable. A second ID for the same escrow is refused.
func (l *MemoryLedger) Settle(req SettlementRequest) (SettlementReceipt, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if req.SettlementID == "" || req.EscrowID == "" || req.Principal == "" || req.Room == "" {
		return SettlementReceipt{}, ErrInvalid
	}
	if old, ok := l.settlements[req.SettlementID]; ok {
		if old.request != req {
			return SettlementReceipt{}, ErrConflict
		}
		return old.receipt, nil
	}
	esc, ok := l.escrows[req.EscrowID]
	if !ok || esc.request.Principal != req.Principal || esc.request.Room != req.Room || esc.settled {
		return SettlementReceipt{}, ErrConflict
	}
	if req.DebitMicro > esc.request.QuoteMicro {
		return SettlementReceipt{}, ErrDenied
	}
	w := l.rooms[walletKey(req.Principal, req.Room)]
	refund := esc.request.QuoteMicro - req.DebitMicro
	nextFunds, err := add(l.funds[req.Principal], refund)
	if err != nil {
		return SettlementReceipt{}, err
	}
	debit, err := add(w.settled, req.DebitMicro)
	if err != nil {
		return SettlementReceipt{}, err
	}
	if w.outstanding < esc.request.MaxLiabilityMicro || w.escrow < esc.request.QuoteMicro {
		return SettlementReceipt{}, ErrInvalid
	}
	l.funds[req.Principal] = nextFunds
	w.settled = debit
	w.outstanding -= esc.request.MaxLiabilityMicro
	w.escrow -= esc.request.QuoteMicro
	esc.settled = true
	esc.settlementID = req.SettlementID
	receipt := SettlementReceipt{req.SettlementID, req.DebitMicro, refund}
	l.settlements[req.SettlementID] = settlementRecord{req, receipt}
	l.version++
	return receipt, nil
}
func (l *MemoryLedger) Snapshot(principal, room string) (WalletSnapshot, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	w, ok := l.rooms[walletKey(principal, room)]
	if !ok {
		return WalletSnapshot{}, ErrInvalid
	}
	return WalletSnapshot{Principal: principal, Room: room, AvailableMicro: l.funds[principal], EscrowMicro: w.escrow, DebitedMicro: w.settled, OutstandingLiabilityMicro: w.outstanding, ProtectedFinalizationMicro: w.protected, ApprovedCapMicro: w.approved, WalletVersion: l.version}, nil
}

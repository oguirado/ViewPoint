package puertos

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"syscall"
)

// Default port configuration and limits for OpenSuperficies capsules.
const (
	DefaultXpraBase uint16 = 10020
	DefaultCDPBase  uint16 = 9220
	DefaultN        uint16 = 80

	// ExitCodeLockCollision is the deterministic exit code (69) on exclusive lock collision.
	ExitCodeLockCollision = 69
)

// ErrLockCollision is returned when an exclusive slot lock cannot be acquired.
var ErrLockCollision = errors.New("slot lock collision")

// SrfParPuertos contains the allocated Xpra and CDP ports for a specific slot.
type SrfParPuertos struct {
	Slot uint16 `json:"slot"`
	Xpra uint16 `json:"xpra"`
	CDP  uint16 `json:"cdp"`
}

// SrfPlanPuertos defines the port allocation plan across N slots.
type SrfPlanPuertos struct {
	XpraBase uint16 `json:"xpra_base"`
	CDPBase  uint16 `json:"cdp_base"`
	N        uint16 `json:"n"`
}

// NuevoPlanPuertos returns an initialized SrfPlanPuertos with canonical defaults:
// XpraBase=10020, CDPBase=9220, N=80.
func NuevoPlanPuertos() SrfPlanPuertos {
	return SrfPlanPuertos{
		XpraBase: DefaultXpraBase,
		CDPBase:  DefaultCDPBase,
		N:        DefaultN,
	}
}

// calcularCDP computes the CDP port from cdpBase, xpra, and slot.
// Can be overridden via build tags (e.g. mutante_formula_v1).
var calcularCDP = func(cdpBase uint16, xpra uint16, slot uint16) uint16 {
	return cdpBase + slot
}

// Par calculates the port pair for the given slot s.
// Returns an error if s >= p.N.
func (p SrfPlanPuertos) Par(s uint16) (SrfParPuertos, error) {
	if s >= p.N {
		return SrfParPuertos{}, fmt.Errorf("slot %d fuera de rango (límite n=%d)", s, p.N)
	}
	xpra := p.XpraBase + s
	cdp := calcularCDP(p.CDPBase, xpra, s)
	return SrfParPuertos{
		Slot: s,
		Xpra: xpra,
		CDP:  cdp,
	}, nil
}

// LockCollisionError records slot lock collision details.
type LockCollisionError struct {
	Slot uint16
	Path string
	Err  error
}

func (e *LockCollisionError) Error() string {
	return fmt.Sprintf("slot %d lock collision on %s: %v", e.Slot, e.Path, e.Err)
}

func (e *LockCollisionError) Unwrap() error {
	return e.Err
}

// IsLockCollision returns true if err indicates a lock collision (EWOULDBLOCK, EAGAIN, or ErrLockCollision).
func IsLockCollision(err error) bool {
	if err == nil {
		return false
	}
	var lce *LockCollisionError
	if errors.As(err, &lce) {
		return true
	}
	return errors.Is(err, ErrLockCollision) ||
		errors.Is(err, syscall.EWOULDBLOCK) ||
		errors.Is(err, syscall.EAGAIN)
}

// SlotLock holds an open file descriptor and an exclusive advisory lock on a slot file.
type SlotLock struct {
	Slot uint16
	Path string
	file *os.File
}

// Release unlocks via syscall.LOCK_UN and closes the underlying lock file.
func (l *SlotLock) Release() error {
	if l == nil || l.file == nil {
		return nil
	}
	fd := int(l.file.Fd())
	_ = syscall.Flock(fd, syscall.LOCK_UN)
	err := l.file.Close()
	l.file = nil
	return err
}

// ReservarSlot attempts to acquire an exclusive lock on the slot lock file in slotsDir.
// If slotsDir is empty, it defaults to "runtime/slots".
// If the lock cannot be acquired immediately (EWOULDBLOCK / EAGAIN), returns a LockCollisionError.
func ReservarSlot(slotsDir string, slot uint16) (*SlotLock, error) {
	if slotsDir == "" {
		slotsDir = filepath.Join("runtime", "slots")
	}
	if err := os.MkdirAll(slotsDir, 0755); err != nil {
		return nil, fmt.Errorf("reservar slot: error creando directorio %s: %w", slotsDir, err)
	}
	lockPath := filepath.Join(slotsDir, fmt.Sprintf("slot-%d.lock", slot))
	f, err := os.OpenFile(lockPath, os.O_CREATE|os.O_RDWR, 0644)
	if err != nil {
		return nil, fmt.Errorf("reservar slot: open %s falló: %w", lockPath, err)
	}
	fd := int(f.Fd())
	if err := syscall.Flock(fd, syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		_ = f.Close()
		if errors.Is(err, syscall.EWOULDBLOCK) || errors.Is(err, syscall.EAGAIN) {
			return nil, &LockCollisionError{
				Slot: slot,
				Path: lockPath,
				Err:  ErrLockCollision,
			}
		}
		return nil, fmt.Errorf("reservar slot: flock %s falló: %w", lockPath, err)
	}
	return &SlotLock{
		Slot: slot,
		Path: lockPath,
		file: f,
	}, nil
}

//go:build cockpit

package main

import (
	"encoding/json"
	"errors"
	"sort"
	"sync"

	"opensuperficies.local/srf/src/orchestratorbook/wal"
)

// Native custody belongs to this cockpit instance, not srfd. The WAL is an
// audit trail only: live holders and credentials are never restored from it.
type slotCustody struct {
	Mode   string `json:"mode"`
	Holder string `json:"holder"`
	Epoch  uint64 `json:"epoch"`
}
type custodyStore struct {
	mu      sync.Mutex
	slots   map[string]slotCustody
	journal func(string, string, slotCustody) error
}

func (c *cooperationStore) journalCustody(action, id string, value slotCustody) error {
	b, err := json.Marshal(struct {
		Action  string      `json:"action"`
		Target  string      `json:"target"`
		Custody slotCustody `json:"custody"`
	}{action, id, value})
	if err != nil {
		return err
	}
	if _, err = c.book.Escribir(&wal.Registro{Clase: wal.ClaseExterna, Tipo: wal.TipoIntencion, Clave: "custody", Detalles: b}); err != nil {
		return err
	}
	return c.book.Sync()
}

func (s *custodyStore) update(id, mode, holder string, epoch uint64) error {
	if _, err := wireSlot(id); err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	v := s.slots[id]
	switch mode {
	case "operator_controlled", "read_only":
		if v.Holder != "" {
			return errors.New("REFUSED: revoke custody before mode change")
		}
		v.Mode = mode
	case "agent_locked":
		if !stateSlugPattern.MatchString(holder) || epoch == 0 || epoch <= v.Epoch || v.Holder != "" {
			return errors.New("REFUSED: invalid custody grant")
		}
		v = slotCustody{Mode: mode, Holder: holder, Epoch: epoch}
	case "revoke":
		if v.Holder == "" || epoch != v.Epoch || epoch == ^uint64(0) {
			return errors.New("REFUSED: stale custody revoke")
		}
		v = slotCustody{Mode: "read_only", Epoch: epoch + 1}
	default:
		return errors.New("REFUSED: invalid custody mode")
	}
	if err := s.journal(mode, id, v); err != nil {
		return err
	}
	s.slots[id] = v
	return nil
}

// project takes ownership of the capsule slice. Pruning uses only a successful
// complete srfd list; a failed list must never look like an empty list.
func (s *custodyStore) project(in stateInput) (stateInput, []string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	live := make(map[string]bool, len(in.Capsules))
	for _, v := range in.Capsules {
		if v.Lifecycle != "stopped" {
			live[cooperationID(v.Slot)] = true
		}
	}
	cleared := []string{}
	for id := range s.slots {
		if !live[id] {
			cleared = append(cleared, id)
		}
	}
	sort.Strings(cleared)
	for _, id := range cleared {
		if err := s.journal("clear", id, s.slots[id]); err != nil {
			return stateInput{}, nil, err
		}
	}
	for _, id := range cleared {
		delete(s.slots, id)
	}
	in.Capsules = append([]stateCapsuleInput(nil), in.Capsules...)
	for i := range in.Capsules {
		v := &in.Capsules[i]
		// srfd supplies lifecycle, never input custody. An unowned ready slot
		// follows the existing operator projection; no upstream holder survives.
		v.Mode, v.LeaseHolder, v.LeaseEpoch = "read_only", nil, 0
		if v.Lifecycle == "ready" {
			v.Mode = "operator_controlled"
		}
		if own, ok := s.slots[cooperationID(v.Slot)]; ok {
			v.Mode, v.LeaseEpoch, v.LeaseHolder = own.Mode, own.Epoch, nil
			if own.Holder != "" {
				holder := own.Holder
				v.LeaseHolder = &holder
			}
		}
	}
	return in, cleared, nil
}

func (c *cooperationStore) custodyInputs() (stateInput, error) {
	in, err := c.base()
	if err != nil || c.custody == nil {
		return in, err
	}
	in, cleared, err := c.custody.project(in)
	if err != nil {
		return in, err
	}
	c.mu.Lock()
	for _, id := range cleared {
		delete(c.sessions, id)
		delete(c.selections, id)
		delete(c.overlay, id)
	}
	c.mu.Unlock()
	for _, id := range cleared {
		if err := c.lessor.RevocarCapsula(id); err != nil {
			return stateInput{}, err
		}
		if err := c.book.Sync(); err != nil {
			return stateInput{}, err
		}
	}
	return in, nil
}

func nativeCustodyUpdate(a *App, id, mode, holder string, epoch uint64) error {
	c := cooperationFor(a)
	if c == nil || c.custody == nil {
		return cooperationMissing()
	}
	in, err := c.custodyInputs()
	if err != nil {
		return err
	}
	for _, v := range in.Capsules {
		if cooperationID(v.Slot) == id && v.Lifecycle != "stopped" {
			return c.custody.update(id, mode, holder, epoch)
		}
	}
	return errors.New("REFUSED: unknown or stopped custody slot")
}

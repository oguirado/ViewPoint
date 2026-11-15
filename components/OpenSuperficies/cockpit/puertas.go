//go:build cockpit

package main

import (
	"fmt"
	"sort"
	"strings"
	"sync"
	"time"
)

// Operator overrides of the nine MicroVM isolation gates (V-A..V-I). AUSENTE in the daemon: src/displaybook/microvm/
// vgates.go EVALUATES the gates of a running QEMU (EvaluarPuertas) and offers no actuator that trips or resets one,
// so a trip from the cockpit is an operator's override kept by the bridge (in memory, monotonic-stamped) and
// reported back to the views; it does not change a capsule's isolation. That limit is declared here, not hidden.

const clavesPuerta = "ABCDEFGHI"

// GateOverride is one gate as the bridge reports it (hypervisor.ts GateOverride).
type GateOverride struct {
	Key      string `json:"key"`
	Tripped  bool   `json:"tripped"`
	Reason   string `json:"reason,omitempty"`
	MonoMs   int64  `json:"monoMs"`
	WallTime string `json:"wallTime,omitempty"`
	Fuente   string `json:"fuente"`
}

type puertas struct {
	mu      sync.Mutex
	inicio  time.Time
	estados map[string]GateOverride
}

func nuevasPuertas() *puertas {
	p := &puertas{inicio: time.Now(), estados: map[string]GateOverride{}}
	for _, c := range clavesPuerta {
		p.estados[string(c)] = GateOverride{Key: string(c), Fuente: "bridge-override"}
	}
	return p
}

func clavesDe(id string) ([]string, error) {
	id = strings.ToUpper(strings.TrimSpace(id))
	if id == "ALL" {
		return strings.Split(clavesPuerta, ""), nil
	}
	if len(id) == 1 && strings.Contains(clavesPuerta, id) {
		return []string{id}, nil
	}
	return nil, fmt.Errorf("gate %q is not one of %s or ALL", id, clavesPuerta)
}

func (p *puertas) monoMs() int64 { return time.Since(p.inicio).Milliseconds() }

// Trip marks a gate (or ALL) tripped with the operator's reason; tripping a tripped gate keeps the first reason.
func (p *puertas) Trip(id, motivo string) error {
	claves, err := clavesDe(id)
	if err != nil {
		return err
	}
	if strings.TrimSpace(motivo) == "" {
		motivo = "MANUAL_OPERATOR_TRIP"
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	for _, c := range claves {
		e := p.estados[c]
		if e.Tripped {
			continue
		}
		p.estados[c] = GateOverride{Key: c, Tripped: true, Reason: motivo, MonoMs: p.monoMs(), WallTime: time.Now().UTC().Format(time.RFC3339Nano), Fuente: "bridge-override"}
	}
	return nil
}

// Reset clears the override of a gate (or ALL).
func (p *puertas) Reset(id string) error {
	claves, err := clavesDe(id)
	if err != nil {
		return err
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	for _, c := range claves {
		p.estados[c] = GateOverride{Key: c, MonoMs: p.monoMs(), Fuente: "bridge-override"}
	}
	return nil
}

// Estado lists the nine gates in order A..I.
func (p *puertas) Estado() []GateOverride {
	p.mu.Lock()
	defer p.mu.Unlock()
	salida := make([]GateOverride, 0, len(p.estados))
	for _, e := range p.estados {
		salida = append(salida, e)
	}
	sort.Slice(salida, func(i, j int) bool { return salida[i].Key < salida[j].Key })
	return salida
}

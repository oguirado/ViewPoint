//go:build cockpit

package main

import "testing"

func TestPuertasTripResetYClaves(t *testing.T) {
	p := nuevasPuertas()
	if e := p.Estado(); len(e) != 9 || e[0].Key != "A" || e[8].Key != "I" || e[3].Tripped {
		t.Fatalf("initial state %+v", e)
	}
	if err := p.Trip("d", "MANUAL_OPERATOR_TRIP"); err != nil {
		t.Fatal(err)
	}
	if err := p.Trip("D", "second reason"); err != nil {
		t.Fatal(err)
	}
	e := p.Estado()
	if !e[3].Tripped || e[3].Reason != "MANUAL_OPERATOR_TRIP" || e[3].WallTime == "" {
		t.Fatalf("gate D after trip: %+v", e[3])
	}
	if err := p.Trip("J", "x"); err == nil {
		t.Fatal("gate J accepted")
	}
	if err := p.Trip("ALL", ""); err != nil {
		t.Fatal(err)
	}
	for _, g := range p.Estado() {
		if !g.Tripped {
			t.Fatalf("gate %s not tripped after ALL", g.Key)
		}
	}
	if g := p.Estado()[0]; g.Reason != "MANUAL_OPERATOR_TRIP" {
		t.Fatalf("empty reason was not defaulted: %q", g.Reason)
	}
	if err := p.Reset("d"); err != nil {
		t.Fatal(err)
	}
	if p.Estado()[3].Tripped || !p.Estado()[4].Tripped {
		t.Fatal("Reset(D) did not clear exactly D")
	}
	if err := p.Reset("ALL"); err != nil {
		t.Fatal(err)
	}
	for _, g := range p.Estado() {
		if g.Tripped {
			t.Fatalf("gate %s still tripped after Reset(ALL)", g.Key)
		}
	}
}

func TestGrabacionEstadoInactivoYDetenerSinSesion(t *testing.T) {
	g := nuevaGrabacion()
	e := g.Estado()
	if e.Recording || e.Fase != "idle" || e.Verdict != "IDLE" || e.Display == "" {
		t.Fatalf("idle state %+v", e)
	}
	if err := g.Detener(""); err == nil {
		t.Fatal("Detener without a session succeeded")
	}
	g.bin = "" // no repository: Iniciar must refuse, never guess a binary
	if _, err := g.Iniciar(0, 0); err == nil {
		t.Fatal("Iniciar without bin/srf-grabar succeeded")
	}
}

func TestTelemetriaLeeElHost(t *testing.T) {
	tel := nuevaTelemetria()
	h, err := tel.Leer()
	if err != nil {
		t.Skipf("host telemetry not readable here: %v", err)
	}
	if len(h.Cores) == 0 || h.Ram.TechoTotalMib == 0 || h.Ram.DisponibleMib == 0 || h.Fuente != "procfs" || h.Gpu.Fuente != "ausente" {
		t.Fatalf("telemetry %+v", h)
	}
	for _, c := range h.Cores {
		if c.Percent < 0 || c.Percent > 100 {
			t.Fatalf("core %d percent %v", c.CoreID, c.Percent)
		}
	}
}

package gobernador

import (
	"errors"
	"testing"
	"time"
)

func TestNoDesborda(t *testing.T) {
	sim := Simular(24, "browser", 6, 60000)
	if sim.Desbordamiento {
		t.Fatalf("desbordamiento de memoria detectado: admitidas=%d comprometida=%d MiB soberana=%d MiB disponible=%d MiB",
			sim.Admitidas, sim.MemComprometidaMiB, sim.MemSoberanaMiB, sim.MemDisponibleMiB)
	}
	if sim.Admitidas != 16 {
		t.Fatalf("esperadas 16 admitidas en escenario 60000 MiB, obtenidas %d", sim.Admitidas)
	}
}

func TestSimulacion80400(t *testing.T) {
	sim := Simular(24, "browser", 6, 80400)
	if sim.Desbordamiento {
		t.Fatalf("desbordamiento en escenario 80400 MiB: holgura=%d", sim.HolguraFinalMiB)
	}
	if sim.Admitidas != 24 {
		t.Fatalf("esperadas 24 admitidas en escenario 80400 MiB, obtenidas %d", sim.Admitidas)
	}
}

func TestGobernadorPeorCaso(t *testing.T) {
	// 24 capsules of 2,112 MiB require 24 * 2,112 = 50,688 MiB + 24,576 MiB (24 GiB sovereign) = 75,264 MiB (73.5 GiB)
	// With exactly 75,264 MiB, all 24 are admitted, 25th is rejected by "techo"
	dispBytes := uint64(75264) * 1024 * 1024
	tel := &TelemetriaSintetica{
		DisponibleBytes: dispBytes,
		PresionCPU:      0.0,
		UsoBytes:        0,
		Origen:          "/proc/meminfo",
	}
	g := NuevoGobernador(tel)
	for i := 0; i < 32; i++ {
		g.ObservarLatencia(50 * time.Millisecond)
	}

	reserva := EstimarReservaBytes("browser", 6)
	for i := 0; i < 24; i++ {
		dec := g.Admitir(SrfSolicitudAdmision{
			Clase:     "browser",
			Reserva:   reserva,
			CPUs:      1.5,
			CandadoOK: true,
		})
		if !dec.Admitida {
			t.Fatalf("capsula %d debio ser admitida, motivo: %s", i+1, dec.Motivo)
		}
	}

	// 25th capsule must be rejected by "techo"
	dec25 := g.Admitir(SrfSolicitudAdmision{
		Clase:     "browser",
		Reserva:   reserva,
		CPUs:      1.5,
		CandadoOK: true,
	})
	if dec25.Admitida || dec25.Motivo != "techo" {
		t.Fatalf("capsula 25 debio ser rechazada por 'techo', obtenida: %+v", dec25)
	}

	// With 73.4 GiB (75,161 MiB), the 24th capsule must be rejected by "ram"
	disp734 := uint64(75161) * 1024 * 1024
	tel2 := &TelemetriaSintetica{
		DisponibleBytes: disp734,
		PresionCPU:      0.0,
		UsoBytes:        0,
		Origen:          "/proc/meminfo",
	}
	g2 := NuevoGobernador(tel2)
	for i := 0; i < 32; i++ {
		g2.ObservarLatencia(50 * time.Millisecond)
	}

	for i := 0; i < 23; i++ {
		dec := g2.Admitir(SrfSolicitudAdmision{
			Clase:     "browser",
			Reserva:   reserva,
			CPUs:      1.5,
			CandadoOK: true,
		})
		if !dec.Admitida {
			t.Fatalf("capsula %d debio ser admitida en 73.4 GiB, motivo: %s", i+1, dec.Motivo)
		}
	}
	dec24 := g2.Admitir(SrfSolicitudAdmision{
		Clase:     "browser",
		Reserva:   reserva,
		CPUs:      1.5,
		CandadoOK: true,
	})
	if dec24.Admitida || dec24.Motivo != "ram" {
		t.Fatalf("capsula 24 en 73.4 GiB debio ser rechazada por 'ram', obtenida: %+v", dec24)
	}
}

func TestGobernadorTelemetriaFailClosed(t *testing.T) {
	tel := &TelemetriaSintetica{
		ErrMem: errors.New("read error"),
	}
	g := NuevoGobernador(tel)
	dec := g.Admitir(SrfSolicitudAdmision{
		Clase:     "browser",
		Reserva:   EstimarReservaBytes("browser", 6),
		CPUs:      1.5,
		CandadoOK: true,
	})
	if dec.Admitida || dec.Motivo != "telemetria" {
		t.Fatalf("esperado motivo 'telemetria' ante error, obtenido: %+v", dec)
	}
}

func TestGobernadorVentanaArranque(t *testing.T) {
	// Cold start: 0 latency samples.
	// Up to 3 capsules admitted; 4th rejected with "latencia"
	tel := &TelemetriaSintetica{
		DisponibleBytes: 100 * 1024 * 1024 * 1024,
		PresionCPU:      0.0,
	}
	g := NuevoGobernador(tel)
	reserva := EstimarReservaBytes("browser", 6)

	for i := 0; i < 3; i++ {
		dec := g.Admitir(SrfSolicitudAdmision{
			Clase:     "browser",
			Reserva:   reserva,
			CPUs:      1.5,
			CandadoOK: true,
		})
		if !dec.Admitida {
			t.Fatalf("capsula %d debio ser admitida en ventana de arranque, motivo: %s", i+1, dec.Motivo)
		}
	}

	// 4th capsule must be rejected with "latencia" because nlat < 32 and vivas >= 3
	dec4 := g.Admitir(SrfSolicitudAdmision{
		Clase:     "browser",
		Reserva:   reserva,
		CPUs:      1.5,
		CandadoOK: true,
	})
	if dec4.Admitida || dec4.Motivo != "latencia" {
		t.Fatalf("esperado rechazo 'latencia' para 4ta capsula sin muestras, obtenido: %+v", dec4)
	}
}

func TestCandadoIdentidad(t *testing.T) {
	tel := &TelemetriaSintetica{
		DisponibleBytes: 100 * 1024 * 1024 * 1024,
	}
	g := NuevoGobernador(tel)
	dec := g.Admitir(SrfSolicitudAdmision{
		Clase:     "browser",
		Reserva:   EstimarReservaBytes("browser", 6),
		CPUs:      1.5,
		CandadoOK: false,
	})
	if dec.Admitida || dec.Motivo != "candado" {
		t.Fatalf("esperado rechazo 'candado', obtenido: %+v", dec)
	}
}

func TestPresionCPU(t *testing.T) {
	tel := &TelemetriaSintetica{
		DisponibleBytes: 100 * 1024 * 1024 * 1024,
		PresionCPU:      25.0, // Above 20%
	}
	g := NuevoGobernador(tel)
	dec := g.Admitir(SrfSolicitudAdmision{
		Clase:     "browser",
		Reserva:   EstimarReservaBytes("browser", 6),
		CPUs:      1.5,
		CandadoOK: true,
	})
	if dec.Admitida || dec.Motivo != "cpu" {
		t.Fatalf("esperado rechazo 'cpu' con PSI=25.0, obtenido: %+v", dec)
	}
}

func TestTechoMicroVM(t *testing.T) {
	tel := &TelemetriaSintetica{
		DisponibleBytes: 100 * 1024 * 1024 * 1024,
	}
	g := NuevoGobernador(tel)
	for i := 0; i < 32; i++ {
		g.ObservarLatencia(50 * time.Millisecond)
	}

	reserva := EstimarReservaBytes("microvm", 0)
	for i := 0; i < 2; i++ {
		dec := g.Admitir(SrfSolicitudAdmision{
			Clase:     "microvm",
			Reserva:   reserva,
			CPUs:      2.0,
			CandadoOK: true,
		})
		if !dec.Admitida {
			t.Fatalf("microvm %d debio ser admitida, motivo: %s", i+1, dec.Motivo)
		}
	}

	// 3rd microvm must exceed MicrovmActivasMax=2
	dec3 := g.Admitir(SrfSolicitudAdmision{
		Clase:     "microvm",
		Reserva:   reserva,
		CPUs:      2.0,
		CandadoOK: true,
	})
	if dec3.Admitida || dec3.Motivo != "techo_microvm" {
		t.Fatalf("esperado rechazo 'techo_microvm' para 3ra microvm, obtenido: %+v", dec3)
	}
}

func TestDevolverYLiberar(t *testing.T) {
	tel := &TelemetriaSintetica{
		DisponibleBytes: 100 * 1024 * 1024 * 1024,
	}
	g := NuevoGobernador(tel)
	for i := 0; i < 32; i++ {
		g.ObservarLatencia(50 * time.Millisecond)
	}

	reserva := EstimarReservaBytes("browser", 6)
	dec := g.Admitir(SrfSolicitudAdmision{
		Clase:     "browser",
		Reserva:   reserva,
		CPUs:      1.5,
		CandadoOK: true,
	})
	if !dec.Admitida {
		t.Fatalf("admision fallida: %s", dec.Motivo)
	}

	g.Devolver(dec, "browser")
	est, _ := g.Estado()
	if est.Vivas != 0 || est.ReservadoBytes != 0 {
		t.Fatalf("estado inconsistente tras devolver: %+v", est)
	}
}

func TestEstimarReserva(t *testing.T) {
	if EstimarBrowserMiB(6) != 2112 {
		t.Fatalf("EstimarBrowserMiB(6) = %d != 2112", EstimarBrowserMiB(6))
	}
	if EstimarBrowserMiB(3) != 1600 {
		t.Fatalf("EstimarBrowserMiB(3) = %d != 1600", EstimarBrowserMiB(3))
	}
	if EstimarDesktopMiB() != 2048 {
		t.Fatalf("EstimarDesktopMiB() = %d != 2048", EstimarDesktopMiB())
	}
	if EstimarMicroVMMiB(4096) != 4352 {
		t.Fatalf("EstimarMicroVMMiB(4096) = %d != 4352", EstimarMicroVMMiB(4096))
	}
}

func TestTelemetriaProcVivo(t *testing.T) {
	tel := NuevaTelemetriaProc(nil)
	disp, err := tel.MemDisponible()
	if err != nil {
		t.Fatalf("error leyendo /proc/meminfo: %v", err)
	}
	if disp == 0 {
		t.Fatal("MemDisponible() devolvio 0")
	}

	psi, err := tel.PresionCPU10()
	if err != nil {
		t.Fatalf("error leyendo /proc/pressure/cpu: %v", err)
	}
	if psi < 0.0 {
		t.Fatalf("PresionCPU10 devolvio valor negativo: %f", psi)
	}

	if tel.OrigenMem() != "/proc/meminfo" {
		t.Fatalf("OrigenMem() = %s != /proc/meminfo", tel.OrigenMem())
	}
}

package arrendador

import (
	"encoding/json"
	"errors"
	"fmt"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"opensuperficies.local/srf/src/orchestratorbook/wal"
)

// Compile-time fixtures: changes to the exported ABI must fail compilation.
var (
	_ func(*SrfArrendador, string, uint32, string, time.Duration, bool) (LeaseSession, error) = (*SrfArrendador).Conceder
	_ func(*SrfArrendador, LeaseSession) error = (*SrfArrendador).Validar
	_ func(*SrfArrendador, string) error = (*SrfArrendador).RevocarCapsula
	_ func(*SrfArrendador, string, uint32, uint64, [16]byte) bool = (*SrfArrendador).AutenticarToken
	_ func(func(string) string, *wal.LibroConsumo) (*SrfArrendador, error) = NuevoSrfArrendador
	_ func(string, uint32, *SrfArrendador, *wal.LibroConsumo) (*BarreraAlmacenamiento, error) = NuevaBarreraAlmacenamiento
	_ func(*BarreraAlmacenamiento, LeaseSession, func() error) error = (*BarreraAlmacenamiento).EjecutarConArriendo
	_ struct {
		Token    [16]byte  `json:"-"`
		Capsula  string    `json:"capsula"`
		Destino  uint32    `json:"destino"`
		Epoca    uint64    `json:"epoca"`
		Titular  string    `json:"titular"`
		Vence    time.Time `json:"vence"`
		Irrevers bool      `json:"irrevers"`
	} = LeaseSession{}
)

// TestRevokeWriteRace tests both a deliberately held local commit and multiple
// simultaneous real WAL-backed writes/revocations of the same capsule.
func TestRevokeWriteRace(t *testing.T) {
	t.Run("commit_critical_section", func(t *testing.T) {
		libro, _ := abrirWALTest(t, "toctou-lock")
		arr, err := NuevoSrfArrendador(nil, libro)
		if err != nil {
			t.Fatal(err)
		}
		b, err := NuevaBarreraAlmacenamiento("race-lock", 8501, arr, libro)
		if err != nil {
			t.Fatal(err)
		}
		lease, err := arr.Conceder("race-lock", 8501, "holder", time.Minute, false)
		if err != nil {
			t.Fatal(err)
		}

		entered := make(chan struct{})
		release := make(chan struct{})
		writeDone := make(chan error, 1)
		go func() {
			writeDone <- b.EjecutarConArriendo(lease, func() error {
				close(entered)
				<-release // test-only pause, not transport or seat I/O
				return nil
			})
		}()
		<-entered

		revokeStarted := make(chan struct{})
		revokeDone := make(chan error, 1)
		go func() {
			close(revokeStarted)
			revokeDone <- arr.RevocarCapsula("race-lock")
		}()
		<-revokeStarted
		// A revoke must not finish while the validated local commit is in flight.
		select {
		case err := <-revokeDone:
			close(release)
			<-writeDone
			t.Fatalf("revoke bypassed storage critical section: %v", err)
		case <-time.After(20 * time.Millisecond):
		}
		close(release)
		if err := <-writeDone; err != nil {
			t.Fatalf("local commit failed: %v", err)
		}
		if err := <-revokeDone; err != nil {
			t.Fatalf("revoke failed: %v", err)
		}
		if err := b.Escribir(lease, []byte("after-revoke")); err == nil {
			t.Fatal("revoked lease was able to commit after revocation")
		}
	})

	t.Run("wal_commit_order", func(t *testing.T) {
		libro, walPath := abrirWALTest(t, "toctou-race")
		arr, err := NuevoSrfArrendador(nil, libro)
		if err != nil {
			t.Fatal(err)
		}
		const capsule = "race-epoch"
		const destination uint32 = 8502
		b, err := NuevaBarreraAlmacenamiento(capsule, destination, arr, libro)
		if err != nil {
			t.Fatal(err)
		}
		// Each round produces a new valid epoch followed by its own revocation.
		for round := 0; round < 8; round++ {
			lease, err := arr.Conceder(capsule, destination, "holder", time.Minute, false)
			if err != nil {
				t.Fatal(err)
			}
			if err := b.Escribir(lease, []byte("positive-control")); err != nil {
				t.Fatalf("normal write before revoke failed: %v", err)
			}
			const workers = 8
			const writesPerWorker = 18
			start := make(chan struct{})
			var wg sync.WaitGroup
			for worker := 0; worker < workers; worker++ {
				wg.Add(1)
				go func() {
					defer wg.Done()
					<-start
					for attempt := 0; attempt < writesPerWorker; attempt++ {
						_ = b.Escribir(lease, []byte("in-flight"))
					}
				}()
			}
			close(start)
			if err := arr.RevocarCapsula(capsule); err != nil {
				t.Fatalf("round %d revoke failed: %v", round, err)
			}
			atRevoke := b.EscriturasOk()
			wg.Wait()
			if got := b.EscriturasOk(); got != atRevoke {
				t.Fatalf("round %d: %d writes committed after revoke returned (at revoke %d)", round, got-atRevoke, atRevoke)
			}
			if err := b.Escribir(lease, []byte("stale")); err == nil {
				t.Fatalf("round %d accepted revoked lease", round)
			}
		}

		// WAL order exposes the original TOCTOU: a storage commit for an
		// epoch may NEVER follow its revocation record, even if an in-flight
		// write began before the revoke was requested.
		records, err := wal.LeerRegistros(walPath)
		if err != nil {
			t.Fatal(err)
		}
		var latestEpoch uint64
		revoked := make(map[uint64]bool)
		revokeCount := 0
		writeCount := 0
		for _, reg := range records {
			if reg.Clase != wal.ClaseLocal {
				continue
			}
			if reg.Tipo == wal.TipoReserva {
				var d struct {
					Capsula string `json:"capsula"`
					Destino uint32 `json:"destino"`
					Epoca uint64 `json:"epoca"`
					Accion string `json:"accion"`
				}
				if err := json.Unmarshal(reg.Detalles, &d); err != nil {
					t.Fatal(err)
				}
				if d.Capsula == capsule && d.Accion == "revocar" {
					if latestEpoch == 0 {
						t.Fatal("revoke appeared before initial grant")
					}
					revoked[latestEpoch] = true
					revokeCount++
				} else if d.Capsula == capsule && d.Destino == destination {
					latestEpoch = d.Epoca
				}
			}
			if reg.Tipo == wal.TipoDesenlace && reg.Clave == fmt.Sprintf("storage:%s:%d", capsule, destination) {
				var d struct { Epoca uint64 `json:"epoca"` }
				if err := json.Unmarshal(reg.Detalles, &d); err != nil {
					t.Fatal(err)
				}
				writeCount++
				if revoked[d.Epoca] {
					t.Fatalf("WAL commits revoked epoch %d after its revoke record", d.Epoca)
				}
			}
		}
		if revokeCount != 8 || writeCount < 8 {
			t.Fatalf("missing positive controls: revocations=%d, writes=%d", revokeCount, writeCount)
		}
	})
}

// TestRevokeWALFailStaysRevoked is a fail-closed regression fixture:
// a failed append must not restore the token or allow another grant. Recovery
// requires both reconnecting a healthy WAL and an explicit health check.
func TestRevokeWALFailStaysRevoked(t *testing.T) {
	walPath := filepath.Join(t.TempDir(), "degraded.wal")
	libro, err := wal.AbrirLibro(walPath)
	if err != nil { t.Fatal(err) }
	defer libro.Cerrar()
	arr, err := NuevoSrfArrendador(nil, libro)
	if err != nil { t.Fatal(err) }
	const capsule = "revoke-wal-fail"
	const destination uint32 = 8503
	b, err := NuevaBarreraAlmacenamiento(capsule, destination, arr, libro)
	if err != nil { t.Fatal(err) }
	lease, err := arr.Conceder(capsule, destination, "holder", time.Minute, false)
	if err != nil { t.Fatal(err) }
	if err := b.Escribir(lease, []byte("before-failure")); err != nil { t.Fatal(err) }

	if err := libro.Cerrar(); err != nil { t.Fatal(err) }
	if err := arr.RevocarCapsula(capsule); err == nil {
		t.Fatal("closed WAL did not fail the revoke append")
	}
	if !arr.Degradado() { t.Fatal("lessor did not latch degraded after failed revoke") }
	state := arr.estados[srfClaveArriendo{capsula: capsule, destino: destination}]
	if state == nil || !state.vence.IsZero() || state.epoca <= lease.Epoca {
		t.Fatal("failed revoke restored an active lease or failed to advance the epoch")
	}
	if err := arr.Validar(lease); !errors.Is(err, SrfErrEpocaVencida) {
		t.Fatalf("failed WAL append revalidated revoked lease: %v", err)
	}
	if arr.AutenticarToken(capsule, destination, lease.Epoca, lease.Token) {
		t.Fatal("failed revoke retained usable token")
	}
	if err := b.Escribir(lease, []byte("after-failure")); err == nil {
		t.Fatal("failed revoke allowed an old lease write")
	}
	if _, err := arr.Conceder(capsule, destination, "new", time.Minute, false); !errors.Is(err, ErrSrfArrendadorDegradado) {
		t.Fatalf("Conceder while degraded: got %v, want ErrSrfArrendadorDegradado", err)
	}
	if err := arr.ComprobarSaludWAL(); err == nil || !arr.Degradado() {
		t.Fatalf("closed WAL incorrectly passed explicit health check: %v", err)
	}

	healthy, err := wal.AbrirLibro(walPath)
	if err != nil { t.Fatal(err) }
	defer healthy.Cerrar()
	if err := arr.ConectarWAL(healthy); err != nil { t.Fatal(err) }
	// Both components must use the reopened WAL for the positive control.
	if err := b.ConectarWAL(healthy); err != nil { t.Fatal(err) }
	if !arr.Degradado() {
		t.Fatal("ConectarWAL cleared degraded latch without health check")
	}
	if err := arr.Validar(lease); !errors.Is(err, SrfErrEpocaVencida) {
		t.Fatalf("ConectarWAL resurrected revoked lease: %v", err)
	}
	if _, err := arr.Conceder(capsule, destination, "too-early", time.Minute, false); !errors.Is(err, ErrSrfArrendadorDegradado) {
		t.Fatalf("Conceder bypassed health check: %v", err)
	}
	if err := arr.ComprobarSaludWAL(); err != nil { t.Fatalf("WAL health recovery failed: %v", err) }
	if arr.Degradado() { t.Fatal("successful health check did not clear latch") }

	fresh, err := arr.Conceder(capsule, destination, "recovered", time.Minute, false)
	if err != nil { t.Fatalf("grant after health check failed: %v", err) }
	if fresh.Epoca <= lease.Epoca {
		t.Fatalf("epoch did not advance after revoked lease: %d <= %d", fresh.Epoca, lease.Epoca)
	}
	if err := b.Escribir(fresh, []byte("after-health")); err != nil {
		t.Fatalf("new valid lease cannot write after recovery: %v", err)
	}
	if err := arr.RevocarCapsula(capsule); err != nil { t.Fatal(err) }
	if err := healthy.Cerrar(); err != nil { t.Fatal(err) }
	reopened, err := wal.AbrirLibro(walPath)
	if err != nil { t.Fatal(err) }
	defer reopened.Cerrar()
	replayed, err := NuevoSrfArrendador(nil, reopened)
	if err != nil { t.Fatal(err) }
	if err := replayed.Validar(lease); !errors.Is(err, SrfErrEpocaVencida) {
		t.Fatalf("WAL replay revived initial revoked lease: %v", err)
	}
	if err := replayed.Validar(fresh); !errors.Is(err, SrfErrEpocaVencida) {
		t.Fatalf("WAL replay revived most recent revoked lease: %v", err)
	}
}

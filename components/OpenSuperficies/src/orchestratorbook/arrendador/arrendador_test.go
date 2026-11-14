package arrendador

import (
	"bytes"
	"crypto/rand"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"

	"opensuperficies.local/srf/src/orchestratorbook/wal"
)

func abrirWALTest(t *testing.T, nombre string) (*wal.LibroConsumo, string) {
	t.Helper()
	tmpDir := t.TempDir()
	walPath := filepath.Join(tmpDir, nombre+".wal")
	libro, err := wal.AbrirLibro(walPath)
	if err != nil {
		t.Fatalf("failed to open test wal: %v", err)
	}
	t.Cleanup(func() {
		_ = libro.Cerrar()
	})
	return libro, walPath
}

func TestZombiRechazado(t *testing.T) {
	libro, _ := abrirWALTest(t, "zombi")
	arr, err := NuevoSrfArrendador(func(capsula string) string {
		return "identidad-" + capsula
	}, libro)
	if err != nil {
		t.Fatalf("unexpected error constructing lessor: %v", err)
	}

	capsula := "browser-slot-0"
	var destino uint32 = 9220

	// 1. Holder 1 (deposed holder) acquires a lease with epoch 1
	sesion1, err := arr.Conceder(capsula, destino, "titular-1", 10*time.Second, false)
	if err != nil {
		t.Fatalf("unexpected error granting lease 1: %v", err)
	}
	if sesion1.Epoca != 1 {
		t.Fatalf("expected epoch 1, got %d", sesion1.Epoca)
	}

	// 2. Storage boundary is initialized with lessor and mandatory WAL
	barrera, err := NuevaBarreraAlmacenamiento(capsula, destino, arr, libro)
	if err != nil {
		t.Fatalf("unexpected error constructing storage boundary: %v", err)
	}

	// Holder 1 writes initial state
	if err := barrera.Escribir(sesion1, []byte("estado-inicial")); err != nil {
		t.Fatalf("unexpected error on initial write: %v", err)
	}

	// 3. Holder 1 experiences a simulated stop-the-world pause.
	// Meanwhile, lease is granted to Holder 2 with higher epoch (epoch 2).
	sesion2, err := arr.Conceder(capsula, destino, "titular-2", 10*time.Second, false)
	if err != nil {
		t.Fatalf("unexpected error granting lease 2: %v", err)
	}
	if sesion2.Epoca != 2 {
		t.Fatalf("expected epoch 2, got %d", sesion2.Epoca)
	}

	// Holder 2 commits its write to the storage boundary
	if err := barrera.Escribir(sesion2, []byte("estado-titular-2")); err != nil {
		t.Fatalf("unexpected error on holder 2 write: %v", err)
	}

	// 4. Holder 1 wakes up (unaware it was deposed) and attempts a delayed write
	errZombi := barrera.Escribir(sesion1, []byte("escritura-zombi-tardia"))

	// 5. The fencing epoch invariant mandates that the storage boundary must reject the zombie write.
	// If mutante_sin_epoca is active, the epoch check is absent and the write succeeds, causing this test to fail.
	if errZombi == nil {
		t.Fatalf("INVARIANT VIOLATION: zombie write from deposed holder (epoch %d < %d) was accepted at storage boundary! mutante_sin_epoca detected",
			sesion1.Epoca, sesion2.Epoca)
	}

	if !errors.Is(errZombi, SrfErrEpocaVencida) {
		t.Fatalf("expected SrfErrEpocaVencida, got: %v", errZombi)
	}
}

func TestArriendoRevocadoNoActua(t *testing.T) {
	libro, _ := abrirWALTest(t, "revocado")
	arr, err := NuevoSrfArrendador(func(capsula string) string {
		return "identidad-" + capsula
	}, libro)
	if err != nil {
		t.Fatalf("unexpected error constructing lessor: %v", err)
	}

	capsula := "browser-slot-1"
	var destino uint32 = 9221

	sesion, err := arr.Conceder(capsula, destino, "titular-revocable", 10*time.Second, false)
	if err != nil {
		t.Fatalf("unexpected error granting lease: %v", err)
	}

	if err := arr.Validar(sesion); err != nil {
		t.Fatalf("expected session to be valid before revocation: %v", err)
	}

	barrera, err := NuevaBarreraAlmacenamiento(capsula, destino, arr, libro)
	if err != nil {
		t.Fatalf("unexpected error constructing storage boundary: %v", err)
	}

	if err := barrera.Escribir(sesion, []byte("pre-revoke")); err != nil {
		t.Fatalf("pre-revoke write failed: %v", err)
	}

	_ = arr.RevocarCapsula(capsula)

	if err := arr.Validar(sesion); !errors.Is(err, SrfErrEpocaVencida) {
		t.Fatalf("expected SrfErrEpocaVencida after revocation, got: %v", err)
	}

	// Boundary must also reject write from revoked session
	errRevokedWrite := barrera.Escribir(sesion, []byte("post-revoke"))
	if errRevokedWrite == nil {
		t.Fatalf("expected write from revoked session to be rejected")
	}
}

func TestIrreversibleExclusivoPorIdentidad(t *testing.T) {
	libro, _ := abrirWALTest(t, "irrevers")
	arr, err := NuevoSrfArrendador(func(capsula string) string {
		return "shared-identity"
	}, libro)
	if err != nil {
		t.Fatalf("unexpected error constructing lessor: %v", err)
	}

	// Grant first irreversible lease
	sesion1, err := arr.Conceder("capsula-A", 1001, "titular-A", 200*time.Millisecond, true)
	if err != nil {
		t.Fatalf("unexpected error granting first irreversible lease: %v", err)
	}
	if !sesion1.Irrevers {
		t.Fatalf("expected irreversible lease")
	}

	// Attempt second irreversible lease on same identity while first is active -> must fail
	_, err2 := arr.Conceder("capsula-B", 1002, "titular-B", 200*time.Millisecond, true)
	if !errors.Is(err2, ErrSrfIrreversOcupado) {
		t.Fatalf("expected ErrSrfIrreversOcupado, got: %v", err2)
	}

	// Non-irreversible lease should still succeed
	_, err3 := arr.Conceder("capsula-B", 1002, "titular-B", 200*time.Millisecond, false)
	if err3 != nil {
		t.Fatalf("expected non-irreversible lease to succeed, got: %v", err3)
	}

	// After expiry, a new irreversible lease must succeed
	time.Sleep(250 * time.Millisecond)
	_, err4 := arr.Conceder("capsula-B", 1002, "titular-B", 200*time.Millisecond, true)
	if err4 != nil {
		t.Fatalf("expected irreversible lease after expiry to succeed, got: %v", err4)
	}
}

func TestComparacionTiempoConstante(t *testing.T) {
	var a, b, c [16]byte
	_, _ = rand.Read(a[:])
	b = a
	c = a
	c[15] ^= 0x01

	if !CompararTokens(a, b) {
		t.Fatalf("expected matching tokens to return true")
	}
	if CompararTokens(a, c) {
		t.Fatalf("expected differing tokens to return false")
	}
}

func TestEpocaMonotona(t *testing.T) {
	libro, _ := abrirWALTest(t, "monotona")
	arr, err := NuevoSrfArrendador(nil, libro)
	if err != nil {
		t.Fatalf("unexpected error constructing lessor: %v", err)
	}
	capsula := "browser-slot-2"
	var destino uint32 = 9222

	for i := uint64(1); i <= 10; i++ {
		sesion, err := arr.Conceder(capsula, destino, "titular", time.Second, false)
		if err != nil {
			t.Fatalf("iteration %d: error granting lease: %v", i, err)
		}
		if sesion.Epoca != i {
			t.Fatalf("iteration %d: expected epoch %d, got %d", i, i, sesion.Epoca)
		}
	}
}

func TestEpochJournallingWAL(t *testing.T) {
	tmpDir := t.TempDir()
	walPath := filepath.Join(tmpDir, "libro.wal")

	libro, err := wal.AbrirLibro(walPath)
	if err != nil {
		t.Fatalf("failed to open WAL: %v", err)
	}

	arr, err := NuevoSrfArrendador(func(c string) string { return c }, libro)
	if err != nil {
		t.Fatalf("failed to construct lessor: %v", err)
	}

	capsula := "browser-slot-wal"
	var destino uint32 = 9225

	_, err = arr.Conceder(capsula, destino, "titular-wal-1", time.Second, false)
	if err != nil {
		t.Fatalf("failed to grant lease 1: %v", err)
	}
	s2, err := arr.Conceder(capsula, destino, "titular-wal-2", time.Second, false)
	if err != nil {
		t.Fatalf("failed to grant lease 2: %v", err)
	}

	if err := libro.Cerrar(); err != nil {
		t.Fatalf("failed to close WAL: %v", err)
	}

	// Read records from disk
	regs, err := wal.LeerRegistros(walPath)
	if err != nil {
		t.Fatalf("failed to read WAL records: %v", err)
	}

	if len(regs) != 2 {
		t.Fatalf("expected 2 WAL records, got %d", len(regs))
	}

	for _, r := range regs {
		if r.Clase != wal.ClaseLocal {
			t.Fatalf("expected ClaseLocal, got %s", r.Clase)
		}
	}

	// Replay into new lessor instance via constructor with WAL
	libro2, err := wal.AbrirLibro(walPath)
	if err != nil {
		t.Fatalf("failed to reopen WAL: %v", err)
	}
	defer libro2.Cerrar()

	arr2, err := NuevoSrfArrendador(func(c string) string { return c }, libro2)
	if err != nil {
		t.Fatalf("failed to recover lessor from WAL: %v", err)
	}

	// Next granted lease must increment recovered epoch
	s3, err := arr2.Conceder(capsula, destino, "titular-wal-3", time.Second, false)
	if err != nil {
		t.Fatalf("failed to grant lease 3: %v", err)
	}

	if s3.Epoca != s2.Epoca+1 {
		t.Fatalf("expected recovered epoch to be %d, got %d", s2.Epoca+1, s3.Epoca)
	}
}

// Regression test for U07-F1: Storage boundary must authenticate capability token in constant time.
func TestRegressionU07F1_BoundaryAuthenticatesToken(t *testing.T) {
	libro, _ := abrirWALTest(t, "f1")
	arr, err := NuevoSrfArrendador(func(c string) string { return c }, libro)
	if err != nil {
		t.Fatalf("failed to construct lessor: %v", err)
	}
	capsula := "slot-f1"
	var destino uint32 = 9301

	sesion1, err := arr.Conceder(capsula, destino, "titular-1", time.Minute, false)
	if err != nil {
		t.Fatalf("failed to grant lease 1: %v", err)
	}

	barrera, err := NuevaBarreraAlmacenamiento(capsula, destino, arr, libro)
	if err != nil {
		t.Fatalf("failed to construct boundary: %v", err)
	}

	// 1. Deposed holder tampers with its session: alters Epoca to 999 with original token
	forgedSession := sesion1
	forgedSession.Epoca = 999
	errForged := barrera.Escribir(forgedSession, []byte("A-forged"))
	if errForged == nil {
		t.Fatalf("REGRESSION U07-F1: write with forged epoch 999 was accepted without authentication")
	}
	if !errors.Is(errForged, ErrSrfTokenInvalido) {
		t.Fatalf("expected ErrSrfTokenInvalido, got: %v", errForged)
	}
	if barrera.UltimaEpoca() != 0 {
		t.Fatalf("boundary epoch corrupted by unauthenticated write: %d", barrera.UltimaEpoca())
	}

	// 2. Zero-value session with nil token and Epoca 1000
	zeroSession := LeaseSession{
		Capsula: capsula,
		Destino: destino,
		Epoca:   1000,
		Vence:   time.Now().Add(time.Hour),
	}
	errZero := barrera.Escribir(zeroSession, []byte("zero-token"))
	if errZero == nil {
		t.Fatalf("REGRESSION U07-F1: zero-value session write was accepted")
	}
	if !errors.Is(errZero, ErrSrfTokenInvalido) {
		t.Fatalf("expected ErrSrfTokenInvalido for zero token, got: %v", errZero)
	}

	// 3. Legitimate holder writes successfully
	sesion2, err := arr.Conceder(capsula, destino, "titular-2", time.Minute, false)
	if err != nil {
		t.Fatalf("failed to grant lease 2: %v", err)
	}
	if err := barrera.Escribir(sesion2, []byte("legit-data")); err != nil {
		t.Fatalf("legitimate write failed: %v", err)
	}
	if barrera.UltimaEpoca() != 2 {
		t.Fatalf("expected boundary epoch 2, got: %d", barrera.UltimaEpoca())
	}
}

// Regression test for U07-F2: Boundary must recover ultimaEpoca across restart.
func TestRegressionU07F2_BoundaryRecoversEpochOnRestart(t *testing.T) {
	tmpDir := t.TempDir()
	walPath := filepath.Join(tmpDir, "f2.wal")
	libro, err := wal.AbrirLibro(walPath)
	if err != nil {
		t.Fatalf("failed to open wal: %v", err)
	}

	arr, err := NuevoSrfArrendador(func(c string) string { return c }, libro)
	if err != nil {
		t.Fatalf("failed to construct lessor: %v", err)
	}
	capsula := "slot-f2"
	var destino uint32 = 9302

	sesion1, _ := arr.Conceder(capsula, destino, "titular-1", time.Minute, false)
	sesion2, _ := arr.Conceder(capsula, destino, "titular-2", time.Minute, false)

	barrera1, err := NuevaBarreraAlmacenamiento(capsula, destino, arr, libro)
	if err != nil {
		t.Fatalf("failed to construct boundary: %v", err)
	}

	if err := barrera1.Escribir(sesion2, []byte("active-write")); err != nil {
		t.Fatalf("initial write failed: %v", err)
	}
	_ = libro.Cerrar()

	// Simulate boundary restart: new instance reading from WAL
	libro2, err := wal.AbrirLibro(walPath)
	if err != nil {
		t.Fatalf("failed to reopen wal: %v", err)
	}
	defer libro2.Cerrar()

	arr2, err := NuevoSrfArrendador(func(c string) string { return c }, libro2)
	if err != nil {
		t.Fatalf("failed to construct restarted lessor: %v", err)
	}
	barrera2, err := NuevaBarreraAlmacenamiento(capsula, destino, arr2, libro2)
	if err != nil {
		t.Fatalf("failed to construct restarted boundary: %v", err)
	}

	if barrera2.UltimaEpoca() != 2 {
		t.Fatalf("REGRESSION U07-F2: restarted boundary failed to recover epoch, got %d want 2", barrera2.UltimaEpoca())
	}

	// Deposed holder with old epoch 1 write must be rejected
	errStale := barrera2.Escribir(sesion1, []byte("stale-epoch-1"))
	if errStale == nil {
		t.Fatalf("REGRESSION U07-F2: restarted boundary accepted stale epoch 1 write")
	}
	if !errors.Is(errStale, SrfErrEpocaVencida) {
		t.Fatalf("expected SrfErrEpocaVencida, got: %v", errStale)
	}

	// Legitimate holder with epoch 3 can write
	sesion3, err := arr2.Conceder(capsula, destino, "titular-3", time.Minute, false)
	if err != nil {
		t.Fatalf("failed to grant lease 3: %v", err)
	}
	if err := barrera2.Escribir(sesion3, []byte("legit-epoch-3")); err != nil {
		t.Fatalf("legitimate write on restarted boundary failed: %v", err)
	}
}

// Regression test for U07-F4: Lessor must recover epoch counter on restart and never swallow journal errors.
func TestRegressionU07F4_LessorRecoversEpochAndNeverSwallowsJournalErrors(t *testing.T) {
	tmpDir := t.TempDir()
	walPath := filepath.Join(tmpDir, "f4.wal")
	libro, err := wal.AbrirLibro(walPath)
	if err != nil {
		t.Fatalf("failed to open wal: %v", err)
	}

	arr1, err := NuevoSrfArrendador(func(c string) string { return c }, libro)
	if err != nil {
		t.Fatalf("failed to construct lessor: %v", err)
	}
	capsula := "slot-f4"
	var destino uint32 = 9304

	_, _ = arr1.Conceder(capsula, destino, "titular-1", time.Minute, false)
	s2, _ := arr1.Conceder(capsula, destino, "titular-2", time.Minute, false)
	if s2.Epoca != 2 {
		t.Fatalf("expected epoch 2, got %d", s2.Epoca)
	}

	_ = libro.Cerrar()

	// 1. Restart lessor: must recover epoch counter = 2
	libro2, err := wal.AbrirLibro(walPath)
	if err != nil {
		t.Fatalf("failed to reopen wal: %v", err)
	}

	arr2, err := NuevoSrfArrendador(func(c string) string { return c }, libro2)
	if err != nil {
		t.Fatalf("failed to construct restarted lessor: %v", err)
	}

	s3, err := arr2.Conceder(capsula, destino, "titular-3", time.Minute, false)
	if err != nil {
		t.Fatalf("failed to grant lease after restart: %v", err)
	}
	if s3.Epoca != 3 {
		t.Fatalf("REGRESSION U07-F4: lessor failed to recover counter, got epoch %d want 3", s3.Epoca)
	}

	// 2. Journal errors must never be swallowed: close WAL and attempt grant
	_ = libro2.Cerrar()
	_, errJournalFail := arr2.Conceder(capsula, destino, "titular-4", time.Minute, false)
	if errJournalFail == nil {
		t.Fatalf("REGRESSION U07-F4: lessor granted lease despite failed journal write")
	}
}

// Regression test for U07-F5: WAL recovery must order records by highest epoch.
func TestRegressionU07F5_WALRecoveryOrderedByEpoch(t *testing.T) {
	libro, _ := abrirWALTest(t, "f5")
	arr, err := NuevoSrfArrendador(func(c string) string { return c }, libro)
	if err != nil {
		t.Fatalf("failed to construct lessor: %v", err)
	}
	capsula := "slot-f5"
	var destino uint32 = 9305

	det10, _ := json.Marshal(map[string]any{
		"capsula":  capsula,
		"destino":  destino,
		"epoca":    10,
		"titular":  "titular-10-new",
		"vence":    time.Now().Add(time.Hour),
		"irrevers": true,
	})
	reg10 := wal.Registro{
		Clase:    wal.ClaseLocal,
		Tipo:     wal.TipoReserva,
		Clave:    fmt.Sprintf("lease:%s:%d", capsula, destino),
		Detalles: det10,
	}

	det5, _ := json.Marshal(map[string]any{
		"capsula":  capsula,
		"destino":  destino,
		"epoca":    5,
		"titular":  "titular-5-stale",
		"vence":    time.Now().Add(-time.Hour),
		"irrevers": false,
	})
	reg5 := wal.Registro{
		Clase:    wal.ClaseLocal,
		Tipo:     wal.TipoReserva,
		Clave:    fmt.Sprintf("lease:%s:%d", capsula, destino),
		Detalles: det5,
	}

	// Replay in inverted order: epoch 10 first, then older epoch 5
	if err := arr.RecuperarDesdeWAL([]wal.Registro{reg10, reg5}); err != nil {
		t.Fatalf("recovery failed: %v", err)
	}

	k := srfClaveArriendo{capsula: capsula, destino: destino}
	e := arr.estados[k]
	if e == nil {
		t.Fatalf("expected state for destination")
	}
	if e.epoca != 10 {
		t.Fatalf("expected epoch 10, got %d", e.epoca)
	}
	if e.titular != "titular-10-new" {
		t.Fatalf("REGRESSION U07-F5: older record overwrote titular to %q", e.titular)
	}
	if !e.irrevers {
		t.Fatalf("REGRESSION U07-F5: older record overwrote irrevers flag")
	}
}

// Regression test for U07-F9: Raw capability token must never be written to WAL.
// If reverted (token marshaled into WAL details): bytes.Contains detects token bytes and fails.
func TestRegressionU07F9_WALNeverContainsTokenBytes(t *testing.T) {
	libro, walPath := abrirWALTest(t, "f9")
	arr, err := NuevoSrfArrendador(func(c string) string { return c }, libro)
	if err != nil {
		t.Fatalf("failed to construct lessor: %v", err)
	}

	sesion, err := arr.Conceder("capsula-f9", 9309, "titular-f9", time.Minute, false)
	if err != nil {
		t.Fatalf("failed to grant lease: %v", err)
	}

	// Read raw WAL file bytes
	walBytes, err := os.ReadFile(walPath)
	if err != nil {
		t.Fatalf("failed to read wal file: %v", err)
	}

	// The raw 16-byte capability secret must NOT appear in the WAL
	if bytes.Contains(walBytes, sesion.Token[:]) {
		t.Fatalf("REGRESSION U07-F9: raw capability token bytes leaked into WAL journal file")
	}
}

// Regression test for U07-F10: Boundary must reject expired leases.
// If reverted (boundary ignores expiry): write with expired lease succeeds.
func TestRegressionU07F10_ExpiredLeaseRejected(t *testing.T) {
	libro, _ := abrirWALTest(t, "f10")
	arr, err := NuevoSrfArrendador(func(c string) string { return c }, libro)
	if err != nil {
		t.Fatalf("failed to construct lessor: %v", err)
	}
	capsula := "capsula-f10"
	var destino uint32 = 9310

	barrera, err := NuevaBarreraAlmacenamiento(capsula, destino, arr, libro)
	if err != nil {
		t.Fatalf("failed to construct boundary: %v", err)
	}

	// Grant lease with very short TTL
	sesion, err := arr.Conceder(capsula, destino, "titular-f10", 15*time.Millisecond, false)
	if err != nil {
		t.Fatalf("failed to grant lease: %v", err)
	}

	// Wait for TTL to elapse
	time.Sleep(30 * time.Millisecond)

	// Boundary must reject the write
	errExpired := barrera.Escribir(sesion, []byte("expired-data"))
	if errExpired == nil {
		t.Fatalf("REGRESSION U07-F10: expired lease write committed successfully at boundary")
	}
	if !errors.Is(errExpired, SrfErrEpocaVencida) {
		t.Fatalf("expected SrfErrEpocaVencida, got: %v", errExpired)
	}

	// Lessor Validar must also report expired
	if errVal := arr.Validar(sesion); !errors.Is(errVal, SrfErrEpocaVencida) {
		t.Fatalf("expected Validar to return SrfErrEpocaVencida, got: %v", errVal)
	}

	// AutenticarToken must return false for expired token
	if arr.AutenticarToken(capsula, destino, sesion.Epoca, sesion.Token) {
		t.Fatalf("REGRESSION U07-F10: AutenticarToken returned true for expired lease")
	}
}

// Regression test for U07-F11: Revoked lease cannot write to storage boundary and its tokens are dropped.
// If reverted (boundary doesn't consult Validar or revocation keeps token): write succeeds.
func TestRegressionU07F11_RevokedLeaseRejected(t *testing.T) {
	libro, _ := abrirWALTest(t, "f11")
	arr, err := NuevoSrfArrendador(func(c string) string { return c }, libro)
	if err != nil {
		t.Fatalf("failed to construct lessor: %v", err)
	}
	capsula := "capsula-f11"
	var destino uint32 = 9311

	barrera, err := NuevaBarreraAlmacenamiento(capsula, destino, arr, libro)
	if err != nil {
		t.Fatalf("failed to construct boundary: %v", err)
	}

	sesion, err := arr.Conceder(capsula, destino, "titular-f11", time.Minute, false)
	if err != nil {
		t.Fatalf("failed to grant lease: %v", err)
	}

	// Revoke capsule
	if err := arr.RevocarCapsula(capsula); err != nil {
		t.Fatalf("failed to revoke capsule: %v", err)
	}

	// Boundary write must be rejected
	errRevoked := barrera.Escribir(sesion, []byte("revoked-write"))
	if errRevoked == nil {
		t.Fatalf("REGRESSION U07-F11: write from revoked lease was accepted by storage boundary")
	}

	// AutenticarToken must return false because token was purged on revocation
	if arr.AutenticarToken(capsula, destino, sesion.Epoca, sesion.Token) {
		t.Fatalf("REGRESSION U07-F11: AutenticarToken succeeded after capsule revocation")
	}
}

// Regression test for U07-F12: Multiple writes under the same valid lease must all succeed.
// If reverted (boundary rejects l.Epoca <= b.ultimaEpoca): write 2 and 3 fail.
func TestRegressionU07F12_MultipleWritesUnderSameLeaseAccepted(t *testing.T) {
	libro, _ := abrirWALTest(t, "f12")
	arr, err := NuevoSrfArrendador(func(c string) string { return c }, libro)
	if err != nil {
		t.Fatalf("failed to construct lessor: %v", err)
	}
	capsula := "capsula-f12"
	var destino uint32 = 9312

	barrera, err := NuevaBarreraAlmacenamiento(capsula, destino, arr, libro)
	if err != nil {
		t.Fatalf("failed to construct boundary: %v", err)
	}

	sesion, err := arr.Conceder(capsula, destino, "titular-f12", time.Hour, false)
	if err != nil {
		t.Fatalf("failed to grant lease: %v", err)
	}

	// Write 1
	if err := barrera.Escribir(sesion, []byte("write-1")); err != nil {
		t.Fatalf("write 1 failed: %v", err)
	}

	// Write 2: MUST SUCCEED under same valid lease (rejecting equal epochs was the F12 bug)
	if err := barrera.Escribir(sesion, []byte("write-2")); err != nil {
		t.Fatalf("REGRESSION U07-F12: write 2 under same valid lease was rejected: %v", err)
	}

	// Write 3: MUST SUCCEED
	if err := barrera.Escribir(sesion, []byte("write-3")); err != nil {
		t.Fatalf("REGRESSION U07-F12: write 3 under same valid lease was rejected: %v", err)
	}

	if barrera.EscriturasOk() != 3 {
		t.Fatalf("expected 3 accepted writes, got %d", barrera.EscriturasOk())
	}

	// However, a strictly lower epoch (e.g. 0) must still be rejected
	staleSession := sesion
	staleSession.Epoca = 0
	if errStale := barrera.Escribir(staleSession, []byte("stale")); errStale == nil {
		t.Fatalf("expected lower epoch 0 to be rejected")
	}
}

// Regression test for U07-F13: Boundary must fail closed without lessor and cannot have epoch poisoned.
// If reverted: arbitrary unauthenticated epochs accepted.
func TestRegressionU07F13_NoValidatorFallbackEpochPoisoning(t *testing.T) {
	libro, _ := abrirWALTest(t, "f13")
	capsula := "capsula-f13"
	var destino uint32 = 9313

	// Attempting to construct boundary with nil lessor must fail
	_, errConstruct := NuevaBarreraAlmacenamiento(capsula, destino, nil, libro)
	if errConstruct == nil {
		t.Fatalf("REGRESSION U07-F13: boundary constructed with nil lessor")
	}

	// Even if a boundary has nil lessor internally, Escribir fails closed
	barrera := &BarreraAlmacenamiento{
		capsula: capsula,
		destino: destino,
	}
	fakeSession := LeaseSession{
		Capsula: capsula,
		Destino: destino,
		Epoca:   12345,
		Token:   [16]byte{1, 2, 3},
		Vence:   time.Now().Add(time.Hour),
	}
	errWrite := barrera.Escribir(fakeSession, []byte("poison"))
	if errWrite == nil {
		t.Fatalf("REGRESSION U07-F13: boundary without lessor accepted write and poisoned epoch")
	}
	if barrera.UltimaEpoca() != 0 {
		t.Fatalf("boundary epoch was poisoned to %d", barrera.UltimaEpoca())
	}
}

// Regression test for U07-F14: Durable WAL state is mandatory in constructors.
// If reverted: constructors allow nil WAL and start amnesiac at 0.
func TestRegressionU07F14_DurableStateMandatory(t *testing.T) {
	// Lessor constructor with nil WAL must fail
	_, errArr := NuevoSrfArrendador(func(c string) string { return c }, nil)
	if errArr == nil {
		t.Fatalf("REGRESSION U07-F14: lessor constructor accepted nil WAL")
	}
	if !errors.Is(errArr, ErrSrfDurableStateRequired) {
		t.Fatalf("expected ErrSrfDurableStateRequired, got: %v", errArr)
	}

	// Storage boundary constructor with nil WAL must fail
	libro, _ := abrirWALTest(t, "f14")
	arr, _ := NuevoSrfArrendador(func(c string) string { return c }, libro)

	_, errBarr := NuevaBarreraAlmacenamiento("slot-f14", 9314, arr, nil)
	if errBarr == nil {
		t.Fatalf("REGRESSION U07-F14: boundary constructor accepted nil WAL")
	}
	if !errors.Is(errBarr, ErrSrfDurableStateRequired) {
		t.Fatalf("expected ErrSrfDurableStateRequired, got: %v", errBarr)
	}
}

// Regression test for U07-F15: tokensEmitidos is pruned on epoch advance, expiry, and revocation.
// If reverted: tokens retained indefinitely.
func TestRegressionU07F15_TokensEmitidosPruned(t *testing.T) {
	libro, _ := abrirWALTest(t, "f15")
	arr, err := NuevoSrfArrendador(func(c string) string { return c }, libro)
	if err != nil {
		t.Fatalf("failed to construct lessor: %v", err)
	}
	capsula := "capsula-f15"
	var destino uint32 = 9315

	// 1. Epoch advance prunes older epoch tokens for same destination
	s1, _ := arr.Conceder(capsula, destino, "titular-1", time.Hour, false)
	if _, ok := arr.tokensEmitidos[srfClaveEpoca{capsula: capsula, destino: destino, epoca: s1.Epoca}]; !ok {
		t.Fatalf("expected s1 token to be present")
	}
	s2, _ := arr.Conceder(capsula, destino, "titular-2", time.Hour, false)
	if _, ok := arr.tokensEmitidos[srfClaveEpoca{capsula: capsula, destino: destino, epoca: s2.Epoca}]; !ok {
		t.Fatalf("expected s2 token to be present")
	}
	s3, _ := arr.Conceder(capsula, destino, "titular-3", time.Hour, false)
	if _, ok := arr.tokensEmitidos[srfClaveEpoca{capsula: capsula, destino: destino, epoca: s1.Epoca}]; ok {
		t.Fatalf("REGRESSION U07-F15: older epoch token not pruned on epoch advance")
	}
	if _, ok := arr.tokensEmitidos[srfClaveEpoca{capsula: capsula, destino: destino, epoca: s3.Epoca}]; !ok {
		t.Fatalf("expected s3 token to be present")
	}

	// 2. Expiry prunes token
	sExp, _ := arr.Conceder("capsula-exp", 9316, "titular-exp", 15*time.Millisecond, false)
	time.Sleep(30 * time.Millisecond)
	// Calling AutenticarToken or podar triggers expiry pruning
	_ = arr.AutenticarToken("capsula-exp", 9316, sExp.Epoca, sExp.Token)
	if _, ok := arr.tokensEmitidos[srfClaveEpoca{capsula: "capsula-exp", destino: 9316, epoca: sExp.Epoca}]; ok {
		t.Fatalf("REGRESSION U07-F15: expired token was not pruned from tokensEmitidos")
	}

	// 3. Revocation prunes tokens for capsule
	sRev, _ := arr.Conceder("capsula-rev", 9317, "titular-rev", time.Hour, false)
	if _, ok := arr.tokensEmitidos[srfClaveEpoca{capsula: "capsula-rev", destino: 9317, epoca: sRev.Epoca}]; !ok {
		t.Fatalf("expected sRev token to be present before revocation")
	}
	_ = arr.RevocarCapsula("capsula-rev")
	if _, ok := arr.tokensEmitidos[srfClaveEpoca{capsula: "capsula-rev", destino: 9317, epoca: sRev.Epoca}]; ok {
		t.Fatalf("REGRESSION U07-F15: revoked capsule tokens not pruned from tokensEmitidos")
	}
}

// TestBateriaCompletaDiezOrdenes explicitly exercises and proves the ten battery orderings (a) through (j).
func TestBateriaCompletaDiezOrdenes(t *testing.T) {
	libro, walPath := abrirWALTest(t, "bateria")
	arr, err := NuevoSrfArrendador(func(c string) string { return c }, libro)
	if err != nil {
		t.Fatalf("failed to construct lessor: %v", err)
	}

	capsula := "slot-bateria"
	var destino uint32 = 9400

	barrera, err := NuevaBarreraAlmacenamiento(capsula, destino, arr, libro)
	if err != nil {
		t.Fatalf("failed to construct boundary: %v", err)
	}

	// Grant valid lease (epoch 1)
	sesion1, err := arr.Conceder(capsula, destino, "holder-1", time.Minute, false)
	if err != nil {
		t.Fatalf("grant failed: %v", err)
	}

	// Battery (a): forged epoch 999 with a real token: REJECTED
	forgedA := sesion1
	forgedA.Epoca = 999
	errA := barrera.Escribir(forgedA, []byte("data-a"))
	if errA == nil {
		t.Fatalf("battery (a) failed: forged epoch 999 accepted")
	}
	fmt.Printf("(a) forged epoch 999 with a real token: REJECTED (%v)\n", errA)

	// Battery (b): nil-token session: REJECTED
	nilB := LeaseSession{
		Capsula: capsula,
		Destino: destino,
		Epoca:   1000,
		Vence:   time.Now().Add(time.Hour),
	}
	errB := barrera.Escribir(nilB, []byte("data-b"))
	if errB == nil {
		t.Fatalf("battery (b) failed: nil-token session accepted")
	}
	fmt.Printf("(b) nil-token session: REJECTED (%v)\n", errB)

	// Set up boundary at epoch 2 for (c) and (d)
	sesion2, err := arr.Conceder(capsula, destino, "holder-2", time.Minute, false)
	if err != nil {
		t.Fatalf("grant 2 failed: %v", err)
	}
	if err := barrera.Escribir(sesion2, []byte("holder-2-init")); err != nil {
		t.Fatalf("boundary advance failed: %v", err)
	}
	if barrera.UltimaEpoca() != 2 {
		t.Fatalf("expected boundary epoch 2, got %d", barrera.UltimaEpoca())
	}

	// Battery (c): stale epoch 1 against a boundary at 2: REJECTED
	errC := barrera.Escribir(sesion1, []byte("data-c-stale"))
	if errC == nil {
		t.Fatalf("battery (c) failed: stale epoch 1 accepted at boundary 2")
	}
	fmt.Printf("(c) stale epoch 1 against a boundary at 2: REJECTED (%v)\n", errC)

	// Battery (d): the same holder writing three times under one valid lease: ALL THREE ACCEPTED
	// holder 2 already wrote once above (count=1), now writes 2 and 3:
	errD2 := barrera.Escribir(sesion2, []byte("holder-2-write-2"))
	errD3 := barrera.Escribir(sesion2, []byte("holder-2-write-3"))
	if errD2 != nil || errD3 != nil {
		t.Fatalf("battery (d) failed: multiple writes under valid lease rejected (err2=%v, err3=%v)", errD2, errD3)
	}
	fmt.Printf("(d) the same holder writing three times under one valid lease: ALL THREE ACCEPTED\n")

	// Battery (e): expired lease: REJECTED
	sesionE, err := arr.Conceder(capsula, destino, "holder-e", 15*time.Millisecond, false)
	if err != nil {
		t.Fatalf("grant e failed: %v", err)
	}
	time.Sleep(30 * time.Millisecond)
	errE := barrera.Escribir(sesionE, []byte("data-e-expired"))
	if errE == nil {
		t.Fatalf("battery (e) failed: expired lease accepted")
	}
	fmt.Printf("(e) expired lease: REJECTED (%v)\n", errE)

	// Battery (f): revoked lease: REJECTED
	sesionF, err := arr.Conceder(capsula, destino, "holder-f", time.Minute, false)
	if err != nil {
		t.Fatalf("grant f failed: %v", err)
	}
	_ = arr.RevocarCapsula(capsula)
	errF := barrera.Escribir(sesionF, []byte("data-f-revoked"))
	if errF == nil {
		t.Fatalf("battery (f) failed: revoked lease accepted")
	}
	fmt.Printf("(f) revoked lease: REJECTED (%v)\n", errF)

	// Battery (g): token for destination A used at destination B: REJECTED
	sesionG, err := arr.Conceder(capsula, 9401, "holder-g", time.Minute, false)
	if err != nil {
		t.Fatalf("grant g failed: %v", err)
	}
	// Try writing to boundary (which is at destination 9400)
	errG := barrera.Escribir(sesionG, []byte("data-g-cross"))
	if errG == nil {
		t.Fatalf("battery (g) failed: destination mismatch accepted")
	}
	// Also test tampered destination field with token from 9401
	tamperedG := sesionG
	tamperedG.Destino = destino
	errGTampered := barrera.Escribir(tamperedG, []byte("data-g-tampered"))
	if errGTampered == nil {
		t.Fatalf("battery (g) failed: tampered destination token accepted")
	}
	fmt.Printf("(g) token for destination A used at destination B: REJECTED (%v)\n", errG)

	// Battery (h): boundary constructed without durable state: MUST FAIL TO CONSTRUCT, not start at 0
	_, errH := NuevaBarreraAlmacenamiento(capsula, destino, arr, nil)
	if errH == nil {
		t.Fatalf("battery (h) failed: boundary constructed without durable state")
	}
	fmt.Printf("(h) boundary constructed without durable state: MUST FAIL TO CONSTRUCT, not start at 0 (%v)\n", errH)

	// Battery (i): lessor constructed without durable state: MUST FAIL TO CONSTRUCT
	_, errI := NuevoSrfArrendador(func(c string) string { return c }, nil)
	if errI == nil {
		t.Fatalf("battery (i) failed: lessor constructed without durable state")
	}
	fmt.Printf("(i) lessor constructed without durable state: MUST FAIL TO CONSTRUCT (%v)\n", errI)

	// Battery (j): the WAL file after a grant: MUST NOT CONTAIN THE TOKEN BYTES
	walBytes, err := os.ReadFile(walPath)
	if err != nil {
		t.Fatalf("battery (j) failed to read wal: %v", err)
	}
	if bytes.Contains(walBytes, sesion1.Token[:]) || bytes.Contains(walBytes, sesion2.Token[:]) {
		t.Fatalf("battery (j) failed: WAL contains capability token bytes")
	}
	fmt.Printf("(j) the WAL file after a grant: MUST NOT CONTAIN THE TOKEN BYTES\n")
}

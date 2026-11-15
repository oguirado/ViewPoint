package wal_test

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"opensuperficies.local/srf/src/orchestratorbook/wal"
)

func TestDesenlaceDeRC(t *testing.T) {
	cases := []struct {
		rc       wal.SrfRCMotor
		expected wal.SrfDesenlace
	}{
		{wal.RCOk, wal.DesenlaceConfirmado},
		{wal.RCLimpio, wal.DesenlaceNoEnviado},
		{wal.RCUso, wal.DesenlaceNoEnviado},
		{wal.RCSustrato, wal.DesenlaceNoEnviado},
		{wal.RCSinFin, wal.DesenlaceEnviadoParcial},
		{wal.RCOcupada, wal.DesenlaceNoEnviado},
		{wal.RCAmbiguo, wal.DesenlaceEnDuda},
		{wal.RCHumano, wal.DesenlaceNoEnviado},
		{wal.RCAbortada, wal.DesenlaceEnDuda},
		{wal.RCNoAuth, wal.DesenlaceNoEnviado},
		{wal.SrfRCMotor(99), wal.DesenlaceEnDuda}, // unknown defaults to EN_DUDA
	}

	for _, tc := range cases {
		got := wal.DesenlaceDeRC(tc.rc)
		if got != tc.expected {
			t.Errorf("DesenlaceDeRC(%v) = %v; want %v", tc.rc, got, tc.expected)
		}
	}

	rcMap := wal.MapaRC()
	if len(rcMap) != 10 {
		t.Fatalf("MapaRC() length = %d; want 10", len(rcMap))
	}
	if rcMap["6"] != "EN_DUDA" {
		t.Errorf("rcMap[\"6\"] = %s; want EN_DUDA", rcMap["6"])
	}
	if rcMap["7"] != "NO_ENVIADO" {
		t.Errorf("rcMap[\"7\"] = %s; want NO_ENVIADO", rcMap["7"])
	}
}

func TestLibroConsumoWritingAndFsync(t *testing.T) {
	tmpDir := t.TempDir()
	logPath := filepath.Join(tmpDir, "test.wal")

	libro, err := wal.AbrirLibro(logPath)
	if err != nil {
		t.Fatalf("AbrirLibro failed: %v", err)
	}
	defer libro.Cerrar()

	txID := "tx-100"
	clave := "key-query-100"

	// 1. BEGIN (Local)
	lsnBegin, err := libro.EscribirBegin(txID)
	if err != nil {
		t.Fatalf("EscribirBegin failed: %v", err)
	}
	if lsnBegin != 1 {
		t.Errorf("lsnBegin = %d; want 1", lsnBegin)
	}

	// 2. RESERVA (Local)
	lsnRes, err := libro.EscribirReserva(txID, map[string]int{"quota": 1})
	if err != nil {
		t.Fatalf("EscribirReserva failed: %v", err)
	}
	if lsnRes != 2 {
		t.Errorf("lsnRes = %d; want 2", lsnRes)
	}

	// 3. INTENCION (External) - must trigger immediate fsync
	lsnInt, err := libro.EscribirIntencion(txID, clave, map[string]string{"action": "dispatch"})
	if err != nil {
		t.Fatalf("EscribirIntencion failed: %v", err)
	}
	if lsnInt != 3 {
		t.Errorf("lsnInt = %d; want 3", lsnInt)
	}
	// Invariant: LSN(INTENCION(k)) <= FlushedLSN
	if libro.FlushedLSN() < lsnInt {
		t.Errorf("FlushedLSN (%d) < lsnInt (%d); external flush invariant breached", libro.FlushedLSN(), lsnInt)
	}

	// 4. DESENLACE (External) - must trigger immediate fsync
	lsnDes, err := libro.EscribirDesenlace(txID, clave, wal.DesenlaceConfirmado, wal.RCOk, nil)
	if err != nil {
		t.Fatalf("EscribirDesenlace failed: %v", err)
	}
	if lsnDes != 4 {
		t.Errorf("lsnDes = %d; want 4", lsnDes)
	}
	if libro.FlushedLSN() < lsnDes {
		t.Errorf("FlushedLSN (%d) < lsnDes (%d); external flush invariant breached", libro.FlushedLSN(), lsnDes)
	}

	// 5. END (Local)
	lsnEnd, err := libro.EscribirEnd(txID)
	if err != nil {
		t.Fatalf("EscribirEnd failed: %v", err)
	}
	if lsnEnd != 5 {
		t.Errorf("lsnEnd = %d; want 5", lsnEnd)
	}

	// Read records back
	registros, err := wal.LeerRegistros(logPath)
	if err != nil {
		t.Fatalf("LeerRegistros failed: %v", err)
	}
	if len(registros) != 5 {
		t.Fatalf("len(registros) = %d; want 5", len(registros))
	}

	rep := wal.VerificarLibro(registros, true, true)
	if rep.Violaciones != 0 {
		t.Errorf("VerificarLibro found %d violations: %v", rep.Violaciones, rep.Detalles)
	}
	if !rep.OrdenValido {
		t.Errorf("expected OrdenValido == true")
	}
	if !rep.ClavesValidas {
		t.Errorf("expected ClavesValidas == true")
	}
}

func TestThreePassRecoveryCrashWithoutIntencion(t *testing.T) {
	tmpDir := t.TempDir()
	logPath := filepath.Join(tmpDir, "crash-local.wal")

	libro, err := wal.AbrirLibro(logPath)
	if err != nil {
		t.Fatalf("AbrirLibro failed: %v", err)
	}

	txID := "tx-loser-no-send"
	_, _ = libro.EscribirBegin(txID)
	_, _ = libro.EscribirReserva(txID, map[string]int{"quota": 1})
	// Simulate crash: no INTENCION, no END written
	_ = libro.Cerrar()

	// Reopen and recover
	libro2, err := wal.AbrirLibro(logPath)
	if err != nil {
		t.Fatalf("reopen libro failed: %v", err)
	}
	defer libro2.Cerrar()

	recRes, err := wal.SrfRecuperar(libro2)
	if err != nil {
		t.Fatalf("SrfRecuperar failed: %v", err)
	}

	if len(recRes.TransaccionesCompensadas) != 1 || recRes.TransaccionesCompensadas[0] != txID {
		t.Errorf("TransaccionesCompensadas = %v; want [%s]", recRes.TransaccionesCompensadas, txID)
	}
	if len(recRes.IntencionesEnDudaForzadas) != 0 {
		t.Errorf("IntencionesEnDudaForzadas = %v; want empty", recRes.IntencionesEnDudaForzadas)
	}

	// Verify log post-recovery
	registros, err := wal.LeerRegistros(logPath)
	if err != nil {
		t.Fatalf("LeerRegistros failed: %v", err)
	}
	// Expected: BEGIN, RESERVA, CLR, END -> 4 records
	if len(registros) != 4 {
		t.Fatalf("expected 4 records post recovery, got %d", len(registros))
	}
	if registros[2].Tipo != wal.TipoCLR {
		t.Errorf("registros[2].Tipo = %s; want CLR", registros[2].Tipo)
	}
	if registros[3].Tipo != wal.TipoEnd {
		t.Errorf("registros[3].Tipo = %s; want END", registros[3].Tipo)
	}
}

func TestThreePassRecoveryCrashWithOpenIntencion(t *testing.T) {
	tmpDir := t.TempDir()
	logPath := filepath.Join(tmpDir, "crash-external.wal")

	libro, err := wal.AbrirLibro(logPath)
	if err != nil {
		t.Fatalf("AbrirLibro failed: %v", err)
	}

	txID := "tx-in-flight"
	clave := "key-inflight-001"
	_, _ = libro.EscribirBegin(txID)
	_, _ = libro.EscribirReserva(txID, map[string]int{"quota": 1})
	_, _ = libro.EscribirIntencion(txID, clave, map[string]string{"target": "search"})
	// Simulate crash right after dispatch: INTENCION written and synced, no DESENLACE
	_ = libro.Cerrar()

	// Reopen and recover
	libro2, err := wal.AbrirLibro(logPath)
	if err != nil {
		t.Fatalf("reopen libro failed: %v", err)
	}
	defer libro2.Cerrar()

	recRes, err := wal.SrfRecuperar(libro2)
	if err != nil {
		t.Fatalf("SrfRecuperar failed: %v", err)
	}

	// Must force DESENLACE(k, EN_DUDA) in Pass 2
	if len(recRes.IntencionesEnDudaForzadas) != 1 || recRes.IntencionesEnDudaForzadas[0] != clave {
		t.Errorf("IntencionesEnDudaForzadas = %v; want [%s]", recRes.IntencionesEnDudaForzadas, clave)
	}
	// Must close with END in Pass 3 retaining quota (no CLR refund)
	if len(recRes.TransaccionesFinalizadas) != 1 || recRes.TransaccionesFinalizadas[0] != txID {
		t.Errorf("TransaccionesFinalizadas = %v; want [%s]", recRes.TransaccionesFinalizadas, txID)
	}
	if len(recRes.TransaccionesCompensadas) != 0 {
		t.Errorf("TransaccionesCompensadas = %v; want empty (no CLR for EN_DUDA)", recRes.TransaccionesCompensadas)
	}

	// Verify log post-recovery
	registros, err := wal.LeerRegistros(logPath)
	if err != nil {
		t.Fatalf("LeerRegistros failed: %v", err)
	}
	// Expected: BEGIN, RESERVA, INTENCION, DESENLACE(EN_DUDA), END -> 5 records
	if len(registros) != 5 {
		t.Fatalf("expected 5 records post recovery, got %d", len(registros))
	}
	if registros[3].Tipo != wal.TipoDesenlace || registros[3].Desenlace != wal.DesenlaceEnDuda {
		t.Errorf("registros[3] = %v; want DESENLACE with EN_DUDA", registros[3])
	}
	if registros[4].Tipo != wal.TipoEnd {
		t.Errorf("registros[4].Tipo = %s; want END", registros[4].Tipo)
	}

	// Verification audit
	rep := wal.VerificarLibro(registros, true, true)
	if rep.Violaciones != 0 {
		t.Errorf("VerificarLibro found %d violations post recovery: %v", rep.Violaciones, rep.Detalles)
	}
}

func TestKeyInvariantDkRk(t *testing.T) {
	now := time.Now().UTC()

	// Case 1: normal single dispatch
	regsNormal := []wal.Registro{
		{LSN: 1, PrevLSN: 0, TxID: "tx1", Clase: wal.ClaseLocal, Tipo: wal.TipoBegin, Timestamp: now},
		{LSN: 2, PrevLSN: 1, TxID: "tx1", Clase: wal.ClaseExterna, Tipo: wal.TipoIntencion, Clave: "k1", Timestamp: now.Add(time.Second)},
		{LSN: 3, PrevLSN: 2, TxID: "tx1", Clase: wal.ClaseExterna, Tipo: wal.TipoDesenlace, Clave: "k1", Desenlace: wal.DesenlaceConfirmado, Timestamp: now.Add(2 * time.Second)},
		{LSN: 4, PrevLSN: 3, TxID: "tx1", Clase: wal.ClaseLocal, Tipo: wal.TipoEnd, Timestamp: now.Add(3 * time.Second)},
	}
	rep1 := wal.VerificarLibro(regsNormal, true, true)
	if rep1.Violaciones != 0 || !rep1.ClavesValidas {
		t.Errorf("rep1 violations = %d; want 0", rep1.Violaciones)
	}

	// Case 2: double dispatch without resolution: D(k)=2 > 1 + R(k)=1 -> VIOLATION
	regsDoble := []wal.Registro{
		{LSN: 1, PrevLSN: 0, TxID: "tx1", Clase: wal.ClaseLocal, Tipo: wal.TipoBegin, Timestamp: now},
		{LSN: 2, PrevLSN: 1, TxID: "tx1", Clase: wal.ClaseExterna, Tipo: wal.TipoIntencion, Clave: "k1", Timestamp: now.Add(time.Second)},
		{LSN: 3, PrevLSN: 2, TxID: "tx1", Clase: wal.ClaseExterna, Tipo: wal.TipoDesenlace, Clave: "k1", Desenlace: wal.DesenlaceConfirmado, Timestamp: now.Add(2 * time.Second)},
		{LSN: 4, PrevLSN: 3, TxID: "tx1", Clase: wal.ClaseLocal, Tipo: wal.TipoEnd, Timestamp: now.Add(3 * time.Second)},
		{LSN: 5, PrevLSN: 0, TxID: "tx2", Clase: wal.ClaseLocal, Tipo: wal.TipoBegin, Timestamp: now.Add(4 * time.Second)},
		{LSN: 6, PrevLSN: 5, TxID: "tx2", Clase: wal.ClaseExterna, Tipo: wal.TipoIntencion, Clave: "k1", Timestamp: now.Add(5 * time.Second)},
		{LSN: 7, PrevLSN: 6, TxID: "tx2", Clase: wal.ClaseLocal, Tipo: wal.TipoEnd, Timestamp: now.Add(6 * time.Second)},
	}
	rep2 := wal.VerificarLibro(regsDoble, true, false)
	if rep2.Violaciones == 0 || rep2.ClavesValidas {
		t.Errorf("rep2 expected violations > 0, got %d", rep2.Violaciones)
	}

	// Case 3: double dispatch with signed resolution RESOLUCION(k, NO_ENVIADO): D(k)=2 <= 1 + R(k)=2 -> VALID
	regsConResolucion := []wal.Registro{
		{LSN: 1, PrevLSN: 0, TxID: "tx1", Clase: wal.ClaseLocal, Tipo: wal.TipoBegin, Timestamp: now},
		{LSN: 2, PrevLSN: 1, TxID: "tx1", Clase: wal.ClaseExterna, Tipo: wal.TipoIntencion, Clave: "k1", Timestamp: now.Add(time.Second)},
		{LSN: 3, PrevLSN: 2, TxID: "tx1", Clase: wal.ClaseExterna, Tipo: wal.TipoDesenlace, Clave: "k1", Desenlace: wal.DesenlaceEnDuda, Timestamp: now.Add(2 * time.Second)},
		{LSN: 4, PrevLSN: 3, TxID: "tx1", Clase: wal.ClaseLocal, Tipo: wal.TipoEnd, Timestamp: now.Add(3 * time.Second)},
		{LSN: 5, PrevLSN: 0, TxID: "audit-01", Clase: wal.ClaseExterna, Tipo: wal.TipoResolucion, Clave: "k1", Desenlace: wal.DesenlaceNoEnviado, Timestamp: now.Add(4 * time.Second)},
		{LSN: 6, PrevLSN: 0, TxID: "tx2", Clase: wal.ClaseLocal, Tipo: wal.TipoBegin, Timestamp: now.Add(5 * time.Second)},
		{LSN: 7, PrevLSN: 6, TxID: "tx2", Clase: wal.ClaseExterna, Tipo: wal.TipoIntencion, Clave: "k1", Timestamp: now.Add(6 * time.Second)},
		{LSN: 8, PrevLSN: 7, TxID: "tx2", Clase: wal.ClaseExterna, Tipo: wal.TipoDesenlace, Clave: "k1", Desenlace: wal.DesenlaceConfirmado, Timestamp: now.Add(7 * time.Second)},
		{LSN: 9, PrevLSN: 8, TxID: "tx2", Clase: wal.ClaseLocal, Tipo: wal.TipoEnd, Timestamp: now.Add(8 * time.Second)},
	}
	rep3 := wal.VerificarLibro(regsConResolucion, true, true)
	if rep3.Violaciones != 0 || !rep3.ClavesValidas {
		t.Errorf("rep3 violations = %d, want 0; details: %v", rep3.Violaciones, rep3.Detalles)
	}
}

func TestOrderMonotonicity(t *testing.T) {
	now := time.Now().UTC()

	// Non-monotonic LSN
	regsNonMono := []wal.Registro{
		{LSN: 2, PrevLSN: 0, TxID: "tx1", Clase: wal.ClaseLocal, Tipo: wal.TipoBegin, Timestamp: now},
		{LSN: 1, PrevLSN: 2, TxID: "tx1", Clase: wal.ClaseLocal, Tipo: wal.TipoEnd, Timestamp: now.Add(time.Second)},
	}
	rep1 := wal.VerificarLibro(regsNonMono, false, true)
	if rep1.Violaciones == 0 || rep1.OrdenValido {
		t.Errorf("rep1 expected order violation for non-monotonic LSN")
	}

	// PrevLSN mismatch
	regsPrevMismatch := []wal.Registro{
		{LSN: 1, PrevLSN: 0, TxID: "tx1", Clase: wal.ClaseLocal, Tipo: wal.TipoBegin, Timestamp: now},
		{LSN: 2, PrevLSN: 99, TxID: "tx1", Clase: wal.ClaseLocal, Tipo: wal.TipoEnd, Timestamp: now.Add(time.Second)},
	}
	rep2 := wal.VerificarLibro(regsPrevMismatch, false, true)
	if rep2.Violaciones == 0 || rep2.OrdenValido {
		t.Errorf("rep2 expected order violation for PrevLSN mismatch")
	}

	// Record after END
	regsAfterEnd := []wal.Registro{
		{LSN: 1, PrevLSN: 0, TxID: "tx1", Clase: wal.ClaseLocal, Tipo: wal.TipoBegin, Timestamp: now},
		{LSN: 2, PrevLSN: 1, TxID: "tx1", Clase: wal.ClaseLocal, Tipo: wal.TipoEnd, Timestamp: now.Add(time.Second)},
		{LSN: 3, PrevLSN: 2, TxID: "tx1", Clase: wal.ClaseLocal, Tipo: wal.TipoReserva, Timestamp: now.Add(2 * time.Second)},
	}
	rep3 := wal.VerificarLibro(regsAfterEnd, false, true)
	if rep3.Violaciones == 0 || rep3.OrdenValido {
		t.Errorf("rep3 expected order violation for record after END")
	}
}

func TestRegressionH18_RetroactiveResolutionDefeated(t *testing.T) {
	now := time.Now().UTC()

	// Double dispatch with RESOLUCION appended *after* the second dispatch.
	// H18: The resolution must precede the second dispatch by LSN to authorize it.
	// When appended at the end, it must NOT retroactively legalize the double dispatch.
	regsMutant := []wal.Registro{
		{LSN: 1, PrevLSN: 0, TxID: "tx1", Clase: wal.ClaseLocal, Tipo: wal.TipoBegin, Timestamp: now},
		{LSN: 2, PrevLSN: 1, TxID: "tx1", Clase: wal.ClaseExterna, Tipo: wal.TipoIntencion, Clave: "k-mutant", Timestamp: now.Add(time.Second)},
		{LSN: 3, PrevLSN: 2, TxID: "tx1", Clase: wal.ClaseExterna, Tipo: wal.TipoDesenlace, Clave: "k-mutant", Desenlace: wal.DesenlaceConfirmado, Timestamp: now.Add(2 * time.Second)},
		{LSN: 4, PrevLSN: 3, TxID: "tx1", Clase: wal.ClaseLocal, Tipo: wal.TipoEnd, Timestamp: now.Add(3 * time.Second)},
		{LSN: 5, PrevLSN: 0, TxID: "tx2", Clase: wal.ClaseLocal, Tipo: wal.TipoBegin, Timestamp: now.Add(4 * time.Second)},
		{LSN: 6, PrevLSN: 5, TxID: "tx2", Clase: wal.ClaseExterna, Tipo: wal.TipoIntencion, Clave: "k-mutant", Timestamp: now.Add(5 * time.Second)},
		{LSN: 7, PrevLSN: 6, TxID: "tx2", Clase: wal.ClaseLocal, Tipo: wal.TipoEnd, Timestamp: now.Add(6 * time.Second)},
		// Trailing RESOLUCION appended after the double dispatch
		{LSN: 8, PrevLSN: 0, TxID: "audit-1", Clase: wal.ClaseExterna, Tipo: wal.TipoResolucion, Clave: "k-mutant", Desenlace: wal.DesenlaceNoEnviado, Timestamp: now.Add(7 * time.Second)},
	}

	rep := wal.VerificarLibro(regsMutant, true, false)
	if rep.Violaciones == 0 || rep.ClavesValidas {
		t.Fatalf("H18 regression failed: retroactive resolution legalized double dispatch (violations=%d, claves_validas=%t)",
			rep.Violaciones, rep.ClavesValidas)
	}

	// Compliant ordering: RESOLUCION precedes the re-dispatch
	regsOrdered := []wal.Registro{
		{LSN: 1, PrevLSN: 0, TxID: "tx1", Clase: wal.ClaseLocal, Tipo: wal.TipoBegin, Timestamp: now},
		{LSN: 2, PrevLSN: 1, TxID: "tx1", Clase: wal.ClaseExterna, Tipo: wal.TipoIntencion, Clave: "k-mutant", Timestamp: now.Add(time.Second)},
		{LSN: 3, PrevLSN: 2, TxID: "tx1", Clase: wal.ClaseExterna, Tipo: wal.TipoDesenlace, Clave: "k-mutant", Desenlace: wal.DesenlaceEnDuda, Timestamp: now.Add(2 * time.Second)},
		{LSN: 4, PrevLSN: 3, TxID: "tx1", Clase: wal.ClaseLocal, Tipo: wal.TipoEnd, Timestamp: now.Add(3 * time.Second)},
		// RESOLUCION precedes the second INTENCION
		{LSN: 5, PrevLSN: 0, TxID: "audit-1", Clase: wal.ClaseExterna, Tipo: wal.TipoResolucion, Clave: "k-mutant", Desenlace: wal.DesenlaceNoEnviado, Timestamp: now.Add(4 * time.Second)},
		{LSN: 6, PrevLSN: 0, TxID: "tx2", Clase: wal.ClaseLocal, Tipo: wal.TipoBegin, Timestamp: now.Add(5 * time.Second)},
		{LSN: 7, PrevLSN: 6, TxID: "tx2", Clase: wal.ClaseExterna, Tipo: wal.TipoIntencion, Clave: "k-mutant", Timestamp: now.Add(6 * time.Second)},
		{LSN: 8, PrevLSN: 7, TxID: "tx2", Clase: wal.ClaseExterna, Tipo: wal.TipoDesenlace, Clave: "k-mutant", Desenlace: wal.DesenlaceConfirmado, Timestamp: now.Add(7 * time.Second)},
		{LSN: 9, PrevLSN: 8, TxID: "tx2", Clase: wal.ClaseLocal, Tipo: wal.TipoEnd, Timestamp: now.Add(8 * time.Second)},
	}

	repOrdered := wal.VerificarLibro(regsOrdered, true, true)
	if repOrdered.Violaciones != 0 || !repOrdered.ClavesValidas {
		t.Fatalf("H18 compliant ordering failed: violations=%d, details=%v", repOrdered.Violaciones, repOrdered.Detalles)
	}
}

func TestRegressionH20_TornTailRecoveryVsMiddleCorruption(t *testing.T) {
	tmpDir := t.TempDir()

	// Part A: Torn record at tail (crash during append)
	// Log has valid BEGIN, RESERVA, INTENCION, followed by truncated bytes
	logPathTorn := filepath.Join(tmpDir, "torn-tail.wal")
	t0 := time.Now().UTC().Add(-10 * time.Minute)
	contentTorn := fmt.Sprintf(`{"lsn":1,"prev_lsn":0,"tx_id":"tx-torn","clase":"LOCAL","tipo":"BEGIN","timestamp":"%s"}
{"lsn":2,"prev_lsn":1,"tx_id":"tx-torn","clase":"LOCAL","tipo":"RESERVA","timestamp":"%s"}
{"lsn":3,"prev_lsn":2,"tx_id":"tx-torn","clase":"EXTERNA","tipo":"INTENCION","clave":"k-torn-01","timestamp":"%s"}
{"lsn":4,"prev_lsn":3,"tx_id":"tx-torn","clase":"EXTERNA","tipo":"DES`,
		t0.Format(time.RFC3339Nano),
		t0.Add(time.Second).Format(time.RFC3339Nano),
		t0.Add(2*time.Second).Format(time.RFC3339Nano)) // torn trailing record

	if err := os.WriteFile(logPathTorn, []byte(contentTorn), 0644); err != nil {
		t.Fatalf("WriteFile failed: %v", err)
	}

	// LeerRegistros must ignore torn tail without error
	registros, err := wal.LeerRegistros(logPathTorn)
	if err != nil {
		t.Fatalf("H20: LeerRegistros failed on torn tail: %v", err)
	}
	if len(registros) != 3 {
		t.Fatalf("H20: expected 3 valid records, got %d", len(registros))
	}

	// Reopen with AbrirLibro and run recovery: must truncate torn tail and force EN_DUDA
	libro, err := wal.AbrirLibro(logPathTorn)
	if err != nil {
		t.Fatalf("H20: AbrirLibro failed on torn tail: %v", err)
	}
	defer libro.Cerrar()

	recRes, err := wal.SrfRecuperar(libro)
	if err != nil {
		t.Fatalf("H20: SrfRecuperar failed on torn tail: %v", err)
	}
	if len(recRes.IntencionesEnDudaForzadas) != 1 || recRes.IntencionesEnDudaForzadas[0] != "k-torn-01" {
		t.Fatalf("H20: expected k-torn-01 in IntencionesEnDudaForzadas, got %v", recRes.IntencionesEnDudaForzadas)
	}

	// Check post-recovery log
	regsPost, err := wal.LeerRegistros(logPathTorn)
	if err != nil {
		t.Fatalf("H20: LeerRegistros post recovery failed: %v", err)
	}
	if len(regsPost) != 5 {
		t.Fatalf("H20: expected 5 records post recovery (BEGIN, RESERVA, INTENCION, DESENLACE, END), got %d", len(regsPost))
	}
	rep := wal.VerificarLibro(regsPost, true, true)
	if rep.Violaciones != 0 {
		t.Fatalf("H20: post-recovery log verification failed: %v", rep.Detalles)
	}

	// Part B: Corrupted record in the MIDDLE of the log must fail hard
	logPathMiddle := filepath.Join(tmpDir, "corrupt-middle.wal")
	contentMiddle := `{"lsn":1,"prev_lsn":0,"tx_id":"tx-mid","clase":"LOCAL","tipo":"BEGIN","timestamp":"2026-09-17T20:00:00Z"}
{"lsn":2,"corrupt_json_line_here
{"lsn":3,"prev_lsn":1,"tx_id":"tx-mid","clase":"LOCAL","tipo":"END","timestamp":"2026-09-17T20:00:02Z"}
`
	if err := os.WriteFile(logPathMiddle, []byte(contentMiddle), 0644); err != nil {
		t.Fatalf("WriteFile failed: %v", err)
	}

	_, err = wal.LeerRegistros(logPathMiddle)
	if err == nil {
		t.Fatalf("H20: expected error for corruption in middle of log, got nil")
	}
	if !strings.Contains(err.Error(), "parse json line 2 failed") {
		t.Fatalf("H20: expected 'parse json line 2 failed', got %v", err)
	}
}

func TestRegressionH21_ConcurrentOpenIntentions(t *testing.T) {
	tmpDir := t.TempDir()
	logPath := filepath.Join(tmpDir, "concurrent-intentions.wal")

	libro, err := wal.AbrirLibro(logPath)
	if err != nil {
		t.Fatalf("AbrirLibro failed: %v", err)
	}

	sharedKey := "k-shared-concurrent"
	// Tx1 writes BEGIN, RESERVA, INTENCION
	_, _ = libro.EscribirBegin("tx-c1")
	_, _ = libro.EscribirReserva("tx-c1", map[string]int{"quota": 1})
	_, _ = libro.EscribirIntencion("tx-c1", sharedKey, map[string]string{"call": "1"})

	// Tx2 writes BEGIN, RESERVA, INTENCION on the SAME key
	_, _ = libro.EscribirBegin("tx-c2")
	_, _ = libro.EscribirReserva("tx-c2", map[string]int{"quota": 1})
	_, _ = libro.EscribirIntencion("tx-c2", sharedKey, map[string]string{"call": "2"})

	_ = libro.Cerrar()

	// Reopen and recover: both intentions must be resolved to EN_DUDA, neither overwritten
	libro2, err := wal.AbrirLibro(logPath)
	if err != nil {
		t.Fatalf("reopen failed: %v", err)
	}
	defer libro2.Cerrar()

	recRes, err := wal.SrfRecuperar(libro2)
	if err != nil {
		t.Fatalf("SrfRecuperar failed: %v", err)
	}

	if len(recRes.IntencionesEnDudaForzadas) != 2 {
		t.Fatalf("H21: expected 2 IntencionesEnDudaForzadas, got %d (%v)",
			len(recRes.IntencionesEnDudaForzadas), recRes.IntencionesEnDudaForzadas)
	}
	if len(recRes.TransaccionesFinalizadas) != 2 {
		t.Fatalf("H21: expected 2 TransaccionesFinalizadas, got %d (%v)",
			len(recRes.TransaccionesFinalizadas), recRes.TransaccionesFinalizadas)
	}

	// Verify log order post recovery
	regs, err := wal.LeerRegistros(logPath)
	if err != nil {
		t.Fatalf("LeerRegistros failed: %v", err)
	}
	rep := wal.VerificarLibro(regs, false, true)
	if !rep.OrdenValido {
		t.Fatalf("H21: order invalid post recovery: %v", rep.Detalles)
	}
}

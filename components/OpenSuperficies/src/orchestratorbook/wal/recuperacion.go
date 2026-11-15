package wal

import (
	"fmt"
	"sort"
	"time"
)

// EstadoTransaccion tracks the runtime state of a transaction during recovery.
type EstadoTransaccion struct {
	TxID         string
	FirstLSN     uint64
	LastLSN      uint64
	HasIntencion bool
	Clave        string
	Ended        bool
	LocalOps     []Registro
	Desenlace    SrfDesenlace
}

// IntencionKey identifies an open intention by key and LSN.
type IntencionKey struct {
	Clave string `json:"clave"`
	LSN   uint64 `json:"lsn"`
}

// ResultadoAnalisis stores the analyzed state after Pass 1 of recovery.
type ResultadoAnalisis struct {
	TransaccionesActivas map[string]*EstadoTransaccion
	TodasTransacciones   map[string]*EstadoTransaccion
	Outcomes             map[string]SrfDesenlace // Delta(k)
	OpenIntentions       map[IntencionKey]string // A(k, lsn) -> txID
	Dispatches           map[string]int          // D(k): INTENCION count per key
	Resolutions          map[string]int          // R(k): RESOLUCION(NO_ENVIADO) count per key
	LastLSN              uint64
}

// ResultadoRecuperacion holds the summary metrics from the three-pass recovery process.
type ResultadoRecuperacion struct {
	TransaccionesAnalizadas    int      `json:"transacciones_analizadas"`
	IntencionesEnDudaForzadas  []string `json:"intenciones_en_duda_forzadas"`
	TransaccionesCompensadas   []string `json:"transacciones_compensadas"`
	TransaccionesFinalizadas   []string `json:"transacciones_finalizadas"`
	LSNFinal                   uint64   `json:"lsn_final"`
}

// ReporteVerificacion contains audit findings produced by VerificarLibro.
type ReporteVerificacion struct {
	TotalRegistros  int      `json:"total_registros"`
	ClavesEvaluadas int      `json:"claves_evaluadas"`
	Violaciones     int      `json:"violaciones"`
	Detalles        []string `json:"detalles"`
	OrdenValido     bool     `json:"orden_valido"`
	ClavesValidas   bool     `json:"claves_validas"`
}

// AnalizarWAL executes Pass 1 (Analysis) over a set of WAL records.
func AnalizarWAL(registros []Registro) *ResultadoAnalisis {
	res := &ResultadoAnalisis{
		TransaccionesActivas: make(map[string]*EstadoTransaccion),
		TodasTransacciones:   make(map[string]*EstadoTransaccion),
		Outcomes:             make(map[string]SrfDesenlace),
		OpenIntentions:       make(map[IntencionKey]string),
		Dispatches:           make(map[string]int),
		Resolutions:          make(map[string]int),
	}

	for _, reg := range registros {
		if reg.LSN > res.LastLSN {
			res.LastLSN = reg.LSN
		}

		if reg.TxID != "" {
			tx, exists := res.TodasTransacciones[reg.TxID]
			if !exists {
				tx = &EstadoTransaccion{
					TxID:     reg.TxID,
					FirstLSN: reg.LSN,
				}
				res.TodasTransacciones[reg.TxID] = tx
				res.TransaccionesActivas[reg.TxID] = tx
			}
			tx.LastLSN = reg.LSN

			if reg.Clase == ClaseLocal && reg.Tipo != TipoBegin && reg.Tipo != TipoEnd && reg.Tipo != TipoCLR {
				tx.LocalOps = append(tx.LocalOps, reg)
			}

			if reg.Tipo == TipoIntencion {
				tx.HasIntencion = true
				tx.Clave = reg.Clave
				res.Dispatches[reg.Clave]++
				res.OpenIntentions[IntencionKey{Clave: reg.Clave, LSN: reg.LSN}] = reg.TxID
			}

			if reg.Tipo == TipoDesenlace {
				tx.Desenlace = reg.Desenlace
				res.Outcomes[reg.Clave] = reg.Desenlace
				for ik, txID := range res.OpenIntentions {
					if ik.Clave == reg.Clave && txID == reg.TxID {
						delete(res.OpenIntentions, ik)
					}
				}
			}

			if reg.Tipo == TipoEnd {
				tx.Ended = true
				delete(res.TransaccionesActivas, reg.TxID)
			}
		}

		if reg.Tipo == TipoResolucion {
			if reg.Desenlace == DesenlaceNoEnviado {
				res.Resolutions[reg.Clave]++
				for ik := range res.OpenIntentions {
					if ik.Clave == reg.Clave {
						delete(res.OpenIntentions, ik)
					}
				}
			}
		}
	}

	return res
}

// SrfRecuperar executes the three-pass recovery algorithm on the provided LibroConsumo:
// 1. Pass 1 (Analysis): Rebuilds active transaction table, open intentions, and outcomes.
// 2. Pass 2 (Redo): Replays local records; forces DESENLACE(k, EN_DUDA) with fsync for any open intention.
// 3. Pass 3 (Undo): Rolls back loser transactions without INTENCION using CLRs; loser transactions with EN_DUDA retain reservation and close with END.
func SrfRecuperar(libro *LibroConsumo) (*ResultadoRecuperacion, error) {
	registros, err := LeerRegistros(libro.Path())
	if err != nil {
		return nil, fmt.Errorf("recuperacion: read records failed: %w", err)
	}

	// Pass 1: Analysis
	analisis := AnalizarWAL(registros)

	resultado := &ResultadoRecuperacion{
		TransaccionesAnalizadas: len(analisis.TodasTransacciones),
	}

	// Pass 2: Redo
	// Invariant: External records are NEVER redone.
	// For open intentions (in-flight dispatch when crash occurred), force DESENLACE(k, EN_DUDA) with fsync.
	var openKeys []IntencionKey
	for ik := range analisis.OpenIntentions {
		openKeys = append(openKeys, ik)
	}
	sort.Slice(openKeys, func(i, j int) bool {
		return openKeys[i].LSN < openKeys[j].LSN
	})

	for _, ik := range openKeys {
		txID := analisis.OpenIntentions[ik]
		detalles := map[string]string{
			"origen": "recuperacion_tres_pasos",
			"motivo": "intencion_abierta_en_reinicio",
		}
		if _, err := libro.EscribirDesenlace(txID, ik.Clave, DesenlaceEnDuda, RCAmbiguo, detalles); err != nil {
			return nil, fmt.Errorf("recuperacion: force desenlace EN_DUDA for key %s failed: %w", ik.Clave, err)
		}
		analisis.Outcomes[ik.Clave] = DesenlaceEnDuda
		resultado.IntencionesEnDudaForzadas = append(resultado.IntencionesEnDudaForzadas, ik.Clave)

		if tx, ok := analisis.TransaccionesActivas[txID]; ok {
			tx.Desenlace = DesenlaceEnDuda
		}
	}

	// Pass 3: Undo
	// Scan active loser transactions in deterministic order (by FirstLSN)
	var activeTxIDs []string
	for txID := range analisis.TransaccionesActivas {
		activeTxIDs = append(activeTxIDs, txID)
	}
	sort.Slice(activeTxIDs, func(i, j int) bool {
		return analisis.TransaccionesActivas[activeTxIDs[i]].FirstLSN < analisis.TransaccionesActivas[activeTxIDs[j]].FirstLSN
	})

	for _, txID := range activeTxIDs {
		tx := analisis.TransaccionesActivas[txID]
		if !tx.HasIntencion {
			// Loser without INTENCION: roll back local records via CLR
			for i := len(tx.LocalOps) - 1; i >= 0; i-- {
				op := tx.LocalOps[i]
				detalles := map[string]any{
					"reembolso_op": op.Tipo,
					"op_lsn":       op.LSN,
				}
				if _, err := libro.EscribirCLR(txID, op.PrevLSN, detalles); err != nil {
					return nil, fmt.Errorf("recuperacion: write clr for tx %s failed: %w", txID, err)
				}
			}
			if _, err := libro.EscribirEnd(txID); err != nil {
				return nil, fmt.Errorf("recuperacion: write end for loser tx %s failed: %w", txID, err)
			}
			resultado.TransaccionesCompensadas = append(resultado.TransaccionesCompensadas, txID)
		} else {
			// Loser with INTENCION (now EN_DUDA): retains quota reservation, emit END
			if _, err := libro.EscribirEnd(txID); err != nil {
				return nil, fmt.Errorf("recuperacion: write end for en_duda tx %s failed: %w", txID, err)
			}
			resultado.TransaccionesFinalizadas = append(resultado.TransaccionesFinalizadas, txID)
		}
	}

	resultado.LSNFinal = libro.LastLSN()
	return resultado, nil
}

// VerificarLibro validates WAL invariants according to the two-class model:
// - checkClaves: verifies the at-most-once execution theorem D(k) <= 1 + R(k), intention-outcome pairing, and no spurious outcomes.
// - checkOrden: verifies strict LSN monotonicity, PrevLSN integrity per transaction, timestamp ordering, and lifecycle bounds.
func VerificarLibro(registros []Registro, checkClaves bool, checkOrden bool) *ReporteVerificacion {
	reporte := &ReporteVerificacion{
		TotalRegistros: len(registros),
		OrdenValido:    true,
		ClavesValidas:  true,
		Detalles:       make([]string, 0),
	}

	dispatches := make(map[string]int)
	activeIntentions := make(map[string]int)
	outcomesPerKey := make(map[string]int)
	keysSeen := make(map[string]struct{})

	txLastLSN := make(map[string]uint64)
	txEnded := make(map[string]bool)

	var prevLSN uint64
	var prevTime time.Time

	for i, reg := range registros {
		// Key Invariants
		if reg.Clave != "" {
			keysSeen[reg.Clave] = struct{}{}
		}

		if checkClaves {
			switch reg.Tipo {
			case TipoIntencion:
				dispatches[reg.Clave]++
				activeIntentions[reg.Clave]++
				// H24: The invariant D(k) <= 1 + R(k) must count, for each INTENCION of a key,
				// only the resolutions of that key whose LSN is strictly less than that intention's LSN,
				// by LSN comparison, independent of the order the records happen to appear in the file.
				priorResolutions := 0
				for _, r := range registros {
					if r.Clave == reg.Clave && r.Tipo == TipoResolucion && r.Desenlace == DesenlaceNoEnviado && r.LSN < reg.LSN {
						priorResolutions++
					}
				}
				limit := 1 + priorResolutions
				if dispatches[reg.Clave] > limit {
					reporte.Violaciones++
					reporte.ClavesValidas = false
					reporte.Detalles = append(reporte.Detalles,
						fmt.Sprintf("invariante de clave violado para clave '%s' en LSN %d: D(k)=%d > 1 + R(k)=%d (resoluciones previas)",
							reg.Clave, reg.LSN, dispatches[reg.Clave], limit))
				}
			case TipoDesenlace:
				outcomesPerKey[reg.Clave]++
				if activeIntentions[reg.Clave] <= 0 {
					reporte.Violaciones++
					reporte.ClavesValidas = false
					reporte.Detalles = append(reporte.Detalles,
						fmt.Sprintf("DESENLACE para clave '%s' en LSN %d sin INTENCION activa", reg.Clave, reg.LSN))
				} else {
					activeIntentions[reg.Clave]--
				}
			case TipoResolucion:
				if reg.Desenlace == DesenlaceNoEnviado {
					if activeIntentions[reg.Clave] > 0 {
						activeIntentions[reg.Clave]--
					}
				}
			}
		}

		// Order Invariants
		if checkOrden {
			// Monotonic LSN
			if i > 0 && reg.LSN <= prevLSN {
				reporte.Violaciones++
				reporte.OrdenValido = false
				reporte.Detalles = append(reporte.Detalles,
					fmt.Sprintf("LSN %d no es estrictamente creciente (previo %d)", reg.LSN, prevLSN))
			}

			// PrevLSN integrity per transaction
			if reg.TxID != "" {
				if txEnded[reg.TxID] {
					reporte.Violaciones++
					reporte.OrdenValido = false
					reporte.Detalles = append(reporte.Detalles,
						fmt.Sprintf("registro en LSN %d recibido para transaccion ya cerrada '%s'", reg.LSN, reg.TxID))
				}

				expectedPrev := txLastLSN[reg.TxID]
				if reg.PrevLSN != expectedPrev {
					reporte.Violaciones++
					reporte.OrdenValido = false
					reporte.Detalles = append(reporte.Detalles,
						fmt.Sprintf("PrevLSN invalido en tx '%s' en LSN %d: esperado %d, obtenido %d",
							reg.TxID, reg.LSN, expectedPrev, reg.PrevLSN))
				}
				txLastLSN[reg.TxID] = reg.LSN

				if reg.Tipo == TipoEnd {
					txEnded[reg.TxID] = true
				}
			}

			// Monotonic timestamp
			if !prevTime.IsZero() && !reg.Timestamp.IsZero() {
				if reg.Timestamp.Before(prevTime) {
					reporte.Violaciones++
					reporte.OrdenValido = false
					reporte.Detalles = append(reporte.Detalles,
						fmt.Sprintf("timestamp retrocede en LSN %d: %v anterior a %v",
							reg.LSN, reg.Timestamp.Format(time.RFC3339Nano), prevTime.Format(time.RFC3339Nano)))
				}
			}
		}

		prevLSN = reg.LSN
		if !reg.Timestamp.IsZero() {
			prevTime = reg.Timestamp
		}
	}

	reporte.ClavesEvaluadas = len(keysSeen)
	return reporte
}

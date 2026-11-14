package wal

import (
	"fmt"
	"strconv"
)

// SrfDesenlace represents the deterministic transactional outcome of an operation.
type SrfDesenlace string

const (
	// DesenlaceConfirmado indicates clean execution with successful harvest; key blocked from re-dispatch.
	DesenlaceConfirmado SrfDesenlace = "CONFIRMADO"
	// DesenlaceNoEnviado indicates execution failed cleanly before dispatch; local quota refunded, key freed.
	DesenlaceNoEnviado SrfDesenlace = "NO_ENVIADO"
	// DesenlaceEnviadoParcial indicates dispatch occurred but output stream timed out or was incomplete; quota consumed, key blocked.
	DesenlaceEnviadoParcial SrfDesenlace = "ENVIADO_PARCIAL"
	// DesenlaceEnDuda indicates dispatch occurred but connection dropped or state unknown; quota consumed, never re-attempted automatically.
	DesenlaceEnDuda SrfDesenlace = "EN_DUDA"
)

// SrfRCMotor represents the return code of the execution engine (replicating motor/cdp_runner.py).
type SrfRCMotor int

const (
	// RCOk indicates clean execution, harvest complete.
	RCOk SrfRCMotor = 0
	// RCLimpio indicates clean failure before dispatch (e.g. G-ATTACH red).
	RCLimpio SrfRCMotor = 1
	// RCUso indicates invalid command arguments.
	RCUso SrfRCMotor = 2
	// RCSustrato indicates substrate or connection error before send.
	RCSustrato SrfRCMotor = 3
	// RCSinFin indicates dispatched, but stream timed out or was incomplete.
	RCSinFin SrfRCMotor = 4
	// RCOcupada indicates instance currently busy.
	RCOcupada SrfRCMotor = 5
	// RCAmbiguo indicates dispatched but connection dropped / state unknown.
	RCAmbiguo SrfRCMotor = 6
	// RCHumano indicates interactive challenge / CAPTCHA before send.
	RCHumano SrfRCMotor = 7
	// RCAbortada indicates execution aborted by supervisor signal.
	RCAbortada SrfRCMotor = 8
	// RCNoAuth indicates authorization or budget check failed before send.
	RCNoAuth SrfRCMotor = 9
)

// String returns the symbolic name of the motor return code.
func (rc SrfRCMotor) String() string {
	switch rc {
	case RCOk:
		return "RC_OK"
	case RCLimpio:
		return "RC_LIMPIO"
	case RCUso:
		return "RC_USO"
	case RCSustrato:
		return "RC_SUSTRATO"
	case RCSinFin:
		return "RC_SIN_FIN"
	case RCOcupada:
		return "RC_OCUPADA"
	case RCAmbiguo:
		return "RC_AMBIGUO"
	case RCHumano:
		return "RC_HUMANO"
	case RCAbortada:
		return "RC_ABORTADA"
	case RCNoAuth:
		return "RC_NOAUTH"
	default:
		return fmt.Sprintf("RC_DESCONOCIDO(%d)", rc)
	}
}

// DesenlaceDeRC maps a motor return code to its deterministic transactional outcome.
func DesenlaceDeRC(rc SrfRCMotor) SrfDesenlace {
	switch rc {
	case RCOk:
		return DesenlaceConfirmado
	case RCLimpio, RCUso, RCSustrato, RCOcupada, RCHumano, RCNoAuth:
		return DesenlaceNoEnviado
	case RCSinFin:
		return DesenlaceEnviadoParcial
	case RCAmbiguo, RCAbortada:
		return DesenlaceEnDuda
	default:
		return DesenlaceEnDuda
	}
}

// MapaRC returns the complete mapping of return codes (0-9) to their SrfDesenlace string values.
func MapaRC() map[string]string {
	m := make(map[string]string, 10)
	for rc := 0; rc < 10; rc++ {
		m[strconv.Itoa(rc)] = string(DesenlaceDeRC(SrfRCMotor(rc)))
	}
	return m
}

package wal

// EvidenciaExitoW8 is a host-validated receipt for one W8 attempt. All four
// checks must be established by the host; capsule claims alone are not evidence.
type EvidenciaExitoW8 struct {
	AttemptID string
	GatesOK   bool
	ExtractOK bool
	PurgeOK   bool
	ProbesOK  bool
}

// ResolverDesenlaceW8 maps W8 exit codes without interpreting them as motor RCs.
// A nil execution flag means the dispatch boundary cannot be established.
func ResolverDesenlaceW8(rc int, ejecucionIniciada *bool, attemptID string, ev *EvidenciaExitoW8) SrfDesenlace {
	if ejecucionIniciada == nil {
		return DesenlaceEnDuda
	}
	switch rc {
	case 0:
		if *ejecucionIniciada && attemptID != "" && ev != nil && ev.AttemptID == attemptID &&
			ev.GatesOK && ev.ExtractOK && ev.PurgeOK && ev.ProbesOK {
			return DesenlaceConfirmado
		}
		return DesenlaceEnDuda
	case 2, 4, 7:
		if !*ejecucionIniciada {
			return DesenlaceNoEnviado
		}
		return DesenlaceEnDuda
	case 3, 5:
		if !*ejecucionIniciada {
			return DesenlaceNoEnviado
		}
		return DesenlaceEnviadoParcial
	default:
		// Includes W8 rc 6, the motor-only rc 1, and unknown codes.
		return DesenlaceEnDuda
	}
}

// Package reprobridge adapts the C3 w8-repro/1 normalizer to harvest.Identity.
// It is a pure, in-memory adapter: host receipts and publication authority stay
// with harvest, while aggregator owns all identity parsing and hashing.
package reprobridge

import (
	"bytes"
	"context"
	"slices"
	"strings"

	"opensuperficies.local/srf/src/w8wave/aggregator"
	"opensuperficies.local/srf/src/w8wave/harvest"
	"opensuperficies.local/srf/src/workerbridge"
)

const maxRecordBytes = 1 << 20 // Defensive local envelope, not a throughput claim.

// Adapter uses the existing C3 implementation; it owns no hash function, sink,
// publisher, receipt verifier, or external resource.
type Adapter struct{}

var _ harvest.Identity = (*Adapter)(nil)

// New constructs the stateless harvest.Identity implementation.
func New() *Adapter { return &Adapter{} }

func reject(code int, class, detail string) error {
	return &workerbridge.Reject{Code: code, Class: class, Detail: detail}
}

func lowerHex(s string, width int) bool {
	if len(s) != width {
		return false
	}
	for _, c := range s {
		if (c < '0' || c > '9') && (c < 'a' || c > 'f') {
			return false
		}
	}
	return true
}

func validBinding(b harvest.ReproBinding) bool {
	if strings.TrimSpace(b.Repo) == "" || !lowerHex(b.Commit, 40) ||
		!lowerHex(b.InputSHA256, 64) || len(b.GateArgv) == 0 ||
		len(b.GateArgv) > 64 || len(b.FrameOrder) > 64 {
		return false
	}
	for _, arg := range b.GateArgv {
		if strings.TrimSpace(arg) == "" || strings.ContainsRune(arg, 0) {
			return false
		}
	}
	// FrameOrder contains opaque, ordered frozen labels, not paths or symbols.
	// Its relation to host extraction is validated by the harvest pipeline.
	for _, frame := range b.FrameOrder {
		if strings.TrimSpace(frame) == "" || strings.ContainsRune(frame, 0) {
			return false
		}
	}
	return true
}

// Canonical refuses a drifting frozen binding before any sink or publication
// operation. The aggregator's exact strict parser, canonical JSON, framing,
// normalizer version and default SHA-256 domain are the only identity authority.
// In particular, the binding's InputSHA256 and FrameOrder remain distinct frozen
// inputs to the consumer finding address; they are not invented C3 JSON fields.
func (Adapter) Canonical(ctx context.Context, in harvest.ReproInput) (harvest.CanonicalRepro, error) {
	if ctx == nil {
		return harvest.CanonicalRepro{}, reject(2, "MALFORMED", "nil context")
	}
	if err := ctx.Err(); err != nil {
		return harvest.CanonicalRepro{}, err
	}
	if !validBinding(in.Binding) {
		return harvest.CanonicalRepro{}, reject(2, "INVALID_BINDING", "incomplete repro binding, including input or ordered frames")
	}
	if len(in.Record) > maxRecordBytes {
		return harvest.CanonicalRepro{}, reject(2, "MALFORMED", "repro record exceeds 1048576 bytes")
	}
	identity, err := aggregator.ParseAndComputeIdentity(in.Record, nil)
	if err != nil {
		// Do not wrap or coerce the parser's source Class/Code/Detail.
		return harvest.CanonicalRepro{}, err
	}
	if identity.Record.Repo != in.Binding.Repo || identity.Record.Commit != in.Binding.Commit ||
		!slices.Equal(identity.Record.GateArgv, in.Binding.GateArgv) {
		return harvest.CanonicalRepro{}, reject(5, "BINDING_MISMATCH", "repro record repo, commit or gate_argv differs from frozen binding")
	}
	if err := ctx.Err(); err != nil {
		return harvest.CanonicalRepro{}, err
	}
	if !bytes.Equal(identity.FramedBytes, identity.CanonicalRecord) ||
		identity.ReproSHA256 == "" || identity.Record.Normalizer != aggregator.ReproVersion {
		return harvest.CanonicalRepro{}, reject(5, "IDENTITY_MISMATCH", "aggregator returned an inconsistent canonical identity")
	}
	return harvest.CanonicalRepro{
		Normalizer: aggregator.ReproVersion,
		Key:        identity.ReproSHA256,
		Canonical:  bytes.Clone(identity.CanonicalRecord),
	}, nil
}

// NormalizeNativeGateExit translates *trusted, host-provided* raw native gate
// evidence using the same success/failure termination categories as harvest's
// receipt validator. Absence and uncertainty are errors, not zero successes.
// This helper does not authenticate raw evidence; only the host may do so.
func NormalizeNativeGateExit(raw *int64, terminatedBy string) (*int64, error) {
	switch terminatedBy {
	case "unknown":
		return nil, reject(6, "UNCERTAIN_GATE", "native gate termination is unknown")
	case "timeout", "cancelled", "oom", "signal":
		failure := int64(5)
		return &failure, nil
	case "":
		if raw == nil {
			return nil, reject(4, "unavailable", "raw native gate exit unavailable")
		}
		if *raw == 0 {
			success := int64(0)
			return &success, nil
		}
		failure := int64(5)
		return &failure, nil
	default:
		return nil, reject(2, "MALFORMED", "unknown native gate termination category")
	}
}

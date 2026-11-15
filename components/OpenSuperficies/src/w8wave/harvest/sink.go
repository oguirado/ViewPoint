package harvest

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"sort"
	"sync"
	"unicode/utf8"

	"opensuperficies.local/srf/src/workerbridge"
)

// MemorySink is the reference host store used by unit-level adapters. Its
// reservations survive repeated Pipeline invocations that share this instance.
// A production Sink must persist the same acknowledgement boundary durably.
// This store is not a lifecycle log and never replaces the supplied WAL.
type MemorySink struct {
	mu sync.Mutex
	findings map[string]Finding
	witnesses map[string]map[string]Witness
	receipts map[string]map[string]LocalReceipt
	attempts map[string]Attempt
	publications map[string]Reservation
	publicationByFinding map[string]string
}

func NewMemorySink() *MemorySink {
	return &MemorySink{
		findings: make(map[string]Finding),
		witnesses: make(map[string]map[string]Witness),
		receipts: make(map[string]map[string]LocalReceipt),
		attempts: make(map[string]Attempt),
		publications: make(map[string]Reservation),
		publicationByFinding: make(map[string]string),
	}
}

func validateCapability(c *Capability) error {
	if c == nil || !present(c.ConsumerVerifier) || !validHex(c.ConsumerVerifierSHA256, 64) || !present(c.Surface) || !present(c.WriterAuthorization) {
		return refusal(4, "HOLD: pinned consumer verifier and writer authorization unavailable")
	}
	if c.Surface != CanonicalSurface {
		return refusal(5, "publication surface is not os-capsule")
	}
	return nil
}

// validateIdentity validates a port result; it neither normalizes a repro nor
// calculates its identity. Input and frame ordering are separate frozen
// bindings because the inherited C3 canonical golden does not contain input.
func validateIdentity(input ReproInput, r CanonicalRepro) error {
	b := input.Binding
	if !present(b.Repo) || !validHex(b.Commit, 40) || !validHex(b.InputSHA256, 64) || len(b.GateArgv) == 0 {
		return refusal(2, "incomplete repro binding, including input")
	}
	for _, arg := range b.GateArgv {
		if !present(arg) { return refusal(2, "empty repro gate argument") }
	}
	for _, frame := range b.FrameOrder {
		if !present(frame) { return refusal(2, "empty repro frame binding") }
	}
	if r.Normalizer != ReproNormalizer || !validHex(r.Key, 64) || len(r.Canonical) == 0 {
		return refusal(5, "canonical repro identity unavailable or wrong normalizer")
	}
	record, err := workerbridge.Parse(r.Canonical)
	if err != nil { return refusal(5, "canonical repro JSON invalid") }
	canonical, err := workerbridge.Canonical(record)
	if err != nil || !bytes.Equal(canonical, r.Canonical) {
		return refusal(5, "repro port did not return canonical bytes")
	}
	if record["normalizer"] != ReproNormalizer || record["repo"] != b.Repo || record["commit"] != b.Commit {
		return refusal(5, "canonical repro identity does not match frozen binding")
	}
	args, ok := record["gate_argv"].([]any)
	if !ok || len(args) != len(b.GateArgv) { return refusal(5, "canonical repro gate mismatch") }
	for i, arg := range args {
		if s, ok := arg.(string); !ok || s != b.GateArgv[i] { return refusal(5, "canonical repro gate mismatch") }
	}
	return nil
}

// findingID is a reversible storage address, not a new repro digest. It keeps
// exact canonical bytes alongside the external key, so even forced collisions
// have distinct buckets. Separate input and ordered-frame bindings never alias.
func findingID(binding ReproBinding, repro CanonicalRepro) (string, error) {
	raw, err := workerbridge.Canonical(struct {
		Binding ReproBinding `json:"binding"`
		Canonical []byte `json:"canonical"`
		Key string `json:"key"`
	}{binding, repro.Canonical, repro.Key})
	if err != nil { return "", refusal(5, "finding storage address is invalid") }
	return "finding:" + base64.RawURLEncoding.EncodeToString(raw), nil
}

func validWitness(w Witness) bool {
	for _, value := range []string{w.TaskID,w.AttemptID,w.LeaseID,w.Worker,w.Outcome} { if !utf8.ValidString(value) { return false } }
	return present(w.TaskID) && present(w.AttemptID) && present(w.LeaseID) && present(w.Worker) && present(w.Outcome) &&
		validHex(w.ReceiptSHA256, 64) && (w.ExitCode == 0 || w.ExitCode >= 2 && w.ExitCode <= 7)
}

func (s *MemorySink) Merge(ctx context.Context, observation Observation) (Finding, error) {
	if err := ctx.Err(); err != nil { return Finding{}, err }
	if err := validateIdentity(ReproInput{Binding: observation.Binding}, observation.Repro); err != nil { return Finding{}, err }
	if !validWitness(observation.Witness) { return Finding{}, refusal(5, "incomplete witness or forbidden outcome code") }
	id, err := findingID(observation.Binding, observation.Repro)
	if err != nil { return Finding{}, err }
	witnessBytes, err := workerbridge.Canonical(observation.Witness)
	if err != nil { return Finding{}, refusal(5, "invalid witness") }
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := ctx.Err(); err != nil { return Finding{}, err }
	f, exists := s.findings[id]
	if !exists {
		f = Finding{ID:id, Binding:sinkCloneBinding(observation.Binding), Repro:sinkCloneRepro(observation.Repro)}
		s.witnesses[id] = make(map[string]Witness)
	}
	s.witnesses[id][string(witnessBytes)] = sinkCloneWitness(observation.Witness)
	keys := make([]string, 0, len(s.witnesses[id]))
	for key := range s.witnesses[id] { keys = append(keys, key) }
	sort.Strings(keys)
	f.Witnesses = make([]Witness, 0, len(keys))
	for _, key := range keys { f.Witnesses = append(f.Witnesses, sinkCloneWitness(s.witnesses[id][key])) }
	s.findings[id] = f
	result := sinkCloneFinding(f)
	result.Duplicate = exists
	return result, nil
}

// Collect retains every distinct local observation, including failed or unknown
// cleanup and missing usage. Upstream host validation decides whether it is
// sufficient for success; retaining evidence never upgrades its status.
func (s *MemorySink) Collect(ctx context.Context, receipt LocalReceipt) error {
	if err := ctx.Err(); err != nil { return err }
	if !present(receipt.Attempt.Key) || !present(receipt.Attempt.TaskID) || !present(receipt.Attempt.AttemptID) || !present(receipt.Attempt.LeaseID) || len(receipt.Receipt) == 0 {
		return refusal(5, "local receipt binding or bytes absent")
	}
	receipt = sinkCloneReceipt(receipt)
	if receipt.Usage.Microunits == nil { receipt.Usage.Reason = "AUSENTE" }
	raw, err := workerbridge.Canonical(receipt)
	if err != nil { return refusal(5, "local receipt observation invalid") }
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := ctx.Err(); err != nil { return err }
	key := receipt.Attempt.Key
	if prior, ok := s.attempts[key]; ok && !sinkSameJSON(prior, receipt.Attempt) {
		return refusal(6, "attempt binding changed during receipt collection")
	}
	if s.receipts[key] == nil {
		s.receipts[key] = make(map[string]LocalReceipt)
		s.attempts[key] = sinkCloneAttempt(receipt.Attempt)
	}
	s.receipts[key][string(raw)] = receipt
	return nil
}

func validatePublication(publication Publication) error {
	if err := validateCapability(&publication.Capability); err != nil { return err }
	if !present(publication.ID) || publication.Surface != CanonicalSurface || publication.Provenance != HostMuse {
		return refusal(5, "publication identity, surface or host Muse provenance mismatch")
	}
	if err := validateIdentity(ReproInput{Binding:publication.Finding.Binding}, publication.Finding.Repro); err != nil { return err }
	id, err := findingID(publication.Finding.Binding, publication.Finding.Repro)
	if err != nil { return err }
	if publication.Finding.ID != id { return refusal(5, "publication finding address mismatch") }
	if len(publication.Finding.Witnesses) == 0 { return refusal(5, "publication witnesses absent") }
	for _, witness := range publication.Finding.Witnesses {
		if !validWitness(witness) { return refusal(5, "publication witness invalid") }
	}
	return nil
}

func sinkSamePublicationIdentity(a, b Publication) bool {
	return a.ID == b.ID && a.Surface == b.Surface && a.Provenance == b.Provenance && a.Capability == b.Capability &&
		a.Finding.ID == b.Finding.ID && sinkSameJSON(a.Finding.Binding, b.Finding.Binding) &&
		a.Finding.Repro.Normalizer == b.Finding.Repro.Normalizer && a.Finding.Repro.Key == b.Finding.Repro.Key && bytes.Equal(a.Finding.Repro.Canonical, b.Finding.Repro.Canonical)
}

// Reserve must complete before Publisher.Publish. Once reserved, another call
// returns the original immutable publication and Fresh=false, even if more
// witnesses arrived later. A crash after the external write is query-only.
func (s *MemorySink) Reserve(ctx context.Context, publication Publication) (Reservation, error) {
	if err := ctx.Err(); err != nil { return Reservation{}, err }
	if err := validatePublication(publication); err != nil { return Reservation{}, err }
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := ctx.Err(); err != nil { return Reservation{}, err }
	if previous, ok := s.publications[publication.ID]; ok {
		if !sinkSamePublicationIdentity(previous.Publication, publication) { return Reservation{}, refusal(6, "publication identity changed") }
		previous.Fresh = false
		return sinkCloneReservation(previous), nil
	}
	stored, ok := s.findings[publication.Finding.ID]
	if !ok { return Reservation{}, refusal(5, "publication before finding collection") }
	if !sinkWitnessSubset(publication.Finding.Witnesses,stored.Witnesses) { return Reservation{}, refusal(5, "publication witnesses include uncollected evidence") }
	if prior, ok := s.publicationByFinding[publication.Finding.ID]; ok && prior != publication.ID {
		return Reservation{}, refusal(6, "finding already has a publication identity")
	}
	reservation := Reservation{Status:PublicationPending, Publication:sinkClonePublication(publication)}
	s.publications[publication.ID] = reservation
	s.publicationByFinding[publication.Finding.ID] = publication.ID
	reservation.Fresh = true
	return sinkCloneReservation(reservation), nil
}

func (s *MemorySink) Confirm(ctx context.Context, ack PublicationAck) error {
	if err := ctx.Err(); err != nil { return err }
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := ctx.Err(); err != nil { return err }
	reservation, ok := s.publications[ack.ID]
	if !ok { return refusal(5, "publication acknowledgement has no reservation") }
	if !ack.Accepted || ack.Surface != CanonicalSurface || ack.Surface != reservation.Publication.Surface || ack.ConsumerVerifierSHA256 != reservation.Publication.Capability.ConsumerVerifierSHA256 {
		return refusal(5, "publication acknowledgement binding or consumer verification failed")
	}
	if reservation.Status == PublicationAccepted {
		if reservation.Ack != ack { return refusal(6, "publication acknowledgement changed") }
		return nil
	}
	reservation.Status = PublicationAccepted
	reservation.Ack = ack
	reservation.Fresh = false
	s.publications[ack.ID] = reservation
	return nil
}

// Findings returns a stable order and independent copies of every bucket.
func (s *MemorySink) Findings() []Finding {
	s.mu.Lock(); defer s.mu.Unlock()
	keys := make([]string, 0, len(s.findings))
	for key := range s.findings { keys = append(keys,key) }; sort.Strings(keys)
	result := make([]Finding, 0, len(keys))
	for _, key := range keys { result = append(result,sinkCloneFinding(s.findings[key])) }
	return result
}

// Receipts is a set of local observations, not a count of paid attempts.
func (s *MemorySink) Receipts(attemptKey string) []LocalReceipt {
	s.mu.Lock(); defer s.mu.Unlock()
	keys := make([]string,0,len(s.receipts[attemptKey]))
	for key := range s.receipts[attemptKey] { keys = append(keys,key) }; sort.Strings(keys)
	result := make([]LocalReceipt,0,len(keys))
	for _, key := range keys { result = append(result,sinkCloneReceipt(s.receipts[attemptKey][key])) }
	return result
}

func (s *MemorySink) Publications() []Reservation {
	s.mu.Lock(); defer s.mu.Unlock()
	keys := make([]string,0,len(s.publications))
	for key := range s.publications { keys = append(keys,key) }; sort.Strings(keys)
	result := make([]Reservation,0,len(keys))
	for _, key := range keys { result = append(result,sinkCloneReservation(s.publications[key])) }
	return result
}

func sinkSameJSON(a,b any) bool {
	x,ex := json.Marshal(a); y,ey := json.Marshal(b)
	return ex == nil && ey == nil && bytes.Equal(x,y)
}
func sinkWitnessSubset(part,whole []Witness) bool {
	available:=make(map[string]bool,len(whole))
	for _, witness:=range whole {
		raw,err:=workerbridge.Canonical(witness);if err!=nil{return false};available[string(raw)]=true
	}
	seen:=make(map[string]bool,len(part))
	for _, witness:=range part {
		raw,err:=workerbridge.Canonical(witness);if err!=nil{return false};key:=string(raw)
		if !available[key]||seen[key]{return false};seen[key]=true
	}
	return len(part)>0
}
func sinkCloneBinding(b ReproBinding) ReproBinding {
	b.GateArgv = sinkCloneStrings(b.GateArgv)
	b.FrameOrder = sinkCloneStrings(b.FrameOrder)
	return b
}
func sinkCloneBytes(b []byte) []byte { if b == nil { return nil }; return append([]byte{},b...) }
func sinkCloneStrings(s []string) []string { if s == nil { return nil }; return append([]string{},s...) }
func sinkCloneRepro(r CanonicalRepro) CanonicalRepro { r.Canonical = sinkCloneBytes(r.Canonical); return r }
func sinkCloneWitness(w Witness) Witness { w.Evidence = sinkCloneBytes(w.Evidence); return w }
func sinkCloneFinding(f Finding) Finding {
	f.Binding = sinkCloneBinding(f.Binding); f.Repro = sinkCloneRepro(f.Repro)
	w := make([]Witness,len(f.Witnesses)); for i := range w { w[i] = sinkCloneWitness(f.Witnesses[i]) }; f.Witnesses = w
	return f
}
func sinkCloneAttempt(a Attempt) Attempt {
	a.WorkerTask = sinkCloneBytes(a.WorkerTask); a.FrameOrder = sinkCloneStrings(a.FrameOrder); return a
}
func sinkCloneCost(c Cost) Cost { if c.Microunits != nil { n := *c.Microunits; c.Microunits = &n }; return c }
func sinkCloneReceipt(r LocalReceipt) LocalReceipt {
	r.Attempt = sinkCloneAttempt(r.Attempt); r.Receipt = sinkCloneBytes(r.Receipt); r.Usage = sinkCloneCost(r.Usage); return r
}
func sinkClonePublication(p Publication) Publication { p.Finding = sinkCloneFinding(p.Finding); return p }
func sinkCloneReservation(r Reservation) Reservation { r.Publication = sinkClonePublication(r.Publication); return r }

var _ Sink = (*MemorySink)(nil)

package harvest

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"math"
	"reflect"
	"sort"
	"sync"
	"time"

	"opensuperficies.local/srf/src/orchestratorbook/wal"
	"opensuperficies.local/srf/src/workerbridge"
)

type work struct {
	candidate Candidate
	attempt Attempt
	record AttemptRecord
	execution Execution
	extraction Extraction
	findings []Finding
	dedupIndex int
	publishIndex int
	submitted bool
	uncertain bool
	durableDone bool
	boundConflict bool
	priorOutcome *wal.Registro
	gateEvidence []byte
	blockedRun uint64
	lastError error
}
type historicAttempt struct {
	intent *wal.Registro
	terminal *wal.Registro
	conflict bool
	failureObserved bool
	gateEvidence []byte
	usage Cost
	usageConflict bool
}
type intentionDetails struct {
	WaveSHA256 string `json:"wave_sha256"`
	Attempt Attempt `json:"attempt"`
}
type outcomeDetails struct {
	ReceiptSHA256 string `json:"receipt_sha256"`
	WaveSHA256 string `json:"wave_sha256"`
	ExitCode *int `json:"exit_code"`
	Usage Cost `json:"usage"`
	UsageConflict bool `json:"usage_conflict"`
	FailureObserved bool `json:"failure_observed"`
	GateEvidence []byte `json:"gate_evidence"`
}
func sameTerminal(a,b *wal.Registro) bool {
	var x,y outcomeDetails
	return json.Unmarshal(a.Detalles,&x) == nil && json.Unmarshal(b.Detalles,&y) == nil && validHex(x.ReceiptSHA256,64) && x.ReceiptSHA256 == y.ReceiptSHA256 && x.WaveSHA256 == y.WaveSHA256 && x.ExitCode != nil && y.ExitCode != nil && *x.ExitCode == *y.ExitCode
}

// mergeUsageEvidence retains every known usage observation and makes a
// contradiction sticky, including while the attempt has only EN_DUDA records.
func mergeUsageEvidence(prior Cost, conflict bool, next Cost, nextConflict bool) (Cost,bool) {
	if conflict || nextConflict { return MissingCost(),true }
	prior = costValue(prior); next = costValue(next)
	if next.Microunits == nil { return prior,false }
	if prior.Microunits != nil && (prior.Unit != next.Unit || *prior.Microunits != *next.Microunits) { return MissingCost(),true }
	return next,false
}

// Pipeline is a cooperative bounded scheduler. Port calls are synchronous and
// must honor context; every queued or blocked item continues to own its credit.
// The receiving queue's append is the handoff acknowledgement. Only then is the
// upstream slot released. No goroutine can dispatch from an empty input queue.
type Pipeline struct {
	mu sync.Mutex
	cfg Config
	ports Ports
	queues map[Stage][]*work
	peaks map[Stage]int
	byKey map[string]*work
	ordered []*work
	byObligation map[string][]*work
	rejected map[string]bool
	candidates map[string][]byte
	history map[string]historicAttempt
	initialized bool
	doneGenerating bool
	generationBlocked uint64
	generationError error
	fatal error
	closed bool
	sequence uint64
	generated uint64
	duplicates uint64
	dispositions []Disposition
	first *time.Time
	collected *time.Time
	distinct map[string]bool
}

func NewPipeline(cfg Config, ports Ports) (*Pipeline,error) {
	f,e := Freeze(cfg.Wave.Wave())
	if e != nil || f.SHA256() != cfg.Wave.SHA256() { return nil,refusal(2,"wave was not frozen") }
	if cfg.WaveDeadline.IsZero() { return nil,refusal(2,"wave deadline absent") }
	if cfg.MaxAttemptsPerObligation == 0 { cfg.MaxAttemptsPerObligation = DefaultAttemptLimit }
	if cfg.MaxAttemptsPerObligation < 1 || cfg.MaxAttemptsPerObligation > DefaultAttemptLimit { return nil,refusal(2,"attempt budget exceeds finite cap") }
	caps := make(map[Stage]int,len(stages))
	if len(cfg.QueueCap) != len(stages) { return nil,refusal(2,"queue inventory must name every stage exactly") }
	for _,s := range stages { if cfg.QueueCap[s] <= 0 { return nil,refusal(2,"nonpositive queue capacity") }; caps[s] = cfg.QueueCap[s] }
	maxInt := int(^uint(0)>>1)
	if len(f.wave.Obligations) > maxInt/cfg.MaxAttemptsPerObligation { return nil,refusal(2,"inventory budget overflow") }
	budget := len(f.wave.Obligations)*cfg.MaxAttemptsPerObligation
	if cfg.MaxCandidates == 0 { cfg.MaxCandidates = budget }
	if cfg.MaxCandidates < 1 { return nil,refusal(2,"candidate budget must be finite and positive") }
	if ports.Generator == nil || ports.Submitter == nil || ports.Extractor == nil || ports.Identity == nil || ports.Sink == nil || ports.Clock == nil || ports.Log == nil || ports.History == nil || ports.Waves == nil {
		return nil,refusal(2,"required host port, shared WAL or wave barrier absent")
	}
	cfg.Wave = f; cfg.QueueCap = caps
	if cfg.Capability != nil { c := *cfg.Capability; cfg.Capability = &c }
	p := &Pipeline{cfg:cfg,ports:ports,queues:map[Stage][]*work{},peaks:map[Stage]int{},byKey:map[string]*work{},byObligation:map[string][]*work{},rejected:map[string]bool{},candidates:map[string][]byte{},history:map[string]historicAttempt{},distinct:map[string]bool{}}
	for _,o := range f.wave.Obligations { p.byObligation[o.ID] = nil }
	return p,nil
}

// SetCapability installs a host-injected C3 capability after a local HOLD.
// A pinned capability cannot be changed underneath an outstanding publication.
func (p *Pipeline) SetCapability(c Capability) error {
	p.mu.Lock(); defer p.mu.Unlock()
	if e := validateCapability(&c); e != nil { return e }
	if p.cfg.Capability != nil && validateCapability(p.cfg.Capability) == nil && *p.cfg.Capability != c { return refusal(5,"publication capability pin changed") }
	p.cfg.Capability = &c
	return nil
}

// cloneValue protects the immutable boundary from mutation by an adapter.
// All callers use concrete JSON-compatible, integer-only package structs.
func cloneValue[T any](v T) T {
	b,e := json.Marshal(v); if e != nil { panic(e) }
	var out T; if e = json.Unmarshal(b,&out); e != nil { panic(e) }; return out
}
func copyFrozen(f FrozenWave) FrozenWave { r,_ := Freeze(f.Wave()); return r }
func (p *Pipeline) loadHistory(ctx context.Context) error {
	if p.initialized { return nil }
	records,e := p.ports.History.Records(ctx)
	if e != nil { return refusal(6,"existing consumption WAL history unavailable") }
	for _,r := range records {
		h := p.history[r.Clave]
		switch r.Tipo {
		case wal.TipoIntencion:
			if h.intent != nil && !bytes.Equal(h.intent.Detalles,r.Detalles) { h.conflict = true }
			if h.intent == nil { x := r; x.Detalles = append([]byte(nil),r.Detalles...); h.intent = &x }
		case wal.TipoDesenlace,wal.TipoResolucion:
			var evidence outcomeDetails
			if json.Unmarshal(r.Detalles,&evidence) == nil {
				if evidence.WaveSHA256 == p.cfg.Wave.SHA256() {
					h.usage,h.usageConflict = mergeUsageEvidence(h.usage,h.usageConflict,evidence.Usage,evidence.UsageConflict)
				}
				h.failureObserved = h.failureObserved || evidence.FailureObserved
				if len(evidence.GateEvidence) != 0 {
					if len(h.gateEvidence) != 0 && !bytes.Equal(h.gateEvidence,evidence.GateEvidence) { h.conflict = true } else { h.gateEvidence = append([]byte(nil),evidence.GateEvidence...) }
				}
			}
			if r.Desenlace == wal.DesenlaceConfirmado || r.Desenlace == wal.DesenlaceEnviadoParcial {
				x := r; x.Detalles = append([]byte(nil),r.Detalles...)
				if h.terminal == nil { h.terminal = &x } else if !sameTerminal(h.terminal,&x) { h.conflict = true }
			} else if h.terminal != nil { h.conflict = true }
		}
		p.history[r.Clave] = h
	}
	p.initialized = true
	return nil
}
func (p *Pipeline) free(s Stage) bool { return len(p.queues[s]) < p.cfg.QueueCap[s] }
func (p *Pipeline) push(s Stage,w *work) {
	p.queues[s] = append(p.queues[s],w)
	if len(p.queues[s]) > p.peaks[s] { p.peaks[s] = len(p.queues[s]) }
}
func (p *Pipeline) remove(s Stage,index int) {
	q := p.queues[s]; copy(q[index:],q[index+1:]); q[len(q)-1] = nil; p.queues[s] = q[:len(q)-1]
}
func (p *Pipeline) handoff(from,to Stage,index int) {
	w := p.queues[from][index]
	p.push(to,w) // Downstream acceptance is acknowledged before returning credit.
	p.remove(from,index)
}
func (p *Pipeline) ready(s Stage) (int,*work) {
	for i,w := range p.queues[s] { if !w.boundConflict && w.blockedRun != p.sequence { return i,w } }
	return -1,nil
}
func (p *Pipeline) disposition(w *work,s Stage,code int,reason string) {
	p.dispositions = append(p.dispositions,Disposition{CandidateID:w.candidate.ID,ObligationID:w.candidate.Attempt.ObligationID,AttemptID:w.candidate.Attempt.AttemptID,Stage:s,Code:code,Reason:reason})
}
func (p *Pipeline) reject(w *work,s Stage,e error) {
	p.disposition(w,s,ExitCode(e),e.Error())
	if _,ok := p.byObligation[w.candidate.Attempt.ObligationID]; ok { p.rejected[w.candidate.Attempt.ObligationID] = true }
	if p.fatal == nil { p.fatal = e }
}
func (p *Pipeline) expired() bool { return !p.ports.Clock.Now().Before(p.cfg.WaveDeadline) }
func (p *Pipeline) operationContext(ctx context.Context) (context.Context,context.CancelFunc) {
	return context.WithTimeout(ctx,p.cfg.WaveDeadline.Sub(p.ports.Clock.Now()))
}

// Run can be called again to reconcile held work. It never regenerates an
// already admitted attempt and never resubmits a recorded external intention.
func (p *Pipeline) Run(ctx context.Context) (Account,error) {
	p.mu.Lock(); defer p.mu.Unlock()
	if ctx == nil { return p.snapshot(),refusal(2,"context absent") }
	if p.closed { a := p.snapshot(); return a,p.accountError(a) }
	if e := ctx.Err(); e != nil { return p.snapshot(),e }
	p.sequence++
	waveCtx,cancel := p.operationContext(ctx)
	e := p.ports.Waves.acquire(waveCtx,p); cancel()
	if e != nil { return p.snapshot(),e }
	if e := p.loadHistory(ctx); e != nil { return p.snapshot(),e }
	for {
		if e := ctx.Err(); e != nil {
			a := p.snapshot(); if a.CapacityHeld > 0 { return a,refusal(6,"caller stopped with capacity held") }; return a,e
		}
		progress := false
		if p.expired() {
			p.doneGenerating = true
			if p.generationError == nil { p.generationError = refusal(6,"wave deadline reached") }
			for len(p.queues[Generate]) > 0 { w := p.queues[Generate][0]; p.disposition(w,Generate,6,"wave deadline; candidate never admitted"); p.remove(Generate,0); progress = true }
		}
		if p.stepCollect(ctx) { progress = true }
		if p.stepDedup(ctx) { progress = true }
		if p.stepExtract(ctx) { progress = true }
		if p.stepSubmit(ctx) { progress = true }
		if ctx.Err() == nil && p.stepAdmit() { progress = true }
		if p.generate(ctx) { progress = true }
		p.markCollected()
		if !progress { break }
	}
	allEmpty := true
	for _,s := range stages { if len(p.queues[s]) != 0 { allEmpty = false } }
	if p.doneGenerating && allEmpty {
		p.closed = true
		p.ports.Waves.release(p)
	}
	a := p.snapshot()
	return a,p.accountError(a)
}

func (p *Pipeline) generate(ctx context.Context) bool {
	if ctx.Err() != nil || p.doneGenerating || p.generationBlocked == p.sequence || !p.free(Generate) || p.expired() { return false }
	remaining := p.cfg.MaxCandidates-int(p.generated)
	if remaining <= 0 { p.doneGenerating = true; return true }
	limit := p.cfg.QueueCap[Generate]-len(p.queues[Generate]); if limit > remaining { limit = remaining }
	req := GenerationRequest{Limit:limit,Cursor:p.generated}
	for _,w := range p.ordered {
		s := w.record.AttemptSummary
		if !w.record.Collected { s.State = Unresolved }
		req.Attempts = append(req.Attempts,s)
	}
	c,cancel := p.operationContext(ctx)
	b,e := p.ports.Generator.Generate(c,copyFrozen(p.cfg.Wave),cloneValue(req)); cancel()
	if e != nil { p.generationError = e; p.generationBlocked = p.sequence; return false }
	if len(b.Candidates) > limit {
		p.generated += uint64(len(b.Candidates)); p.doneGenerating = true
		e = refusal(5,"generator overproduced beyond granted queue credits")
		for _,candidate := range b.Candidates { p.reject(&work{candidate:cloneValue(candidate)},Generate,e) }
		return true
	}
	if len(b.Candidates) == 0 && !b.Done {
		p.generationError = refusal(4,"generator has no acknowledged input available")
		p.generationBlocked = p.sequence; return false
	}
	p.generationError = nil
	for _,candidate := range b.Candidates { p.generated++; p.push(Generate,&work{candidate:cloneValue(candidate)}) }
	p.doneGenerating = b.Done || int(p.generated) == p.cfg.MaxCandidates
	return len(b.Candidates) > 0 || p.doneGenerating
}

func (p *Pipeline) stepAdmit() bool {
	i,w := p.ready(Generate)
	if w == nil || !p.free(AdmitExecute) || p.expired() { return false }
	a,e := validateCandidate(p.cfg.Wave,w.candidate)
	if e != nil { p.reject(w,Generate,e); p.remove(Generate,i); return true }
	if !present(w.candidate.ID) { p.reject(w,Generate,refusal(2,"candidate ID absent")); p.remove(Generate,i); return true }
	cb,_ := json.Marshal(w.candidate)
	if prior,ok := p.candidates[w.candidate.ID]; ok {
		if !bytes.Equal(cb,prior) { p.reject(w,Generate,refusal(5,"candidate ID reused with changed bytes")) } else { p.duplicates++; p.disposition(w,Generate,0,"duplicate candidate delivery; original attempt retained") }
		p.remove(Generate,i); return true
	}
	p.candidates[w.candidate.ID] = append([]byte(nil),cb...)
	if prior,ok := p.byKey[a.Key]; ok {
		if !reflect.DeepEqual(a,prior.attempt) { p.reject(w,Generate,refusal(5,"attempt identity reused with changed binding")) } else { p.duplicates++; p.disposition(w,Generate,0,"duplicate attempt delivery; receipt and cost retained once") }
		p.remove(Generate,i); return true
	}
	prior,ok := p.byObligation[a.ObligationID]
	if !ok { p.reject(w,Generate,refusal(2,"candidate names an unknown obligation")); p.remove(Generate,i); return true }
	if len(prior) >= p.cfg.MaxAttemptsPerObligation { p.reject(w,Generate,refusal(5,"obligation attempt budget exhausted")); p.remove(Generate,i); return true }
	if len(prior) == 0 {
		if a.ParentAttemptID != "" { p.reject(w,Generate,refusal(5,"original attempt has no parent input")); p.remove(Generate,i); return true }
	} else {
		parent := prior[len(prior)-1]
		if a.ParentAttemptID != parent.attempt.AttemptID || a.AttemptID == parent.attempt.AttemptID || parent.record.State != ExplainedFailure || parent.record.CapacityHeld || !parent.record.Collected {
			p.reject(w,Generate,refusal(5,"retry lacks a collected failed parent with verified cleanup")); p.remove(Generate,i); return true
		}
	}
	w.attempt = cloneValue(a)
	w.record = AttemptRecord{AttemptSummary:AttemptSummary{Key:a.Key,ObligationID:a.ObligationID,AttemptID:a.AttemptID,ParentAttemptID:a.ParentAttemptID,State:Unresolved},TaskID:a.TaskID,LeaseID:a.LeaseID,Usage:MissingCost()}
	p.byKey[a.Key] = w; p.ordered = append(p.ordered,w); p.byObligation[a.ObligationID] = append(prior,w)
	if p.first == nil { now := p.ports.Clock.Now(); p.first = &now }
	p.disposition(w,Generate,0,"admitted through bounded handoff")
	p.handoff(Generate,AdmitExecute,i)
	return true
}

func (p *Pipeline) doubtful(w *work,reason string) {
	w.uncertain = true; w.record.State = Unresolved; w.record.ExitCode = 6; w.record.CapacityHeld = true
	w.lastError = refusal(6,reason); w.blockedRun = p.sequence
	if _,e := p.ports.Log.EscribirDesenlace(p.cfg.Wave.wave.WaveID,w.attempt.Key,wal.DesenlaceEnDuda,wal.SrfRCMotor(6),map[string]any{"reason":reason,"wave_sha256":p.cfg.Wave.SHA256(),"failure_observed":w.record.FailureObserved,"gate_evidence":w.gateEvidence,"usage":w.record.Usage,"usage_conflict":w.record.UsageConflict}); e != nil { w.lastError = refusal(6,"uncertain outcome could not be acknowledged by existing WAL") }
}
func (p *Pipeline) stepSubmit(ctx context.Context) bool {
	if ctx.Err() != nil { return false }
	i,w := p.ready(AdmitExecute)
	if w == nil || !p.free(ExtractVerify) { return false }
	if !w.submitted {
		if p.expired() {
			w.record.State = MissingEvidence; w.record.ExitCode = 6; w.lastError = refusal(6,"deadline before send")
			p.disposition(w,AdmitExecute,6,"deadline before send; no external effect")
			p.remove(AdmitExecute,i); return true
		}
		if h,ok := p.history[w.attempt.Key]; ok {
			w.submitted = true; w.record.Submitted = true; w.record.CapacityHeld = true
			var d intentionDetails
			if h.conflict || h.intent == nil || json.Unmarshal(h.intent.Detalles,&d) != nil || d.WaveSHA256 != p.cfg.Wave.SHA256() || !reflect.DeepEqual(d.Attempt,w.attempt) {
				w.boundConflict = true; w.uncertain = true; w.record.ExitCode = 6; w.lastError = refusal(6,"historic attempt binding differs; operator reconciliation required"); w.blockedRun = p.sequence; return false
			}
			w.priorOutcome = h.terminal
			w.record.FailureObserved = h.failureObserved; w.gateEvidence = append([]byte(nil),h.gateEvidence...)
			w.record.Usage = costValue(h.usage); w.record.UsageConflict = h.usageConflict
			w.durableDone = h.terminal != nil
			w.uncertain = !w.durableDone
		} else {
			d := intentionDetails{WaveSHA256:p.cfg.Wave.SHA256(),Attempt:cloneValue(w.attempt)}
			// Even an ambiguous log-write error can have recorded the intention.
			// Quarantine it without issuing a send or writing another intention.
			w.submitted = true; w.record.CapacityHeld = true
			if _,e := p.ports.Log.EscribirIntencion(p.cfg.Wave.wave.WaveID,w.attempt.Key,d); e != nil {
				w.uncertain = true; w.lastError = refusal(6,"intention acknowledgement unavailable; no submit issued"); w.blockedRun = p.sequence; return false
			}
			c,cancel := p.operationContext(ctx)
			if c.Err() != nil { cancel(); p.doubtful(w,"deadline or cancellation after intention; no submit issued"); return true }
			ex,e := p.ports.Submitter.Submit(c,Submission{Route:MuseSeatRoute,Wave:copyFrozen(p.cfg.Wave),Attempt:cloneValue(w.attempt)}); cancel()
			w.record.Submitted = true; w.execution = cloneValue(ex); w.record.Usage = costValue(ex.Usage)
			if e != nil || !ex.Accepted || !present(ex.Handle) {
				p.doubtful(w,"seat refused, unavailable, or submit acknowledgement uncertain; reconcile only")
				return true
			}
		}
	}
	p.handoff(AdmitExecute,ExtractVerify,i)
	return true
}

func trustedCleanup(a Attempt,x Extraction) bool {
	return x.HostValidated && x.AttemptKey == a.Key && x.Nonce == a.Nonce && x.Cleanup.AttemptKey == a.Key && x.Cleanup.LeaseID == a.LeaseID && x.Cleanup.Nonce == a.Nonce && x.Cleanup.Known && x.Cleanup.Verified
}
// observeReceipt preserves bound host evidence even while purge is unresolved.
// Status improvement cannot erase a failed gate or replace its ordered result.
func (p *Pipeline) observeReceipt(ctx context.Context,w *work,x Extraction) error {
	a := w.attempt
	if !x.HostValidated || x.AttemptKey != a.Key || x.Nonce != a.Nonce || len(x.Receipt) == 0 || len(x.Receipt) > 1<<20 { return nil }
	r,e := workerbridge.Parse(x.Receipt)
	if e != nil || r["task_id"] != a.TaskID || r["attempt_id"] != a.AttemptID || r["lease_id"] != a.LeaseID || r["lease_nonce"] != a.Nonce { return nil }
	task,e := workerbridge.Parse(a.WorkerTask); if e != nil { return nil }
	canonical,e := workerbridge.Canonical(task); if e != nil || r["task_sha256"] != digest(canonical) { return nil }
	seen := false
	for _,b := range w.record.ReceiptEvidence { if bytes.Equal(b,x.Receipt) { seen = true; break } }
	if !seen {
		w.record.ReceiptEvidence = append(w.record.ReceiptEvidence,append([]byte(nil),x.Receipt...))
		sort.Slice(w.record.ReceiptEvidence,func(i,j int) bool { return bytes.Compare(w.record.ReceiptEvidence[i],w.record.ReceiptEvidence[j]) < 0 })
	}
	if len(w.record.Receipt) == 0 { w.record.Receipt = append([]byte(nil),x.Receipt...) }
	updateUsage(w,x.Usage)
	rc := int(receiptNumber(r["exit_code"]))
	conflict := w.record.FailureObserved && rc == 0
	if rc >= 2 && rc <= 7 && rc != 6 { w.record.FailureObserved = true }
	gate := receiptObject(r["gate"])
	if gate != nil {
		w.record.ExecutionObserved = true
		gateRC := int(receiptNumber(gate["exit_code"]))
		if gateRC != 6 && (gate["raw_exit_code"] != nil || gate["terminated_by"] != nil) {
			evidence,_ := workerbridge.Canonical(map[string]any{"argv":gate["argv"],"cwd":gate["cwd"],"timeout_ms":gate["timeout_ms"],"raw_exit_code":gate["raw_exit_code"],"exit_code":gate["exit_code"],"terminated_by":gate["terminated_by"]})
			if len(w.gateEvidence) != 0 && !bytes.Equal(w.gateEvidence,evidence) { conflict = true }
			if len(w.gateEvidence) == 0 { w.gateEvidence = evidence }
			if gateRC != 0 { w.record.FailureObserved = true }
		}
	}
	// Latch failure and usage before collection: even an ambiguous Sink error
	// must not erase an observation before it is recorded in the existing WAL.
	if conflict { w.boundConflict = true }
	// Collection is independent of finding dedup and does not mark success.
	if e = p.ports.Sink.Collect(ctx,LocalReceipt{Attempt:cloneValue(a),Receipt:append([]byte(nil),x.Receipt...),Usage:costValue(x.Usage),Cleanup:x.Cleanup}); e != nil { return refusal(6,"host receipt evidence collection is unacknowledged") }
	if conflict { return refusal(6,"contradictory host receipt cannot erase earlier failure or gate evidence") }
	return nil
}
func (p *Pipeline) stepExtract(ctx context.Context) bool {
	if ctx.Err() != nil { return false }
	i,w := p.ready(ExtractVerify)
	if w == nil || !p.free(Deduplicate) { return false }
	x,e := p.ports.Extractor.Extract(ctx,cloneValue(w.attempt),cloneValue(w.execution))
	if e != nil { p.doubtful(w,"host extraction unavailable; cleanup and attempt remain unresolved"); return false }
	if e = p.observeReceipt(ctx,w,x); e != nil { p.doubtful(w,e.Error()); return false }
	if w.uncertain && (!x.ResolutionVerified || len(x.ResolutionEvidence) == 0) {
		w.lastError = refusal(6,"uncertain intention needs authenticated same-identity resolution"); w.blockedRun = p.sequence; return false
	}
	code,e := validateExtraction(p.cfg.Wave,w.attempt,w.execution,x)
	if e != nil {
		if ExitCode(e) == 6 || !trustedCleanup(w.attempt,x) { p.doubtful(w,"receipt or cleanup cannot authorize capacity release"); return false }
		w.record.State = MissingEvidence; w.record.ExitCode = 5; w.record.CapacityHeld = false
		w.lastError = e; w.extraction = cloneValue(x); w.extraction.Repros = nil; w.record.Receipt = append([]byte(nil),x.Receipt...); updateUsage(w,x.Usage)
		p.reject(w,ExtractVerify,e)
	} else {
		if w.priorOutcome != nil {
			var prior outcomeDetails
			if json.Unmarshal(w.priorOutcome.Detalles,&prior) != nil || !validHex(prior.ReceiptSHA256,64) || prior.WaveSHA256 != p.cfg.Wave.SHA256() || prior.ExitCode == nil || *prior.ExitCode != code || prior.ReceiptSHA256 != digest(x.Receipt) || (w.priorOutcome.RC != nil && *w.priorOutcome.RC != code) {
				// Keep the original terminal WAL evidence. Preserve the conflicting
				// host observation separately, but never let it upgrade the account.
				_ = p.ports.Sink.Collect(ctx,LocalReceipt{Attempt:cloneValue(w.attempt),Receipt:append([]byte(nil),x.Receipt...),Usage:costValue(x.Usage),Cleanup:x.Cleanup})
				w.boundConflict = true; w.record.State = Unresolved; w.record.CapacityHeld = true; w.lastError = refusal(6,"terminal receipt changed after collection; prior evidence retained"); return false
			}
		}
		w.extraction = cloneValue(x); w.record.Receipt = append([]byte(nil),x.Receipt...)
		w.record.ExitCode = code; w.record.CapacityHeld = false; updateUsage(w,x.Usage)
		if code == 0 { w.record.State = Succeeded } else { w.record.State = ExplainedFailure }
		w.lastError = nil
	}
	if !w.durableDone {
		d := wal.DesenlaceConfirmado
		if w.record.State == MissingEvidence { d = wal.DesenlaceEnviadoParcial }
		details := map[string]any{"receipt_sha256":digest(w.record.Receipt),"wave_sha256":p.cfg.Wave.SHA256(),"exit_code":w.record.ExitCode,"cleanup_verified":true,"usage":w.record.Usage,"usage_conflict":w.record.UsageConflict,"failure_observed":w.record.FailureObserved,"gate_evidence":w.gateEvidence}
		if w.uncertain {
			details["resolution_evidence"] = append([]byte(nil),x.ResolutionEvidence...)
			_,e = p.ports.Log.EscribirResolucion(w.attempt.Key,d,details)
		} else { _,e = p.ports.Log.EscribirDesenlace(p.cfg.Wave.wave.WaveID,w.attempt.Key,d,wal.SrfRCMotor(w.record.ExitCode),details) }
		if e != nil {
			w.record.State = Unresolved; w.record.CapacityHeld = true; w.uncertain = true; w.lastError = refusal(6,"outcome WAL acknowledgement unknown"); w.blockedRun = p.sequence; return false
		}
		w.durableDone = true; w.uncertain = false
	}
	p.handoff(ExtractVerify,Deduplicate,i)
	return true
}

func (p *Pipeline) stepDedup(ctx context.Context) bool {
	if ctx.Err() != nil { return false }
	i,w := p.ready(Deduplicate)
	if w == nil || !p.free(CollectPublish) { return false }
	if w.dedupIndex < len(w.extraction.Repros) {
		in := cloneValue(w.extraction.Repros[w.dedupIndex])
		want := ReproBinding{Repo:p.cfg.Wave.wave.Repo,Commit:p.cfg.Wave.wave.BaseCommit,InputSHA256:p.cfg.Wave.wave.InputSHA256,GateArgv:append([]string(nil),p.cfg.Wave.wave.GateArgv...),FrameOrder:append([]string(nil),w.attempt.FrameOrder...)}
		var e error
		if !reflect.DeepEqual(in.Binding,want) { e = refusal(5,"observation drifted from frozen input, commit, argv or frame order") }
		var r CanonicalRepro
		if e == nil { r,e = p.ports.Identity.Canonical(ctx,cloneValue(in)) }
		if e == nil { e = validateIdentity(in,r) }
		var f Finding
		if e == nil {
			witness := Witness{TaskID:w.attempt.TaskID,AttemptID:w.attempt.AttemptID,LeaseID:w.attempt.LeaseID,Worker:HostMuse,Outcome:string(w.record.State),ExitCode:w.record.ExitCode,ReceiptSHA256:digest(w.record.Receipt),Evidence:append([]byte(nil),w.record.Receipt...)}
			f,e = p.ports.Sink.Merge(ctx,Observation{Binding:cloneValue(in.Binding),Repro:cloneValue(r),Witness:witness})
		}
		if e != nil {
			if ExitCode(e) == 4 || ExitCode(e) == 6 || ctx.Err() != nil { w.lastError = e; w.blockedRun = p.sequence; return false }
			p.reject(w,Deduplicate,e); w.record.State = MissingEvidence; w.record.ExitCode = 5
			w.dedupIndex = len(w.extraction.Repros); w.findings = nil
		} else {
			if f.ID == "" || !reflect.DeepEqual(f.Binding,in.Binding) || !reflect.DeepEqual(f.Repro,r) { p.reject(w,Deduplicate,refusal(5,"sink returned a different canonical record")); w.record.State = MissingEvidence; w.record.ExitCode = 5; w.dedupIndex = len(w.extraction.Repros); w.findings = nil } else {
				if f.Duplicate { p.duplicates++ }
				p.distinct[f.ID] = true; w.findings = append(w.findings,cloneValue(f)); w.dedupIndex++
			}
		}
		return true
	}
	w.lastError = nil
	p.handoff(Deduplicate,CollectPublish,i)
	return true
}

func (p *Pipeline) stepCollect(ctx context.Context) bool {
	if ctx.Err() != nil { return false }
	i,w := p.ready(CollectPublish)
	if w == nil { return false }
	if !w.record.Collected {
		e := p.ports.Sink.Collect(ctx,LocalReceipt{Attempt:cloneValue(w.attempt),Receipt:append([]byte(nil),w.record.Receipt...),Usage:cloneValue(w.record.Usage),Cleanup:w.extraction.Cleanup})
		if e != nil { w.lastError = refusal(6,"local receipt collection not acknowledged"); w.blockedRun = p.sequence; return false }
		w.record.Collected = true
		p.generationBlocked = 0
	}
	if w.publishIndex < len(w.findings) {
		if e := p.publish(ctx,w.findings[w.publishIndex]); e != nil { w.lastError = e; w.blockedRun = p.sequence; return false }
		w.publishIndex++; w.lastError = nil; return true
	}
	w.lastError = nil; p.remove(CollectPublish,i)
	return true
}

func (p *Pipeline) publish(ctx context.Context,f Finding) error {
	if e := validateCapability(p.cfg.Capability); e != nil { return e }
	if p.ports.Publisher == nil { return refusal(4,"pinned consumer publisher unavailable") }
	pub := Publication{ID:f.ID,Surface:CanonicalSurface,Provenance:HostMuse,Finding:cloneValue(f),Capability:*p.cfg.Capability}
	reservation,e := p.ports.Sink.Reserve(ctx,pub)
	if e != nil { return refusal(6,"publication reservation acknowledgement unknown; reconcile only") }
	pub = reservation.Publication
	if pub.ID != f.ID || pub.Finding.ID != f.ID || pub.Surface != CanonicalSurface || pub.Provenance != HostMuse || pub.Capability != *p.cfg.Capability || !reflect.DeepEqual(pub.Finding.Repro,f.Repro) || !reflect.DeepEqual(pub.Finding.Binding,f.Binding) {
		return refusal(5,"publication reservation binding drift")
	}
	if reservation.Status == PublicationAccepted { return validateAck(pub,reservation.Ack) }
	if reservation.Status != PublicationPending { return refusal(6,"unknown publication reservation state") }
	if ctx.Err() != nil { return refusal(6,"publication reserved without send after cancellation; query-only") }
	var ack PublicationAck
	if reservation.Fresh { ack,e = p.ports.Publisher.Publish(ctx,cloneValue(pub)) } else { ack,e = p.ports.Publisher.Reconcile(ctx,cloneValue(pub)) }
	if e != nil { return refusal(6,"external result uncertain; same publication identity is query-only") }
	if e = validateAck(pub,ack); e != nil { return e }
	if e = p.ports.Sink.Confirm(ctx,ack); e != nil { return refusal(6,"external write acknowledged but local confirmation uncertain; reconcile only") }
	return nil
}
func validateAck(p Publication,a PublicationAck) error {
	if a.ID != p.ID || !a.Accepted || a.Surface != CanonicalSurface || a.ConsumerVerifierSHA256 != p.Capability.ConsumerVerifierSHA256 { return refusal(6,"consumer acknowledgement is absent or bound to different publication") }
	return nil
}

func costValue(c Cost) Cost {
	if c.Microunits == nil || !present(c.Unit) { return MissingCost() }
	v := *c.Microunits; return Cost{Microunits:&v,Unit:c.Unit}
}
func updateUsage(w *work,next Cost) {
	w.record.Usage,w.record.UsageConflict = mergeUsageEvidence(w.record.Usage,w.record.UsageConflict,next,false)
}
func (p *Pipeline) markCollected() {
	if p.collected != nil || p.first == nil || !p.doneGenerating { return }
	for _,s := range []Stage{Generate,AdmitExecute,ExtractVerify,Deduplicate} { if len(p.queues[s]) != 0 { return } }
	for _,w := range p.ordered { if w.record.CapacityHeld || (w.record.Submitted && !w.record.Collected) { return } }
	now := p.ports.Clock.Now()
	if now.Before(*p.first) { if p.fatal == nil { p.fatal = refusal(5,"clock moved before first admission") }; return }
	p.collected = &now
}

func (p *Pipeline) snapshot() Account {
	a := Account{WaveID:p.cfg.Wave.wave.WaveID,GeneratedCandidates:p.generated,Duplicates:p.duplicates,Dispositions:cloneValue(p.dispositions),Closed:p.closed,Accounted:true,TotalCost:MissingCost(),QueuePeaks:map[Stage]int{},QueueHeld:map[Stage]int{}}
	for _,s := range stages { a.QueuePeaks[s] = p.peaks[s]; a.QueueHeld[s] = len(p.queues[s]); a.Pending += uint64(len(p.queues[s])) }
	allSucceeded := true
	for _,o := range p.cfg.Wave.wave.Obligations {
		oa := ObligationAccount{Obligation:o,State:MissingEvidence}
		items := p.byObligation[o.ID]
		if p.rejected[o.ID] { oa.State = ExplainedFailure }
		for _,w := range items {
			oa.Attempts = append(oa.Attempts,w.attempt.AttemptID)
			if w.record.State == Unresolved || w.record.CapacityHeld { oa.State = Unresolved; break }
			oa.State = w.record.State
		}
		// The inventory remains exact even when generation never names an item.
		if oa.State == MissingEvidence || oa.State == Unresolved { a.Accounted = false }
		if oa.State == MissingEvidence && len(items) == 0 { a.Pending++ }
		if oa.State != Succeeded { allSucceeded = false }
		a.Obligations = append(a.Obligations,oa)
	}
	known := len(p.ordered)>0; var total uint64; unit := ""
	for _,w := range p.ordered {
		r := cloneValue(w.record); a.Attempts = append(a.Attempts,r)
		if r.Submitted { a.SubmittedAttempts++ }
		if r.ExecutionObserved { a.ExecutedAttempts++ }
		if r.CapacityHeld { a.CapacityHeld++ }
		if r.FailureObserved || r.State == ExplainedFailure || (r.State == MissingEvidence && r.ExitCode != 0) { a.Failures++ }
		if r.Usage.Microunits == nil || !present(r.Usage.Unit) { known = false; continue }
		if unit == "" { unit = r.Usage.Unit }; if unit != r.Usage.Unit || math.MaxUint64-total < *r.Usage.Microunits { known = false; continue }; total += *r.Usage.Microunits
	}
	if known { a.TotalCost = Cost{Microunits:&total,Unit:unit} }
	if p.first != nil { t := *p.first; a.FirstAdmitted = &t }
	if p.collected != nil { t := *p.collected; a.CollectedAndCleaned = &t }
	a.UsefulHarvest.VerifiedDistinctRepros = uint64(len(p.distinct))
	if p.first != nil && p.collected != nil { n := p.collected.Sub(*p.first).Nanoseconds(); a.UsefulHarvest.WallNanoseconds = &n }
	a.Successful = a.Accounted && a.Closed && allSucceeded && p.fatal == nil && a.Pending == 0
	return a
}
func (p *Pipeline) accountError(a Account) error {
	for _,w := range p.ordered { if w.record.CapacityHeld || ExitCode(w.lastError) == 6 { return refusal(6,"unresolved work or publication; reconciliation required") } }
	if p.fatal != nil { return p.fatal }
	for _,s := range stages { for _,w := range p.queues[s] { if w.lastError != nil { return w.lastError } } }
	if p.generationError != nil { return p.generationError }
	if a.Successful { return nil }
	if !a.Closed { return refusal(4,"bounded pipeline is waiting for input or downstream acknowledgement") }
	return refusal(5,fmt.Sprintf("wave closed unsuccessfully; accounted=%t",a.Accounted))
}

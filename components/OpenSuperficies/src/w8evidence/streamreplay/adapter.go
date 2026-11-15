// Package streamreplay contains a host-side, offline replay and evidence adapter.
// It never opens a network connection, invokes a process, or grants authority.
package streamreplay

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"sync"
	"time"

	"opensuperficies.local/srf/src/workerbridge"
)

// Codes follow the frozen worker contract: 2 input, 4 unavailable,
// 5 containment/storage, 6 quarantine. There is no successful code 1.
type Failure struct {
	Code   int
	Reason string
}

func (f *Failure) Error() string            { return f.Reason }
func failure(code int, reason string) error { return &Failure{Code: code, Reason: reason} }
func ExitCode(err error) int {
	if err == nil {
		return 0
	}
	var f *Failure
	if errors.As(err, &f) && f.Code >= 2 && f.Code <= 7 {
		return f.Code
	}
	return 3
}

// Sidecar is locally proposed metadata, NOT a registered speech wire schema.
// Authentication belongs entirely to Verifier, not to untrusted captured text.
type Sidecar struct {
	Sender     string `json:"sender"`
	TaskID     string `json:"task_id"`
	AttemptID  string `json:"attempt_id"`
	LeaseID    string `json:"lease_id"`
	Epoch      uint64 `json:"epoch"`
	Seq        uint64 `json:"seq"`
	BodySHA256 string `json:"body_sha256"`
	BodyLength int    `json:"body_length"`
}

// Grant is returned by an injected authoritative verifier. The digest is bound
// to the exact source bytes before an intent can be durably appended.
type Grant struct {
	Sender, TaskID, AttemptID, LeaseID string
	Epoch                              uint64
	SourceBodySHA256                   string
}
type Stream struct {
	Sender, TaskID, AttemptID, LeaseID string
	Epoch                              uint64
}

func (g Grant) stream() Stream { return Stream{g.Sender, g.TaskID, g.AttemptID, g.LeaseID, g.Epoch} }

type Verifier interface {
	Current(context.Context, Sidecar, []byte) (Grant, error)
	DownstreamDurable(context.Context, Stream, uint64) error
}
type BodyStore interface {
	Get(context.Context, string) ([]byte, error) // returns the entire body, never a truncated prefix
	Delete(context.Context, string) error        // idempotent; only after verified downstream ack
}

// Record is a durable replay ledger entry. Its Body bytes are owned and must
// remain readable after a body-store liability has been discharged.
type Record struct {
	Stream     Stream
	Seq        uint64
	BodySHA256 string
	Body       []byte
	Applied    bool
	Downstream bool
	Deleted    bool
}

// Journal operations are durable on successful return; AppendIntent and
// MarkApplied must be idempotent. Loading includes unresolved intents and
// downstream-confirmed but not-yet-deleted liabilities.
type Journal interface {
	Load(context.Context) ([]Record, error)
	AppendIntent(context.Context, Record) error
	MarkApplied(context.Context, Stream, uint64) error
	MarkDownstream(context.Context, Stream, uint64) error
	MarkDeleted(context.Context, Stream, uint64) error
}

// Apply must be durable and idempotent by (Stream, Seq, Body). The adapter
// writes intent first; a crash after Apply but before MarkApplied retries Apply.
type Sink interface {
	Apply(context.Context, Record) error
	Stop(context.Context) error
}
type Clock interface{ Now() time.Time }

type Limits struct {
	MaxRecordBytes  int
	MaxQueueRecords int
	MaxSenderBytes  int
	DataSlots       int
	ControlSlots    int
	MaxStopDelay    time.Duration
}

func (l Limits) valid() bool {
	return l.MaxRecordBytes > 0 && l.MaxQueueRecords > 0 && l.MaxSenderBytes > 0 &&
		l.DataSlots > 0 && l.ControlSlots > 0 && l.MaxStopDelay > 0
}

type receipt struct {
	rec  Record
	held bool
}
type senderState struct {
	highest   uint64
	records   map[uint64]*receipt
	held      map[uint64]*receipt
	liability int
}
type Ack struct {
	Valid     bool
	Through   uint64
	AppliedAt time.Time
}
type Adapter struct {
	mu       sync.Mutex
	store    BodyStore
	journal  Journal
	verifier Verifier
	sink     Sink
	clock    Clock
	limits   Limits
	states   map[Stream]*senderState
	queued   int
}

// New refuses missing policy, body store, durable journal, clock, sink or
// non-positive bounds. No synthetic fallback is provided.
func New(ctx context.Context, store BodyStore, journal Journal, verifier Verifier, sink Sink, clock Clock, l Limits) (*Adapter, error) {
	if store == nil || journal == nil || verifier == nil || sink == nil || clock == nil {
		return nil, failure(4, "missing required host policy or port")
	}
	if !l.valid() {
		return nil, failure(2, "every capacity and STOP bound must be positive")
	}
	loaded, err := journal.Load(ctx)
	if err != nil {
		return nil, failure(4, "durable replay recovery unavailable: "+err.Error())
	}
	a := &Adapter{store: store, journal: journal, verifier: verifier, sink: sink, clock: clock, limits: l, states: make(map[Stream]*senderState)}
	for _, r := range loaded {
		if r.Stream.Sender == "" || r.Seq == 0 || !digestOK(r.BodySHA256) || digest(r.Body) != r.BodySHA256 || len(r.Body) == 0 || len(r.Body) > l.MaxRecordBytes {
			return nil, failure(6, "corrupt replay journal entry")
		}
		s := a.state(r.Stream)
		if _, found := s.records[r.Seq]; found {
			return nil, failure(6, "duplicate durable sequence")
		}
		rr := r
		rr.Body = bytes.Clone(r.Body)
		s.records[r.Seq] = &receipt{rec: rr}
		if !r.Deleted {
			s.liability += len(r.Body)
		}
		if s.liability > l.MaxSenderBytes {
			return nil, failure(6, "recovered sender liability exceeds bound")
		}
	}
	for _, s := range a.states {
		for next := uint64(1); ; next++ {
			r, ok := s.records[next]
			if !ok || !r.rec.Applied {
				break
			}
			s.highest = next
		}
		for seq, r := range s.records {
			if seq > s.highest+1 && r.rec.Applied {
				return nil, failure(6, "noncontiguous committed journal")
			}
		}
	}
	return a, nil
}
func (a *Adapter) state(id Stream) *senderState {
	s := a.states[id]
	if s == nil {
		s = &senderState{records: make(map[uint64]*receipt), held: make(map[uint64]*receipt)}
		a.states[id] = s
	}
	return s
}
func digest(b []byte) string { h := sha256.Sum256(b); return hex.EncodeToString(h[:]) }
func digestOK(s string) bool {
	if len(s) != 64 {
		return false
	}
	for _, c := range s {
		if !((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')) {
			return false
		}
	}
	return true
}
func copyRecord(r Record) Record { r.Body = bytes.Clone(r.Body); return r }

func validateSpeech(raw []byte, env Sidecar) error {
	// workerbridge.Parse rejects duplicate keys, non-integer JSON and malformed
	// UTF-8. Retain the source-backed field inventory, without inventing wire
	// types for fields that the bound sources do not define here.
	obj, err := workerbridge.Parse(raw)
	if err != nil {
		return failure(2, "ambiguous speech JSON: "+err.Error())
	}
	allowed := map[string]bool{"schema": true, "act": true, "wave_id": true, "task_id": true, "attempt_id": true, "lease_id": true, "seq": true, "repro_sha256": true, "affected": true, "status": true, "body": true}
	for field := range obj {
		if !allowed[field] {
			return failure(2, "unknown speech field: "+field)
		}
	}
	str := func(k string) string { v, _ := obj[k].(string); return v }
	if str("schema") != "os.cu.speech-act/v1" {
		return failure(2, "speech schema")
	}
	switch str("act") {
	case "finding", "repro", "affected", "status":
	default:
		return failure(2, "speech act")
	}
	if str("wave_id") == "" || str("task_id") != env.TaskID || str("attempt_id") != env.AttemptID || str("lease_id") != env.LeaseID {
		return failure(5, "speech binding mismatch")
	}
	n, ok := obj["seq"].(json.Number)
	if !ok {
		return failure(2, "speech seq missing")
	}
	seq, err := strconv.ParseUint(string(n), 10, 64)
	if err != nil || seq != env.Seq {
		return failure(5, "speech seq mismatch")
	}
	if v, ok := obj["repro_sha256"]; ok && v != nil {
		h, ok := v.(string)
		if !ok || !digestOK(h) {
			return failure(2, "speech repro digest")
		}
	}
	return nil
}

// Receive never acknowledges a gap or a failed durability operation.
// A replay with altered full bytes quarantines, even if its digest collides.
func (a *Adapter) Receive(ctx context.Context, env Sidecar) (Ack, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if env.Sender == "" || env.TaskID == "" || env.AttemptID == "" || env.LeaseID == "" || env.Epoch == 0 || env.Seq == 0 || !digestOK(env.BodySHA256) || env.BodyLength <= 0 || env.BodyLength > a.limits.MaxRecordBytes {
		return Ack{}, failure(2, "invalid sidecar or record bound")
	}
	body, err := a.store.Get(ctx, env.BodySHA256)
	if err != nil {
		return Ack{}, failure(4, "full body retrieval unavailable: "+err.Error())
	}
	if len(body) != env.BodyLength || digest(body) != env.BodySHA256 {
		return Ack{}, failure(6, "truncated or mutated body")
	}
	grant, err := a.verifier.Current(ctx, env, body)
	if err != nil {
		return Ack{}, failure(5, "source authentication refused: "+err.Error())
	}
	if grant.Sender != env.Sender || grant.TaskID != env.TaskID || grant.AttemptID != env.AttemptID || grant.LeaseID != env.LeaseID || grant.Epoch != env.Epoch || grant.SourceBodySHA256 != env.BodySHA256 {
		return Ack{}, failure(5, "stale or forged source binding")
	}
	if err := validateSpeech(body, env); err != nil {
		return Ack{}, err
	}
	id := grant.stream()
	s := a.state(id)
	if old := s.records[env.Seq]; old != nil {
		if old.rec.BodySHA256 != env.BodySHA256 || !bytes.Equal(old.rec.Body, body) {
			return Ack{}, failure(6, "changed replay at committed sequence")
		}
		if env.Seq <= s.highest && old.rec.Applied {
			return Ack{true, s.highest, a.clock.Now()}, nil
		}
		// An unresolved durable intent is retried, not skipped.
	} else if old := s.held[env.Seq]; old != nil {
		if old.rec.BodySHA256 != env.BodySHA256 || !bytes.Equal(old.rec.Body, body) {
			return Ack{}, failure(6, "changed replay at held sequence")
		}
		return Ack{}, nil
	} else {
		if s.liability+len(body) > a.limits.MaxSenderBytes {
			return Ack{}, failure(5, "per-sender byte cap")
		}
		rec := Record{Stream: id, Seq: env.Seq, BodySHA256: env.BodySHA256, Body: bytes.Clone(body)}
		if env.Seq > s.highest+1 {
			if a.queued >= a.limits.MaxQueueRecords {
				return Ack{}, failure(5, "gap queue cap")
			}
			s.held[env.Seq] = &receipt{rec: rec, held: true}
			s.liability += len(body)
			a.queued++
			return Ack{}, nil
		}
		s.liability += len(body)
		s.records[env.Seq] = &receipt{rec: rec}
	}
	if env.Seq != s.highest+1 {
		return Ack{}, failure(6, "noncontiguous durable state")
	}
	if err := a.apply(ctx, s, s.records[env.Seq]); err != nil {
		return Ack{}, err
	}
	for {
		next := s.highest + 1
		held := s.held[next]
		if held == nil {
			break
		}
		delete(s.held, next)
		a.queued--
		held.held = false
		s.records[next] = held
		if err := a.apply(ctx, s, held); err != nil {
			return Ack{}, err
		}
	}
	return Ack{true, s.highest, a.clock.Now()}, nil
}
func (a *Adapter) apply(ctx context.Context, s *senderState, r *receipt) error {
	rec := &r.rec
	if !r.rec.Applied {
		if !r.held {
			if err := a.journal.AppendIntent(ctx, copyRecord(*rec)); err != nil {
				return failure(5, "durable intent failed: "+err.Error())
			}
		}
		if err := a.sink.Apply(ctx, copyRecord(*rec)); err != nil {
			return failure(5, "durable sink failed: "+err.Error())
		}
		if err := a.journal.MarkApplied(ctx, rec.Stream, rec.Seq); err != nil {
			return failure(5, "durable application mark failed: "+err.Error())
		}
		rec.Applied = true
	}
	s.highest = rec.Seq
	return nil
}

// Release requires external verification of a DURABLE downstream acknowledgement.
// It retains liabilities until the body deletion and its journal mark succeed.
func (a *Adapter) Release(ctx context.Context, id Stream, through uint64) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	s := a.states[id]
	if s == nil || through == 0 || through > s.highest {
		return failure(2, "unknown or noncontiguous downstream acknowledgement")
	}
	if err := a.verifier.DownstreamDurable(ctx, id, through); err != nil {
		return failure(5, "downstream ack not durable: "+err.Error())
	}
	for seq := uint64(1); seq <= through; seq++ {
		r := s.records[seq]
		if r == nil || !r.rec.Applied {
			return failure(6, "missing applied record")
		}
		if r.rec.Deleted {
			continue
		}
		if !r.rec.Downstream {
			if err := a.journal.MarkDownstream(ctx, id, seq); err != nil {
				return failure(5, "downstream mark failed: "+err.Error())
			}
			r.rec.Downstream = true
		}
		if err := a.store.Delete(ctx, r.rec.BodySHA256); err != nil {
			return failure(5, "body deletion failed: "+err.Error())
		}
		if err := a.journal.MarkDeleted(ctx, id, seq); err != nil {
			return failure(5, "delete mark failed: "+err.Error())
		}
		r.rec.Deleted = true
		s.liability -= len(r.rec.Body)
	}
	return nil
}

// Queue is an independent bounded control/data bus implementation for offline
// tests. No caller connects it to an actual bus or extension registry.
type Message struct {
	Data   *Sidecar
	Stop   bool
	SentAt time.Time
}
type Bus interface {
	Next(context.Context) (Message, error)
}
type Queue struct {
	data    chan Sidecar
	control chan Message
}

func NewQueue(l Limits) (*Queue, error) {
	if !l.valid() {
		return nil, failure(2, "invalid queue bounds")
	}
	return &Queue{data: make(chan Sidecar, l.DataSlots), control: make(chan Message, l.ControlSlots)}, nil
}
func (q *Queue) Data(v Sidecar) bool {
	select {
	case q.data <- v:
		return true
	default:
		return false
	}
}
func (q *Queue) Stop(sent time.Time) bool {
	select {
	case q.control <- Message{Stop: true, SentAt: sent}:
		return true
	default:
		return false
	}
}
func (q *Queue) Next(ctx context.Context) (Message, error) {
	// The first nonblocking poll guarantees STOP wins when already queued,
	// independent of the state of the data lane.
	select {
	case msg := <-q.control:
		return msg, nil
	default:
	}
	select {
	case msg := <-q.control:
		return msg, nil
	case d := <-q.data:
		return Message{Data: &d}, nil
	case <-ctx.Done():
		return Message{}, ctx.Err()
	}
}

// Step consumes a single host bus item. An independent STOP never needs free
// data slots, and is checked against the injected monotonic test clock.
func (a *Adapter) Step(ctx context.Context, bus Bus) (Ack, error) {
	if bus == nil {
		return Ack{}, failure(4, "bus unavailable")
	}
	msg, err := bus.Next(ctx)
	if err != nil {
		return Ack{}, err
	}
	if msg.Stop {
		if err := a.sink.Stop(ctx); err != nil {
			return Ack{}, failure(5, "stop failed: "+err.Error())
		}
		elapsed := a.clock.Now().Sub(msg.SentAt)
		if elapsed < 0 || elapsed > a.limits.MaxStopDelay {
			return Ack{}, failure(5, fmt.Sprintf("STOP delay exceeded: %v", elapsed))
		}
		return Ack{}, nil
	}
	if msg.Data == nil {
		return Ack{}, failure(2, "empty data message")
	}
	return a.Receive(ctx, *msg.Data)
}

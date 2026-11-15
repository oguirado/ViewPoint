// Package proto1harvest composes the frozen Muse harvest scheduler and the
// existing local w8-repro/1 aggregator. It does not launch guests or validate
// host receipts itself: those decisions belong to harvest and its host ports.
package proto1harvest

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math"
	"sync"

	"opensuperficies.local/srf/src/w8wave/aggregator"
	"opensuperficies.local/srf/src/w8wave/harvest"
)

// Config supplies independently owned host capabilities. In particular,
// Ports.Log and Ports.History must refer to the same existing consumption WAL;
// Ports.Sink must durably implement the real harvest sink contract. The
// aggregator must have been constructed with explicit authorized bindings.
// MaxObservations bounds receipt projections; it is not an attempt retry limit.
type Config struct {
	Harvest         harvest.Config
	Ports           harvest.Ports
	Aggregator      *aggregator.Aggregator
	HashFn          aggregator.HashFunc
	BaseSeq         int64
	MaxObservations int
}

// AttemptResult (A-result) is a projection of authenticated harvest accounting.
// NativeGateRawExit remains nullable and distinct from NormalizedExit.
type AttemptResult struct {
	AttemptKey        string
	TaskID            string
	AttemptID         string
	LeaseID           string
	State             harvest.ObligationState
	NormalizedExit    int
	NativeGateRawExit *int64
	ReceiptSHA256     string
	Usage             harvest.Cost
	Collected         bool
	CapacityHeld      bool
}

// ACompletion is the final A-completion decision. Accounted means every
// obligation has a terminal explanation, not that the application succeeded.
type ACompletion struct {
	WaveID            string
	Accounted         bool
	Closed            bool
	DoneSuccess       bool
	ExitCode          int
	Pending           uint64
	CapacityHeld      uint64
	SubmittedAttempts uint64
}

// Result retains both the real pipeline account and aggregator summary. No
// missing measurement, gate result or cost is coerced to numerical zero.
type Result struct {
	Harvest    harvest.Account
	Aggregate  aggregator.WaveSummary
	Attempts   []AttemptResult
	Completion ACompletion
}

type Flow struct {
	mu              sync.Mutex
	pipeline        *harvest.Pipeline
	aggregator      *aggregator.Aggregator
	sink            *joiningSink
	waveID          string
	baseSeq         int64
	maxObservations int
}

// New checks the composition boundary without replacing any of the producer's
// host provenance checks, publication reservations, or WAL state machines.
func New(cfg Config) (*Flow, error) {
	if cfg.Aggregator == nil {
		return nil, &harvest.Error{Code: 4, Detail: "AUSENTE: authorized local aggregator"}
	}
	if cfg.BaseSeq < 0 || cfg.MaxObservations < 1 {
		return nil, &harvest.Error{Code: 2, Detail: "invalid or unbounded aggregator projection budget"}
	}
	if cfg.Ports.Sink == nil {
		return nil, &harvest.Error{Code: 2, Detail: "required harvest sink absent"}
	}
	waveID := cfg.Harvest.Wave.Wave().WaveID
	s := &joiningSink{Sink: cfg.Ports.Sink, agg: cfg.Aggregator, hash: cfg.HashFn, waveID: waveID, baseSeq: cfg.BaseSeq, max: cfg.MaxObservations, counts: make(map[string]int)}
	cfg.Ports.Sink = s
	// Force the independent, real C3 canonicalizer. The aggregator rechecks the
	// same identity on Ingest; no new digest framing or repro schema is defined.
	cfg.Ports.Identity = aggregatorIdentity{hash: cfg.HashFn}
	pipeline, err := harvest.NewPipeline(cfg.Harvest, cfg.Ports)
	if err != nil {
		return nil, err
	}
	return &Flow{pipeline: pipeline, aggregator: cfg.Aggregator, sink: s, waveID: waveID, baseSeq: cfg.BaseSeq, maxObservations: cfg.MaxObservations}, nil
}

type aggregatorIdentity struct{ hash aggregator.HashFunc }

func (i aggregatorIdentity) Canonical(ctx context.Context, in harvest.ReproInput) (harvest.CanonicalRepro, error) {
	if err := ctx.Err(); err != nil {
		return harvest.CanonicalRepro{}, err
	}
	id, err := aggregator.ParseAndComputeIdentity(in.Record, i.hash)
	if err != nil {
		return harvest.CanonicalRepro{}, projectionError(aggregator.ExitCode(err), "C3 canonical identity refused: "+err.Error())
	}
	return harvest.CanonicalRepro{Normalizer: aggregator.ReproVersion, Key: id.ReproSHA256, Canonical: append([]byte(nil), id.CanonicalRecord...)}, nil
}

// joiningSink delegates all durable operations to the host's original sink.
// The real aggregator independently checks the canonical record and the
// admitted witness BEFORE the pipeline may reserve external publication.
type joiningSink struct {
	harvest.Sink
	agg          *aggregator.Aggregator
	hash         aggregator.HashFunc
	waveID       string
	baseSeq      int64
	max          int
	counts       map[string]int
	observations int
}

func attemptStream(task, attempt, lease string) string {
	return "proto1/repro/" + task + "/" + attempt + "/" + lease
}
func receiptStream(key string) string { return "proto1/receipt/" + key }
func projectionError(code int, detail string) error {
	return &harvest.Error{Code: code, Detail: detail}
}

func (s *joiningSink) Merge(ctx context.Context, obs harvest.Observation) (harvest.Finding, error) {
	if err := ctx.Err(); err != nil {
		return harvest.Finding{}, err
	}
	if s.observations >= s.max {
		return harvest.Finding{}, projectionError(6, "aggregator observation budget exhausted")
	}
	key := attemptStream(obs.Witness.TaskID, obs.Witness.AttemptID, obs.Witness.LeaseID)
	count := s.counts[key]
	if int64(count) > math.MaxInt64-s.baseSeq {
		return harvest.Finding{}, projectionError(2, "aggregator sequence overflow")
	}
	f, err := s.Sink.Merge(ctx, obs)
	if err != nil {
		return harvest.Finding{}, err
	}
	observation := aggregator.Observation{
		WaveID: s.waveID, StreamKey: key, Seq: s.baseSeq + int64(count),
		ClaimedReproSHA256: obs.Repro.Key, IdentityRaw: append([]byte(nil), obs.Repro.Canonical...),
		Provenance: aggregator.Provenance{TaskID: obs.Witness.TaskID, AttemptID: obs.Witness.AttemptID, LeaseID: obs.Witness.LeaseID, WorkerID: obs.Witness.Worker, Sender: harvest.HostMuse},
	}
	if err := s.agg.Ingest(observation); err != nil {
		return harvest.Finding{}, projectionError(aggregator.ExitCode(err), "aggregator refused frozen repro or witness: "+err.Error())
	}
	s.counts[key] = count + 1
	s.observations++
	return f, nil
}

// Run is safe under concurrent callers. The harvest pipeline retains the
// original intention and reconciles a missing seat handle/ACK by stable key;
// this composition never invokes Submitter itself or replays a publication.
func (f *Flow) Run(ctx context.Context) (Result, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	account, harvestErr := f.pipeline.Run(ctx)
	result := Result{Harvest: account, Aggregate: f.aggregator.Summary()}
	var projectionErr error
	projected := 0
	for _, attempt := range account.Attempts {
		item := AttemptResult{
			AttemptKey: attempt.Key, TaskID: attempt.TaskID, AttemptID: attempt.AttemptID, LeaseID: attempt.LeaseID,
			State: attempt.State, NormalizedExit: attempt.ExitCode, Usage: attempt.Usage,
			Collected: attempt.Collected, CapacityHeld: attempt.CapacityHeld,
		}
		if len(attempt.Receipt) != 0 {
			h := sha256.Sum256(attempt.Receipt)
			item.ReceiptSHA256 = hex.EncodeToString(h[:])
			if attempt.Collected && !attempt.CapacityHeld && (attempt.State == harvest.Succeeded || attempt.State == harvest.ExplainedFailure) {
				item.NativeGateRawExit = rawGateCode(attempt.Receipt)
			}
		}
		result.Attempts = append(result.Attempts, item)
		if !attempt.Collected || attempt.CapacityHeld || (attempt.State != harvest.Succeeded && attempt.State != harvest.ExplainedFailure) || len(attempt.Receipt) == 0 {
			continue
		}
		if projectionErr != nil {
			continue
		}
		if projected >= f.maxObservations {
			projectionErr = projectionError(6, "aggregator receipt projection budget exhausted")
			continue
		}
		var cost *int64
		reason := aggregator.ReasonAusente
		if attempt.Usage.Microunits != nil && attempt.Usage.Unit == "microUSD" && *attempt.Usage.Microunits <= math.MaxInt64 {
			n := int64(*attempt.Usage.Microunits)
			cost = &n
			reason = ""
		}
		ref := aggregator.AttemptReceiptRef{
			TaskID: attempt.TaskID, AttemptID: attempt.AttemptID, LeaseID: attempt.LeaseID,
			ReceiptSHA256: item.ReceiptSHA256, ExitCode: attempt.ExitCode, CostMicroUSD: cost, CostReason: reason,
		}
		observation := aggregator.Observation{
			WaveID: f.waveID, StreamKey: receiptStream(attempt.Key), Seq: f.baseSeq,
			Provenance:     aggregator.Provenance{TaskID: attempt.TaskID, AttemptID: attempt.AttemptID, LeaseID: attempt.LeaseID, WorkerID: harvest.HostMuse, Sender: harvest.HostMuse, CostMicroUSD: cost},
			AttemptReceipt: &ref,
		}
		if err := f.aggregator.Ingest(observation); err != nil {
			projectionErr = projectionError(aggregator.ExitCode(err), fmt.Sprintf("aggregator refused attempt receipt %s: %v", attempt.Key, err))
			continue
		}
		projected++
	}
	result.Aggregate = f.aggregator.Summary()
	if harvestErr == nil && projectionErr != nil {
		harvestErr = projectionErr
	}
	code := harvest.ExitCode(harvestErr)
	result.Completion = ACompletion{WaveID: f.waveID, Accounted: account.Accounted, Closed: account.Closed, DoneSuccess: account.Successful && projectionErr == nil && harvestErr == nil, ExitCode: code, Pending: account.Pending, CapacityHeld: account.CapacityHeld, SubmittedAttempts: account.SubmittedAttempts}
	return result, harvestErr
}

func rawGateCode(raw []byte) *int64 {
	var doc struct {
		Gate *struct {
			RawExitCode *json.Number `json:"raw_exit_code"`
		} `json:"gate"`
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	if dec.Decode(&doc) != nil || doc.Gate == nil || doc.Gate.RawExitCode == nil {
		return nil
	}
	n, e := (*doc.Gate.RawExitCode).Int64()
	if e != nil {
		return nil
	}
	return &n
}

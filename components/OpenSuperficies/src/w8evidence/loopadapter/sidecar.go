package loopadapter

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"sync"
)

// Speech is a local, strictly enumerated projection of the frozen payload.
// Schema validation and signature authority remain external to this adapter.
type Speech struct {
	Schema      string          `json:"schema"`
	Act         string          `json:"act"`
	WaveID      string          `json:"wave_id"`
	TaskID      string          `json:"task_id"`
	AttemptID   string          `json:"attempt_id"`
	LeaseID     string          `json:"lease_id"`
	Seq         uint64          `json:"seq"`
	ReproSHA256 string          `json:"repro_sha256"`
	Affected    []string        `json:"affected"`
	Status      string          `json:"status"`
	Body        json.RawMessage `json:"body"`
}
type SidecarHeader struct {
	Sender, TaskID, AttemptID, LeaseID string
	Epoch, Seq                         uint64
	BodySHA256                         string
	BodyLength                         uint64
}
type Frame struct {
	Header SidecarHeader
	Body   []byte
}
type SidecarVerifier interface {
	Verify(SidecarHeader, []byte) (bool, error)
}
type SidecarJournal interface {
	// Apply must be durable before it returns success; this is the ack boundary.
	Apply(Frame) error
	Stop(Frame) error
}
type Received struct {
	Status string
	Ack    uint64
}
type Receiver struct {
	mu         sync.Mutex
	stopMu     sync.Mutex
	verifier   SidecarVerifier
	journal    SidecarJournal
	maxPending int
	maxStop    int
	stopping   int
	last       uint64
	applied    map[uint64]Frame
	pending    map[uint64]Frame
	quarantine bool
}

func NewReceiver(verifier SidecarVerifier, journal SidecarJournal, maxPending, maxStop int) (*Receiver, error) {
	if verifier == nil || journal == nil || maxPending <= 0 || maxStop <= 0 {
		return nil, ErrRefused
	}
	return &Receiver{verifier: verifier, journal: journal, maxPending: maxPending, maxStop: maxStop, applied: map[uint64]Frame{}, pending: map[uint64]Frame{}}, nil
}
func parseSpeech(b []byte) (Speech, error) {
	var s Speech
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&s); err != nil {
		return s, ErrRefused
	}
	var excess any
	if err := dec.Decode(&excess); !errors.Is(err, io.EOF) {
		return s, ErrRefused
	}
	switch s.Act {
	case "finding", "repro", "affected", "status":
	default:
		return s, ErrRefused
	}
	if s.Schema != "os.cu.speech-act/v1" || s.WaveID == "" || s.TaskID == "" || s.AttemptID == "" || s.LeaseID == "" || s.Seq == 0 || len(s.Body) == 0 || !json.Valid(s.Body) {
		return s, ErrRefused
	}
	if s.ReproSHA256 != "" && !validDigest(s.ReproSHA256) {
		return s, ErrRefused
	}
	return s, nil
}
func sameFrame(a, b Frame) bool { return a.Header == b.Header && bytes.Equal(a.Body, b.Body) }
func cloneFrame(f Frame) Frame  { f.Body = append([]byte(nil), f.Body...); return f }
func (r *Receiver) verify(f Frame) error {
	h := f.Header
	if h.Sender == "" || h.TaskID == "" || h.AttemptID == "" || h.LeaseID == "" || h.Epoch == 0 || h.Seq == 0 || h.BodyLength != uint64(len(f.Body)) || !validDigest(h.BodySHA256) || h.BodySHA256 != Digest(f.Body) {
		return ErrRefused
	}
	s, err := parseSpeech(f.Body)
	if err != nil || h.TaskID != s.TaskID || h.AttemptID != s.AttemptID || h.LeaseID != s.LeaseID || h.Seq != s.Seq {
		return ErrRefused
	}
	yes, err := r.verifier.Verify(h, f.Body)
	if err != nil || !yes {
		return ErrHold
	}
	return nil
}

// Submit records gaps without acknowledging them. Contiguous durable application
// proceeds in order, stopping at the first failed append.
func (r *Receiver) Submit(f Frame) (Received, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.quarantine {
		return Received{Status: "quarantined", Ack: r.last}, ErrQuarantine
	}
	if err := r.verify(f); err != nil {
		return Received{Status: "refused", Ack: r.last}, err
	}
	if old, ok := r.applied[f.Header.Seq]; ok {
		if sameFrame(old, f) {
			return Received{Status: "replayed", Ack: r.last}, nil
		}
		r.quarantine = true
		return Received{Status: "quarantined", Ack: r.last}, ErrQuarantine
	}
	if old, ok := r.pending[f.Header.Seq]; ok {
		if !sameFrame(old, f) {
			r.quarantine = true
			return Received{Status: "quarantined", Ack: r.last}, ErrQuarantine
		}
		if f.Header.Seq != r.last+1 {
			return Received{Status: "held", Ack: r.last}, ErrHold
		}
	} else {
		if f.Header.Seq <= r.last {
			r.quarantine = true
			return Received{Status: "quarantined", Ack: r.last}, ErrQuarantine
		}
		if len(r.pending) >= r.maxPending && f.Header.Seq != r.last+1 {
			return Received{Status: "held", Ack: r.last}, ErrHold
		}
		r.pending[f.Header.Seq] = cloneFrame(f)
	}
	for {
		next, ok := r.pending[r.last+1]
		if !ok {
			break
		}
		if err := r.journal.Apply(cloneFrame(next)); err != nil {
			return Received{Status: "held", Ack: r.last}, fmt.Errorf("%w: append not durable: %v", ErrHold, err)
		}
		r.last++
		r.applied[r.last] = next
		delete(r.pending, r.last)
	}
	if _, waiting := r.pending[f.Header.Seq]; waiting {
		return Received{Status: "held", Ack: r.last}, ErrHold
	}
	return Received{Status: "ack", Ack: r.last}, nil
}

// SubmitStop has its own bounded capacity, independent of the data gap buffer.
// Its durable path is distinct; a stop is not a new speech-act kind.
func (r *Receiver) SubmitStop(f Frame) error {
	r.stopMu.Lock()
	defer r.stopMu.Unlock()
	if r.stopping >= r.maxStop {
		return ErrHold
	}
	r.stopping++
	defer func() { r.stopping-- }()
	if err := r.verify(f); err != nil {
		return err
	}
	if err := r.journal.Stop(cloneFrame(f)); err != nil {
		return fmt.Errorf("%w: stop not durable: %v", ErrHold, err)
	}
	return nil
}
func (r *Receiver) Quarantined() bool { r.mu.Lock(); defer r.mu.Unlock(); return r.quarantine }

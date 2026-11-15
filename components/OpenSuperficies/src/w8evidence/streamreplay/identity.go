package streamreplay

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"path"
	"strings"
	"sync"
	"time"

	"opensuperficies.local/srf/src/workerbridge"
)

// Identity is semantic only; task/attempt/lease, sender, epoch, paid cost and
// timestamps intentionally belong in Witness, never in the repro hash.
type Identity struct {
	NormalizationVersion string   `json:"normalization_version"`
	Repo                 string   `json:"repo"`
	BaseCommit           string   `json:"base_commit"`
	Image                string   `json:"image"`
	Toolchain            string   `json:"toolchain"`
	CacheManifest        string   `json:"cache_manifest"`
	GateArgv             []string `json:"gate_argv"`
	RelativeCWD          string   `json:"relative_cwd"`
	Outcome              string   `json:"outcome"`
	Signature            string   `json:"signature"`
	SelectedFrames       []string `json:"selected_frames"`
	AffectedPath         string   `json:"affected_path"`
	AffectedSymbol       string   `json:"affected_symbol"`
	InputIdentity        string   `json:"input_identity"`
}

func validRelative(p string) bool {
	return p != "" && !strings.HasPrefix(p, "/") && p != ".." && !strings.HasPrefix(p, "../") && path.Clean(p) == p && !strings.Contains(p, "\\")
}
func CanonicalIdentity(i Identity) ([]byte, error) {
	if i.NormalizationVersion == "" || i.Repo == "" || i.BaseCommit == "" || i.Image == "" || i.Toolchain == "" || i.CacheManifest == "" || len(i.GateArgv) == 0 || !validRelative(i.RelativeCWD) || i.Outcome == "" || i.InputIdentity == "" {
		return nil, failure(2, "identity required fields or relative cwd")
	}
	for _, s := range i.GateArgv {
		if s == "" {
			return nil, failure(2, "empty gate argv")
		}
	}
	if i.SelectedFrames == nil {
		i.SelectedFrames = []string{}
	}
	return workerbridge.Canonical(i)
}

// ReproDigest hashes domain bytes followed by the canonical byte count as an
// unsigned big-endian 8-byte length, followed by canonical integer JSON.
func ReproDigest(i Identity) (string, []byte, error) {
	c, err := CanonicalIdentity(i)
	if err != nil {
		return "", nil, err
	}
	return reproDigestBytes(c), c, nil
}

func reproDigestBytes(c []byte) string {
	var n [8]byte
	binary.BigEndian.PutUint64(n[:], uint64(len(c)))
	h := sha256.New()
	_, _ = h.Write([]byte("w8-repro-v1"))
	_, _ = h.Write(n[:])
	_, _ = h.Write(c)
	return hex.EncodeToString(h.Sum(nil))
}

type Witness struct {
	TaskID, AttemptID, LeaseID, Sender string
	Epoch                              uint64
	PaidUnits                          int64
	At                                 time.Time
}

func (w Witness) valid() bool {
	return w.TaskID != "" && w.AttemptID != "" && w.LeaseID != "" && w.Sender != "" && w.Epoch > 0 && w.PaidUnits >= 0 && !w.At.IsZero()
}
func sameWitness(a, b Witness) bool {
	return a.TaskID == b.TaskID && a.AttemptID == b.AttemptID && a.LeaseID == b.LeaseID && a.Sender == b.Sender && a.Epoch == b.Epoch
}

type EvidenceEntry struct {
	Digest    string
	Canonical []byte
	Witnesses []Witness
}
type EvidenceJournal interface {
	LoadEvidence(context.Context) ([]EvidenceEntry, error)
	AppendWitness(context.Context, string, []byte, Witness) error
}
type Hasher func([]byte) string

// Ledger stores digest buckets, not just digests. Hash collisions are isolated
// by comparing the COMPLETE canonical bytes. Journal is durable and append-only.
type Ledger struct {
	mu      sync.Mutex
	journal EvidenceJournal
	hash    Hasher
	buckets map[string][]*EvidenceEntry
}

func NewLedger(ctx context.Context, j EvidenceJournal, h Hasher) (*Ledger, error) {
	if j == nil {
		return nil, failure(4, "evidence journal unavailable")
	}
	if h == nil {
		h = func(c []byte) string { return reproDigestBytes(c) }
	}
	loaded, err := j.LoadEvidence(ctx)
	if err != nil {
		return nil, failure(4, "evidence recovery unavailable")
	}
	l := &Ledger{journal: j, hash: h, buckets: make(map[string][]*EvidenceEntry)}
	for _, e := range loaded {
		if e.Digest == "" || len(e.Canonical) == 0 || h(e.Canonical) != e.Digest {
			return nil, failure(6, "bad durable evidence entry")
		}
		for _, existing := range l.buckets[e.Digest] {
			if bytes.Equal(existing.Canonical, e.Canonical) {
				return nil, failure(6, "duplicate durable evidence identity")
			}
		}
		copyE := EvidenceEntry{Digest: e.Digest, Canonical: bytes.Clone(e.Canonical), Witnesses: append([]Witness(nil), e.Witnesses...)}
		for x, w := range copyE.Witnesses {
			if !w.valid() {
				return nil, failure(6, "bad durable witness")
			}
			for y := 0; y < x; y++ {
				if sameWitness(w, copyE.Witnesses[y]) {
					return nil, failure(6, "duplicate durable witness")
				}
			}
		}
		l.buckets[e.Digest] = append(l.buckets[e.Digest], &copyE)
	}
	return l, nil
}
func (l *Ledger) Add(ctx context.Context, i Identity, w Witness) (EvidenceEntry, error) {
	if !w.valid() {
		return EvidenceEntry{}, failure(2, "invalid provenance witness")
	}
	_, canonical, err := ReproDigest(i)
	if err != nil {
		return EvidenceEntry{}, err
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	hash := l.hash(canonical)
	if hash == "" {
		return EvidenceEntry{}, errors.New("empty identity digest")
	}
	for _, e := range l.buckets[hash] {
		if bytes.Equal(e.Canonical, canonical) {
			for _, old := range e.Witnesses {
				if sameWitness(old, w) {
					if old != w {
						return EvidenceEntry{}, failure(6, "changed paid witness replay")
					}
					return copyEvidence(*e), nil
				}
			}
			if err := l.journal.AppendWitness(ctx, hash, bytes.Clone(canonical), w); err != nil {
				return EvidenceEntry{}, failure(5, "evidence durability failed: "+err.Error())
			}
			e.Witnesses = append(e.Witnesses, w)
			return copyEvidence(*e), nil
		}
	}
	if err := l.journal.AppendWitness(ctx, hash, bytes.Clone(canonical), w); err != nil {
		return EvidenceEntry{}, failure(5, "evidence durability failed: "+err.Error())
	}
	e := &EvidenceEntry{Digest: hash, Canonical: bytes.Clone(canonical), Witnesses: []Witness{w}}
	l.buckets[hash] = append(l.buckets[hash], e)
	return copyEvidence(*e), nil
}
func copyEvidence(e EvidenceEntry) EvidenceEntry {
	e.Canonical = bytes.Clone(e.Canonical)
	e.Witnesses = append([]Witness(nil), e.Witnesses...)
	return e
}

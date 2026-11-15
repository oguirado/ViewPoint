// Package sbbridge admits closed W8 speech acts through the existing Switchboard
// adapter, then coordinates strictly verified, contiguous completion receipts.
// It is a host-only boundary: EpochAuthority and CompletionSink are trusted ports.
package sbbridge

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"regexp"
	"strings"
	"sync"
	"unicode/utf8"

	"opensuperficies.local/srf/src/w8wave/switchboard"
)

// EpochSnapshot is authenticated HOST evidence, not data decoded from a worker
// speech act or a native preview. The complete grant is bound to both epochs.
type EpochSnapshot struct {
	Grant     switchboard.Grant
	BusEpoch  uint64
	RoomEpoch uint64 // zero only for a mailbox, never for a live topic
}

// EpochAuthority must consult current host-controlled grant/epoch state. A
// caller-controlled struct, a native ACK, or a cached room stamp is not one.
type EpochAuthority interface {
	Current(targetID, topic string) (EpochSnapshot, error)
}

// Submission holds comparison-only control evidence outside the closed act.
// PayloadSHA256 is optional corroboration and never substitutes for full bytes.
type Submission struct {
	Endpoint      string
	Extension     int
	LeaseEpoch    uint64
	BusEpoch      uint64
	RoomEpoch     uint64
	PayloadSHA256 string
	Topic         string
	Raw           []byte
}

// Completion is strictly a contiguous, body-authenticated, host-attested
// status/repro sequence. It does not assert that a remote task was accepted.
type Completion struct {
	Grant          switchboard.Grant
	LastContiguous int
	ReproSHA256    string
	AttestationRef string
	Status         string
}

// CompletionSink atomically and idempotently records a batch by grant and final
// sequence. Errors are uncertain; they must not be interpreted as acceptance.
type CompletionSink interface {
	CommitCompletions([]Completion) error
}

type outgoingStream struct {
	last        int
	seen        map[int][]byte
	quarantined bool
}

type incomingStream struct {
	next          int
	seen          map[int][]byte
	held          map[int][]byte
	quarantined   bool
	statusDone    bool
	verifiedRepro bool
	reproDigest   string
	attestation   string
	completed     bool
}

type roomBinding struct {
	room  *switchboard.Room
	epoch uint64
}

// Coverage reports *coordinator* admission, not native publish ACK counters.
type Coverage struct {
	LastContiguous int
	Held           int
	StatusDone     bool
	VerifiedRepro  bool
	Completed      bool
	Quarantined    bool
}

// Coordinator serializes admission, holds, cursor movement and completion.
// The host must route all writes through Publish, and room registration through
// AttachRoom; direct Adapter.Send bypasses this higher-level epoch fence.
type Coordinator struct {
	Adapter      *switchboard.Adapter
	Epochs       EpochAuthority
	SourceCommit string
	OnComplete   CompletionSink

	mu       sync.Mutex
	cursor   uint64
	outbound map[string]*outgoingStream
	incoming map[string]*incomingStream
	rooms    map[string]roomBinding
	stopped  map[string]uint64
}

func refusal(code int, reason string) error {
	return &switchboard.Refusal{Code: code, Reason: reason}
}

func validCommit(s string) bool {
	if len(s) != 40 {
		return false
	}
	_, err := hex.DecodeString(s)
	return err == nil && strings.ToLower(s) == s
}

func positive(p switchboard.IngressPolicy) bool {
	return p.MaxRecordBytes > 0 && p.MaxQueueBytes > 0 &&
		p.PerSenderMsgsPerPulse > 0 && p.PerSenderBytesPerPulse > 0 &&
		p.RoomMsgsPerPulse > 0 && p.RoomBytesPerPulse > 0 &&
		p.MaxMembers > 0 && p.HeartbeatExpiry > 0 && p.StagnationWindow > 0
}

func (c *Coordinator) ready() error {
	if c == nil || c.Adapter == nil || c.Epochs == nil ||
		c.Adapter.Auth == nil || c.Adapter.Grants == nil ||
		c.Adapter.Clock == nil || c.Adapter.Store == nil ||
		c.Adapter.Transport == nil || c.Adapter.Repo == "" ||
		c.Adapter.BusEpoch == 0 || !positive(c.Adapter.Policy) ||
		!validCommit(c.SourceCommit) {
		return refusal(4, "trusted coordinator capability or finite policy absent")
	}
	return nil
}

func (c *Coordinator) init() {
	if c.outbound == nil {
		c.outbound = make(map[string]*outgoingStream)
	}
	if c.incoming == nil {
		c.incoming = make(map[string]*incomingStream)
	}
	if c.rooms == nil {
		c.rooms = make(map[string]roomBinding)
	}
	if c.stopped == nil {
		c.stopped = make(map[string]uint64)
	}
}

func streamKey(g switchboard.Grant) string {
	return strings.Join([]string{g.WaveID, g.TaskID, g.AttemptID, g.LeaseID,
		g.TargetID, g.WorkerID, fmt.Sprint(g.Epoch)}, "\x00")
}

func digest(raw []byte) string {
	hash := sha256.Sum256(raw)
	return hex.EncodeToString(hash[:])
}

var encodedRun = regexp.MustCompile(`(?i)(?:[0-9a-f]{128,}|[a-z0-9+/]{192,}={0,2})`)
var mediaTerm = regexp.MustCompile(`(?i)\b(?:frames?|pixels?|rgba?|bitmap|framebuffer|state)\b`)

// This is deliberately conservative. A producer-side pixel classifier is not
// defined by the bound source; opaque frame/STATE material is never admissible.
func unsafeMedia(body string) bool {
	lower := strings.ToLower(body)
	for _, tag := range []string{
		"data:image/", "image/png", "image/jpeg", "image/webp", "image/svg",
		"base64,", ";base64", "framebuffer", "frame_bytes", "pixel",
		"rgba", "rgb24", "bitmap", "<svg", "\"state\"", "state:",
		"frame:", "frames:", "png:", "jpeg:",
	} {
		if strings.Contains(lower, tag) {
			return true
		}
	}
	return encodedRun.MatchString(body) || mediaTerm.MatchString(body)
}

// live requires an authenticated fresh grant/epoch snapshot on *every* path,
// including reads and identical replays. Neither an act nor a stamped sidecar
// is permitted to supply the purportedly current epoch.
func (c *Coordinator) live(g switchboard.Grant, topic string, bus, room uint64) error {
	if g.Epoch == 0 || !validCommit(g.SourceCommit) ||
		g.SourceCommit != c.SourceCommit || g.ExpiresMS <= c.Adapter.Clock.NowMS() {
		return refusal(5, "expired or stale source grant")
	}
	s, err := c.Epochs.Current(g.TargetID, topic)
	if err != nil {
		return refusal(4, "authenticated live epoch evidence unavailable")
	}
	if s.BusEpoch == 0 || (topic != "" && s.RoomEpoch == 0) {
		return refusal(4, "positive live epoch evidence missing")
	}
	if s.Grant != g || s.BusEpoch != bus || s.BusEpoch != c.Adapter.BusEpoch {
		return refusal(5, "stale bus epoch or grant capability")
	}
	if topic == "" {
		if room != 0 || s.RoomEpoch != 0 {
			return refusal(5, "mailbox has no room epoch")
		}
		return nil
	}
	r, ok := c.rooms[topic]
	if !ok || r.room == nil || r.room.Closed || r.room.Topic != topic ||
		r.room.Epoch != r.epoch || r.room.Commit != g.SourceCommit ||
		r.room.WaveID != g.WaveID || !r.room.HasMember(g.TargetID) ||
		r.epoch != room || s.RoomEpoch != room {
		return refusal(5, "stale or unauthenticated room epoch")
	}
	return nil
}

// AttachRoom permits only the adapter's real room validator to issue topics.
// The room's initial epoch is frozen; an in-place mutation is never authority.
func (c *Coordinator) AttachRoom(r *switchboard.Room) error {
	if err := c.ready(); err != nil {
		return err
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.init()
	if r == nil || r.Epoch == 0 || r.Topic == "" || r.Closed || r.Repo != c.Adapter.Repo || r.Commit != c.SourceCommit {
		return refusal(5, "unbound room registration")
	}
	if _, exists := c.rooms[r.Topic]; exists {
		return refusal(5, "room already registered")
	}
	if err := c.Adapter.AttachRoom(r); err != nil {
		return err
	}
	c.rooms[r.Topic] = roomBinding{room: r, epoch: r.Epoch}
	return nil
}

// Publish checks producer identity, the exact closed parser and live epoch
// evidence before allowing the adapter to store or publish any bytes.
func (c *Coordinator) Publish(s Submission) (switchboard.Ack, error) {
	if err := c.ready(); err != nil {
		return switchboard.Ack{}, err
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.init()
	if s.Endpoint == "" || s.Extension <= 0 || s.LeaseEpoch == 0 || s.BusEpoch == 0 {
		return switchboard.Ack{}, refusal(2, "invalid admission control evidence")
	}
	p, err := c.Adapter.Auth.Authenticate(s.Endpoint)
	if err != nil || p.HostID == "" || p.Extension != s.Extension || p.Extension <= 0 {
		return switchboard.Ack{}, refusal(5, "unauthenticated endpoint extension")
	}
	rec, ok := c.Adapter.Grants.Lookup(p.TargetID)
	if !ok || rec.Grant.TargetID != p.TargetID || rec.Grant.WorkerID != p.WorkerID {
		return switchboard.Ack{}, refusal(5, "forged or missing sender grant")
	}
	g := rec.Grant
	if g.Epoch != s.LeaseEpoch {
		return switchboard.Ack{}, refusal(5, "stale lease epoch")
	}
	if stoppedEpoch, stopped := c.stopped[g.TargetID]; stopped && stoppedEpoch == g.Epoch {
		return switchboard.Ack{}, refusal(5, "target stopped at lease epoch")
	}
	if len(s.Raw) > c.Adapter.Policy.MaxRecordBytes {
		return switchboard.Ack{}, refusal(5, "complete record exceeds finite budget")
	}
	if !utf8.Valid(s.Raw) {
		return switchboard.Ack{}, refusal(2, "invalid source UTF-8")
	}
	parsed, err := switchboard.ParseSpeechAct(s.Raw, g)
	if err != nil {
		return switchboard.Ack{}, err
	}
	if unsafeMedia(parsed.Body) {
		return switchboard.Ack{}, refusal(5, "frame, STATE or pixel encoding forbidden")
	}
	if s.PayloadSHA256 != "" {
		if len(s.PayloadSHA256) != 64 || !validHexDigest(s.PayloadSHA256) {
			return switchboard.Ack{}, refusal(2, "invalid caller digest shape")
		}
		if s.PayloadSHA256 != digest(s.Raw) {
			return switchboard.Ack{}, refusal(5, "caller digest differs from complete source")
		}
	}
	if err := c.live(g, s.Topic, s.BusEpoch, s.RoomEpoch); err != nil {
		return switchboard.Ack{}, err
	}
	key := streamKey(g)
	st := c.outbound[key]
	if st != nil {
		if st.quarantined {
			return switchboard.Ack{LastContiguous: st.last, Quarantined: true}, refusal(6, "equivocation quarantine is sticky")
		}
		if previous, exists := st.seen[parsed.Seq]; exists {
			if !bytes.Equal(previous, s.Raw) {
				st.quarantined = true
				return switchboard.Ack{LastContiguous: st.last, Quarantined: true}, refusal(6, "changed-byte sequence equivocation")
			}
			return switchboard.Ack{LastContiguous: st.last, Held: parsed.Seq > st.last, Replay: true}, nil
		}
	}
	ack, err := c.Adapter.Send(s.Endpoint, s.Extension, s.LeaseEpoch, s.Raw, s.Topic)
	if err != nil {
		// A failed native publish may already have reached its destination.
		// Never retry it blindly: quarantine the coordinator's lease stream.
		var r *switchboard.Refusal
		if errors.As(err, &r) && r.Code == 4 &&
			r.Reason == "native transport publish unavailable; no fallback" {
			if st == nil {
				st = &outgoingStream{last: ack.LastContiguous, seen: make(map[int][]byte)}
				c.outbound[key] = st
			}
			st.quarantined = true
			return switchboard.Ack{LastContiguous: ack.LastContiguous, Held: true,
				Quarantined: true}, refusal(6, "native transport delivery indeterminate")
		}
		if ack.Quarantined {
			if st == nil {
				st = &outgoingStream{last: ack.LastContiguous, seen: make(map[int][]byte)}
				c.outbound[key] = st
			}
			st.quarantined = true
		}
		return ack, err
	}
	if st == nil {
		st = &outgoingStream{last: -1, seen: make(map[int][]byte)}
		c.outbound[key] = st
	}
	st.seen[parsed.Seq] = bytes.Clone(s.Raw)
	st.last = ack.LastContiguous
	return ack, nil
}

func validHexDigest(s string) bool {
	_, err := hex.DecodeString(s)
	return err == nil && strings.ToLower(s) == s
}

// Stop is an independent host control action. It fences all later admissions
// for the stopped target/lease before the adapter's priority STOP is enqueued.
func (c *Coordinator) Stop(target string, epoch uint64) error {
	if err := c.ready(); err != nil {
		return err
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.init()
	rec, ok := c.Adapter.Grants.Lookup(target)
	if !ok || rec.Grant.SourceCommit != c.SourceCommit || epoch == 0 || rec.Grant.Epoch != epoch {
		return refusal(5, "STOP target or lease not authorized")
	}
	if err := c.Adapter.Stop(target, epoch); err != nil {
		return err
	}
	c.stopped[target] = epoch
	return nil
}

func cloneIncoming(old map[string]*incomingStream) map[string]*incomingStream {
	copyMap := make(map[string]*incomingStream, len(old))
	for k, st := range old {
		x := *st
		x.seen = make(map[int][]byte, len(st.seen))
		for seq, raw := range st.seen {
			x.seen[seq] = bytes.Clone(raw)
		}
		x.held = make(map[int][]byte, len(st.held))
		for seq, raw := range st.held {
			x.held[seq] = bytes.Clone(raw)
		}
		copyMap[k] = &x
	}
	return copyMap
}

// retainUncertainty is the only deliberate state mutation on a failed batch.
// The cursor and contiguous coverage remain unchanged, but suspicious bytes
// stay held or quarantined for a host investigation.
func (c *Coordinator) retainUncertainty(key string, seq int, raw []byte, equivocation bool) {
	st := c.incoming[key]
	if st == nil {
		st = &incomingStream{seen: make(map[int][]byte), held: make(map[int][]byte)}
		c.incoming[key] = st
	}
	if equivocation {
		st.quarantined = true
	}
	if old, ok := st.held[seq]; ok && !bytes.Equal(old, raw) {
		st.quarantined = true
	} else {
		st.held[seq] = bytes.Clone(raw)
	}
}

// Consume verifies every complete native body again, then atomically advances
// the cursor and sequence coverage only for a fully valid finite batch. Gaps,
// equivocation, stale epochs or callback uncertainty never advance the cursor.
func (c *Coordinator) Consume(limit int) ([]switchboard.Envelope, error) {
	if err := c.ready(); err != nil {
		return nil, err
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.init()
	if limit <= 0 || limit > c.Adapter.Policy.MaxMembers {
		return nil, refusal(2, "invalid finite cursor limit")
	}
	envelopes, err := c.Adapter.ReadVerifiedAfterCursor(c.cursor, limit)
	if err != nil {
		return nil, err
	}
	if len(envelopes) == 0 {
		return nil, nil
	}
	staged := cloneIncoming(c.incoming)
	completions := make([]Completion, 0)
	for _, env := range envelopes {
		g := env.Binding
		if err := c.live(g, env.Topic, env.Sidecar.BusEpoch, env.Sidecar.RoomEpoch); err != nil {
			return nil, err
		}
		if env.Sidecar.Epoch != g.Epoch || env.Sidecar.TargetID != g.TargetID ||
			env.Sidecar.SourceBodyBytes != len(env.Payload) ||
			env.Sidecar.PayloadSHA256 != digest(env.Payload) ||
			env.Sidecar.SourceBodyRef != "sha256:"+digest(env.Payload) {
			return nil, refusal(5, "complete native body digest or identity failed")
		}
		parsed, err := switchboard.ParseSpeechAct(env.Payload, g)
		if err != nil {
			return nil, err
		}
		if unsafeMedia(parsed.Body) {
			return nil, refusal(5, "frame, STATE or pixel encoding forbidden")
		}
		key := streamKey(g)
		st := staged[key]
		if st == nil {
			st = &incomingStream{seen: make(map[int][]byte), held: make(map[int][]byte)}
			staged[key] = st
		}
		if st.quarantined {
			return nil, refusal(6, "incoming stream quarantined")
		}
		if prev, found := st.seen[parsed.Seq]; found {
			if !bytes.Equal(prev, env.Payload) {
				c.retainUncertainty(key, parsed.Seq, env.Payload, true)
				return nil, refusal(6, "incoming changed-byte equivocation")
			}
			continue // genuine replay, no counters or completion callbacks
		}
		if pending, held := st.held[parsed.Seq]; held && !bytes.Equal(pending, env.Payload) {
			c.retainUncertainty(key, parsed.Seq, env.Payload, true)
			return nil, refusal(6, "held sequence equivocation")
		}
		if parsed.Seq != st.next {
			c.retainUncertainty(key, parsed.Seq, env.Payload, false)
			return nil, refusal(6, "noncontiguous incoming sequence held")
		}
		st.seen[parsed.Seq] = bytes.Clone(env.Payload)
		delete(st.held, parsed.Seq)
		st.next++
		if parsed.Act == "status" && parsed.Status != nil && *parsed.Status == "done" {
			st.statusDone = true
		}
		if parsed.Act == "repro" && parsed.ReproSHA256 != nil &&
			!env.PendingEvidence && env.Sidecar.NativeAct == "evidence" &&
			env.Sidecar.AttestationRef != "" {
			st.verifiedRepro = true
			st.reproDigest = *parsed.ReproSHA256
			st.attestation = env.Sidecar.AttestationRef
		}
		if st.statusDone && st.verifiedRepro && !st.completed {
			st.completed = true
			completions = append(completions, Completion{Grant: g, LastContiguous: st.next - 1,
				ReproSHA256: st.reproDigest, AttestationRef: st.attestation, Status: "done"})
		}
	}
	if uint64(len(envelopes)) > ^uint64(0)-c.cursor {
		return nil, refusal(6, "native cursor overflow")
	}
	if len(completions) > 0 {
		if c.OnComplete == nil {
			return nil, refusal(4, "trusted completion sink absent")
		}
		if err := c.OnComplete.CommitCompletions(completions); err != nil {
			for _, done := range completions {
				key := streamKey(done.Grant)
				if snapshot := staged[key]; snapshot != nil {
					c.retainUncertainty(key, snapshot.next-1, snapshot.seen[snapshot.next-1], false)
				}
			}
			return nil, refusal(6, "completion commit indeterminate")
		}
	}
	c.incoming = staged
	c.cursor += uint64(len(envelopes))
	return envelopes, nil
}

// Cursor only advances after successful full-body and completion processing.
func (c *Coordinator) Cursor() uint64 {
	if c == nil {
		return 0
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.cursor
}

// SequenceCoverage starts at -1 when nothing contiguous was admitted.
func (c *Coordinator) SequenceCoverage(g switchboard.Grant) Coverage {
	res := Coverage{LastContiguous: -1}
	if c == nil {
		return res
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if st := c.incoming[streamKey(g)]; st != nil {
		res.LastContiguous = st.next - 1
		res.Held = len(st.held)
		res.StatusDone = st.statusDone
		res.VerifiedRepro = st.verifiedRepro
		res.Completed = st.completed
		res.Quarantined = st.quarantined
	}
	return res
}

// RefusalCode provides the existing Switchboard taxonomy without treating
// arbitrary, non-refusal errors as success.
func RefusalCode(err error) int {
	if err == nil {
		return 0
	}
	var refusal *switchboard.Refusal
	if errors.As(err, &refusal) {
		return refusal.Code
	}
	return 5
}

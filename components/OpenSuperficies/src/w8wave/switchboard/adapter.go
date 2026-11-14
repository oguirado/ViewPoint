package switchboard

import (
	"bytes"
	"fmt"
	"sort"
	"strings"
	"sync"
	"unicode/utf8"
)

// IngressPolicy is host configuration. Every field must be strictly positive.
type IngressPolicy struct {
	MaxRecordBytes, MaxQueueBytes                 int
	PerSenderMsgsPerPulse, PerSenderBytesPerPulse int
	RoomMsgsPerPulse, RoomBytesPerPulse           int
	MaxMembers, HeartbeatExpiry, StagnationWindow int
}

func (p IngressPolicy) valid() bool {
	return p.MaxRecordBytes > 0 && p.MaxQueueBytes > 0 && p.PerSenderMsgsPerPulse > 0 && p.PerSenderBytesPerPulse > 0 && p.RoomMsgsPerPulse > 0 && p.RoomBytesPerPulse > 0 && p.MaxMembers > 0 && p.HeartbeatExpiry > 0 && p.StagnationWindow > 0
}

// Principal is populated exclusively by host endpoint authentication.
type Principal struct {
	HostID    string
	Extension int
	TargetID  string
	WorkerID  string
}
type Authenticator interface {
	Authenticate(endpoint string) (Principal, error)
}

// Envelope wraps the original bytes without adding to the closed W8 object.
// Preview is passive UI data, never a source of verified evidence.
type Envelope struct {
	Binding         Grant
	Sidecar         Sidecar
	Payload         []byte
	Preview         string
	Topic           string
	ScoreDecimal    string
	PendingEvidence bool
}

// Transport is a native adapter port. Implementations must provide the actual
// complete-body read; local files or a preview cannot substitute for it.
type Transport interface {
	PublishMailbox(targetID string, envelope Envelope) error
	PublishTopic(topic string, envelope Envelope) error
	FetchCompleteBody(ref string) ([]byte, error)
	ReadAfterCursor(cursor uint64, limit int) ([]Envelope, error)
}

type Verification struct {
	Authenticated  bool
	ScoreMilli     int
	AttestationRef string
	BodySHA256     string
	ReproSHA256    string
	TargetID       string
	Epoch          uint64
	SourceCommit   string
}
type Verifier interface {
	Verify(raw []byte, grant Grant) (Verification, error)
}

// AckEvent is the narrow consumer-side contract for P2-07 and P2-05.
// It does not itself assert result acceptance or a verified finding.
type AckEvent struct {
	Grant         Grant
	Seq           int
	BodyRef       string
	BodyBytes     int
	PayloadSHA256 string
	NativeAct     string
	Topic         string
	BusEpoch      uint64
	RoomEpoch     uint64
}
type Acknowledged interface{ Acknowledged(event AckEvent) error }

type Ack struct {
	LastContiguous int
	Held           bool
	Replay         bool
	Quarantined    bool
}
type quota struct {
	pulse       uint64
	msgs, bytes int
}
type held struct {
	raw       []byte
	env       Envelope
	hash      string
	delivered bool
}
type stream struct {
	next        int
	seen        map[int]*held
	targetID    string
	workerID    string
	quarantined bool
}
type ControlKind string

const (
	ControlSTOP        ControlKind = "stop"
	ControlHeartbeat   ControlKind = "heartbeat"
	ControlThrottleAck ControlKind = "throttle_ack"
	ControlWhisper     ControlKind = "whisper"
)

type Control struct {
	Kind     ControlKind
	TargetID string
	Epoch    uint64
	Text     string
	Pulse    uint64
}
type Delivery struct {
	Control *Control
	Data    *Envelope
}
type progress struct {
	pulse       uint64
	lastAdvance uint64
	completed   uint64
}

// ReassignmentFencer is satisfied by arrendador.SrfArrendador's existing
// RevocarCapsula method; granting and validation remain with arrendador.
type ReassignmentFencer interface{ RevocarCapsula(capsula string) error }

// Adapter has no network/file-bus fallback, guest connection, scheduler, or
// embedded verifier. All nonlocal decisions enter through explicit host ports.
type Adapter struct {
	Policy     IngressPolicy
	Clock      Clock
	Auth       Authenticator
	Grants     GrantTable
	Store      BodyStore
	Transport  Transport
	Verifier   Verifier
	Aggregator Acknowledged
	Janitor    Acknowledged
	Fencer     ReassignmentFencer
	Repo       string
	BusEpoch   uint64

	mu           sync.Mutex
	streams      map[string]*stream
	senderBudget map[string]quota
	roomBudget   map[string]quota
	queueBytes   int
	deliveries   []Envelope
	lastDrained  string
	control      map[string]Control
	rooms        map[string]*Room
	pulses       map[string]progress
}

func (a *Adapter) ready() error {
	if a == nil || !a.Policy.valid() || a.Clock == nil || a.Auth == nil || a.Grants == nil || a.Store == nil || a.Transport == nil || a.Repo == "" || a.BusEpoch == 0 {
		return refuse(4, "host adapter prerequisite or positive policy missing")
	}
	return nil
}
func (a *Adapter) init() {
	if a.streams == nil {
		a.streams = map[string]*stream{}
	}
	if a.senderBudget == nil {
		a.senderBudget = map[string]quota{}
	}
	if a.roomBudget == nil {
		a.roomBudget = map[string]quota{}
	}
	if a.rooms == nil {
		a.rooms = map[string]*Room{}
	}
	if a.control == nil {
		a.control = map[string]Control{}
	}
	if a.pulses == nil {
		a.pulses = map[string]progress{}
	}
}
func streamID(s SpeechAct) string {
	return strings.Join([]string{s.WaveID, s.TaskID, s.AttemptID, s.LeaseID}, "\x00")
}
func checkPrincipal(p Principal, g Grant, claimedExtension int, epoch uint64, now int64) error {
	if p.HostID == "" || p.TargetID != g.TargetID || p.WorkerID != g.WorkerID || p.Extension <= 0 || p.Extension != claimedExtension {
		return refuse(5, "forged or unauthenticated sender extension")
	}
	if epoch != g.Epoch {
		return refuse(5, "stale or future lease epoch")
	}
	if now >= g.ExpiresMS {
		return refuse(5, "lease expired")
	}
	return nil
}
func increment(old quota, pulse uint64, byteCount int) quota {
	if old.pulse != pulse {
		old = quota{pulse: pulse}
	}
	old.msgs++
	old.bytes += byteCount
	return old
}
func makePreview(raw []byte, limit int) string {
	r := []rune(string(raw))
	if len(r) > limit {
		r = r[:limit]
	}
	return string(r)
}
func verifiedEvidence(verifier Verifier, raw []byte, s SpeechAct, g Grant) (bool, string, string) {
	if verifier == nil || s.ReproSHA256 == nil {
		return false, "", ""
	}
	v, e := verifier.Verify(append([]byte(nil), raw...), g)
	if e != nil || !v.Authenticated || v.ScoreMilli < 800 || v.ScoreMilli > 1000 || v.AttestationRef == "" || v.BodySHA256 != digest(raw) || v.ReproSHA256 != *s.ReproSHA256 || v.TargetID != g.TargetID || v.Epoch != g.Epoch || v.SourceCommit != g.SourceCommit {
		return false, "", ""
	}
	return true, fmt.Sprintf("%d.%03d", v.ScoreMilli/1000, v.ScoreMilli%1000), v.AttestationRef
}

// Send authenticates an endpoint, validates the unchanged raw speech act,
// applies a global sender quota, and publishes only the contiguous prefix.
// The epoch and native extension supplied by a caller are comparison-only.
func (a *Adapter) Send(endpoint string, claimedExtension int, epoch uint64, raw []byte, topic string) (Ack, error) {
	if e := a.ready(); e != nil {
		return Ack{}, e
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	a.init()
	p, e := a.Auth.Authenticate(endpoint)
	if e != nil {
		return Ack{}, refuse(5, "host endpoint authentication failed")
	}
	record, ok := a.Grants.Lookup(p.TargetID)
	if !ok {
		return Ack{}, refuse(5, "ungranted target")
	}
	g := record.Grant
	if e = g.validate(); e != nil {
		return Ack{}, e
	}
	if e = checkPrincipal(p, g, claimedExtension, epoch, a.Clock.NowMS()); e != nil {
		return Ack{}, e
	}
	s, e := ParseSpeechAct(raw, g)
	if e != nil {
		return Ack{}, e
	}
	if len(raw) > a.Policy.MaxRecordBytes {
		return Ack{}, refuse(5, "oversized multibyte record refused whole")
	}
	if !utf8.Valid(raw) {
		return Ack{}, refuse(2, "invalid bytes")
	}
	var room *Room
	if topic != "" {
		room = a.rooms[topic]
		if room == nil || room.Closed || room.Topic != room.issuedTopic || room.Repo != a.Repo || room.Commit != g.SourceCommit || room.WaveID != g.WaveID || !room.HasMember(g.TargetID) {
			return Ack{}, refuse(5, "unauthorized topic membership")
		}
	}
	sid := streamID(s)
	st := a.streams[sid]
	if st != nil && (st.targetID != g.TargetID || st.workerID != g.WorkerID) {
		return Ack{}, refuse(5, "lease stream belongs to a different granted target")
	}
	if st != nil && st.quarantined {
		return Ack{LastContiguous: st.next - 1, Quarantined: true}, refuse(6, "stream quarantined")
	}
	sum := digest(raw)
	if st != nil {
		if old, found := st.seen[s.Seq]; found && (old.hash != sum || !bytes.Equal(old.raw, raw)) {
			st.quarantined = true
			return Ack{LastContiguous: st.next - 1, Quarantined: true}, refuse(6, "equivocation at a seen sequence")
		}
	}
	pulse := a.Clock.Pulse() // never taken from the payload or native envelope
	senderNext := increment(a.senderBudget[g.WorkerID], pulse, len(raw))
	if senderNext.msgs > a.Policy.PerSenderMsgsPerPulse || senderNext.bytes > a.Policy.PerSenderBytesPerPulse {
		return Ack{}, refuse(5, "global sender flood quota")
	}
	var roomNext quota
	if room != nil {
		roomNext = increment(a.roomBudget[topic], pulse, len(raw))
		if roomNext.msgs > a.Policy.RoomMsgsPerPulse || roomNext.bytes > a.Policy.RoomBytesPerPulse {
			return Ack{}, refuse(5, "room flood quota")
		}
	}
	// Replays are counted against flood quotas, but never publish or store again.
	if st != nil {
		if old, found := st.seen[s.Seq]; found {
			a.senderBudget[g.WorkerID] = senderNext
			if room != nil {
				a.roomBudget[topic] = roomNext
			}
			if old.delivered {
				return Ack{LastContiguous: st.next - 1, Replay: true}, nil
			}
			e = a.flush(st)
			return Ack{LastContiguous: st.next - 1, Held: s.Seq >= st.next, Replay: true}, e
		}
	}
	if a.queueBytes+len(raw) > a.Policy.MaxQueueBytes {
		return Ack{}, refuse(4, "ordinary queue full")
	}
	native, e := NativeAct(s)
	if e != nil {
		return Ack{}, e
	}
	score, attestation := "", ""
	pending := false
	if s.Act == "repro" {
		var yes bool
		yes, score, attestation = verifiedEvidence(a.Verifier, raw, s, g)
		if !yes {
			native = "claim"
			pending = true
		}
	}
	// Prepublication checks: the local store and native full-body read must
	// independently agree. Missing native functionality never falls back.
	ref, e := a.Store.Put(raw)
	if e != nil {
		return Ack{}, e
	}
	local, e := a.Store.Resolve(ref)
	if e != nil || !bytes.Equal(local, raw) {
		return Ack{}, refuse(6, "local body content indeterminate")
	}
	full, e := a.Transport.FetchCompleteBody(ref)
	if e != nil {
		return Ack{}, refuse(4, "native full-body retrieval unavailable")
	}
	if e = checkBody(full, ref); e != nil {
		return Ack{}, e
	}
	if !bytes.Equal(full, raw) {
		return Ack{}, refuse(6, "native full body differs from source")
	}
	side := Sidecar{CodecVersion: "w8-c3/1", SenderExtension: p.Extension, NativeAct: native, TargetID: g.TargetID, Epoch: g.Epoch, BusEpoch: a.BusEpoch, PayloadSHA256: sum, SourceBodyRef: ref, SourceBodyBytes: len(raw), AttestationRef: attestation}
	if room != nil {
		side.RoomEpoch = room.Epoch
	}
	limit := 240
	if room == nil {
		limit = 180
	}
	env := Envelope{Binding: g, Sidecar: side, Payload: append([]byte(nil), raw...), Preview: makePreview(raw, limit), Topic: topic, ScoreDecimal: score, PendingEvidence: pending}
	if st == nil {
		st = &stream{seen: map[int]*held{}, targetID: g.TargetID, workerID: g.WorkerID}
		a.streams[sid] = st
	}
	st.seen[s.Seq] = &held{raw: append([]byte(nil), raw...), env: env, hash: sum}
	a.queueBytes += len(raw)
	a.senderBudget[g.WorkerID] = senderNext
	if room != nil {
		a.roomBudget[topic] = roomNext
	}
	if e = a.flush(st); e != nil {
		return Ack{LastContiguous: st.next - 1, Held: true, Quarantined: st.quarantined}, e
	}
	return Ack{LastContiguous: st.next - 1, Held: s.Seq >= st.next}, nil
}
func (a *Adapter) flush(st *stream) error {
	for {
		next, found := st.seen[st.next]
		if !found {
			return nil
		}
		env := next.env
		var e error
		if env.Topic == "" {
			e = a.Transport.PublishMailbox(env.Binding.TargetID, env)
		} else {
			e = a.Transport.PublishTopic(env.Topic, env)
		}
		if e != nil {
			return refuse(4, "native transport publish unavailable; no fallback")
		}
		event := AckEvent{Grant: env.Binding, Seq: st.next, BodyRef: env.Sidecar.SourceBodyRef, BodyBytes: env.Sidecar.SourceBodyBytes, PayloadSHA256: env.Sidecar.PayloadSHA256, NativeAct: env.Sidecar.NativeAct, Topic: env.Topic, BusEpoch: env.Sidecar.BusEpoch, RoomEpoch: env.Sidecar.RoomEpoch}
		// A published event with an uncertain consumer notification is fenced
		// rather than blindly sent a second time.
		if a.Aggregator != nil {
			if e = a.Aggregator.Acknowledged(event); e != nil {
				st.quarantined = true
				return refuse(6, "aggregator acknowledgement indeterminate")
			}
		}
		if a.Janitor != nil {
			if e = a.Janitor.Acknowledged(event); e != nil {
				st.quarantined = true
				return refuse(6, "body janitor acknowledgement indeterminate")
			}
		}
		next.delivered = true
		st.next++
		a.deliveries = append(a.deliveries, env)
	}
}

// ReadVerifiedAfterCursor never promotes a native preview to authoritative data.
func (a *Adapter) ReadVerifiedAfterCursor(cursor uint64, limit int) ([]Envelope, error) {
	if e := a.ready(); e != nil {
		return nil, e
	}
	if limit <= 0 || limit > a.Policy.MaxMembers {
		return nil, refuse(2, "bad read bound")
	}
	messages, e := a.Transport.ReadAfterCursor(cursor, limit)
	if e != nil {
		return nil, refuse(4, "native cursor unavailable")
	}
	if len(messages) > limit {
		return nil, refuse(6, "native cursor exceeded read allowance")
	}
	out := make([]Envelope, 0, len(messages))
	for _, m := range messages {
		record, exists := a.Grants.Lookup(m.Sidecar.TargetID)
		if !exists || m.Binding != record.Grant || m.Sidecar.Epoch != record.Grant.Epoch || m.Sidecar.BusEpoch != a.BusEpoch || m.Sidecar.CodecVersion != "w8-c3/1" || m.Sidecar.SourceBodyBytes < 0 || m.Sidecar.SourceBodyBytes > a.Policy.MaxRecordBytes {
			return nil, refuse(6, "native cursor grant, epoch, codec or bounds mismatch")
		}
		full, er := a.Transport.FetchCompleteBody(m.Sidecar.SourceBodyRef)
		if er != nil {
			return nil, refuse(4, "missing native complete body")
		}
		if len(full) != m.Sidecar.SourceBodyBytes || digest(full) != m.Sidecar.PayloadSHA256 {
			return nil, refuse(6, "native cursor body mismatch")
		}
		if er = checkBody(full, m.Sidecar.SourceBodyRef); er != nil {
			return nil, er
		}
		parsed, er := ParseSpeechAct(full, m.Binding)
		if er != nil {
			return nil, er
		}
		mapped, er := NativeAct(parsed)
		if er != nil {
			return nil, er
		}
		if parsed.Act == "repro" {
			verified, score, att := verifiedEvidence(a.Verifier, full, parsed, m.Binding)
			if verified {
				if m.Sidecar.AttestationRef != att || m.ScoreDecimal != score {
					return nil, refuse(5, "unbound native attestation")
				}
			} else {
				mapped = "claim"
				if !m.PendingEvidence || m.Sidecar.AttestationRef != "" {
					return nil, refuse(5, "unverified native evidence")
				}
			}
		}
		if mapped != m.Sidecar.NativeAct {
			return nil, refuse(5, "native act does not match unchanged source")
		}
		m.Payload = full
		out = append(out, m)
	}
	return out, nil
}

func (a *Adapter) AttachRoom(room *Room) error {
	if e := a.ready(); e != nil {
		return e
	}
	if room == nil || room.Topic == "" || room.Topic != room.issuedTopic || room.Closed || room.Repo != a.Repo || room.ConferenceTopic != "" || len(room.Members) < 2 || len(room.Members) > a.Policy.MaxMembers {
		return refuse(5, "unopened or unsafe room")
	}
	expected, err := topicID(room.WaveID, room.Repo, room.Commit, "", room.Members)
	if err != nil || expected != room.Topic || !room.HasMember(room.InitiatorTarget) {
		return refuse(5, "room identity does not match admitted finding")
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	a.init()
	if _, ok := a.rooms[room.Topic]; ok {
		return refuse(5, "room exists")
	}
	for _, target := range room.Members {
		r, ok := a.Grants.Lookup(target)
		if !ok || r.Grant.SourceCommit != room.Commit || r.Grant.WaveID != room.WaveID || r.Grant.ExpiresMS <= a.Clock.NowMS() {
			return refuse(5, "invalid current topic member")
		}
	}
	a.rooms[room.Topic] = room
	return nil
}

// Conference grants a room to authenticated Muse seats only, never by a
// participant-supplied membership list alone.
func (a *Adapter) Conference(topic string, members []string) (*Room, error) {
	if e := a.ready(); e != nil {
		return nil, e
	}
	if topic == "" || len(members) < 2 || len(members) > a.Policy.MaxMembers {
		return nil, refuse(4, "conference scope or capacity")
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	a.init()
	seen := map[string]bool{}
	var wave, commit string
	for _, id := range members {
		if seen[id] {
			return nil, refuse(5, "duplicate conference seat")
		}
		seen[id] = true
		rec, ok := a.Grants.Lookup(id)
		if !ok || !rec.Muse || rec.Grant.ExpiresMS <= a.Clock.NowMS() {
			return nil, refuse(5, "ungranted Muse seat")
		}
		if wave == "" {
			wave = rec.Grant.WaveID
			commit = rec.Grant.SourceCommit
		}
		if rec.Grant.WaveID != wave || rec.Grant.SourceCommit != commit {
			return nil, refuse(5, "cross-wave conference")
		}
	}
	ident, e := topicID(wave, a.Repo, commit, topic, members)
	if e != nil {
		return nil, e
	}
	if _, exists := a.rooms[ident]; exists {
		return nil, refuse(5, "conference already exists")
	}
	list := append([]string(nil), members...)
	sort.Strings(list)
	r := &Room{Repo: a.Repo, Commit: commit, WaveID: wave, MaxMembers: a.Policy.MaxMembers, Clock: a.Clock, Topic: ident, issuedTopic: ident, Epoch: 1, Members: list, ConferenceTopic: topic}
	a.rooms[ident] = r
	return r, nil
}
func controlKey(target string, epoch uint64, kind ControlKind) string {
	return fmt.Sprintf("%s\x00%d\x00%s", target, epoch, kind)
}
func (a *Adapter) controlAdd(c Control) error {
	if e := a.ready(); e != nil {
		return e
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	a.init()
	rec, ok := a.Grants.Lookup(c.TargetID)
	if !ok || rec.Grant.Epoch != c.Epoch {
		return refuse(5, "control target is not granted at epoch")
	}
	c.Pulse = a.Clock.Pulse()
	key := controlKey(c.TargetID, c.Epoch, c.Kind)
	if _, ok = a.control[key]; !ok && len(a.control) >= a.Policy.MaxMembers*4 {
		// STOP is independent of ordinary traffic; evict an unserved heartbeat
		// before denying the most important control message.
		if c.Kind == ControlSTOP {
			for k, v := range a.control {
				if v.Kind == ControlHeartbeat || v.Kind == ControlThrottleAck {
					delete(a.control, k)
					break
				}
			}
		}
		if len(a.control) >= a.Policy.MaxMembers*4 {
			return refuse(4, "control lane full")
		}
	}
	a.control[key] = c
	return nil
}
func (a *Adapter) Stop(target string, epoch uint64) error {
	return a.controlAdd(Control{Kind: ControlSTOP, TargetID: target, Epoch: epoch})
}
func (a *Adapter) Heartbeat(target string, epoch uint64) error {
	if e := a.controlAdd(Control{Kind: ControlHeartbeat, TargetID: target, Epoch: epoch}); e != nil {
		return e
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	a.init()
	p := a.pulses[target]
	p.pulse = a.Clock.Pulse()
	if p.lastAdvance == 0 {
		p.lastAdvance = p.pulse
	}
	a.pulses[target] = p
	return nil
}
func (a *Adapter) ThrottleAck(target string, epoch uint64) error {
	return a.controlAdd(Control{Kind: ControlThrottleAck, TargetID: target, Epoch: epoch})
}
func (a *Adapter) Whisper(target, text string) error {
	if !utf8.ValidString(text) || utf8.RuneCountInString(text) > 180 {
		return refuse(5, "whisper UTF-8 invalid or exceeds native preview cap")
	}
	if e := a.ready(); e != nil {
		return e
	}
	r, ok := a.Grants.Lookup(target)
	if !ok || !r.Muse {
		return refuse(5, "whisper is Muse-only")
	}
	// Text remains passive; it is never parsed as a grant or a scheduling act.
	return a.controlAdd(Control{Kind: ControlWhisper, TargetID: target, Epoch: r.Grant.Epoch, Text: text})
}

// DrainNext always serves coalesced control before ordinary data. A full data
// lane cannot suppress STOP, and repeated STOP consumes just one slot.
func (a *Adapter) DrainNext() (Delivery, bool) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.init()
	if len(a.control) > 0 {
		keys := make([]string, 0, len(a.control))
		for k := range a.control {
			keys = append(keys, k)
		}
		sort.Slice(keys, func(i, j int) bool {
			x, y := a.control[keys[i]], a.control[keys[j]]
			if x.Kind == ControlSTOP && y.Kind != ControlSTOP {
				return true
			}
			if x.Kind != ControlSTOP && y.Kind == ControlSTOP {
				return false
			}
			return keys[i] < keys[j]
		})
		c := a.control[keys[0]]
		delete(a.control, keys[0])
		return Delivery{Control: &c}, true
	}
	if len(a.deliveries) == 0 {
		return Delivery{}, false
	}
	// Round-robin among present worker origins, preserving per-origin order.
	// No single sender can monopolize draining the ordinary data lane.
	origins := map[string]bool{}
	for _, d := range a.deliveries {
		origins[d.Binding.WorkerID] = true
	}
	names := make([]string, 0, len(origins))
	for name := range origins {
		names = append(names, name)
	}
	sort.Strings(names)
	chosen := names[0]
	for _, name := range names {
		if name > a.lastDrained {
			chosen = name
			break
		}
	}
	index := 0
	for i, item := range a.deliveries {
		if item.Binding.WorkerID == chosen {
			index = i
			break
		}
	}
	d := a.deliveries[index]
	a.deliveries = append(a.deliveries[:index], a.deliveries[index+1:]...)
	a.lastDrained = chosen
	a.queueBytes -= len(d.Payload)
	if a.queueBytes < 0 {
		a.queueBytes = 0
	}
	return Delivery{Data: &d}, true
}
func (a *Adapter) Progress(target string, completed uint64) error {
	if e := a.ready(); e != nil {
		return e
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	a.init()
	if _, ok := a.Grants.Lookup(target); !ok {
		return refuse(5, "ungranted progress")
	}
	p := a.pulses[target]
	if completed < p.completed {
		return refuse(5, "progress counter regressed")
	}
	if completed > p.completed {
		p.lastAdvance = a.Clock.Pulse()
	}
	p.completed = completed
	p.pulse = a.Clock.Pulse()
	a.pulses[target] = p
	return nil
}

// Watch returns authenticated target IDs needing supervisory action, not a
// permit to issue a new lease. Epoch fencing remains arrendador's authority.
func (a *Adapter) Watch() ([]string, error) {
	if e := a.ready(); e != nil {
		return nil, e
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	a.init()
	now := a.Clock.Pulse()
	out := []string{}
	for id, p := range a.pulses {
		if now >= p.pulse && now-p.pulse >= uint64(a.Policy.HeartbeatExpiry) || now >= p.lastAdvance && now-p.lastAdvance >= uint64(a.Policy.StagnationWindow) {
			out = append(out, id)
		}
	}
	sort.Strings(out)
	return out, nil
}

// RequestReassignment fails closed for a shared capsule: existing
// RevocarCapsula is capsule-wide, so a destination-scoped C2 fence is needed
// before this operation can safely occur when sibling grants are active.
func (a *Adapter) RequestReassignment(capsule, target string) error {
	if e := a.ready(); e != nil {
		return e
	}
	if a.Fencer == nil || capsule == "" {
		return refuse(4, "arrendador fencing prerequisite unavailable")
	}
	rec, ok := a.Grants.Lookup(target)
	if !ok {
		return refuse(5, "unknown target")
	}
	for _, other := range a.Grants.Active(rec.Grant.WaveID, rec.Grant.SourceCommit) {
		if other.Grant.TargetID != target && other.Grant.WorkerID == rec.Grant.WorkerID && other.Grant.ExpiresMS > a.Clock.NowMS() {
			return refuse(4, "cannot revoke a shared capsule without destination fencing")
		}
	}
	if e := a.Fencer.RevocarCapsula(capsule); e != nil {
		return refuse(6, "arrendador revocation indeterminate")
	}
	return nil
}

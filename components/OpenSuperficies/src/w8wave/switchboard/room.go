package switchboard

import (
	"sort"
	"strings"
	"sync"

	"opensuperficies.local/srf/src/workerbridge"
)

// GrantRecord holds a host-owned target path scope and seat kind. A worker's
// `affected` array cannot create or edit any of these scopes or members.
type GrantRecord struct {
	Grant Grant
	Paths []string
	Muse  bool
}
type GrantTable interface {
	Lookup(targetID string) (GrantRecord, bool)
	Active(waveID, sourceCommit string) []GrantRecord
}
type MemGrantTable struct {
	mu      sync.RWMutex
	entries map[string]GrantRecord
}

func NewMemGrantTable() *MemGrantTable { return &MemGrantTable{entries: map[string]GrantRecord{}} }
func (t *MemGrantTable) Add(r GrantRecord) error {
	if e := r.Grant.validate(); e != nil {
		return e
	}
	for _, p := range r.Paths {
		if !validPath(p) {
			return refuse(2, "invalid granted path scope")
		}
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.entries == nil {
		t.entries = map[string]GrantRecord{}
	}
	r.Paths = append([]string(nil), r.Paths...)
	t.entries[r.Grant.TargetID] = r
	return nil
}
func (t *MemGrantTable) Lookup(id string) (GrantRecord, bool) {
	t.mu.RLock()
	defer t.mu.RUnlock()
	v, ok := t.entries[id]
	v.Paths = append([]string(nil), v.Paths...)
	return v, ok
}
func (t *MemGrantTable) Active(wave, commit string) []GrantRecord {
	t.mu.RLock()
	defer t.mu.RUnlock()
	out := []GrantRecord{}
	for _, r := range t.entries {
		if r.Grant.WaveID == wave && r.Grant.SourceCommit == commit {
			r.Paths = append([]string(nil), r.Paths...)
			out = append(out, r)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Grant.TargetID < out[j].Grant.TargetID })
	return out
}
func scopesOverlap(a, b string) bool {
	return a == b || strings.HasPrefix(a, b+"/") || strings.HasPrefix(b, a+"/")
}

// Clock is injected and monotonic: caller-supplied pulse or timestamps are ignored.
type Clock interface {
	Pulse() uint64
	NowMS() int64
}

type Obligation struct {
	TargetID        string
	TerminalReceipt string
	Outcome         string
}

// Room is an authorized topic or Muse conference, not a new grant or lease.
type Room struct {
	Repo            string
	Commit          string
	WaveID          string
	InitiatorTarget string
	MaxMembers      int
	Clock           Clock
	Topic           string
	Epoch           uint64
	Members         []string
	Obligations     []Obligation
	ConferenceTopic string
	Closed          bool
	issuedTopic     string // private proof this room was created by Open/Conference
}

func topicID(wave, repo, commit, conference string, members []string) (string, error) {
	set := append([]string(nil), members...)
	sort.Strings(set)
	raw, e := workerbridge.Canonical(map[string]any{"wave": wave, "repo": repo, "commit": commit, "targets": set, "topic": conference})
	if e != nil {
		return "", refuse(2, "topic identity")
	}
	return "sha256:" + digest(raw), nil
}
func (r *Room) Open(trigger SpeechAct, grants GrantTable) error {
	if r == nil || grants == nil || r.Clock == nil || r.MaxMembers <= 0 || r.Repo == "" || r.Commit == "" || r.InitiatorTarget == "" {
		return refuse(4, "missing scoped room prerequisite")
	}
	if r.Topic != "" || r.Closed {
		return refuse(5, "room already used")
	}
	self, ok := grants.Lookup(r.InitiatorTarget)
	if !ok || self.Grant.SourceCommit != r.Commit || self.Grant.WaveID != trigger.WaveID || self.Grant.ExpiresMS <= r.Clock.NowMS() {
		return refuse(5, "room initiator not active")
	}
	parsed, e := ParseSpeechAct(trigger.Raw(), self.Grant)
	if e != nil {
		return e
	}
	if parsed.Act != "finding" || parsed.ReproSHA256 == nil || len(parsed.Affected) == 0 {
		return refuse(5, "room only opens for intersection finding")
	}
	members := []string{r.InitiatorTarget}
	peer := false
	for _, rec := range grants.Active(trigger.WaveID, r.Commit) {
		if rec.Grant.TargetID == r.InitiatorTarget || rec.Grant.ExpiresMS <= r.Clock.NowMS() {
			continue
		}
		intersects := false
		for _, claimed := range parsed.Affected {
			for _, allowed := range rec.Paths {
				if scopesOverlap(claimed, allowed) {
					intersects = true
					break
				}
			}
		}
		if intersects {
			peer = true
			members = append(members, rec.Grant.TargetID)
		}
	}
	if !peer {
		return refuse(5, "no intersecting active peer grant")
	}
	if len(members) > r.MaxMembers {
		return refuse(4, "room membership capacity exceeded")
	}
	id, e := topicID(trigger.WaveID, r.Repo, r.Commit, "", members)
	if e != nil {
		return e
	}
	sort.Strings(members)
	r.Topic = id
	r.issuedTopic = id
	r.WaveID = trigger.WaveID
	r.Epoch = 1
	r.Members = members
	return nil
}
func (r *Room) HasMember(id string) bool {
	for _, m := range r.Members {
		if m == id {
			return true
		}
	}
	return false
}
func (r *Room) Close(obligations []Obligation) error {
	if r == nil || r.Topic == "" || r.Topic != r.issuedTopic || r.Closed {
		return refuse(5, "room not open")
	}
	if len(obligations) == 0 || len(obligations) != len(r.Members) {
		return refuse(5, "close requires every member's obligation")
	}
	seen := map[string]bool{}
	for _, o := range obligations {
		if !r.HasMember(o.TargetID) || seen[o.TargetID] {
			return refuse(5, "room obligation is unbound or duplicate")
		}
		seen[o.TargetID] = true
		if o.TerminalReceipt == "" && o.Outcome != "blocked" && o.Outcome != "indeterminate" {
			return refuse(5, "missing terminal receipt or explicit outcome")
		}
	}
	r.Obligations = append([]Obligation(nil), obligations...)
	r.Closed = true
	return nil
}

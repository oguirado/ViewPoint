package proxy

import (
	"errors"
	"sync"
	"time"
)

const PendingHosts = 20
const PendingBytes int64 = 50 << 20

var errHosts = errors.New("pending_host_limit")

type event struct {
	at    time.Time
	bytes int64
}
type quota struct {
	mu    sync.Mutex
	clock func() time.Time
	hosts map[string]map[string]time.Time
	bytes map[string]map[string][]event
}

func newQuota(clock func() time.Time) *quota {
	return &quota{clock: clock, hosts: map[string]map[string]time.Time{}, bytes: map[string]map[string][]event{}}
}
func (q *quota) admit(cap, host string) error {
	q.mu.Lock()
	defer q.mu.Unlock()
	now := q.clock()
	q.gc(now)
	if q.remainingLocked(cap, host, now) == 0 {
		return errLimit
	}
	m := q.hosts[cap]
	if m == nil {
		m = map[string]time.Time{}
		q.hosts[cap] = m
	}
	for h, at := range m {
		if !now.Before(at.Add(time.Hour)) {
			delete(m, h)
		}
	}
	if _, ok := m[host]; ok {
		return nil
	}
	if len(m) >= PendingHosts {
		return errHosts
	}
	m[host] = now
	return nil
}

// reserve debits both directions atomically before I/O.
func (q *quota) reserve(cap, host string, want int64) int64 {
	n, _ := q.reserveAt(cap, host, want)
	return n
}
func (q *quota) reserveAt(cap, host string, want int64) (int64, time.Time) {
	q.mu.Lock()
	defer q.mu.Unlock()
	now := q.clock()
	m := q.bytes[cap]
	if m == nil {
		m = map[string][]event{}
		q.bytes[cap] = m
	}
	events := m[host]
	start := 0
	used := int64(0)
	for start < len(events) && !now.Before(events[start].at.Add(time.Hour)) {
		start++
	}
	events = events[start:]
	for _, e := range events {
		used += e.bytes
	}
	n := min(want, PendingBytes-used)
	if n <= 0 {
		m[host] = events
		return 0, now.Truncate(time.Second)
	}
	at := now
	if len(events) > 0 && events[len(events)-1].at.Truncate(time.Second).Equal(at.Truncate(time.Second)) {
		events[len(events)-1].bytes += n
		events[len(events)-1].at = at
	} else {
		events = append(events, event{at, n})
	}
	m[host] = events
	return n, now.Truncate(time.Second)
}
func (q *quota) remaining(cap, host string) int64 {
	q.mu.Lock()
	defer q.mu.Unlock()
	return q.remainingLocked(cap, host, q.clock())
}
func (q *quota) remainingLocked(cap, host string, now time.Time) int64 {
	used := int64(0)
	for _, e := range q.bytes[cap][host] {
		if now.Before(e.at.Add(time.Hour)) {
			used += e.bytes
		}
	}
	return max(int64(0), PendingBytes-used)
}

// refund returns unconsumed reservations to their original second bucket.
// Buckets expire from their last reservation, never before an hour elapsed.
func (q *quota) refund(cap, host string, at time.Time, n int64) {
	if n <= 0 {
		return
	}
	q.mu.Lock()
	defer q.mu.Unlock()
	events := q.bytes[cap][host]
	for i := range events {
		if events[i].at.Truncate(time.Second).Equal(at) {
			events[i].bytes -= min(n, events[i].bytes)
			break
		}
	}
}

// Called under mu. Expired capsules/hosts cannot accumulate across hours.
func (q *quota) gc(now time.Time) {
	for cap, m := range q.hosts {
		for h, at := range m {
			if !now.Before(at.Add(time.Hour)) {
				delete(m, h)
			}
		}
		if len(m) == 0 {
			delete(q.hosts, cap)
		}
	}
	for cap, m := range q.bytes {
		for h, events := range m {
			i := 0
			for i < len(events) && !now.Before(events[i].at.Add(time.Hour)) {
				i++
			}
			if i == len(events) {
				delete(m, h)
			} else {
				m[h] = events[i:]
			}
		}
		if len(m) == 0 {
			delete(q.bytes, cap)
		}
	}
}

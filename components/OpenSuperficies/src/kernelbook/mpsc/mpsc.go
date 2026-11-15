package mpsc

import (
	"runtime"
	"sync/atomic"
	"unsafe"
)

// CacheLine is the target cache line size (64 bytes) to eliminate false sharing.
const CacheLine = 64

// MPSCNode represents an intrusive node in the Dmitry Vyukov MPSC queue.
type MPSCNode struct {
	next atomic.Pointer[MPSCNode]
	Val  uint64
	Data any
}

// Node is an alias for MPSCNode.
type Node = MPSCNode

// MPSC is a wait-free multi-producer single-consumer queue based on Dmitry Vyukov's intrusive design.
// Enqueue (Push) is wait-free: 1 atomic swap (XCHG / SWPAL) + 1 atomic store (release).
// Dequeue (Pop) is lock-free and handled exclusively by the single consumer.
type MPSC struct {
	head atomic.Pointer[MPSCNode] // Cache line 0: shared by N producers
	_    [CacheLine - 8]byte
	tail *MPSCNode                // Cache line 1: consumer private
	_    [CacheLine - 8]byte
	stub MPSCNode                 // Cache line 2: permanent sentinel stub
	_    [CacheLine - unsafe.Sizeof(MPSCNode{})]byte
}

// Queue is an alias for MPSC.
type Queue = MPSC

// popFunc is the active Pop implementation.
// Swapped by pop_mutante_r21.go under build tag mutante_r21.
var popFunc = (*MPSC).popNormal

// NewMPSC creates and initializes a new Vyukov MPSC queue.
func NewMPSC() *MPSC {
	q := &MPSC{}
	q.head.Store(&q.stub)
	q.tail = &q.stub
	return q
}

// New creates and initializes a new Vyukov MPSC queue.
func New() *MPSC {
	return NewMPSC()
}

// Push enqueues a node onto the queue.
// Push is wait-free: exactly 1 atomic swap (XCHG / SWPAL) followed by 1 atomic store.
// Memory allocation is 0 B/op, 0 allocs/op.
func (q *MPSC) Push(n *MPSCNode) {
	n.next.Store(nil)
	prev := q.head.Swap(n)
	prev.next.Store(n)
}

// Pop dequeues a node from the queue.
// Returns nil if the queue is empty or if a producer is mid-enqueue.
func (q *MPSC) Pop() *MPSCNode {
	return popFunc(q)
}

// popNormal is the canonical Vyukov Pop implementation.
// When the last node is dequeued, it re-enqueues the stub sentinel (&q.stub).
func (q *MPSC) popNormal() *MPSCNode {
	tail := q.tail
	next := tail.next.Load()
	if tail == &q.stub {
		if next == nil {
			return nil
		}
		q.tail = next
		tail = next
		next = next.next.Load()
	}
	if next != nil {
		q.tail = next
		return tail
	}
	if tail != q.head.Load() {
		return nil // Concurrent producer in flight
	}
	q.Push(&q.stub) // Re-enqueue stub sentinel
	next = tail.next.Load()
	if next != nil {
		q.tail = next
		return tail
	}
	return nil
}

// PopSpin spins up to maxSpins attempting to pop a node, yielding cooperatively.
func (q *MPSC) PopSpin(maxSpins int) *MPSCNode {
	for i := 0; ; i++ {
		if n := q.Pop(); n != nil {
			return n
		}
		if i >= maxSpins {
			return nil
		}
		if i&63 == 63 {
			runtime.Gosched()
		}
	}
}

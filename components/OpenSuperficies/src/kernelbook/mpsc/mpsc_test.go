package mpsc

import (
	"sync"
	"testing"
	"time"
)

// BenchmarkMPSCPush measures wait-free Push throughput and validates 0 B/op and 0 allocs/op.
func BenchmarkMPSCPush(b *testing.B) {
	q := NewMPSC()
	nodes := make([]MPSCNode, b.N)
	b.ResetTimer()
	b.ReportAllocs()
	for i := 0; i < b.N; i++ {
		q.Push(&nodes[i])
	}
}

// TestMPSCDrenaUltimo verifies that draining an MPSC queue does not lose the last node.
// Under mutant_r21 (which fails to re-enqueue the stub), this test MUST fail.
func TestMPSCDrenaUltimo(t *testing.T) {
	q := NewMPSC()
	const N = 10
	nodes := make([]MPSCNode, N)
	for i := 0; i < N; i++ {
		nodes[i].Val = uint64(i + 1)
		q.Push(&nodes[i])
	}

	var drained []*MPSCNode
	for {
		n := q.Pop()
		if n == nil {
			break
		}
		drained = append(drained, n)
	}

	if len(drained) != N {
		t.Fatalf("TestMPSCDrenaUltimo: drained count mismatch: got %d, want %d (last node lost)", len(drained), N)
	}
	for i := 0; i < N; i++ {
		if drained[i].Val != uint64(i+1) {
			t.Fatalf("TestMPSCDrenaUltimo: index %d expected val %d, got %d", i, i+1, drained[i].Val)
		}
	}
}

// TestMPSCConcurrent verifies FIFO ordering per producer and no lost elements under high concurrency.
func TestMPSCConcurrent(t *testing.T) {
	q := NewMPSC()
	const P = 8
	const N = 5000
	nodes := make([][]MPSCNode, P)
	var wg sync.WaitGroup

	for p := 0; p < P; p++ {
		nodes[p] = make([]MPSCNode, N)
		wg.Add(1)
		go func(p int) {
			defer wg.Done()
			for i := 0; i < N; i++ {
				nodes[p][i].Val = uint64(p)<<32 | uint64(i)
				q.Push(&nodes[p][i])
			}
		}(p)
	}

	got := 0
	seen := make([]int, P)
	deadline := time.Now().Add(5 * time.Second)

	for got < P*N && time.Now().Before(deadline) {
		n := q.PopSpin(1024)
		if n == nil {
			continue
		}
		p, i := int(n.Val>>32), int(n.Val&0xffffffff)
		if seen[p] != i {
			t.Fatalf("producer %d: expected %d, got %d (broken FIFO)", p, seen[p], i)
		}
		seen[p]++
		got++
	}
	wg.Wait()

	if got != P*N {
		t.Fatalf("lost nodes: got %d of %d", got, P*N)
	}
}

// TestMPSCEmpty verifies that an empty queue returns nil.
func TestMPSCEmpty(t *testing.T) {
	q := NewMPSC()
	if n := q.Pop(); n != nil {
		t.Fatalf("expected nil from empty queue, got %v", n)
	}
}

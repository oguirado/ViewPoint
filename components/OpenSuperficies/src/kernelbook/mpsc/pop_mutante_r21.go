//go:build mutante_r21

package mpsc

func init() {
	popFunc = (*MPSC).popMutanteR21
}

// popMutanteR21 omits re-enqueuing the stub (R-21).
// As a result, when the last element is reached (tail == head, next == nil),
// it returns nil without re-enqueuing &q.stub, permanently losing the last node during drain.
func (q *MPSC) popMutanteR21() *MPSCNode {
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
		return nil
	}
	// R-21 defect: DO NOT re-enqueue the stub!
	// Omission: q.Push(&q.stub) is skipped.
	return nil
}

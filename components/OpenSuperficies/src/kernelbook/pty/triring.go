package pty

import (
	"sync/atomic"
	"unsafe"
)

// RingBufferSize defines the fixed capacity of each ring buffer segment (512 KiB [TARGET]).
const RingBufferSize = 512 * 1024

// ByteBuffer represents a fixed-size ring segment.
type ByteBuffer struct {
	data   [RingBufferSize]byte
	length uint32
}

// Bytes returns the slice of valid written bytes.
func (b *ByteBuffer) Bytes() []byte {
	if b == nil {
		return nil
	}
	l := atomic.LoadUint32(&b.length)
	return b.data[:l]
}

// Len returns the current written byte count.
func (b *ByteBuffer) Len() int {
	if b == nil {
		return 0
	}
	return int(atomic.LoadUint32(&b.length))
}

// Cap returns the buffer capacity.
func (b *ByteBuffer) Cap() int {
	return RingBufferSize
}

// TriRingThrottle implements lock-free triple buffering with atomic pointer swaps.
// It isolates high-frequency master PTY writes from staging and network rendering.
type TriRingThrottle struct {
	ingress *ByteBuffer
	staging *ByteBuffer
	publish *ByteBuffer

	swapLock uint32
	epoch    uint64
	overflow uint64
}

// NewTriRingThrottle initializes a new triple-ring buffer instance.
func NewTriRingThrottle() *TriRingThrottle {
	return &TriRingThrottle{
		ingress: new(ByteBuffer),
		staging: new(ByteBuffer),
		publish: new(ByteBuffer),
	}
}

// WriteIngress appends bytes directly from PTY master read. Called by single writer.
func (t *TriRingThrottle) WriteIngress(p []byte) int {
	cur := (*ByteBuffer)(atomic.LoadPointer((*unsafe.Pointer)(unsafe.Pointer(&t.ingress))))
	currLen := atomic.LoadUint32(&cur.length)
	space := uint32(RingBufferSize) - currLen

	toWrite := uint32(len(p))
	if toWrite > space {
		toWrite = space
		atomic.AddUint64(&t.overflow, 1)
	}

	copy(cur.data[currLen:currLen+toWrite], p[:toWrite])
	atomic.StoreUint32(&cur.length, currLen+toWrite)
	return int(toWrite)
}

// SwapAndFlush is called by the ticker (e.g. at 60 FPS = 16.6 ms).
// It atomically cycles Ingress -> Staging -> Publish.
func (t *TriRingThrottle) SwapAndFlush() *ByteBuffer {
	if !atomic.CompareAndSwapUint32(&t.swapLock, 0, 1) {
		return nil // Swap in progress
	}
	defer atomic.StoreUint32(&t.swapLock, 0)

	// Swap pointers atomically
	ingressPtr := (*unsafe.Pointer)(unsafe.Pointer(&t.ingress))
	stagingPtr := (*unsafe.Pointer)(unsafe.Pointer(&t.staging))
	publishPtr := (*unsafe.Pointer)(unsafe.Pointer(&t.publish))

	oldPublish := atomic.LoadPointer(publishPtr)
	oldStaging := atomic.LoadPointer(stagingPtr)
	oldIngress := atomic.LoadPointer(ingressPtr)

	// Ingress becomes Staging, Staging becomes Publish, Publish becomes fresh Ingress
	bufForFreshIngress := (*ByteBuffer)(oldPublish)
	atomic.StoreUint32(&bufForFreshIngress.length, 0) // Reset length for fresh writes

	atomic.StorePointer(publishPtr, oldStaging)
	atomic.StorePointer(stagingPtr, oldIngress)
	atomic.StorePointer(ingressPtr, unsafe.Pointer(bufForFreshIngress))

	atomic.AddUint64(&t.epoch, 1)
	return (*ByteBuffer)(atomic.LoadPointer(publishPtr))
}

// Epoch returns the number of completed buffer swaps.
func (t *TriRingThrottle) Epoch() uint64 {
	return atomic.LoadUint64(&t.epoch)
}

// Overflow returns the count of write events that exceeded buffer capacity.
func (t *TriRingThrottle) Overflow() uint64 {
	return atomic.LoadUint64(&t.overflow)
}

// Staging returns the current staging buffer pointer.
func (t *TriRingThrottle) Staging() *ByteBuffer {
	stagingPtr := (*unsafe.Pointer)(unsafe.Pointer(&t.staging))
	return (*ByteBuffer)(atomic.LoadPointer(stagingPtr))
}

// Publish returns the current publish buffer pointer.
func (t *TriRingThrottle) Publish() *ByteBuffer {
	publishPtr := (*unsafe.Pointer)(unsafe.Pointer(&t.publish))
	return (*ByteBuffer)(atomic.LoadPointer(publishPtr))
}

// Ingress returns the current ingress buffer pointer.
func (t *TriRingThrottle) Ingress() *ByteBuffer {
	ingressPtr := (*unsafe.Pointer)(unsafe.Pointer(&t.ingress))
	return (*ByteBuffer)(atomic.LoadPointer(ingressPtr))
}

package recstream

import (
	"io"
	"opensuperficies.local/srf/src/ramcap"
	"sync"
)

// Native recording has no W8 worker-seat grant or image/reader attestation.
// AUSENTE is explicit metadata, never a fabricated attestation digest. The
// existing cockpit lease/operator guard and canonical container resolver own
// authority. This binding identifies one native stream incarnation only.
func nativeBinding(id, container string, xid uint32) (ramcap.SeatBinding, error) {
	raw, err := ramcap.CanonicalJSON(map[string]any{
		"schema": "w8u-binding/1", "run_id": id, "lease_id": "cockpit-native-rec",
		"seat_grant_sha256": "AUSENTE", "container_id": container,
		"target_handle": map[string]any{"kind": "capsule", "canonical_id": container, "generation": id},
		"epoch":         0, "display": ":100", "xid": uint64(xid), "width": Width, "height": Height,
		"frames_root": ramcap.StreamFramesRoot, "frame_run_root": ramcap.StreamRunParent + "/" + id,
		"image_sha256": "AUSENTE", "reader_sha256": "AUSENTE", "exec_allowlist_sha256": "AUSENTE",
		"max_record_bytes": ramcap.RecordCap(Width, Height), "max_inflight_frames": 2, "tick_mode": ramcap.TickModeUnavailable,
	})
	if err != nil {
		return ramcap.SeatBinding{}, err
	}
	return ramcap.ParseSeatBinding(raw)
}

// Serialize ACK/STOP records so no credit record can follow the terminal STOP.
// A forced process cancellation closes w and bounds any blocked control write.
type streamControl struct {
	mu      sync.Mutex
	host    *ramcap.HostControl
	w       io.Writer
	initial uint64
	stopped bool
}

func newStreamControl(w io.Writer, b ramcap.SeatBinding) *streamControl {
	return &streamControl{host: ramcap.NewHostControl(b), w: w, initial: b.MaxInflightFrames}
}
func (c *streamControl) hello() error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.stopped {
		return io.ErrClosedPipe
	}
	if _, err := c.w.Write(c.host.Hello()); err != nil {
		return err
	}
	ack, err := c.host.Ack(0, c.initial)
	if err != nil {
		return err
	}
	_, err = c.w.Write(ack)
	return err
}
func (c *streamControl) ack(seq uint64) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.stopped {
		return nil
	}
	ack, err := c.host.Ack(seq, seq+c.initial)
	if err != nil {
		return err
	}
	_, err = c.w.Write(ack)
	return err
}
func (c *streamControl) stop() error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.stopped {
		return nil
	}
	c.stopped = true
	_, err := c.w.Write(c.host.Stop())
	return err
}
